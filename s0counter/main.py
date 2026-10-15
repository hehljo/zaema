#!/usr/bin/env python3
"""Dienst zur Erfassung der Eltako-S0-Impulse."""
import argparse
import csv
import functools
import hashlib
import io
import json
import logging
import os
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

from . import __version__
from . import config as config_module
from . import ordnung
from . import system
from .counter import GpioBinding, PulseCounter
from .db import Database

LOG = logging.getLogger('s0counter')


@functools.lru_cache(maxsize=1)
def laufende_fassung() -> dict:
    """Beschreibt den Code, der gerade läuft.

    Einmal berechnet und gemerkt: Der Statusabruf läuft im Dashboard alle paar
    Sekunden, und der geladene Code ändert sich zur Laufzeit nicht — eine
    neue Fassung kommt über einen Neustart des Dienstes. Ohne den Merker
    läse jeder Abruf achtzehn Dateien von der SD-Karte.

    `__version__` allein wäre eine gepflegte Zahl: sie steht auf 1.0.0,
    während sich der Code darunter mehrfach geändert hat. Deshalb kommt der
    Fingerabdruck der tatsächlich geladenen Dateien dazu — er ändert sich bei
    jeder Übertragung, ohne dass jemand daran denken muss.

    Der Zeitstempel ist die jüngste Änderung im Paketverzeichnis. Auf dem Pi
    ist das der Moment des letzten Einspielens; damit lässt sich vom Dashboard
    aus beantworten, ob eine Änderung wirklich angekommen ist.

    Bewusst ohne Git: auf dem Pi liegt kein Repository (siehe `deploy.sh`),
    und ein Aufruf, der dort ins Leere läuft, wäre eine Fehlerquelle im
    Dienst — für eine Anzeige.
    """
    verzeichnis = Path(__file__).parent
    dateien = sorted(p for p in verzeichnis.iterdir()
                     if p.suffix in ('.py', '.js', '.html'))

    hasher = hashlib.sha256()
    jüngste = 0.0
    for pfad in dateien:
        try:
            hasher.update(pfad.name.encode())
            hasher.update(pfad.read_bytes())
            jüngste = max(jüngste, pfad.stat().st_mtime)
        except OSError:
            # Eine Datei, die sich gerade nicht lesen lässt, darf den
            # Statusabruf nicht scheitern lassen.
            continue

    return {
        'version': __version__,
        'build': hasher.hexdigest()[:12],
        'dateien': len(dateien),
        'stand_ts': jüngste or None,
    }


def fassung_fuer_status() -> dict:
    """Kopie der gemerkten Fassung.

    `lru_cache` reicht immer dasselbe Objekt heraus; landete es unverändert
    in der Statusantwort, könnte ein Aufrufer den Merker verändern.
    """
    return dict(laufende_fassung())


class Service:
    def __init__(self, config):
        self.config = config
        self.stop_event = threading.Event()
        self.started_at = time.time()

        directory = os.path.dirname(config.database)
        if directory:
            os.makedirs(directory, exist_ok=True)

        self.db = Database(config.database)
        for meter_id, meter in config.meters.items():
            self.db.ensure_meter(meter_id, meter.name, meter.pulses_per_kwh)

        self.counters = {
            meter_id: PulseCounter(meter, self.db, on_pulse=self._on_pulse)
            for meter_id, meter in config.meters.items()
        }
        self.gpio = GpioBinding(self.counters)
        self.mqtt = None
        self.ha = None
        self.http_server = None
        self.balance_store = None
        self.balance_collector = None
        self.allocators = {}
        self.history_store = None
        self.history_collector = None
        self.panel = None
        self.panel_button = None
        self.panel_display = None

    # --- Ereignisse ------------------------------------------------------

    def _on_pulse(self, meter_id: str) -> None:
        state = self.db.state(meter_id)
        LOG.info('Impuls %s — Stand %.3f kWh, %.0f W',
                 meter_id, state['energy_kwh'], state['power_w'])

    # --- Zustand ---------------------------------------------------------

    def states(self) -> dict:
        """Aktuelle Stände, mit Leistung 0 nach Ablauf des Timeouts.

        Ohne diesen Timeout würde nach dem letzten Impuls dauerhaft die
        Leistung des letzten Impulsabstands stehenbleiben — bei einer
        Wohnung, in der nachts nichts läuft, wäre das schlicht falsch.

        Der Timeout kommt je Zähler aus dessen `min_power_w` und hängt
        damit an der Impulsrate: ein Zähler mit 100 Imp/kWh braucht für
        dieselbe Grenzleistung das Zehnfache an Zeit. Ein gemeinsamer Wert
        für alle Zähler wäre nur richtig, solange alle dieselbe Rate haben.
        """
        states = self.db.all_states()
        fallback = self.config.power_timeout_seconds
        now = time.time()

        for meter_id, state in states.items():
            meter = self.config.meters.get(meter_id)
            timeout = (meter.power_timeout_seconds
                       if meter is not None else fallback)
            last = state['last_pulse']
            if last is None or now - last > timeout:
                state['power_w'] = 0.0
        return states

    def house_balance(self) -> dict | None:
        """Letzter Messwert der Hausbilanz, für die Statusanzeige."""
        if self.balance_store is None:
            return None
        row = self.balance_store.latest()
        if row is None:
            return None
        ts, production, consumption, grid, battery, percent, source = row
        return {
            'ts': ts,
            'production_w': production,
            'consumption_w': consumption,
            'grid_w': grid,
            'grid_import_w': max(0.0, grid),
            'grid_export_w': max(0.0, -grid),
            'battery_w': battery,
            'battery_percent': percent,
            'source': source,
        }

    def comparison(self) -> dict | None:
        """Wohnungen gegen Hausbilanz, über Energie statt Leistung.

        Der Vergleich der Momentanleistungen schwankt stark: ein Zähler
        meldet aus dem Abstand zweier Impulse und springt bei jeder
        Lastspitze, die Hausbilanz misst nur alle 60 s. Über die aufsummierte
        Energie mittelt sich das heraus — je länger der Zeitraum, desto
        belastbarer die Abweichung.

        Verglichen wird nur der Zeitraum, für den BEIDE Seiten Daten haben.
        Die Impulse laufen ab Dienststart, die Bilanzreihe kann älter sein.
        """
        if self.balance_store is None:
            return None

        house_kwh, seconds, samples = self.balance_store.consumption_energy()
        if samples < 2 or seconds <= 0:
            return None

        # Die Bilanzreihe gibt den Zeitraum vor. Impulse davor gehören nicht
        # dazu, sonst stünde mehr Wohnungsenergie gegen weniger Hausenergie.
        first_row = self.balance_store.first_ts()
        counts, _, _ = self.db.pulse_span(since=first_row)

        flats_kwh = 0.0
        for meter_id, count in counts.items():
            meter = self.config.meters.get(meter_id)
            if meter is None:
                continue
            flats_kwh += count * meter.wh_per_pulse / 1000.0

        rest_kwh = house_kwh - flats_kwh
        return {
            'seconds': round(seconds, 1),
            'samples': samples,
            'house_kwh': round(house_kwh, 4),
            'flats_kwh': round(flats_kwh, 4),
            'rest_kwh': round(rest_kwh, 4),
            'rest_percent': (round(rest_kwh / house_kwh * 100.0, 2)
                             if house_kwh > 0 else None),
        }

    # Was sich als Verlauf abrufen lässt: Kennung -> (Quelle, Feld, Einheit).
    # Eine feste Liste, damit kein Feldname aus der Anfrage in eine Abfrage
    # gerät — und damit die Anzeige weiß, was es überhaupt gibt.
    VERLAEUFE = {
        'system.temperature': ('system', 'temperature_c', '°C'),
        'system.load': ('system', 'load_percent', '%'),
        'system.memory': ('system', 'memory_used_percent', '%'),
        'system.disk': ('system', 'disk_used_percent', '%'),
        'house.consumption': ('balance', 'consumption_w', 'W'),
        'house.production': ('balance', 'production_w', 'W'),
        'house.grid': ('balance', 'grid_w', 'W'),
        'house.battery': ('balance', 'battery_percent', '%'),
    }

    def history(self, kennung: str, seit_sekunden: float,
                max_punkte: int = 300) -> dict:
        """Verlauf einer Größe. Wirft ValueError bei unbekannter Kennung.

        Zählerverläufe (`meter.<id>`) kommen aus den Rohimpulsen und werden
        gezählt, nicht aus dem Impulsabstand gerechnet — siehe
        `Database.power_series`.
        """
        jetzt = time.time()
        ab = jetzt - seit_sekunden

        if kennung.startswith('meter.'):
            meter_id = kennung.split('.', 1)[1]
            meter = self.config.meters.get(meter_id)
            if meter is None:
                raise ValueError(f'unbekannter Zähler: {meter_id}')

            raster = max(60.0, seit_sekunden / max_punkte)
            zeilen = self.db.power_series(meter_id, ab, jetzt, raster)
            punkte = [
                {'t': feld,
                 'avg': anzahl * meter.wh_per_pulse * 3600.0 / raster,
                 'min': None, 'max': None}
                for feld, anzahl in zeilen
            ]
            return {'key': kennung, 'unit': 'W', 'level': 'pulses',
                    'bucket_seconds': raster, 'from': ab, 'to': jetzt,
                    'points': punkte}

        if kennung not in self.VERLAEUFE:
            raise ValueError(f'unbekannter Verlauf: {kennung}')

        quelle, feld, einheit = self.VERLAEUFE[kennung]

        if quelle == 'system':
            if self.history_store is None:
                raise LookupError('Verlauf der Rechnerwerte ist abgeschaltet')
            reihe = self.history_store.series(feld, seit_sekunden, jetzt,
                                              max_punkte)
            reihe['key'] = kennung
            reihe['unit'] = einheit
            return reihe

        if self.balance_store is None:
            raise LookupError('Hausbilanz ist abgeschaltet')
        raster = max(60.0, seit_sekunden / max_punkte)
        zeilen = self.balance_store.series(feld, ab, jetzt, raster)
        return {
            'key': kennung, 'unit': einheit, 'level': 'balance',
            'bucket_seconds': raster, 'from': ab, 'to': jetzt,
            'points': [{'t': z[0], 'avg': z[1], 'min': z[2], 'max': z[3]}
                       for z in zeilen],
        }

    # Zeiträume, die die Anzeige anbietet. Feste Liste, damit kein beliebiger
    # Wert aus der Anfrage eine Rechnung über Jahre auslöst — auf einem Pi 3B
    # dauert das Minuten und hielte solange die Datenbank.
    ABRECHNUNGSZEITRAEUME = {
        'today': ('heute', None),        # seit Mitternacht, Sonderfall
        '24h': ('24 Stunden', 86400.0),
        '7d': ('7 Tage', 7 * 86400.0),
        '30d': ('30 Tage', 30 * 86400.0),
        '365d': ('365 Tage', 365 * 86400.0),
    }

    # Wie viele Kalenderjahre und -monate rückwärts angeboten werden. Nicht
    # unbegrenzt: jeder Eintrag wird zu einem Knopf in der Anzeige, und für
    # ein Jahr ohne Daten eine Rechnung anzustoßen bringt niemandem etwas.
    JAHRE_RUECKWAERTS = 3
    MONATE_RUECKWAERTS = 12

    # Ab dieser Abdeckung gilt eine Abrechnung als belastbar. Darunter fehlen
    # zu viele Intervalle, als dass die Verteilung den Tagesverlauf noch
    # richtig träfe — und genau der Verlauf ist der Kern des Verfahrens.
    ABDECKUNG_BELASTBAR = 0.9
    # Darunter ist die Aussage wertlos und wird als solche gekennzeichnet.
    ABDECKUNG_UNBRAUCHBAR = 0.5

    def _zeitraum_aufloesen(self, zeitraum: str, jetzt: float) -> tuple:
        """Zeitraumkennung in (start, ende, beschriftung) übersetzen.

        Drei Formen:

          rollierend   '24h', '7d' — die letzten n Sekunden ab jetzt
          Kalenderjahr '2026'      — 1. Januar bis 1. Januar des Folgejahrs
          Kalendermonat '2026-08'  — Monatsanfang bis Monatsanfang

        Kalenderzeiträume rechnen in **Ortszeit**, nicht in UTC: eine
        Jahresabrechnung, die am 1. Januar um 01:00 beginnt, wäre keine.
        `mktime` mit `tm_isdst=-1` lässt die Bibliothek entscheiden, ob
        Sommer- oder Winterzeit gilt — dadurch sind Jahre über die
        Zeitumstellung hinweg korrekt begrenzt und 23- oder 25-stündige
        Tage richtig enthalten.

        Ein zukünftiger Kalenderzeitraum wird am aktuellen Zeitpunkt
        abgeschnitten, statt bis in die Zukunft zu rechnen: sonst stünde
        das laufende Jahr mit einer Abdeckung von 60 % da, obwohl kein
        einziger Messpunkt fehlt.
        """
        if zeitraum in self.ABRECHNUNGSZEITRAEUME:
            label, dauer = self.ABRECHNUNGSZEITRAEUME[zeitraum]
            if dauer is None:      # 'today' — seit Mitternacht Ortszeit
                heute = time.localtime(jetzt)
                start = time.mktime((heute.tm_year, heute.tm_mon,
                                     heute.tm_mday, 0, 0, 0, 0, 0, -1))
            else:
                start = jetzt - dauer
            return (start, jetzt, label)

        jahr, monat = self._kalender_zerlegen(zeitraum)
        start = time.mktime((jahr, monat or 1, 1, 0, 0, 0, 0, 0, -1))
        if monat is None:
            ende = time.mktime((jahr + 1, 1, 1, 0, 0, 0, 0, 0, -1))
            label = str(jahr)
        else:
            folgejahr = jahr + (1 if monat == 12 else 0)
            folgemonat = 1 if monat == 12 else monat + 1
            ende = time.mktime(
                (folgejahr, folgemonat, 1, 0, 0, 0, 0, 0, -1))
            label = f'{self.MONATSNAMEN[monat - 1]} {jahr}'

        if start > jetzt:
            raise ValueError(
                f'Zeitraum {zeitraum!r} liegt vollständig in der Zukunft')
        return (start, min(ende, jetzt), label)

    MONATSNAMEN = ('Januar', 'Februar', 'März', 'April', 'Mai', 'Juni',
                   'Juli', 'August', 'September', 'Oktober', 'November',
                   'Dezember')

    @staticmethod
    def _kalender_zerlegen(zeitraum: str) -> tuple:
        """'2026' oder '2026-08' in (jahr, monat|None) zerlegen.

        Streng geprüft: was hier durchrutscht, geht als Zeitspanne in eine
        Abrechnung. Die Jahresgrenzen sind großzügig, aber endlich — eine
        Anfrage nach dem Jahr 9999 würde eine Rechnung über Jahrtausende
        anstoßen.
        """
        teile = zeitraum.split('-')
        if len(teile) > 2 or not all(t.isdigit() for t in teile):
            raise ValueError(
                f'Unbekannter Zeitraum {zeitraum!r} — erwartet wird eine '
                f'Kennung wie 24h, 7d, 2026 oder 2026-08')

        jahr = int(teile[0])
        if not 2000 <= jahr <= 2200:
            raise ValueError(f'Jahr {jahr} liegt außerhalb des Erwartbaren')

        if len(teile) == 1:
            return (jahr, None)

        monat = int(teile[1])
        if not 1 <= monat <= 12:
            raise ValueError(f'Monat {monat} gibt es nicht')
        return (jahr, monat)

    def _kalenderzeitraeume(self, jetzt: float) -> list:
        """Kalenderjahre und -monate, für die Daten vorliegen könnten.

        Angeboten wird nur, was der Datenbestand hergibt: ohne Messpunkte
        vor dem 1. Januar hat ein Knopf für das Vorjahr keinen Zweck. Der
        früheste Zeitstempel der Bilanzreihe gibt die Grenze vor.
        """
        if self.balance_store is None:
            return []
        erster = self.balance_store.first_ts()
        if erster is None:
            return []

        eintraege = []
        heute = time.localtime(jetzt)
        beginn = time.localtime(erster)

        for jahr in range(heute.tm_year, beginn.tm_year - 1, -1):
            if len(eintraege) >= self.JAHRE_RUECKWAERTS:
                break
            eintraege.append({'key': str(jahr), 'label': str(jahr),
                              'kind': 'jahr'})

        monate = []
        jahr, monat = heute.tm_year, heute.tm_mon
        for _ in range(self.MONATE_RUECKWAERTS):
            folgejahr = jahr + (1 if monat == 12 else 0)
            folgemonat = 1 if monat == 12 else monat + 1
            schluss = time.mktime(
                (folgejahr, folgemonat, 1, 0, 0, 0, 0, 0, -1))
            # Verglichen wird das ENDE des Monats, nicht sein Anfang: der
            # laufende Monat hat Daten, auch wenn die Aufzeichnung erst am
            # Fünfzehnten begann. Am Anfang gemessen fiele er heraus.
            if schluss <= erster:
                break
            monate.append({'key': f'{jahr}-{monat:02d}',
                           'label': f'{self.MONATSNAMEN[monat - 1]} {jahr}',
                           'kind': 'monat'})
            monat -= 1
            if monat == 0:
                jahr, monat = jahr - 1, 12

        return eintraege + monate

    def allocation(self, zeitraum: str = '24h',
                   model: str = 'proportional') -> dict:
        """Verteilung des Netzbezugs auf die Wohnungen für einen Zeitraum.

        Das ist die Rechnung, für die die Anlage gebaut wurde: was hat jede
        Wohnung verbraucht, und wie viel davon kam aus der PV statt aus dem
        Netz. Bisher gab es sie nur im Jahresbericht.

        Der Zeitraum wird auf das Abrechnungsraster gelegt, damit die
        Intervallgrenzen dieselben sind wie in der Bilanzreihe. Ohne das
        wäre das erste und letzte Intervall angeschnitten und die Anteile
        entsprechend daneben.
        """
        if self.balance_store is None:
            raise LookupError('Hausbilanz ist abgeschaltet')
        allocator = self.allocators.get(model)
        if allocator is None:
            raise ValueError(
                f'Unbekanntes Verteilmodell {model!r}. Möglich: '
                + ', '.join(self.allocators))

        raster = allocator.interval_seconds
        jetzt = time.time()
        start, ende, label = self._zeitraum_aufloesen(zeitraum, jetzt)

        # Auf Rastergrenzen legen: das laufende Intervall bleibt draußen,
        # weil es noch unvollständig ist und die letzte Viertelstunde sonst
        # als Einbruch erschiene.
        ende = (ende // raster) * raster
        start = (start // raster) * raster
        if start >= ende:
            start = ende - raster

        ergebnis = allocator.allocate(start, ende)
        # Die Klarnamen mitgeben: die Anzeige soll sie nicht aus einer
        # zweiten Quelle zusammensuchen müssen und auch nicht selbst führen.
        ergebnis['names'] = {
            meter_id: meter.name
            for meter_id, meter in self.config.meters.items()
        }
        ergebnis['range'] = {'key': zeitraum, 'label': label,
                             'start_ts': start, 'end_ts': ende}
        ergebnis['quality']['verdict'] = self._abrechnung_bewerten(
            ergebnis['quality'], start, ende, ergebnis['house'])
        from .balance import MODEL_LABELS
        ergebnis['models'] = [
            {'key': k, 'label': MODEL_LABELS[k]} for k in self.allocators]
        ergebnis['ranges'] = [
            {'key': k, 'label': v[0], 'kind': 'rollierend'}
            for k, v in self.ABRECHNUNGSZEITRAEUME.items()
        ] + self._kalenderzeitraeume(jetzt)
        return ergebnis

    @staticmethod
    def _dauer_text(sekunden: float) -> str:
        """Zeitspanne in einer Form, die man vorlesen kann.

        Für Begründungen gedacht, nicht zum Rechnen: "3 h 20 min" sagt mehr
        als "12013 s", und die Nachkommastelle interessiert dort niemanden.
        """
        sekunden = max(0.0, sekunden)
        if sekunden < 90:
            return f'{sekunden:.0f} s'
        minuten = sekunden / 60
        if minuten < 90:
            return f'{minuten:.0f} min'
        stunden = int(minuten // 60)
        rest = int(minuten % 60)
        if stunden < 48:
            return f'{stunden} h {rest} min' if rest else f'{stunden} h'
        tage, stunden = divmod(stunden, 24)
        return f'{tage} d {stunden} h' if stunden else f'{tage} d'

    def _abrechnung_bewerten(self, quality: dict, start: float,
                             end: float, house: dict | None = None) -> dict:
        """Wie weit trägt diese Abrechnung?

        Drei Ausgänge statt zwei: belastbar, eingeschränkt, unbrauchbar.
        Ein fehlender dritter Ausgang würde eine Rechnung über zwei Stunden
        Daten genauso aussehen lassen wie eine über ein Jahr — die Zahlen
        stimmen in beiden Fällen, aber nur eine davon sagt etwas aus.

        Bewertet werden zwei verschiedene Dinge: wie vollständig die Daten
        im Zeitraum sind (Abdeckung) und ob der Zeitraum überhaupt lang
        genug ist, um einen Tagesverlauf abzubilden. Beides kann einzeln
        schiefgehen.
        """
        abdeckung = quality['coverage']
        gemessen = end - start
        gruende = []

        if abdeckung < self.ABDECKUNG_UNBRAUCHBAR:
            stufe = 'unbrauchbar'
            gruende.append(
                f'Nur {abdeckung*100:.0f} % des Zeitraums sind aufgezeichnet.')
        elif abdeckung < self.ABDECKUNG_BELASTBAR:
            stufe = 'eingeschraenkt'
            gruende.append(
                f'{(1-abdeckung)*100:.0f} % des Zeitraums fehlen in der '
                f'Bilanzreihe.')
        else:
            stufe = 'belastbar'

        # Unter einem vollen Tag ist die PV-Verteilung nicht aussagekräftig:
        # eine Rechnung über den Vormittag sieht für einen Nachtverbraucher
        # ganz anders aus als über 24 Stunden.
        if gemessen < 86400.0:
            if stufe == 'belastbar':
                stufe = 'eingeschraenkt'
            gruende.append(
                'Der Zeitraum ist kürzer als ein Tag — Tag- und '
                'Nachtverbrauch sind noch nicht ausgeglichen.')

        if quality['intervals_without_pulses']:
            gruende.append(
                f'{quality["intervals_without_pulses"]} Intervalle ohne '
                f'Impuls einer Wohnung; deren Netzbezug ist nicht zugeordnet.')

        if quality['gaps']:
            gruende.append(
                f'{len(quality["gaps"])} vermerkte Ausfälle der Quelle.')

        # Erkannte Löcher: hier war der Rechner selbst weg. Wichtiger als
        # die vermerkten, weil in diesem Fall niemand etwas eintragen
        # konnte — und weil die Impulse dieser Zeit ebenfalls fehlen.
        erkannt = quality.get('detected_gaps') or []
        if erkannt:
            fehlend = sum(g['seconds'] for g in erkannt)
            laengste = max(erkannt, key=lambda g: g['seconds'])
            wann = time.strftime('%d.%m.%Y %H:%M',
                                 time.localtime(laengste['start_ts']))
            gruende.append(
                f'{len(erkannt)} Aufzeichnungslücken über zusammen '
                f'{self._dauer_text(fehlend)} — längste ab {wann} '
                f'({self._dauer_text(laengste["seconds"])}). In dieser Zeit '
                f'lief die Erfassung nicht; die Impulse fehlen ebenfalls.')
            if stufe == 'belastbar':
                stufe = 'eingeschraenkt'

        # Der ungedeckelte Anteil sagt, wie viel vom Hausbezug die Rechnung
        # überhaupt zuordnen konnte. Steht er hoch, läuft ein großer Teil
        # hinter keinem Zähler — die Wohnungszahlen bleiben richtig, aber
        # sie beschreiben dann nur einen Teil des Hauses.
        if house and house.get('grid_kwh', 0) > 0:
            rest = house.get('unallocated_grid_kwh', 0.0)
            anteil = rest / house['grid_kwh']
            if anteil > 0.25:
                gruende.append(
                    f'{anteil*100:.0f} % des Hausbezugs sind keiner Wohnung '
                    f'zugeordnet ({rest:.1f} kWh) — Anlagentechnik und '
                    f'Verbraucher hinter keinem Zähler.')

        return {'level': stufe, 'reasons': gruende}

    # --- Ausgabe der Abrechnung ------------------------------------------
    #
    # Beide Wege bekommen eine fertig gerechnete Zuteilung übergeben und
    # rechnen selbst nichts. Damit stehen im Dokument zwangsläufig dieselben
    # Zahlen wie am Bildschirm; ein eigener Rechenweg wäre eine zweite
    # Wahrheit, die eines Tages auseinanderläuft.

    def allocation_csv(self, daten: dict) -> bytes:
        """Abrechnung als CSV für Tabellenkalkulationen.

        Semikolon als Trenner und ein BOM voran: Excel im deutschen
        Gebietsschema erwartet beides. Ohne BOM zerlegt es die Umlaute, ohne
        Semikolon steht die ganze Zeile in einer Spalte. LibreOffice kommt
        mit beidem zurecht.

        Die Kopfzeilen nennen den Zeitraum, das Verteilmodell und das Urteil
        über die Belastbarkeit — eine Tabelle wird weitergereicht, und ohne
        diese Angaben wüsste der Empfänger nicht, was er vor sich hat.
        """
        puffer = io.StringIO()
        schreiber = csv.writer(puffer, delimiter=';', lineterminator='\r\n')

        def datum(ts):
            return time.strftime('%d.%m.%Y %H:%M', time.localtime(ts))

        qualitaet = daten.get('quality', {})
        urteil = qualitaet.get('verdict', {})
        schreiber.writerow(['Stromabrechnung'])
        schreiber.writerow(['Zeitraum von', datum(daten['start_ts'])])
        schreiber.writerow(['Zeitraum bis', datum(daten['end_ts'])])
        schreiber.writerow(['Verteilmodell', daten.get('model_label', '')])
        schreiber.writerow(['Belastbarkeit', urteil.get('level', '')])
        for grund in urteil.get('reasons', []):
            schreiber.writerow(['Hinweis', grund])
        schreiber.writerow([])

        # Zahlen mit Komma: dieselbe Rücksicht auf das Gebietsschema wie beim
        # Trennzeichen. Eine Zahl mit Punkt liest Excel als Text.
        def zahl(wert, stellen=3):
            return f'{wert:.{stellen}f}'.replace('.', ',')

        namen = {mid: z['name'] for mid, z in self.db.all_states().items()}
        schreiber.writerow(['Wohnung', 'Name', 'Verbrauch kWh',
                            'aus PV und Speicher kWh', 'aus dem Netz kWh',
                            'PV-Anteil %'])
        for mid in ordnung.sortiert(daten.get('meters', {})):
            m = daten['meters'][mid]
            verbrauch = m['consumption_kwh']
            anteil = (m['solar_kwh'] / verbrauch * 100) if verbrauch > 0 else 0.0
            name = namen.get(mid, mid.upper())
            schreiber.writerow([mid, name, zahl(verbrauch),
                                zahl(m['solar_kwh']), zahl(m['grid_kwh']),
                                zahl(anteil, 1)])

        haus = daten.get('house', {})
        if haus:
            schreiber.writerow([])
            schreiber.writerow(['Haus'])
            schreiber.writerow(['Netzbezug gesamt kWh',
                                zahl(haus.get('grid_kwh', 0.0))])
            schreiber.writerow(['davon nicht zugeordnet kWh',
                                zahl(haus.get('unallocated_grid_kwh', 0.0))])
            schreiber.writerow(['Erzeugung kWh', zahl(haus.get('pv_kwh', 0.0))])

        # utf-8-sig setzt das BOM. Der Rest ist gewöhnliches UTF-8.
        return puffer.getvalue().encode('utf-8-sig')

    def allocation_pdf(self, daten: dict) -> bytes:
        """Abrechnung als PDF.

        Nutzt denselben Berichtsbau wie der Jahresbericht, statt ein zweites
        Layout zu pflegen — `YearReport` bekommt ohnehin eine fertige
        Zuteilung übergeben und erkennt am Zeitraum selbst, ob es ein volles
        Kalenderjahr ist.
        """
        from . import report as report_module

        # Namen und Zählerstände aus derselben Quelle wie der Jahresbericht.
        zustaende = self.db.all_states()
        namen = {mid: z['name'] for mid, z in zustaende.items()}
        bericht = report_module.YearReport(
            daten, namen, zustaende, self.config.report)
        # YearReport.build() liefert das Dokument, dessen build() die Bytes.
        return bericht.build().build()

    def history_keys(self) -> dict:
        """Welche Verläufe abrufbar sind — Quelle der Wahrheit für die Anzeige.

        Ohne diese Liste müsste das Dashboard wissen, ob Bilanz und
        Rechnerreihe eingeschaltet sind, und die Kennungen doppeln. Eine
        zweite Liste, die man synchron halten muss, ist eine Gelegenheit für
        einen stillen Fehler.
        """
        verfuegbar = []
        for kennung, (quelle, _, einheit) in self.VERLAEUFE.items():
            if quelle == 'system' and self.history_store is None:
                continue
            if quelle == 'balance' and self.balance_store is None:
                continue
            verfuegbar.append({'key': kennung, 'unit': einheit})

        for meter_id, meter in self.config.meters.items():
            verfuegbar.append({'key': f'meter.{meter_id}', 'unit': 'W',
                               'name': meter.name})

        return {'keys': verfuegbar}

    def status(self) -> dict:
        balance = {'enabled': self.balance_collector is not None}
        if self.balance_collector is not None:
            balance.update(self.balance_collector.statistics())
            balance['latest'] = self.house_balance()
            balance['samples'] = self.balance_store.count()
            balance['comparison'] = self.comparison()

        return {
            'service': 's0counter',
            'fassung': fassung_fuer_status(),
            'time': time.time(),
            'uptime_seconds': round(time.time() - self.started_at, 1),
            'meters': self.states(),
            'counters': {mid: c.statistics()
                         for mid, c in self.counters.items()},
            'homeassistant': {
                'enabled': self.ha is not None,
                'publishes': self.ha.publishes if self.ha else 0,
                'failures': self.ha.failures if self.ha else 0,
            },
            'mqtt': {'enabled': self.mqtt is not None},
            'balance': balance,
            # Der Rechner selbst: ein Pi im Schrank fällt nicht laut aus, er
            # drosselt bei Hitze und füllt langsam die Karte.
            'system': system.status(self.config.database),
            'history': {
                'enabled': self.history_collector is not None,
                **(self.history_collector.statistics()
                   if self.history_collector is not None else {}),
                'samples': (self.history_store.count()
                            if self.history_store is not None else 0),
            },
            'panel': {
                'enabled': self.panel is not None,
                'awake': self.panel.awake if self.panel else False,
                'page': self.panel.page if self.panel else None,
            },
        }

    # --- Anbindungen -----------------------------------------------------

    def setup_mqtt(self) -> None:
        if not self.config.mqtt.enabled:
            return
        from .mqtt import MqttPublisher
        try:
            self.mqtt = MqttPublisher(self.config.mqtt, self.config.meters)
            self.mqtt.connect()
        except Exception as exc:
            LOG.error('MQTT-Verbindung fehlgeschlagen: %s '
                      '(Erfassung läuft weiter)', exc)
            self.mqtt = None

    def setup_homeassistant(self) -> None:
        if not self.config.homeassistant.enabled:
            return
        from .homeassistant import HomeAssistantClient, HomeAssistantError
        settings = self.config.homeassistant
        try:
            self.ha = HomeAssistantClient(
                settings.url, settings.token, settings.prefix, settings.timeout)
        except HomeAssistantError as exc:
            LOG.error('Home Assistant nicht erreichbar: %s '
                      '(Erfassung läuft weiter, Anbindung wird erneut '
                      'versucht)', exc)
            self.ha = HomeAssistantClient(
                settings.url, settings.token, settings.prefix, settings.timeout,
                verify_on_start=False)

    def setup_history(self) -> None:
        """Aufzeichnung der Rechnerwerte starten.

        Eigene Datei neben Impuls- und Bilanzdatenbank. Wie beim Bilanz-
        sammler gilt: ein Fehler bleibt in diesem Thread.
        """
        if not self.config.history.enabled:
            return

        from .history import HistoryCollector, HistoryStore

        directory = os.path.dirname(self.config.history.database)
        if directory:
            os.makedirs(directory, exist_ok=True)

        self.history_store = HistoryStore(self.config.history.database)
        self.history_collector = HistoryCollector(
            self.history_store,
            lambda: system.status(self.config.database),
            self.config.history.sample_interval_seconds)
        self.history_collector.start()
        LOG.info('Verlauf der Rechnerwerte: alle %.0f s nach %s',
                 self.config.history.sample_interval_seconds,
                 self.config.history.database)

    def setup_balance(self) -> None:
        """Aufzeichnung der Hausbilanz starten.

        Läuft in einem eigenen Thread. Ein Ausfall der Quelle bleibt dort
        und erreicht die Impulserfassung nicht.
        """
        if not self.config.balance.enabled:
            return

        from . import sources
        from .balance import (MODELS, Allocator, BalanceCollector,
                              BalanceStore)

        source = sources.build(self.config)
        if source is None:
            LOG.error('Hausbilanz aktiviert, aber keine Quelle konfiguriert')
            return

        directory = os.path.dirname(self.config.balance.database)
        if directory:
            os.makedirs(directory, exist_ok=True)

        self.balance_store = BalanceStore(self.config.balance.database)
        self.balance_collector = BalanceCollector(
            source, self.balance_store,
            self.config.balance.sample_interval_seconds)
        self.balance_collector.start()

        # Je Verteilmodell ein Allocator, dauerhaft gehalten — sonst wäre
        # der Zwischenspeicher nach jedem Abruf wieder leer. Sie halten nur
        # Ergebnisse, keine eigene Verbindung; mehrere HTTP-Threads dürfen
        # sie gleichzeitig benutzen.
        self.allocators = {
            name: Allocator(
                self.balance_store, self.db,
                self.config.balance.allocation_interval_seconds, name)
            for name in MODELS
        }

    def setup_panel(self) -> None:
        """Anzeigepanel starten.

        Scheitert der Aufbau — Adapter nicht gesteckt, Display ohne
        Versorgung —, läuft die Erfassung ohne Anzeige weiter. Ein Panel
        im Hausflur darf die Zählung nicht aufhalten.
        """
        if not self.config.panel.enabled:
            return

        settings = self.config.panel
        try:
            from edip320.transport import Display, open_serial
        except ImportError:
            LOG.error('Anzeigepanel aktiviert, aber das Paket edip320 fehlt. '
                      'Siehe README, Abschnitt Anzeigepanel.')
            return

        try:
            port = open_serial(settings.port, settings.baud)
        except Exception as exc:
            LOG.error('Anzeigepanel: %s kann nicht geöffnet werden: %s '
                      '(Erfassung läuft weiter)', settings.port, exc)
            return

        from .panel import Panel, PanelButton

        self.panel_display = Display(port)
        self.panel = Panel(self.panel_display, self,
                           settings.blank_after_seconds,
                           settings.refresh_seconds)
        self.panel.start()
        LOG.info('Anzeigepanel an %s mit %d Baud',
                 settings.port, settings.baud)

        if settings.button_gpio >= 0:
            try:
                self.panel_button = PanelButton(settings.button_gpio,
                                                self.panel)
                self.panel_button.setup()
            except Exception as exc:
                LOG.error('Taster an GPIO%d: %s',
                          settings.button_gpio, exc)
                self.panel_button = None

    def setup_http(self) -> None:
        service = self

        # Die Anzeige besteht aus mehreren Dateien neben diesem Modul. Sie
        # werden aus einer festen Zuordnung geliefert, nicht aus dem Pfad der
        # Anfrage zusammengesetzt: ein aus der URL gebauter Dateiname wäre
        # ein Pfaddurchgriff auf das ganze Dateisystem.
        #
        # Wer hier ein Modul vergisst, bekommt keinen Fehler im Dienst,
        # sondern eine leere Seite im Browser — deshalb prüft
        # test_service.py, dass jeder `import` aus dashboard.js hier steht.
        assets = {
            '/dashboard': ('dashboard.html', 'text/html; charset=utf-8'),
            '/dashboard.js': ('dashboard.js',
                              'text/javascript; charset=utf-8'),
            '/dashboard-chart.js': ('dashboard-chart.js',
                                    'text/javascript; charset=utf-8'),
            '/dashboard-verteilung.js': ('dashboard-verteilung.js',
                                         'text/javascript; charset=utf-8'),
        }
        asset_dir = Path(__file__).resolve().parent

        class Handler(BaseHTTPRequestHandler):
            def send_asset(self, name: str, content_type: str) -> None:
                try:
                    body = (asset_dir / name).read_bytes()
                except OSError:
                    self.send_error(404, 'Anzeige nicht installiert')
                    return
                self.send_response(200)
                self.send_header('Content-Type', content_type)
                self.send_header('Content-Length', str(len(body)))
                # Die Anzeige holt sich ihre Werte selbst; ein Zwischen-
                # speicher würde nach einem Update die alte Fassung zeigen.
                self.send_header('Cache-Control', 'no-store')
                self.end_headers()
                self.wfile.write(body)

            def send_json(self, daten: dict, status_code: int = 200) -> None:
                body = json.dumps(daten, ensure_ascii=False,
                                  indent=2).encode('utf-8')
                self.send_response(status_code)
                self.send_header('Content-Type',
                                 'application/json; charset=utf-8')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def handle_history(self, query: str) -> None:
                felder = parse_qs(query)
                kennung = (felder.get('key') or [''])[0]

                try:
                    fenster = float((felder.get('seconds') or ['3600'])[0])
                    punkte = int((felder.get('points') or ['300'])[0])
                except ValueError:
                    self.send_json({'error': 'seconds und points müssen '
                                             'Zahlen sein'}, 400)
                    return

                # Grenzen, damit eine Anfrage den Dienst nicht lahmlegt: ein
                # Jahr in Rohauflösung wären 525.000 Zeilen, und die Erfassung
                # läuft im selben Prozess.
                if not 60.0 <= fenster <= 366 * 86400.0:
                    self.send_json({'error': 'seconds muss zwischen 60 und '
                                             '31622400 liegen'}, 400)
                    return
                if not 10 <= punkte <= 2000:
                    self.send_json({'error': 'points muss zwischen 10 und '
                                             '2000 liegen'}, 400)
                    return

                try:
                    self.send_json(service.history(kennung, fenster, punkte))
                except ValueError as exc:
                    self.send_json({'error': str(exc)}, 404)
                except LookupError as exc:
                    self.send_json({'error': str(exc)}, 503)

            def send_datei(self, body: bytes, content_type: str,
                           dateiname: str) -> None:
                """Eine Datei zum Herunterladen ausliefern.

                `attachment` mit Namen, sonst zeigt der Browser die CSV als
                Textwüste an und das PDF in einem Betrachter ohne Hinweis,
                worum es sich handelt.
                """
                self.send_response(200)
                self.send_header('Content-Type', content_type)
                self.send_header('Content-Length', str(len(body)))
                self.send_header('Content-Disposition',
                                 f'attachment; filename="{dateiname}"')
                self.send_header('Cache-Control', 'no-store')
                self.end_headers()
                self.wfile.write(body)

            def handle_allocation_export(self, query: str, format: str) -> None:
                """Abrechnung als CSV oder PDF.

                Rechnet dieselbe Zuteilung wie `/allocation` und gießt sie in
                eine Datei — die Zahlen im Dokument sind damit zwangsläufig
                dieselben wie am Bildschirm. Ein zweiter Rechenweg wäre eine
                zweite Wahrheit.
                """
                felder = parse_qs(query)
                zeitraum = (felder.get('range') or ['24h'])[0]
                model = (felder.get('model') or ['proportional'])[0]

                try:
                    daten = service.allocation(zeitraum, model)
                except ValueError as exc:
                    self.send_json({'error': str(exc)}, 400)
                    return
                except LookupError as exc:
                    self.send_json({'error': str(exc)}, 503)
                    return

                stempel = time.strftime(
                    '%Y-%m-%d', time.localtime(daten['start_ts']))
                try:
                    if format == 'csv':
                        body = service.allocation_csv(daten)
                        self.send_datei(body, 'text/csv; charset=utf-8',
                                        f'abrechnung-{stempel}-{zeitraum}.csv')
                    else:
                        body = service.allocation_pdf(daten)
                        self.send_datei(body, 'application/pdf',
                                        f'abrechnung-{stempel}-{zeitraum}.pdf')
                except Exception as exc:       # noqa: BLE001
                    # Ein misslungener Export darf die Erfassung nicht
                    # mitreißen: sie läuft im selben Prozess.
                    LOG.exception('Export (%s) fehlgeschlagen', format)
                    self.send_json({'error': f'Export fehlgeschlagen: {exc}'},
                                   500)

            def handle_allocation(self, query: str) -> None:
                felder = parse_qs(query)
                zeitraum = (felder.get('range') or ['24h'])[0]
                model = (felder.get('model') or ['proportional'])[0]

                try:
                    self.send_json(service.allocation(zeitraum, model))
                except ValueError as exc:
                    self.send_json({'error': str(exc)}, 400)
                except LookupError as exc:
                    self.send_json({'error': str(exc)}, 503)

            def do_GET(self):
                pfad, _, query = self.path.partition('?')

                asset = assets.get(pfad)
                if asset is not None:
                    self.send_asset(*asset)
                    return

                if pfad == '/history':
                    self.handle_history(query)
                    return

                if pfad == '/allocation':
                    self.handle_allocation(query)
                    return

                if pfad == '/allocation.csv':
                    self.handle_allocation_export(query, 'csv')
                    return

                if pfad == '/allocation.pdf':
                    self.handle_allocation_export(query, 'pdf')
                    return

                routes = {
                    '/': service.status,
                    '/status': service.status,
                    '/health': lambda: {'ok': True},
                    '/audit': lambda: {'audit': service.db.audit_log()},
                    # Was sich als Verlauf abrufen lässt — die Anzeige baut
                    # ihre Umschalter daraus, statt die Liste zu doppeln.
                    '/history/keys': service.history_keys,
                }
                handler = routes.get(pfad)
                if handler is None:
                    self.send_error(404, 'Unbekannter Pfad')
                    return

                self.send_json(handler())

            def log_message(self, fmt, *args):
                LOG.debug('HTTP ' + fmt, *args)

        self.http_server = ThreadingHTTPServer(
            (self.config.http_host, self.config.http_port), Handler)
        threading.Thread(target=self.http_server.serve_forever,
                         daemon=True).start()
        LOG.info('Status unter http://%s:%d/status, Anzeige unter '
                 'http://%s:%d/dashboard',
                 self.config.http_host, self.config.http_port,
                 self.config.http_host, self.config.http_port)

    def publish(self) -> None:
        states = self.states()
        if self.mqtt is not None:
            try:
                self.mqtt.publish_states(states)
            except Exception as exc:
                LOG.warning('MQTT-Veröffentlichung fehlgeschlagen: %s', exc)
        if self.ha is not None:
            self.ha.publish_all(states)

    # --- Ablauf ----------------------------------------------------------

    def run(self) -> int:
        self.gpio.setup()
        self.setup_mqtt()
        self.setup_homeassistant()
        self.setup_balance()
        self.setup_history()
        self.setup_panel()
        self.setup_http()

        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: self.stop_event.set())

        LOG.info('Erfassung läuft für %d Zähler', len(self.counters))
        self.publish()

        interval = self.config.publish_interval_seconds
        while not self.stop_event.wait(interval):
            self.publish()

        return self.shutdown()

    def shutdown(self) -> int:
        LOG.info('Beende ...')
        if self.http_server is not None:
            self.http_server.shutdown()
        if self.balance_collector is not None:
            self.balance_collector.stop()
        if self.history_collector is not None:
            self.history_collector.stop()
        if self.panel is not None:
            self.panel.stop()
        if self.panel_button is not None:
            self.panel_button.close()
        if self.panel_display is not None:
            self.panel_display.close()
        self.gpio.close()
        if self.mqtt is not None:
            self.mqtt.disconnect()
        if self.balance_store is not None:
            self.balance_store.close()
        if self.history_store is not None:
            self.history_store.close()
        self.db.close()

        for meter_id, counter in self.counters.items():
            stats = counter.statistics()
            LOG.info('%s: %d Impulse, %d entprellt, %d unplausibel',
                     meter_id, stats['accepted'], stats['bounced'],
                     stats['implausible'])
        if self.balance_collector is not None:
            LOG.info('Hausbilanz: %d Messpunkte, %d Ausfälle',
                     self.balance_collector.collected,
                     self.balance_collector.failed)
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description='Eltako S0 Impulszähler')
    parser.add_argument('-c', '--config',
                        default='/etc/s0counter/config.toml')
    parser.add_argument('--check-config', action='store_true',
                        help='Konfiguration prüfen und beenden')
    parser.add_argument('--verbose', action='store_true')
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format='%(asctime)s %(levelname)s %(message)s')

    try:
        config = config_module.load(args.config)
    except config_module.ConfigError as exc:
        print(f'Konfigurationsfehler: {exc}', file=sys.stderr)
        return 2

    if args.check_config:
        print(f'Konfiguration in Ordnung: {args.config}')
        print(f'  Datenbank: {config.database}')
        print(f'  HTTP: {config.http_host}:{config.http_port}')
        for meter_id, meter in config.meters.items():
            print(f'  {meter_id:4} {meter.name:20} GPIO{meter.gpio:<3} '
                  f'{meter.pulses_per_kwh} Imp/kWh, '
                  f'active_{"low" if meter.active_low else "high"}')
        print()
        if config.balance.enabled:
            quellen = []
            if config.sonnenbatterie.enabled:
                quellen.append(f'Sonnenbatterie {config.sonnenbatterie.host}')
            if config.homeassistant.enabled:
                quellen.append('Home Assistant')
            print(f'  Hausbilanz: an — alle '
                  f'{config.balance.sample_interval_seconds:.0f} s, '
                  f'Abrechnungsfenster '
                  f'{config.balance.allocation_interval_seconds / 60:.0f} min')
            print(f'    Quellen: {" vor ".join(quellen)}')
        else:
            print('  Hausbilanz: aus — ohne sie keine Zuordnung des '
                  'Netzbezugs')

        if config.history.enabled:
            print(f'  Verlauf Rechnerwerte: an — alle '
                  f'{config.history.sample_interval_seconds:.0f} s '
                  f'nach {config.history.database}')
        else:
            print('  Verlauf Rechnerwerte: aus — keine Diagramme im '
                  'Dashboard')

        if config.panel.enabled:
            taster = (f'Taster GPIO{config.panel.button_gpio}'
                      if config.panel.button_gpio >= 0 else 'ohne Taster')
            print(f'  Anzeigepanel: an — {config.panel.port}, '
                  f'{config.panel.baud} Baud, {taster}')
        else:
            print('  Anzeigepanel: aus')

        if config.report.enabled:
            preis = (f'{config.report.price_ct_per_kwh:.2f} ct/kWh'
                     if config.report.price_ct_per_kwh > 0
                     else 'ohne Preis, nur kWh')
            print(f'  Jahresbericht: an — {config.report.directory}, {preis}')
        else:
            print('  Jahresbericht: aus')

        print(f'  Telegram: {"an" if config.telegram.enabled else "aus"}')
        print(f'  MQTT: {"an" if config.mqtt.enabled else "aus"}')
        print(f'  Home Assistant: '
              f'{"an — " + config.homeassistant.url if config.homeassistant.enabled else "aus"}')
        return 0

    return Service(config).run()


if __name__ == '__main__':
    sys.exit(main())
