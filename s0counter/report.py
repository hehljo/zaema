"""Jahresbericht je Wohnung als PDF.

Der Bericht trennt strikt zwischen **gemessen** und **gerechnet**:

  gemessen   Verbrauch je Wohnung, aus den S0-Impulsen
             Netzbezug des Hauses, aus der Sonnenbatterie
  gerechnet  Aufteilung des Netzbezugs auf die Wohnungen

Diese Trennung steht auch im PDF, damit ein Leser sie nicht erst erfragen
muss. Ein Beleg, der eine Rechnung als Messung ausgibt, wäre unbrauchbar.

Nicht eichfähig: Abrechnungsgrundlage bleiben die Zählerstände der
geeichten Eltako-Zähler. Der Bericht weist die Softwarestände aus und
fordert zum Abgleich auf.
"""
import logging
import os
import time

from . import ordnung
from . import pdf

LOG = logging.getLogger(__name__)

MARGIN = 56.0
RIGHT = pdf.PAGE_WIDTH - MARGIN
CONTENT = RIGHT - MARGIN


def _fmt(value: float, digits: int = 3) -> str:
    """Zahl mit deutschem Dezimalkomma und Tausenderpunkt."""
    text = f'{value:,.{digits}f}'
    return text.replace(',', '\x00').replace('.', ',').replace('\x00', '.')


def _date(ts: float) -> str:
    return time.strftime('%d.%m.%Y', time.localtime(ts))


class YearReport:
    """Baut den Jahresbericht aus einer Zuteilung."""

    def __init__(self, allocation: dict, meter_names: dict,
                 readings: dict | None = None, config=None):
        self.allocation = allocation
        self.meter_names = meter_names
        self.readings = readings or {}
        self.config = config
        self.doc = pdf.Document(
            title=self.titel,
            author=(config.landlord if config and config.landlord
                    else 's0counter'))

    @property
    def year(self) -> int:
        return time.localtime(self.allocation['start_ts']).tm_year

    @property
    def titel(self) -> str:
        """Überschrift des Berichts.

        Ein Bericht über ein volles Kalenderjahr heißt „Stromabrechnung 2026";
        über einen beliebigen Zeitraum wäre diese Jahreszahl irreführend —
        sie behauptete eine Vollständigkeit, die nicht da ist. Erkannt wird
        das an den Grenzen selbst, nicht an einem Schalter: wer den Bericht
        über exakt ein Kalenderjahr baut, bekommt die Jahresüberschrift, alle
        anderen bekommen den Zeitraum genannt.
        """
        start, ende = self.allocation['start_ts'], self.allocation['end_ts']
        jahr_start, jahr_ende = year_bounds(self.year)
        if abs(start - jahr_start) < 1 and abs(ende - jahr_ende) < 1:
            return f'Stromabrechnung {self.year}'
        return 'Stromabrechnung'

    @property
    def price_per_kwh(self) -> float:
        return (self.config.price_ct_per_kwh / 100.0) if self.config else 0.0

    # --- Bausteine -------------------------------------------------------

    @property
    def model(self) -> str:
        return self.allocation.get('model', 'proportional')

    @property
    def model_label(self) -> str:
        return self.allocation.get('model_label', 'anteilig nach Verbrauch')

    def _header(self, page, subtitle: str) -> float:
        page.text(MARGIN, 64, self.titel, 19, 'bold')
        page.text(MARGIN, 82, subtitle, 10, 'regular', 0.35)
        page.text(MARGIN, 96, f'Verteilmodell: {self.model_label}',
                  9, 'regular', 0.45)
        page.line(MARGIN, 106, RIGHT, 106, 1.0, 0.0)

        zeitraum = (f'{_date(self.allocation["start_ts"])} bis '
                    f'{_date(self.allocation["end_ts"] - 1)}')
        page.text_right(RIGHT, 64, zeitraum, 9, 'mono', 0.35)
        if self.config and self.config.landlord:
            page.text_right(RIGHT, 78, self.config.landlord, 9, 'regular', 0.35)
        return 138.0

    def _footer(self, page, number: int, total: int) -> None:
        y = pdf.PAGE_HEIGHT - 52
        page.line(MARGIN, y, RIGHT, y, 0.5, 0.7)
        page.text(MARGIN, y + 14,
                  f'Erstellt am {_date(time.time())} · nicht eichfähig, '
                  f'maßgeblich sind die Zählerstände',
                  7.5, 'regular', 0.45)
        page.text_right(RIGHT, y + 14, f'Seite {number} von {total}',
                        7.5, 'mono', 0.45)

    def _section(self, page, y: float, title: str) -> float:
        page.text(MARGIN, y, title.upper(), 8, 'bold', 0.4)
        page.line(MARGIN, y + 5, RIGHT, y + 5, 0.5, 0.75)
        return y + 22

    # --- Seite 1: Übersicht ---------------------------------------------

    def _overview(self, page) -> None:
        y = self._header(page, 'Übersicht aller Wohnungen')
        house = self.allocation['house']
        meters = self.allocation['meters']

        y = self._section(page, y, 'Haus gesamt')

        gesamt_verbrauch = sum(m['consumption_kwh'] for m in meters.values())
        # Die Anzahl kommt aus den Daten, nicht aus dem Text: der Bericht
        # lässt sich auch über eine Auswahl von Zählern bauen, und „drei"
        # stand hier fest verdrahtet, weil diese Anlage zufällig drei hat.
        kennzahlen = [
            ('Verbrauch der Wohnung' if len(meters) == 1
             else f'Verbrauch der {len(meters)} Wohnungen',
             gesamt_verbrauch, 'kWh'),
            ('Netzbezug des Hauses', house['grid_kwh'], 'kWh'),
            ('davon den Wohnungen zugeordnet',
             house['grid_kwh'] - house['unallocated_grid_kwh'], 'kWh'),
            ('davon nicht zugeordnet', house['unallocated_grid_kwh'], 'kWh'),
        ]
        for label, value, unit in kennzahlen:
            page.text(MARGIN, y, label, 10)
            page.text_right(RIGHT - 34, y, _fmt(value), 10, 'mono')
            page.text(RIGHT - 30, y, unit, 9, 'regular', 0.45)
            y += 17

        if house['unallocated_grid_kwh'] > 0.01:
            y += 4
            page.text(MARGIN, y,
                      'Nicht zugeordnet: Netzbezug in Zeiträumen ohne '
                      'Wohnungsverbrauch —', 8.5, 'regular', 0.4)
            y += 12
            page.text(MARGIN, y,
                      'Allgemeinstrom, Wärmepumpe oder ähnliche Verbraucher '
                      'hinter keinem der Zähler.', 8.5, 'regular', 0.4)
            y += 16

        y += 16
        y = self._section(page, y, 'Je Wohnung')

        # Tabellenkopf
        columns = [
            ('Wohnung', MARGIN, 'left'),
            ('Verbrauch', MARGIN + 200, 'right'),
            ('davon Netz', MARGIN + 292, 'right'),
            ('davon PV', MARGIN + 384, 'right'),
            ('Anteil', RIGHT, 'right'),
        ]
        for label, x, align in columns:
            if align == 'left':
                page.text(x, y, label, 8, 'bold', 0.4)
            else:
                page.text_right(x, y, label, 8, 'bold', 0.4)
        y += 6
        page.line(MARGIN, y, RIGHT, y, 0.5, 0.6)
        y += 16

        netz_gesamt = sum(m['grid_kwh'] for m in meters.values())

        for meter_id in ordnung.sortiert(meters):
            values = meters[meter_id]
            name = self.meter_names.get(meter_id, meter_id.upper())
            anteil = (values['grid_kwh'] / netz_gesamt * 100
                      if netz_gesamt > 0 else 0.0)

            page.text(MARGIN, y, name, 10)
            page.text_right(MARGIN + 200, y,
                            _fmt(values['consumption_kwh']), 10, 'mono')
            page.text_right(MARGIN + 292, y,
                            _fmt(values['grid_kwh']), 10, 'mono')
            page.text_right(MARGIN + 384, y,
                            _fmt(values['solar_kwh']), 10, 'mono')
            page.text_right(RIGHT, y, f'{_fmt(anteil, 1)} %', 10, 'mono')
            y += 8
            page.line(MARGIN, y, RIGHT, y, 0.3, 0.85)
            y += 14

        # Summenzeile
        y += 2
        page.text(MARGIN, y, 'Summe', 10, 'bold')
        page.text_right(MARGIN + 200, y, _fmt(gesamt_verbrauch), 10, 'mono-bold')
        page.text_right(MARGIN + 292, y, _fmt(netz_gesamt), 10, 'mono-bold')
        page.text_right(MARGIN + 384, y,
                        _fmt(sum(m['solar_kwh'] for m in meters.values())),
                        10, 'mono-bold')
        y += 32

        if self.price_per_kwh > 0:
            y = self._section(page, y, 'Kosten des Netzbezugs')
            page.text(MARGIN, y,
                      f'Arbeitspreis {_fmt(self.config.price_ct_per_kwh, 2)} '
                      f'Cent je kWh', 9, 'regular', 0.35)
            y += 20

            grundpreis = (self.config.base_price_eur_per_year / len(meters)
                          if self.config.base_price_eur_per_year > 0 else 0.0)

            page.text_right(MARGIN + 232, y, 'Netzbezug', 8, 'bold', 0.4)
            page.text_right(MARGIN + 330, y, 'Arbeitspreis', 8, 'bold', 0.4)
            if grundpreis > 0:
                page.text_right(MARGIN + 414, y, 'Grundpreis', 8, 'bold', 0.4)
            page.text_right(RIGHT, y, 'Betrag', 8, 'bold', 0.4)
            y += 6
            page.line(MARGIN, y, RIGHT, y, 0.5, 0.6)
            y += 16

            summe = 0.0
            for meter_id in ordnung.sortiert(meters):
                name = self.meter_names.get(meter_id, meter_id.upper())
                kwh = meters[meter_id]['grid_kwh']
                arbeit = kwh * self.price_per_kwh
                betrag = arbeit + grundpreis
                summe += betrag

                page.text(MARGIN, y, name, 10)
                page.text_right(MARGIN + 232, y, f'{_fmt(kwh)} kWh', 10, 'mono')
                page.text_right(MARGIN + 330, y, f'{_fmt(arbeit, 2)}', 10, 'mono')
                if grundpreis > 0:
                    page.text_right(MARGIN + 414, y, f'{_fmt(grundpreis, 2)}',
                                    10, 'mono')
                page.text_right(RIGHT, y, f'{_fmt(betrag, 2)} EUR', 10, 'mono')
                y += 8
                page.line(MARGIN, y, RIGHT, y, 0.3, 0.85)
                y += 14

            y += 2
            page.text(MARGIN, y, 'Summe', 10, 'bold')
            page.text_right(RIGHT, y, f'{_fmt(summe, 2)} EUR', 10, 'mono-bold')
            y += 22

            if self.config.base_price_eur_per_year > 0:
                y += 4
                page.text(MARGIN, y,
                          f'Enthält je Wohnung '
                          f'{_fmt(self.config.base_price_eur_per_year / len(meters), 2)} '
                          f'EUR Grundpreisanteil.', 8.5, 'regular', 0.4)

    # --- Seite 2: Verfahren und Belastbarkeit ---------------------------

    def _method(self, page) -> None:
        y = self._header(page, 'Verfahren, Datengrundlage und Grenzen')
        quality = self.allocation['quality']

        y = self._section(page, y, 'Was gemessen und was gerechnet ist')
        zeilen = [
            ('gemessen',
             'Verbrauch je Wohnung — aus den S0-Impulsen der Eltako-Zähler, '
             '1000 Impulse je kWh.'),
            ('gemessen',
             'Netzbezug des Hauses — aus der Sonnenbatterie, aufgezeichnet '
             f'alle {self.allocation["interval_seconds"] / 60:.0f} Minuten.'),
            ('gerechnet',
             'Aufteilung des Netzbezugs auf die Wohnungen — nach dem unten '
             'beschriebenen Schlüssel.'),
        ]
        for art, text in zeilen:
            gray = 0.0 if art == 'gemessen' else 0.35
            page.text(MARGIN, y, art, 8.5, 'bold', gray)
            page.text(MARGIN + 62, y, text, 9, 'regular', 0.15)
            y += 20

        y += 12
        y = self._section(page, y, f'Verteilschlüssel: {self.model_label}')

        gemeinsam = [
            'Strom trägt keine Herkunftskennzeichnung. Welche Wattstunde aus '
            'dem Netz in welche',
            'Wohnung geflossen ist, lässt sich physikalisch nicht feststellen. '
            'Der Netzbezug wird',
            f'deshalb je Zeitfenster von '
            f'{self.allocation["interval_seconds"] / 60:.0f} Minuten nach '
            f'folgendem Schlüssel aufgeteilt:',
            '',
        ]

        if self.model == 'waterfall':
            besonderes = [
                'Der Netzbezug des Fensters wird zunächst zu gleichen Teilen '
                'auf alle Wohnungen',
                'aufgeteilt. Eine Wohnung, die in diesem Fenster weniger '
                'verbraucht hat als ihr Anteil,',
                'gibt den Überschuss ab; er wird unter den übrigen erneut '
                'gleichmäßig verteilt.',
                '',
                'Wirkung: Wohnungen mit geringem Verbrauch decken einen '
                'größeren Anteil ihres',
                'Verbrauchs aus dem Netz als Wohnungen mit hohem Verbrauch. '
                'Der Solarstrom',
                'verschiebt sich damit zugunsten der verbrauchsstärkeren '
                'Wohnung.',
            ]
        else:
            besonderes = [
                'Eine Wohnung mit 40 Prozent des Hausverbrauchs in diesem '
                'Fenster erhält',
                '40 Prozent des Netzbezugs dieses Fensters zugerechnet.',
                '',
                'Wirkung: Jede Wohnung deckt denselben Anteil ihres '
                'Verbrauchs aus dem Netz.',
                'Der Solarstrom verteilt sich im selben Verhältnis wie der '
                'Verbrauch.',
            ]

        gemeinsam_schluss = [
            '',
            'Die Rechnung erfolgt fensterweise und nicht über Jahressummen. '
            'Wer nachts verbraucht,',
            'bezieht fast alles aus dem Netz; wer mittags verbraucht, deckt '
            'viel aus der Photovoltaik.',
            'Eine Jahressumme würde beide gleich behandeln.',
            '',
            'In beiden Modellen gilt: Keiner Wohnung wird mehr Netzbezug '
            'zugerechnet, als sie',
            'insgesamt verbraucht hat.',
        ]

        for zeile in gemeinsam + besonderes + gemeinsam_schluss:
            if zeile:
                page.text(MARGIN, y, zeile, 9, 'regular', 0.15)
            y += 13

        y += 14
        y = self._section(page, y, 'Belastbarkeit der Datengrundlage')

        abdeckung = quality['coverage'] * 100
        kennzahlen = [
            ('Ausgewertete Zeitfenster',
             f'{quality["intervals_used"]} von {quality["intervals_expected"]}'),
            ('Abdeckung', f'{_fmt(abdeckung, 1)} %'),
            ('Fenster ohne Wohnungsverbrauch',
             str(quality['intervals_without_pulses'])),
            ('Erfasste Ausfälle der Datenquelle', str(len(quality['gaps']))),
        ]
        for label, value in kennzahlen:
            page.text(MARGIN, y, label, 10)
            page.text_right(RIGHT, y, value, 10, 'mono')
            y += 17

        if abdeckung < 95:
            y += 10
            page.rect(MARGIN, y - 10, CONTENT, 42, 0.93)
            page.text(MARGIN + 10, y + 4,
                      f'Hinweis: Nur {_fmt(abdeckung, 1)} Prozent der '
                      f'Zeitfenster enthalten Messwerte.', 9, 'bold')
            page.text(MARGIN + 10, y + 18,
                      'Der zugeordnete Netzbezug ist entsprechend '
                      'unvollständig.', 9)
            y += 46

        if quality['gaps']:
            y += 16
            y = self._section(page, y, 'Ausfälle im Zeitraum')
            for start, ende, grund in quality['gaps'][:12]:
                dauer = (ende - start) / 60.0
                page.text(MARGIN, y,
                          f'{_date(start)} '
                          f'{time.strftime("%H:%M", time.localtime(start))} '
                          f'bis '
                          f'{time.strftime("%H:%M", time.localtime(ende))}',
                          9, 'mono')
                page.text(MARGIN + 190, y, grund, 9, 'regular', 0.3)
                page.text_right(RIGHT, y, f'{_fmt(dauer, 0)} min', 9, 'mono')
                y += 15
            if len(quality['gaps']) > 12:
                page.text(MARGIN, y,
                          f'... und {len(quality["gaps"]) - 12} weitere',
                          9, 'regular', 0.4)
                y += 15

        y += 20
        y = self._section(page, y, 'Grenzen dieses Berichts')
        grenzen = [
            'Nicht eichfähig. Abrechnungsgrundlage bleiben die Zählerstände '
            'der geeichten',
            'Eltako-Zähler. Die Softwarestände sind mit den abgelesenen '
            'Ständen abzugleichen.',
            '',
            'Ausfallzeiten sind nicht rekonstruierbar. Impulse, die während '
            'eines Ausfalls von',
            'Rechner oder Versorgung angefallen sind, fehlen dauerhaft.',
        ]
        for zeile in grenzen:
            if zeile:
                page.text(MARGIN, y, zeile, 9, 'regular', 0.15)
            y += 13

    # --- Seite 3: Zählerstände ------------------------------------------

    def _meter_readings(self, page) -> None:
        y = self._header(page, 'Zählerstände zum Abgleich')

        y = self._section(page, y, 'Softwarestände')
        page.text(MARGIN, y,
                  'Diese Stände stammen aus der Impulserfassung. Sie sind mit '
                  'den abgelesenen', 9, 'regular', 0.15)
        y += 13
        page.text(MARGIN, y,
                  'Ständen der geeichten Zähler zu vergleichen.',
                  9, 'regular', 0.15)
        y += 26

        page.text(MARGIN, y, 'Wohnung', 8, 'bold', 0.4)
        page.text_right(MARGIN + 230, y, 'Softwarestand', 8, 'bold', 0.4)
        page.text_right(MARGIN + 340, y, 'Impulse', 8, 'bold', 0.4)
        page.text_right(RIGHT, y, 'Zählerstand', 8, 'bold', 0.4)
        y += 6
        page.line(MARGIN, y, RIGHT, y, 0.5, 0.6)
        y += 16

        for meter_id in ordnung.sortiert(self.allocation['meters']):
            name = self.meter_names.get(meter_id, meter_id.upper())
            state = self.readings.get(meter_id, {})

            page.text(MARGIN, y, name, 10)
            if state:
                page.text_right(MARGIN + 230, y,
                                f'{_fmt(state["energy_kwh"])} kWh', 10, 'mono')
                page.text_right(MARGIN + 340, y,
                                f'{state["pulses"]:,}'.replace(',', '.'),
                                10, 'mono')
            page.line(RIGHT - 96, y + 3, RIGHT, y + 3, 0.5, 0.5)
            y += 10
            page.line(MARGIN, y, RIGHT, y, 0.3, 0.85)
            y += 18

        y += 20
        page.rect(MARGIN, y, CONTENT, 62, 0.95)
        page.text(MARGIN + 12, y + 20, 'Abgleich', 9, 'bold')
        page.text(MARGIN + 12, y + 36,
                  'Weichen Software- und Zählerstand voneinander ab, gilt '
                  'der Zählerstand.', 9)
        page.text(MARGIN + 12, y + 50,
                  'Die Differenz entspricht den Impulsen, die während eines '
                  'Ausfalls verloren gingen.', 9)

    # --- Zusammenbau -----------------------------------------------------

    def build(self) -> pdf.Document:
        seiten = [self._overview, self._method, self._meter_readings]
        for index, aufbau in enumerate(seiten, start=1):
            page = self.doc.new_page()
            aufbau(page)
            self._footer(page, index, len(seiten))
        return self.doc

    def save(self, path: str) -> str:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.build().save(path)
        LOG.info('Bericht geschrieben: %s', path)
        return path


def year_bounds(year: int) -> tuple[float, float]:
    """Anfang und Ende eines Kalenderjahres als Zeitstempel, lokale Zeit."""
    start = time.mktime((year, 1, 1, 0, 0, 0, 0, 1, -1))
    end = time.mktime((year + 1, 1, 1, 0, 0, 0, 0, 1, -1))
    return start, end


def build_year_report(year: int, db, balance_store, config,
                      directory: str | None = None,
                      model: str = 'proportional') -> str:
    """Jahresbericht nach einem Verteilmodell erzeugen und ablegen."""
    from .balance import Allocator

    start, end = year_bounds(year)
    allocator = Allocator(balance_store, db,
                          config.balance.allocation_interval_seconds,
                          model)
    allocation = allocator.allocate(start, end)

    states = db.all_states()
    names = {mid: state['name'] for mid, state in states.items()}

    report = YearReport(allocation, names, states, config.report)
    target = directory or config.report.directory

    # Der Dateiname trägt das Modell, damit beide Berichte nebeneinander
    # liegen können und nicht verwechselt werden.
    suffix = '' if model == 'proportional' else f'-{model}'
    path = os.path.join(target, f'stromabrechnung-{year}{suffix}.pdf')
    return report.save(path)


def build_all_year_reports(year: int, db, balance_store, config,
                           directory: str | None = None) -> dict:
    """Beide Verteilmodelle rechnen und je einen Bericht ablegen.

    Die Rohdaten bleiben unangetastet — beide Berichte lassen sich für
    jedes Jahr jederzeit neu erzeugen, auch rückwirkend.
    """
    from .balance import MODELS

    return {
        model: build_year_report(year, db, balance_store, config,
                                 directory, model)
        for model in MODELS
    }
