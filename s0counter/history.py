"""Zeitreihe der Rechnerwerte, mit Verdichtung alter Daten.

Eigene Datei neben Impuls- und Bilanzdatenbank. Der Grund ist derselbe wie
bei der Bilanz: die Impulse sind der beweisrelevante Rohbestand und werden so
wenig wie möglich angefasst. Rechnerwerte sind Betriebsdaten und im Zweifel
verzichtbar.

**Warum verdichtet wird.** Ein Messpunkt je Minute ergibt 525.000 Zeilen und
rund 56 MB im Jahr. Das passt auf die Karte, aber ein Diagramm über ein Jahr
müsste alle Zeilen lesen — auf einem Pi 3B spürbar träge. Deshalb drei
Stufen:

    roh        alle 60 s          7 Tage
    viertel    15-Minuten-Mittel  90 Tage
    stunde     Stundenmittel      dauerhaft

Der Verlauf reicht damit Jahre zurück und bleibt unter 20 MB. Beim Abruf
wählt `series()` selbst die Stufe, die zum Zeitraum passt.

**Mittelwert und Extremwerte zusammen.** Ein verdichteter Punkt trägt neben
dem Mittel auch Minimum und Maximum. Ohne sie verschwindet genau das, wofür
man in den Verlauf schaut: die kurze Temperaturspitze, die einzelne
Lastspitze. Ein Stundenmittel von 52 °C sagt nichts darüber, ob zwischendrin
78 °C anlagen.
"""
from __future__ import annotations

import sqlite3
import threading
import time

# Die aufgezeichneten Größen. Neue Werte hier ergänzen — Tabellen und
# Abfragen leiten sich daraus ab, damit die Liste nicht an drei Stellen
# gepflegt werden muss.
FELDER = (
    'temperature_c',
    'load_percent',
    'memory_used_percent',
    'disk_used_percent',
)

# Stufe -> (Rasterweite in Sekunden, Aufbewahrung in Sekunden oder None)
STUFEN = {
    'raw': (0.0, 7 * 86400.0),
    'quarter': (900.0, 90 * 86400.0),
    'hour': (3600.0, None),
}

_SPALTEN_ROH = ',\n  '.join(f'{name} REAL' for name in FELDER)
_SPALTEN_AGG = ',\n  '.join(
    f'{name}_avg REAL,\n  {name}_min REAL,\n  {name}_max REAL'
    for name in FELDER)

SCHEMA = f"""
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS system_raw (
  ts REAL PRIMARY KEY,
  {_SPALTEN_ROH}
);

CREATE TABLE IF NOT EXISTS system_quarter (
  ts REAL PRIMARY KEY,
  samples INTEGER NOT NULL,
  {_SPALTEN_AGG}
);

CREATE TABLE IF NOT EXISTS system_hour (
  ts REAL PRIMARY KEY,
  samples INTEGER NOT NULL,
  {_SPALTEN_AGG}
);
"""


def _raster(ts: float, weite: float) -> float:
    """Zeitstempel auf den Beginn seines Rasterfeldes abrunden."""
    return ts - (ts % weite)


class HistoryStore:
    """Ablage und Abruf der Rechner-Zeitreihe."""

    def __init__(self, path: str):
        self.path = path
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(path, check_same_thread=False, timeout=30)
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        with self.lock:
            self.conn.close()

    # --- Schreiben ------------------------------------------------------

    def add(self, werte: dict, ts: float | None = None) -> None:
        """Einen Messpunkt ablegen.

        Fehlende Größen werden als NULL gespeichert, nicht als 0 — ein nicht
        gemessener Wert darf im Diagramm nicht als Nulllinie erscheinen.
        """
        zeit = time.time() if ts is None else ts
        spalten = ', '.join(FELDER)
        platz = ', '.join('?' for _ in FELDER)
        daten = [werte.get(name) for name in FELDER]
        with self.lock:
            self.conn.execute(
                f'INSERT OR REPLACE INTO system_raw(ts, {spalten}) '
                f'VALUES(?, {platz})', [zeit] + daten)
            self.conn.commit()

    def count(self, stufe: str = 'raw') -> int:
        with self.lock:
            return self.conn.execute(
                f'SELECT COUNT(*) FROM system_{stufe}').fetchone()[0]

    # --- Verdichten -----------------------------------------------------

    def compact(self, jetzt: float | None = None) -> dict:
        """Alte Rohwerte zu Mitteln zusammenfassen und Abgelaufenes löschen.

        Verdichtet wird nur, was älter ist als die Aufbewahrungsfrist der
        jeweils feineren Stufe. Ein noch laufendes Rasterfeld bleibt
        unangetastet — sonst entstünde ein Mittelwert aus einer halben
        Viertelstunde, der später nicht mehr ergänzt werden kann.
        """
        zeit = time.time() if jetzt is None else jetzt
        ergebnis = {}

        ergebnis['quarter'] = self._verdichte(
            'system_raw', 'system_quarter', 900.0,
            grenze=zeit - STUFEN['raw'][1], roh=True)
        ergebnis['hour'] = self._verdichte(
            'system_quarter', 'system_hour', 3600.0,
            grenze=zeit - STUFEN['quarter'][1], roh=False)

        ergebnis['deleted_raw'] = self._loesche(
            'system_raw', zeit - STUFEN['raw'][1])
        ergebnis['deleted_quarter'] = self._loesche(
            'system_quarter', zeit - STUFEN['quarter'][1])
        return ergebnis

    def _verdichte(self, quelle: str, ziel: str, weite: float,
                   grenze: float, roh: bool) -> int:
        """Alles vor `grenze` aus `quelle` in `ziel` zusammenfassen.

        `roh` unterscheidet die beiden Fälle: aus Rohwerten wird gemittelt,
        aus bereits verdichteten Werten wird gewichtet weitergemittelt — ein
        ungewichtetes Mittel über Mittelwerte verzerrt, sobald ein Feld
        weniger Messpunkte enthält als ein anderes.
        """
        if roh:
            auswahl = ', '.join(
                f'AVG({n}), MIN({n}), MAX({n})' for n in FELDER)
            gruppe = (f'SELECT CAST(ts / {weite} AS INTEGER) * {weite} AS feld, '
                      f'COUNT(*), {auswahl} FROM {quelle} '
                      f'WHERE ts < ? GROUP BY feld')
        else:
            # Gewichtet: Summe(avg * samples) / Summe(samples).
            auswahl = ', '.join(
                f'SUM({n}_avg * samples) / NULLIF(SUM(CASE WHEN {n}_avg '
                f'IS NULL THEN 0 ELSE samples END), 0), '
                f'MIN({n}_min), MAX({n}_max)' for n in FELDER)
            gruppe = (f'SELECT CAST(ts / {weite} AS INTEGER) * {weite} AS feld, '
                      f'SUM(samples), {auswahl} FROM {quelle} '
                      f'WHERE ts < ? GROUP BY feld')

        spalten = ['ts', 'samples']
        for name in FELDER:
            spalten += [f'{name}_avg', f'{name}_min', f'{name}_max']
        platz = ', '.join('?' for _ in spalten)

        with self.lock:
            zeilen = self.conn.execute(gruppe, (grenze,)).fetchall()
            if not zeilen:
                return 0
            self.conn.executemany(
                f'INSERT OR REPLACE INTO {ziel}({", ".join(spalten)}) '
                f'VALUES({platz})', zeilen)
            self.conn.commit()
        return len(zeilen)

    def _loesche(self, tabelle: str, vor: float) -> int:
        with self.lock:
            cur = self.conn.execute(
                f'DELETE FROM {tabelle} WHERE ts < ?', (vor,))
            self.conn.commit()
            return cur.rowcount

    # --- Lesen ----------------------------------------------------------

    def stufe_fuer(self, sekunden: float, max_punkte: int = 300) -> str:
        """Die gröbste Stufe, die den Zeitraum noch fein genug abbildet.

        Ausgangspunkt ist die Anzahl Punkte, die eine Kurve verträgt: mehr
        als ein paar hundert bringen im Diagramm nichts und kosten auf einem
        Pi 3B messbar Zeit.
        """
        if sekunden / 60.0 <= max_punkte:
            return 'raw'
        if sekunden / 900.0 <= max_punkte:
            return 'quarter'
        return 'hour'

    def series(self, feld: str, seit_sekunden: float,
               jetzt: float | None = None,
               max_punkte: int = 300) -> dict:
        """Verlauf einer Größe über den gewünschten Zeitraum.

        Liefert die Punkte als flache Listen (Zeit, Mittel, Minimum,
        Maximum). Fehlende Werte bleiben None und werden im Diagramm als
        Lücke gezeichnet, nicht als Nulllinie.
        """
        if feld not in FELDER:
            raise ValueError(f'unbekannte Größe: {feld}')

        zeit = time.time() if jetzt is None else jetzt
        ab = zeit - seit_sekunden
        gewaehlt = self.stufe_fuer(seit_sekunden, max_punkte)

        # Von der gewählten Stufe aus nach unten durchgehen, bis eine Daten
        # hat. Nötig, weil die passende Stufe leer sein kann, ohne dass die
        # Reihe leer ist: kurz nach dem Start liegt alles noch roh, und
        # zwischen zwei Verdichtungsläufen fehlt der gröbsten Stufe der
        # jüngste Abschnitt. Ein Sprung um nur eine Stufe reicht dafür
        # nicht — von 'hour' aus liegen die Daten oft in 'quarter'.
        reihenfolge = ['hour', 'quarter', 'raw']
        kandidaten = reihenfolge[reihenfolge.index(gewaehlt):]

        for stufe in kandidaten:
            if stufe == 'raw':
                sql = (f'SELECT ts, {feld}, {feld}, {feld} FROM system_raw '
                       f'WHERE ts >= ? ORDER BY ts')
            else:
                sql = (f'SELECT ts, {feld}_avg, {feld}_min, {feld}_max '
                       f'FROM system_{stufe} WHERE ts >= ? ORDER BY ts')

            with self.lock:
                zeilen = self.conn.execute(sql, (ab,)).fetchall()

            if zeilen or stufe == 'raw':
                return {
                    'field': feld,
                    'level': stufe,
                    'from': ab,
                    'to': zeit,
                    'points': [
                        {'t': z[0], 'avg': z[1], 'min': z[2], 'max': z[3]}
                        for z in zeilen
                    ],
                }


class HistoryCollector:
    """Schreibt die Rechnerwerte in festem Takt in die Zeitreihe.

    Läuft als eigener Thread. Wie beim Bilanzsammler gilt: ein Fehler hier
    darf die Impulserfassung nicht beeinträchtigen, deshalb wird alles
    abgefangen und nur gezählt.
    """

    def __init__(self, store: HistoryStore, quelle,
                 interval_seconds: float = 60.0,
                 compact_interval_seconds: float = 3600.0):
        self.store = store
        self.quelle = quelle
        self.interval_seconds = interval_seconds
        self.compact_interval_seconds = compact_interval_seconds
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.collected = 0
        self.failed = 0
        self._last_compact = 0.0

    def start(self) -> None:
        self.thread = threading.Thread(target=self._run, daemon=True,
                                       name='history-collector')
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join(timeout=5)

    def collect_once(self) -> bool:
        try:
            werte = self.quelle()
            self.store.add({
                'temperature_c': werte.get('temperature_c'),
                'load_percent': (werte.get('load') or {}).get('load_percent'),
                'memory_used_percent': (werte.get('memory') or {}).get('used_percent'),
                'disk_used_percent': (werte.get('disk') or {}).get('used_percent'),
            })
            self.collected += 1
            return True
        except Exception:
            self.failed += 1
            return False

    def _run(self) -> None:
        self.collect_once()
        self._last_compact = time.monotonic()
        while not self.stop_event.wait(self.interval_seconds):
            self.collect_once()
            # Verdichten läuft im selben Thread: es dauert Millisekunden und
            # braucht keinen eigenen Timer, der ausfallen könnte.
            if (time.monotonic() - self._last_compact
                    >= self.compact_interval_seconds):
                try:
                    self.store.compact()
                except Exception:
                    self.failed += 1
                self._last_compact = time.monotonic()

    def statistics(self) -> dict:
        return {'collected': self.collected, 'failed': self.failed}
