"""Zustand des Rechners selbst — Temperatur, Last, Speicher, Ablage.

Bewusst ohne `vcgencmd`: das Werkzeug braucht Zugriff auf `/dev/vcio` und
scheitert als Dienstbenutzer mit "Can't open device file". Alle Werte hier
kommen aus `/sys` und `/proc` und sind für jeden lesbar.

Ein Pi, der im Schrank Impulse zählt, fällt nicht laut aus: er drosselt bei
Hitze, füllt langsam die Karte und wird erst auffällig, wenn Messwerte
fehlen. Diese Werte gehören deshalb neben die Zählerstände.
"""
from __future__ import annotations

import os
import time

# Ab hier drosselt der Pi (Raspberry Pi OS: soft limit 60 °C, hard 85 °C).
# Als Warnschwelle die weichere Grenze, dort ändert sich schon das Verhalten.
TEMP_WARN_C = 60.0
TEMP_KRITISCH_C = 80.0
DISK_WARN_PROZENT = 85.0
RAM_WARN_PROZENT = 90.0

_THERMAL = '/sys/class/thermal'
_THROTTLED = '/sys/devices/platform/soc/soc:firmware/get_throttled'


def _lies(pfad: str) -> str | None:
    try:
        with open(pfad, encoding='utf-8') as datei:
            return datei.read().strip()
    except OSError:
        return None


def temperatur_c() -> float | None:
    """Wärmste gemeldete Zone in Grad Celsius.

    Der Pi 3B hat nur `cpu-thermal`, neuere Modelle mehrere Zonen. Genommen
    wird die höchste: eine Zone, die kühl bleibt, sagt nichts darüber, ob
    eine andere gerade drosselt.
    """
    werte = []
    try:
        zonen = sorted(os.listdir(_THERMAL))
    except OSError:
        return None

    for zone in zonen:
        if not zone.startswith('thermal_zone'):
            continue
        roh = _lies(os.path.join(_THERMAL, zone, 'temp'))
        if roh is None:
            continue
        try:
            werte.append(int(roh) / 1000.0)
        except ValueError:
            continue

    return max(werte) if werte else None


def last() -> dict | None:
    """Systemlast über 1, 5 und 15 Minuten, dazu die Kernzahl.

    Ohne die Kernzahl ist die Last nicht einzuordnen: 3.0 ist auf einem
    Vierkerner entspannt und auf einem Einkerner Überlast.
    """
    try:
        eins, fuenf, fuenfzehn = os.getloadavg()
    except OSError:
        return None
    kerne = os.cpu_count() or 1
    return {
        'load_1': round(eins, 2),
        'load_5': round(fuenf, 2),
        'load_15': round(fuenfzehn, 2),
        'cores': kerne,
        'load_percent': round(eins / kerne * 100.0, 1),
    }


def speicher() -> dict | None:
    """Arbeitsspeicher aus /proc/meminfo.

    Gerechnet wird gegen `MemAvailable`, nicht gegen `MemFree`: der Kernel
    hält Puffer und Cache, die er jederzeit freigeben kann. `MemFree` sieht
    dadurch dramatisch aus, ohne dass Speicherdruck herrscht.
    """
    roh = _lies('/proc/meminfo')
    if roh is None:
        return None

    felder = {}
    for zeile in roh.splitlines():
        teile = zeile.split(':', 1)
        if len(teile) != 2:
            continue
        wert = teile[1].strip().split()
        if wert:
            try:
                felder[teile[0]] = int(wert[0]) * 1024
            except ValueError:
                continue

    gesamt = felder.get('MemTotal')
    verfuegbar = felder.get('MemAvailable')
    if not gesamt or verfuegbar is None:
        return None

    belegt = gesamt - verfuegbar
    ergebnis = {
        'total_bytes': gesamt,
        'available_bytes': verfuegbar,
        'used_bytes': belegt,
        'used_percent': round(belegt / gesamt * 100.0, 1),
    }

    swap_gesamt = felder.get('SwapTotal')
    swap_frei = felder.get('SwapFree')
    if swap_gesamt:
        ergebnis['swap_total_bytes'] = swap_gesamt
        ergebnis['swap_used_bytes'] = swap_gesamt - (swap_frei or 0)
    return ergebnis


def ablage(pfad: str = '/') -> dict | None:
    """Belegung des Dateisystems, in dem `pfad` liegt.

    Gerechnet wird gegen den für nicht-privilegierte Nutzer verfügbaren
    Platz (`f_bavail`), nicht gegen `f_bfree`: die für root reservierten
    Blöcke stehen dem Dienst nicht zur Verfügung und würden die Anzeige
    beschönigen.
    """
    try:
        s = os.statvfs(pfad)
    except OSError:
        return None

    gesamt = s.f_blocks * s.f_frsize
    verfuegbar = s.f_bavail * s.f_frsize
    belegt = gesamt - s.f_bfree * s.f_frsize
    if gesamt <= 0:
        return None

    return {
        'path': pfad,
        'total_bytes': gesamt,
        'available_bytes': verfuegbar,
        'used_bytes': belegt,
        # Bezugsgröße ist belegt + verfügbar, nicht die Gesamtgröße —
        # sonst erreicht die Anzeige nie 100 %, obwohl nichts mehr geht.
        'used_percent': round(belegt / (belegt + verfuegbar) * 100.0, 1)
        if (belegt + verfuegbar) > 0 else 0.0,
    }


def betriebsdauer_s() -> float | None:
    roh = _lies('/proc/uptime')
    if roh is None:
        return None
    try:
        return round(float(roh.split()[0]), 1)
    except (ValueError, IndexError):
        return None


def gedrosselt() -> dict | None:
    """Drosselung laut Firmware, falls über sysfs lesbar.

    Nicht jedes System stellt das ohne `vcgencmd` bereit; dann bleibt der
    Wert None. Das ist ausdrücklich kein Fehler und darf nicht als
    "nicht gedrosselt" angezeigt werden — nicht gemessen ist nicht grün.
    """
    roh = _lies(_THROTTLED)
    if roh is None:
        return None
    try:
        wert = int(roh, 0)
    except ValueError:
        return None
    return {
        'raw': wert,
        # Bit 0/1/2: gerade aktiv. Bit 16/17/18: seit dem Start aufgetreten.
        'undervoltage_now': bool(wert & 0x1),
        'throttled_now': bool(wert & 0x4),
        'undervoltage_since_boot': bool(wert & 0x10000),
        'throttled_since_boot': bool(wert & 0x40000),
    }


def sicherung(ziel: str = '/mnt/synology/s0counter',
              ersatz: str = '/var/backups/s0counter') -> dict:
    """Wann wurde zuletzt gesichert, wie viele Stände liegen, und wo?

    Gehört in die Anzeige, weil eine fehlende Sicherung nichts hörbar macht:
    Der Dienst läuft weiter, die Zahlen stimmen, und erst der Ausfall der
    Karte deckt auf, dass seit Wochen nichts weggeschrieben wurde. Genau das
    ist dieser Anlage passiert — der Timer stand auf `disabled`, und niemand
    hat es gesehen (docs/BEFUNDE.md, F20).

    `extern` unterscheidet die beiden Fälle: eine Sicherung auf der eigenen
    Speicherkarte schützt gegen kaputte Datenbanken und Fehlgriffe, nicht
    gegen deren Ausfall. Die Anzeige soll das nicht verwischen.
    """
    extern = os.path.ismount(ziel) or os.path.ismount(
        os.path.dirname(ziel.rstrip('/')) or '/')
    ort = ziel if extern else ersatz

    juengste, anzahl, belegt = None, 0, 0
    try:
        for eintrag in os.listdir(ort):
            if not eintrag.startswith('impulse-') \
                    or not eintrag.endswith('.sqlite3'):
                continue
            pfad = os.path.join(ort, eintrag)
            try:
                st = os.stat(pfad)
            except OSError:
                continue
            anzahl += 1
            belegt += st.st_size
            if juengste is None or st.st_mtime > juengste:
                juengste = st.st_mtime
    except OSError:
        # Kein Zugriff auf das Verzeichnis — die Anzeige zeigt dann „unbekannt“
        # statt einer erfundenen Null.
        return {'ort': ort, 'extern': extern, 'letzte_ts': None,
                'anzahl': None, 'bytes': None}

    return {
        'ort': ort,
        'extern': extern,
        'letzte_ts': juengste,
        'anzahl': anzahl,
        'bytes': belegt,
    }


def status(datenbank_pfad: str | None = None) -> dict:
    """Alle Systemwerte in einem Satz, für die Statusausgabe.

    `datenbank_pfad` bestimmt, welches Dateisystem geprüft wird — die
    Datenbank kann auf einer anderen Partition liegen als das Wurzel-
    verzeichnis, und vollläuft dann dort zuerst.
    """
    pfad = '/'
    if datenbank_pfad:
        verzeichnis = os.path.dirname(datenbank_pfad) or '/'
        if os.path.isdir(verzeichnis):
            pfad = verzeichnis

    temperatur = temperatur_c()
    return {
        'time': time.time(),
        'temperature_c': round(temperatur, 1) if temperatur is not None else None,
        'temperature_warn_c': TEMP_WARN_C,
        'temperature_critical_c': TEMP_KRITISCH_C,
        'load': last(),
        'memory': speicher(),
        'disk': ablage(pfad),
        'uptime_seconds': betriebsdauer_s(),
        'throttling': gedrosselt(),
        'backup': sicherung(),
    }
