"""Persistenz der S0-Impulse.

Jeder Impuls wird einzeln gespeichert, damit der Zählerstand jederzeit aus
den Rohdaten nachvollziehbar bleibt. Der abgelesene Stand des geeichten
Zählers wird nicht überschrieben, sondern als Offset hinterlegt.
"""
import os
import shutil
import sqlite3
import tempfile
import threading
import time

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=FULL;

CREATE TABLE IF NOT EXISTS meters (
  meter_id       TEXT PRIMARY KEY,
  name           TEXT NOT NULL,
  pulses_per_kwh INTEGER NOT NULL,
  offset_kwh     REAL NOT NULL DEFAULT 0,
  -- Zeitpunkt der Stilllegung (`s0ctl retire`), NULL = aktiv. Ein
  -- stillgelegter Zähler verschwindet aus den laufenden Anzeigen, seine
  -- Impulse bleiben für vergangene Zeiträume Teil der Rechnung.
  retired_ts     REAL
);

CREATE TABLE IF NOT EXISTS pulses (
  id       INTEGER PRIMARY KEY AUTOINCREMENT,
  meter_id TEXT NOT NULL,
  ts       REAL NOT NULL,
  mono     REAL NOT NULL,
  FOREIGN KEY(meter_id) REFERENCES meters(meter_id)
);
-- Der einzige Index auf `pulses`. Er trägt alle Abfragen des Betriebs:
-- Verlauf und Abrechnung (Zeitfenster je Zähler), den Abgleich von
-- `pulse_minutes`, den Export — und über `ORDER BY ts DESC` auch die Suche
-- nach den zuletzt eingetroffenen Impulsen bei der Reparatur.
--
-- Ein zweiter Index auf (meter_id, id DESC) stand hier und ist entfernt: er
-- bediente eine einzige Abfrage, die nur nach einem Absturz läuft und zwei
-- Zeilen holt, und kostete dafür 95 MB im Jahr (gemessen an 6,3 Mio Zeilen).
-- Siehe docs/BEFUNDE.md, F19.
CREATE INDEX IF NOT EXISTS idx_pulses_meter_ts ON pulses(meter_id, ts);

CREATE TABLE IF NOT EXISTS audit (
  id       INTEGER PRIMARY KEY AUTOINCREMENT,
  ts       REAL NOT NULL,
  action   TEXT NOT NULL,
  meter_id TEXT,
  details  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS counters (
  meter_id TEXT PRIMARY KEY,
  pulses   INTEGER NOT NULL DEFAULT 0,
  last_ts  REAL,
  prev_ts  REAL,
  FOREIGN KEY(meter_id) REFERENCES meters(meter_id)
);

-- Impulse je Zähler und Minute, beim Eintreffen fortgeschrieben.
-- Abgeleitet aus `pulses` und jederzeit daraus neu berechenbar; die
-- Rohimpulse bleiben die einzige Wahrheit. Die Minute ist die feinste
-- Auflösung, aus der sich jedes Abrechnungsraster ab 60 s exakt
-- zusammensetzen lässt — die Bilanzreihe misst ohnehin nur minütlich.
CREATE TABLE IF NOT EXISTS pulse_minutes (
  minute   INTEGER NOT NULL,
  meter_id TEXT NOT NULL,
  pulses   INTEGER NOT NULL,
  PRIMARY KEY (minute, meter_id),
  FOREIGN KEY(meter_id) REFERENCES meters(meter_id)
) WITHOUT ROWID;
"""

# Kantenlänge des Aggregats in Sekunden. Nicht frei wählbar: `minute` ist
# Teil des Primärschlüssels, eine Änderung würde alle bestehenden Zeilen
# ungültig machen.
AGGREGAT_SEKUNDEN = 60


def _sichern_ueber_zwischenkopie(schreiben, ziel: str,
                                 quelle: str) -> None:
    """SQLite lokal sichern, dann als Datenstrom ans Ziel kopieren.

    Der Umweg ist nicht überflüssig. Liegt das Ziel auf einer SMB-Freigabe,
    legt `sqlite3.connect()` die Datei zwar an, aber die Backup-API braucht
    Dateisperren (`fcntl`), die über CIFS nicht zuverlässig funktionieren.

    Belegt am 27.08.2026 an der Anlage: die Sicherung stand mit **0 Bytes**
    auf der Synology, der Dienst hing 42 Minuten in der Schreiboperation und
    verbrauchte dabei 9 Sekunden Rechenzeit — er wartete also, statt zu
    arbeiten. Ein `dd` mit 20 MB lief über dieselbe Freigabe in Sekunden
    durch: reines Schreiben geht, nur SQLite-Sperren nicht. Der Doctor
    meldete die 0-Byte-Datei als „Sicherung aktuell".

    `shutil.copyfile` überträgt anschließend einen einfachen Datenstrom ohne
    Sperren. Kopiert wird auf einen Zwischennamen und erst danach umbenannt,
    damit am Zielort nie eine halb geschriebene Datei unter dem endgültigen
    Namen steht: eine abgeschnittene Sicherung, die vollständig aussieht,
    ist schlimmer als gar keine.

    `schreiben` bekommt den lokalen Pfad und legt die Datenbank dort an.
    """
    ordner = os.path.dirname(os.path.abspath(ziel)) or '.'
    os.makedirs(ordner, exist_ok=True)

    # Die Zwischenkopie liegt neben der QUELLE, nicht neben dem Ziel: das
    # Ziel ist ja gerade das, was keine Sperren kann. Und nicht in /tmp —
    # das ist auf manchen Aufbauten eine tmpfs und damit im RAM.
    quell_ordner = os.path.dirname(os.path.abspath(quelle)) or '.'
    fd, temp = tempfile.mkstemp(dir=quell_ordner, prefix='.backup-',
                                suffix='.sqlite3')
    os.close(fd)
    ziel_temp = os.path.join(ordner, f'.{os.path.basename(ziel)}.teil')
    try:
        # mkstemp hat die Datei bereits angelegt; SQLite will sie selbst
        # erzeugen, sonst hält es sie für eine beschädigte Datenbank.
        os.unlink(temp)
        schreiben(temp)

        shutil.copyfile(temp, ziel_temp)
        os.replace(ziel_temp, ziel)
    except BaseException:
        # Ein Torso am Zielort darf nicht liegenbleiben — die Rotation
        # zählte ihn sonst als gültige Sicherung mit.
        if os.path.exists(ziel_temp):
            os.unlink(ziel_temp)
        raise
    finally:
        for rest in (temp, f'{temp}-wal', f'{temp}-shm'):
            if os.path.exists(rest):
                os.unlink(rest)


def sqlite_kopieren(quelle: str, ziel: str) -> None:
    """Eine SQLite-Datei sichern, auch auf ein Netzlaufwerk.

    Für Datenbanken, die dieser Dienst nicht selbst offen hält (Hausbilanz,
    Verlauf). `backup.sh` ruft das auf, statt die Logik ein zweites und
    drittes Mal zu tragen — sie stand dort zweimal inline und hatte damit
    denselben Fehler zweimal.
    """
    def schreiben(temp: str) -> None:
        src = sqlite3.connect(quelle)
        dst = sqlite3.connect(temp)
        try:
            with dst:
                src.backup(dst)
        finally:
            dst.close()
            src.close()

    _sichern_ueber_zwischenkopie(schreiben, ziel, quelle)


class Database:
    """SQLite-Ablage der Impulse.

    Die laufenden Summen stehen in `counters` und werden bei jedem Impuls
    in derselben Transaktion fortgeschrieben. Ein COUNT über die gesamte
    Impulstabelle bei jeder Abfrage würde mit der Zeit immer langsamer —
    bei 1.000 Impulsen je kWh und drei Wohnungen sind das nach einem Jahr
    mehrere Millionen Zeilen.
    """

    def __init__(self, path: str):
        self.path = path
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(path, check_same_thread=False, timeout=30)
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()
        self._repair_counters()

    def close(self) -> None:
        with self.lock:
            self.conn.close()

    # --- Einrichtung -----------------------------------------------------

    def _migrate(self) -> None:
        """Spalten nachrüsten, die eine ältere Datenbank noch nicht hat.

        `CREATE TABLE IF NOT EXISTS` legt neue Spalten nur in einer neuen
        Datei an; eine bestehende Tabelle bleibt, wie sie war.
        """
        spalten = {r[1] for r in self.conn.execute('PRAGMA table_info(meters)')}
        if 'retired_ts' not in spalten:
            self.conn.execute('ALTER TABLE meters ADD COLUMN retired_ts REAL')

    def ensure_meter(self, meter_id: str, name: str, pulses_per_kwh: int) -> None:
        """Zähler aus der Konfiguration anlegen oder abgleichen.

        Steht ein stillgelegter Zähler wieder in der Konfiguration, ist er
        damit wieder aktiv; das landet im Audit-Log.
        """
        with self.lock:
            war_stillgelegt = self.conn.execute(
                'SELECT retired_ts IS NOT NULL FROM meters WHERE meter_id=?',
                (meter_id,)).fetchone()
            self.conn.execute(
                'INSERT INTO meters(meter_id, name, pulses_per_kwh) '
                'VALUES(?,?,?) ON CONFLICT(meter_id) DO UPDATE SET '
                'name=excluded.name, pulses_per_kwh=excluded.pulses_per_kwh, '
                'retired_ts=NULL',
                (meter_id, name, pulses_per_kwh))
            if war_stillgelegt and war_stillgelegt[0]:
                self.conn.execute(
                    'INSERT INTO audit(ts, action, meter_id, details) '
                    'VALUES(?,?,?,?)',
                    (time.time(), 'reactivate', meter_id,
                     'wieder in der Konfiguration'))
            self.conn.execute(
                'INSERT OR IGNORE INTO counters(meter_id, pulses) VALUES(?, 0)',
                (meter_id,))
            self.conn.commit()

    def _repair_counters(self) -> None:
        """Laufende Summen aus den Rohimpulsen neu aufbauen.

        Nötig nach einem Absturz zwischen Impuls- und Zählerschreibvorgang
        und bei der Migration einer Datenbank ohne `counters`-Tabelle.
        """
        with self.lock:
            rows = self.conn.execute('SELECT meter_id FROM meters').fetchall()
            for (meter_id,) in rows:
                actual = self.conn.execute(
                    'SELECT COUNT(*) FROM pulses WHERE meter_id=?',
                    (meter_id,)).fetchone()[0]
                cached = self.conn.execute(
                    'SELECT pulses FROM counters WHERE meter_id=?',
                    (meter_id,)).fetchone()
                if cached is not None and cached[0] == actual:
                    continue

                # Sortiert nach `ts`, nicht nach `id`: gesucht sind die beiden
                # jüngsten Zeitstempel, und genau die liefert `ts`. Über die
                # Einfügereihenfolge zu gehen wäre der Umweg — er bräuchte
                # einen eigenen Index über (meter_id, id DESC), während dieser
                # Weg den ohnehin vorhandenen idx_pulses_meter_ts nutzt und
                # ihn sogar als COVERING INDEX bedient.
                #
                # Ein rückwärts gestellter Uhrzeitstempel könnte die Ordnung
                # von `id` und `ts` auseinanderlaufen lassen. Das ist hier
                # unschädlich: die beiden Werte gehen als `last_ts`/`prev_ts`
                # in die Leistungsberechnung, und die soll den jüngsten
                # *Zeitpunkt* kennen, nicht den zuletzt geschriebenen Datensatz.
                last_two = self.conn.execute(
                    'SELECT ts FROM pulses WHERE meter_id=? ORDER BY ts DESC '
                    'LIMIT 2', (meter_id,)).fetchall()
                last_ts = last_two[0][0] if last_two else None
                prev_ts = last_two[1][0] if len(last_two) > 1 else None

                self.conn.execute(
                    'INSERT INTO counters(meter_id, pulses, last_ts, prev_ts) '
                    'VALUES(?,?,?,?) ON CONFLICT(meter_id) DO UPDATE SET '
                    'pulses=excluded.pulses, last_ts=excluded.last_ts, '
                    'prev_ts=excluded.prev_ts',
                    (meter_id, actual, last_ts, prev_ts))
                if cached is not None:
                    self.conn.execute(
                        'INSERT INTO audit(ts, action, meter_id, details) '
                        'VALUES(?,?,?,?)',
                        (time.time(), 'counter-repair', meter_id,
                         f'zwischengespeichert={cached[0]}; tatsaechlich={actual}'))
            self.conn.commit()
        self._repair_pulse_minutes()

    def _repair_pulse_minutes(self) -> None:
        """Minutenaggregat gegen die Rohimpulse abgleichen.

        Dieselbe Absicherung wie bei `counters`: das Aggregat ist abgeleitet
        und darf nie zur zweiten Wahrheit werden. Geprüft wird gegen die
        Gesamtzahl je Zähler — weicht sie ab, wird das Aggregat dieses
        Zählers vollständig neu aufgebaut und die Korrektur protokolliert.

        Deckt drei Fälle ab: die Migration einer Datenbank aus der Zeit vor
        dem Aggregat, einen Absturz zwischen Impuls und Fortschreibung, und
        von Hand eingespielte Rohdaten.

        Die Summe je Zähler ist ein billiger Vergleich (das Aggregat hat
        rund 1/60 der Zeilen), erkennt aber keine Verschiebung zwischen
        zwei Minuten bei gleicher Gesamtzahl. Dafür gibt es `verify()`.
        """
        with self.lock:
            meter_ids = [r[0] for r in
                         self.conn.execute('SELECT meter_id FROM meters')]
            for meter_id in meter_ids:
                roh = self.conn.execute(
                    'SELECT COUNT(*) FROM pulses WHERE meter_id=?',
                    (meter_id,)).fetchone()[0]
                aggregiert = self.conn.execute(
                    'SELECT COALESCE(SUM(pulses), 0) FROM pulse_minutes '
                    'WHERE meter_id=?', (meter_id,)).fetchone()[0]
                if roh == aggregiert:
                    continue

                self.conn.execute(
                    'DELETE FROM pulse_minutes WHERE meter_id=?', (meter_id,))
                self.conn.execute(
                    'INSERT INTO pulse_minutes(minute, meter_id, pulses) '
                    'SELECT CAST(ts / ? AS INTEGER), meter_id, COUNT(*) '
                    'FROM pulses WHERE meter_id=? '
                    'GROUP BY CAST(ts / ? AS INTEGER)',
                    (AGGREGAT_SEKUNDEN, meter_id, AGGREGAT_SEKUNDEN))
                self.conn.execute(
                    'INSERT INTO audit(ts, action, meter_id, details) '
                    'VALUES(?,?,?,?)',
                    (time.time(), 'aggregat-repair', meter_id,
                     f'aggregiert={aggregiert}; tatsaechlich={roh}'))
            self.conn.commit()

    def verify_pulse_minutes(self) -> list:
        """Aggregat Minute für Minute gegen die Rohimpulse prüfen.

        Vollständiger als der Summenvergleich beim Start: findet auch
        Verschiebungen zwischen zwei Minuten, bei denen die Gesamtzahl
        stimmt. Läuft über alle Rohimpulse und ist deshalb nichts für den
        Startvorgang — gedacht für `s0ctl` und die Tests.

        Liefert die Abweichungen als [(minute, meter_id, aggregiert, roh)].
        Eine leere Liste heißt: das Aggregat deckt sich mit den Rohdaten.

        Bewusst ohne FULL OUTER JOIN — den gibt es erst ab SQLite 3.39, und
        der Code soll nicht an der Version der Zielplattform hängen. Die
        Vereinigung beider Seiten leistet dasselbe.
        """
        with self.lock:
            roh = dict(((m, minute), n) for minute, m, n in self.conn.execute(
                'SELECT CAST(ts / ? AS INTEGER), meter_id, COUNT(*) '
                'FROM pulses GROUP BY 1, 2', (AGGREGAT_SEKUNDEN,)))
            agg = dict(((m, minute), n) for minute, m, n in self.conn.execute(
                'SELECT minute, meter_id, pulses FROM pulse_minutes'))

        abweichungen = []
        for schluessel in sorted(set(roh) | set(agg)):
            meter_id, minute = schluessel
            a, r = agg.get(schluessel, 0), roh.get(schluessel, 0)
            if a != r:
                abweichungen.append((minute, meter_id, a, r))
        return sorted(abweichungen)

    # --- Erfassung -------------------------------------------------------

    def add_pulse(self, meter_id: str, ts: float, mono: float) -> None:
        """Einen Impuls speichern und die abgeleiteten Summen fortschreiben.

        Alles in einer Transaktion: entweder der Impuls und beide Summen
        oder nichts. Ein Absturz mittendrin darf keinen Zustand
        hinterlassen, in dem die Summen behaupten, mehr gesehen zu haben,
        als in `pulses` steht.
        """
        with self.lock:
            self.conn.execute(
                'INSERT INTO pulses(meter_id, ts, mono) VALUES(?,?,?)',
                (meter_id, ts, mono))
            self.conn.execute(
                'UPDATE counters SET pulses = pulses + 1, '
                'prev_ts = last_ts, last_ts = ? WHERE meter_id = ?',
                (ts, meter_id))
            self.conn.execute(
                'INSERT INTO pulse_minutes(minute, meter_id, pulses) '
                'VALUES(?,?,1) ON CONFLICT(minute, meter_id) DO UPDATE SET '
                'pulses = pulses + 1',
                (int(ts // AGGREGAT_SEKUNDEN), meter_id))
            self.conn.commit()

    # --- Abfrage ---------------------------------------------------------

    def state(self, meter_id: str) -> dict:
        """Aktueller Stand eines Zählers.

        `power_w` wird aus dem Abstand der letzten beiden Impulse gebildet
        und ist erst ab dem zweiten Impuls belastbar.
        """
        with self.lock:
            row = self.conn.execute(
                'SELECT m.name, m.pulses_per_kwh, m.offset_kwh, '
                '       c.pulses, c.last_ts, c.prev_ts '
                'FROM meters m JOIN counters c USING(meter_id) '
                'WHERE m.meter_id = ?', (meter_id,)).fetchone()

        if row is None:
            raise KeyError(f'Unbekannter Zähler: {meter_id}')

        name, ppk, offset, pulses, last_ts, prev_ts = row

        power = 0.0
        if last_ts is not None and prev_ts is not None and last_ts > prev_ts:
            # Ein Impuls entspricht 1/ppk kWh. Über den Abstand zweier
            # Impulse ergibt das die mittlere Leistung in Watt.
            power = 3_600_000.0 / (ppk * (last_ts - prev_ts))

        return {
            'id': meter_id,
            'name': name,
            'pulses_per_kwh': ppk,
            'offset_kwh': offset,
            'pulses': pulses,
            'energy_kwh': offset + pulses / ppk,
            'last_pulse': last_ts,
            'power_w': power,
        }

    def all_states(self, include_retired: bool = False) -> dict:
        """Stände aller Zähler; stillgelegte nur auf ausdrücklichen Wunsch.

        Laufende Anzeigen (Status, Dashboard, Home Assistant, Panel) wollen
        nur aktive Zähler. Rückblicke (Aufteilung, Jahresbericht) brauchen
        auch die stillgelegten, weil deren Impulse in alten Zeiträumen liegen.
        """
        return {i: self.state(i) for i in self.meter_ids(include_retired)}

    def meter_ids(self, include_retired: bool = False) -> list[str]:
        sql = 'SELECT meter_id FROM meters'
        if not include_retired:
            sql += ' WHERE retired_ts IS NULL'
        with self.lock:
            return [r[0] for r in self.conn.execute(sql + ' ORDER BY meter_id')]

    def retired_meter_ids(self) -> list[str]:
        with self.lock:
            return [r[0] for r in self.conn.execute(
                'SELECT meter_id FROM meters WHERE retired_ts IS NOT NULL '
                'ORDER BY meter_id')]

    # --- Verwaltung ------------------------------------------------------

    def set_reading(self, meter_id: str, kwh: float) -> float:
        """Softwarestand auf den abgelesenen Zählerstand setzen.

        Verändert nur den Offset. Die Rohimpulse bleiben unangetastet, jede
        Korrektur landet im Audit-Log.
        """
        state = self.state(meter_id)
        offset = kwh - state['pulses'] / state['pulses_per_kwh']
        with self.lock:
            self.conn.execute(
                'UPDATE meters SET offset_kwh=? WHERE meter_id=?',
                (offset, meter_id))
            self.conn.execute(
                'INSERT INTO audit(ts, action, meter_id, details) VALUES(?,?,?,?)',
                (time.time(), 'set-reading', meter_id,
                 f'ablesung={kwh:.6f}; offset={offset:.6f}; '
                 f'impulse={state["pulses"]}'))
            self.conn.commit()
        return offset

    def retire(self, meter_id: str) -> float:
        """Zähler stilllegen. Löscht nichts: Impulse, Offset und Historie
        bleiben, der Zähler fällt nur aus den laufenden Anzeigen."""
        state = self.state(meter_id)
        jetzt = time.time()
        with self.lock:
            self.conn.execute(
                'UPDATE meters SET retired_ts=? WHERE meter_id=?',
                (jetzt, meter_id))
            self.conn.execute(
                'INSERT INTO audit(ts, action, meter_id, details) VALUES(?,?,?,?)',
                (jetzt, 'retire', meter_id,
                 f'stand={state["energy_kwh"]:.6f}; impulse={state["pulses"]}'))
            self.conn.commit()
        return jetzt

    def audit_log(self, limit: int = 50) -> list[dict]:
        with self.lock:
            rows = self.conn.execute(
                'SELECT ts, action, meter_id, details FROM audit '
                'ORDER BY id DESC LIMIT ?', (limit,)).fetchall()
        return [{'ts': r[0], 'action': r[1], 'meter_id': r[2], 'details': r[3]}
                for r in rows]

    def backup_to(self, path: str) -> None:
        """Transaktionssichere Kopie über die SQLite-Backup-API.

        Der Umweg über eine lokale Zwischendatei steckt in
        `_sichern_ueber_zwischenkopie` — siehe dort, warum er nötig ist.
        """
        def schreiben(ziel: str) -> None:
            dest = sqlite3.connect(ziel)
            try:
                with self.lock:
                    self.conn.backup(dest)
            finally:
                dest.close()

        _sichern_ueber_zwischenkopie(schreiben, path, self.path)

    def pulse_span(self, since: float | None = None) -> tuple:
        """Impulse je Zähler in einem Zeitraum, mit dessen Grenzen.

        Für den Vergleich gegen die Hausbilanz. Anders als der Zählerstand
        enthält das keinen Offset — gezählt wird nur, was tatsächlich in
        diesem Zeitraum an Impulsen ankam.

        Liefert ({meter_id: anzahl}, erster_ts, letzter_ts).
        """
        query = ('SELECT meter_id, COUNT(*), MIN(ts), MAX(ts) '
                 'FROM pulses')
        params: list = []
        if since is not None:
            query += ' WHERE ts >= ?'
            params.append(since)
        query += ' GROUP BY meter_id'

        with self.lock:
            rows = self.conn.execute(query, params).fetchall()

        counts = {row[0]: row[1] for row in rows}
        erster = min((row[2] for row in rows), default=None)
        letzter = max((row[3] for row in rows), default=None)
        return (counts, erster, letzter)

    def power_series(self, meter_id: str, seit_ts: float, bis_ts: float,
                     raster_s: float) -> list:
        """Mittlere Leistung je Zeitfeld, aus den Rohimpulsen.

        Anders als die Momentanleistung im Status wird hier nicht aus dem
        Abstand zweier Impulse gerechnet, sondern gezählt: Impulse je Feld
        mal Energie je Impuls, geteilt durch die Felddauer. Das ist genau
        die Größe, die sich mit der Hausbilanz vergleichen lässt.

        Felder ohne Impuls fehlen im Ergebnis. Sie als 0 W einzusetzen wäre
        falsch — bei einer Grundlast unter der Impulsschwelle bedeutet
        "kein Impuls" nicht "kein Verbrauch", sondern "noch nicht genug für
        den nächsten Impuls".

        Liefert [(feld_start_ts, impulse), ...] chronologisch.
        """
        with self.lock:
            return self.conn.execute(
                'SELECT CAST(ts / ? AS INTEGER) * ? AS feld, COUNT(*) '
                'FROM pulses WHERE meter_id = ? AND ts >= ? AND ts < ? '
                'GROUP BY feld ORDER BY feld',
                (raster_s, raster_s, meter_id, seit_ts, bis_ts)).fetchall()

    def pulses_per_bucket(self, start_ts: float, end_ts: float,
                          interval_seconds: float) -> dict:
        """Impulse je Zähler und Intervall, in SQLite gruppiert.

        Gleiche Aufgabe wie eine Zählschleife über `iter_pulses()`, nur
        ohne jede Zeile einzeln nach Python zu holen. Bei drei Wohnungen
        und 1.000 Impulsen je kWh fallen im Jahr rund 7 Mio. Impulse an;
        die geben gruppiert etwa 140.000 Zeilen statt 7 Mio. Genau daran
        hing die Laufzeit, nicht an der Verteilrechnung selbst.

        Der Raster-Ursprung ist `start_ts`, nicht 0 — anders als bei
        `power_series()`. Die Intervallgrenzen müssen dieselben sein, die
        der Allocator auf der Bilanzreihe verwendet, sonst werden Impulse
        gegen den Netzbezug eines anderen Zeitfensters verrechnet.

        Impulse genau auf `start_ts` gehören ins erste Intervall, solche
        auf `end_ts` gar nicht mehr dazu — dieselbe Grenzbehandlung wie
        bei der Bilanzreihe, damit an der Nahtstelle zweier Abfragen
        weder etwas doppelt noch gar nicht gezählt wird.

        Gezählt wird bevorzugt über das Minutenaggregat `pulse_minutes`:
        es hat rund ein Sechzigstel der Zeilen, und weil `minute` im
        Primärschlüssel führt, liest SQLite den gesuchten Bereich sortiert
        und ohne temporären B-Baum. Für den Jahreszeitraum sind das auf dem
        Prüfstand 145 ms statt 6.400 ms.

        Voraussetzung dafür ist, dass sich jedes Intervall lückenlos aus
        ganzen Minuten zusammensetzt. Ist das nicht der Fall, wird über die
        Rohimpulse gezählt — langsamer, aber richtig. Ein leise falsch
        einsortierter Impuls wäre schlimmer als eine langsame Abfrage.

        Liefert {intervall_start: {meter_id: anzahl}}.
        """
        if interval_seconds <= 0:
            raise ValueError('interval_seconds muss größer als 0 sein')

        if self._aggregat_nutzbar(start_ts, end_ts, interval_seconds):
            rows = self._buckets_aus_aggregat(
                start_ts, end_ts, interval_seconds)
        else:
            rows = self._buckets_aus_rohimpulsen(
                start_ts, end_ts, interval_seconds)

        buckets: dict[float, dict[str, int]] = {}
        for meter_id, feld, anzahl in rows:
            # Der Bucket-Schlüssel wird aus dem Feldindex zurückgerechnet
            # statt aus dem Zeitstempel — so entsteht exakt derselbe
            # Fließkommawert wie beim Aufsummieren im Allocator.
            bucket = start_ts + feld * interval_seconds
            buckets.setdefault(bucket, {})[meter_id] = anzahl
        return buckets

    @staticmethod
    def _aggregat_nutzbar(start_ts: float, end_ts: float,
                          interval_seconds: float) -> bool:
        """Lässt sich jedes Intervall aus ganzen Minuten zusammensetzen?

        Drei Bedingungen, alle nötig:

        - das Intervall ist ein ganzzahliges Vielfaches einer Minute,
          sonst fiele eine Minute in zwei Intervalle;
        - Start und Ende liegen auf einer Minutengrenze, sonst wäre das
          erste und letzte Intervall angeschnitten;
        - der Zeitraum ist nicht negativ.

        Fließkommazeitstempel werden mit kleiner Toleranz geprüft: `time()`
        liefert keine exakten Vielfachen, und ein Rundungsrest von
        Mikrosekunden darf nicht dazu führen, dass still auf den langsamen
        Weg umgeschaltet wird.
        """
        if end_ts < start_ts:
            return False

        tol = 1e-6
        def auf_raster(wert: float, raster: float) -> bool:
            rest = wert % raster
            return rest < tol or (raster - rest) < tol

        return (auf_raster(interval_seconds, AGGREGAT_SEKUNDEN)
                and interval_seconds >= AGGREGAT_SEKUNDEN
                and auf_raster(start_ts, AGGREGAT_SEKUNDEN)
                and auf_raster(end_ts, AGGREGAT_SEKUNDEN))

    def _buckets_aus_aggregat(self, start_ts: float, end_ts: float,
                              interval_seconds: float) -> list:
        """Der schnelle Weg: über `pulse_minutes` summieren."""
        erste = int(round(start_ts / AGGREGAT_SEKUNDEN))
        letzte = int(round(end_ts / AGGREGAT_SEKUNDEN))
        minuten_je_intervall = int(round(interval_seconds
                                         / AGGREGAT_SEKUNDEN))
        with self.lock:
            return self.conn.execute(
                'SELECT meter_id, (minute - ?) / ? AS feld, SUM(pulses) '
                'FROM pulse_minutes WHERE minute >= ? AND minute < ? '
                'GROUP BY meter_id, feld',
                (erste, minuten_je_intervall, erste, letzte)).fetchall()

    def _buckets_aus_rohimpulsen(self, start_ts: float, end_ts: float,
                                 interval_seconds: float) -> list:
        """Der allgemeine Weg: über `pulses`, für beliebige Raster."""
        with self.lock:
            return self.conn.execute(
                'SELECT meter_id, '
                '       CAST((ts - ?) / ? AS INTEGER) AS feld, '
                '       COUNT(*) '
                'FROM pulses WHERE ts >= ? AND ts < ? '
                'GROUP BY meter_id, feld',
                (start_ts, interval_seconds, start_ts, end_ts)).fetchall()

    def iter_pulses(self, meter_id: str | None = None,
                    since: float | None = None):
        """Rohimpulse für den Export, chronologisch."""
        query = 'SELECT id, meter_id, ts FROM pulses'
        conditions, params = [], []
        if meter_id:
            conditions.append('meter_id = ?')
            params.append(meter_id)
        if since is not None:
            conditions.append('ts >= ?')
            params.append(since)
        if conditions:
            query += ' WHERE ' + ' AND '.join(conditions)
        query += ' ORDER BY id'

        with self.lock:
            yield from self.conn.execute(query, params)
