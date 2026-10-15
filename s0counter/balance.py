"""Hausbilanz aufzeichnen und den Netzbezug auf die Wohnungen verteilen.

Der Kern des Verfahrens in einem Satz: In jedem Zeitintervall bekommt eine
Wohnung denselben Anteil am Netzbezug des Hauses, den sie am Hausverbrauch
hatte.

Das ist ein Verteilschlüssel, keine Messung. Physikalisch lässt sich nicht
feststellen, welche Wattstunde aus dem Netz in welche Wohnung geflossen ist —
Strom trägt keine Herkunftskennzeichnung. Wer 40 % des Hausverbrauchs hat,
bekommt 40 % des gleichzeitigen Netzbezugs zugerechnet.

Warum intervallweise und nicht über das ganze Jahr: Wer nachts verbraucht,
wenn keine Sonne scheint, bezieht praktisch alles aus dem Netz. Wer mittags
verbraucht, deckt viel aus der PV. Eine Jahresrechnung über Summen würde
beide gleich behandeln und die Nachtverbraucher begünstigen.
"""
import logging
import sqlite3
import threading
import time
from dataclasses import dataclass

LOG = logging.getLogger(__name__)

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS balance (
  ts              REAL PRIMARY KEY,
  production_w    REAL NOT NULL,
  consumption_w   REAL NOT NULL,
  grid_w          REAL NOT NULL,
  battery_w       REAL NOT NULL,
  battery_percent REAL NOT NULL,
  source          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_balance_ts ON balance(ts);

CREATE TABLE IF NOT EXISTS balance_gaps (
  id       INTEGER PRIMARY KEY AUTOINCREMENT,
  start_ts REAL NOT NULL,
  end_ts   REAL NOT NULL,
  reason   TEXT NOT NULL
);
"""


@dataclass
class IntervalShare:
    """Ergebnis eines Intervalls für eine Wohnung."""
    meter_id: str
    consumption_kwh: float      # gemessen, aus den S0-Impulsen
    grid_kwh: float             # zugerechnet, anteilig
    solar_kwh: float            # zugerechnet, Rest aus PV und Speicher


# --- Verteilmodelle ------------------------------------------------------
#
# Beide arbeiten auf denselben Rohdaten. Welches Modell gilt, ist eine
# Entscheidung über Fairness, keine über Technik — deshalb sind sie
# austauschbar und jederzeit rückwirkend neu rechenbar.

def share_proportional(consumption: dict, grid_kwh: float) -> dict:
    """Anteilig nach Verbrauch.

    Wer 40 Prozent des Verbrauchs hatte, bekommt 40 Prozent des
    Netzbezugs. Jede Wohnung deckt damit denselben Prozentsatz ihres
    Verbrauchs aus dem Netz — niemand wird bevorzugt.

    Übersteigt der Netzbezug den Wohnungsverbrauch, wird auf den
    Verbrauch gedeckelt. Das kommt vor, wenn Verbraucher hinter keinem
    der Zähler mitlaufen — Wärmepumpe, Allgemeinstrom, Ladepunkt. Ohne
    den Deckel bekäme eine Wohnung mehr Netzbezug zugerechnet, als sie
    überhaupt verbraucht hat.
    """
    total = sum(consumption.values())
    if total <= 0:
        return {m: 0.0 for m in consumption}
    verteilbar = min(grid_kwh, total)
    return {m: verteilbar * kwh / total for m, kwh in consumption.items()}


def share_waterfall(consumption: dict, grid_kwh: float) -> dict:
    """Zu gleichen Teilen, Überschuss fließt an die übrigen weiter.

    Der Netzbezug wird gleichmäßig auf alle Wohnungen aufgeteilt. Wer
    dabei mehr zugeteilt bekäme, als er verbraucht hat, gibt den
    Überschuss ab; er wird unter den übrigen erneut gleichmäßig verteilt,
    bis nichts mehr übrig ist oder alle gedeckelt sind.

    Wirkung: Kleinverbraucher zahlen bis zu ihrem vollen Verbrauch aus dem
    Netz, während der Größte anteilig weniger trägt. Der Solarstrom
    verschiebt sich damit zum Großverbraucher.
    """
    remaining = grid_kwh
    result = {m: 0.0 for m in consumption}
    open_meters = {m for m, kwh in consumption.items() if kwh > 0}

    while remaining > 1e-12 and open_meters:
        share = remaining / len(open_meters)
        distributed = 0.0
        capped = set()

        for meter_id in open_meters:
            headroom = consumption[meter_id] - result[meter_id]
            take = min(share, headroom)
            result[meter_id] += take
            distributed += take
            if take < share - 1e-12:
                capped.add(meter_id)

        remaining -= distributed
        if not capped:
            # Alle konnten ihren vollen Anteil aufnehmen — fertig.
            break
        open_meters -= capped

    return result


MODELS = {
    'proportional': share_proportional,
    'waterfall': share_waterfall,
}

MODEL_LABELS = {
    'proportional': 'anteilig nach Verbrauch',
    'waterfall': 'zu gleichen Teilen mit Überlauf',
}


class BalanceStore:
    """Ablage der Hausbilanz.

    Eigene Datei neben der Impulsdatenbank. Grund: die Impulse sind der
    beweisrelevante Rohbestand und sollen so wenig wie möglich angefasst
    werden — die Bilanz ist nachgelagert und im Zweifel neu beschaffbar.
    """

    def __init__(self, path: str):
        self.path = path
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(path, check_same_thread=False, timeout=30)
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        with self.lock:
            self.conn.close()

    def add(self, reading) -> None:
        with self.lock:
            self.conn.execute(
                'INSERT OR REPLACE INTO balance(ts, production_w, '
                'consumption_w, grid_w, battery_w, battery_percent, source) '
                'VALUES(?,?,?,?,?,?,?)',
                (reading.ts, reading.production_w, reading.consumption_w,
                 reading.grid_w, reading.battery_w, reading.battery_percent,
                 reading.source))
            self.conn.commit()

    def note_gap(self, start_ts: float, end_ts: float, reason: str) -> None:
        """Eine Lücke festhalten, damit sie im Bericht auftaucht."""
        with self.lock:
            self.conn.execute(
                'INSERT INTO balance_gaps(start_ts, end_ts, reason) '
                'VALUES(?,?,?)', (start_ts, end_ts, reason))
            self.conn.commit()

    def latest(self):
        with self.lock:
            row = self.conn.execute(
                'SELECT ts, production_w, consumption_w, grid_w, battery_w, '
                'battery_percent, source FROM balance '
                'ORDER BY ts DESC LIMIT 1').fetchone()
        return row

    def count(self, start_ts: float | None = None,
              end_ts: float | None = None) -> int:
        query = 'SELECT COUNT(*) FROM balance'
        conditions, params = [], []
        if start_ts is not None:
            conditions.append('ts >= ?')
            params.append(start_ts)
        if end_ts is not None:
            conditions.append('ts < ?')
            params.append(end_ts)
        if conditions:
            query += ' WHERE ' + ' AND '.join(conditions)
        with self.lock:
            return self.conn.execute(query, params).fetchone()[0]

    def first_ts(self) -> float | None:
        """Zeitpunkt des ältesten Messpunkts, oder None bei leerer Reihe."""
        with self.lock:
            row = self.conn.execute(
                'SELECT MIN(ts) FROM balance').fetchone()
        return row[0] if row else None

    def consumption_energy(self, start_ts: float | None = None) -> tuple:
        """Hausverbrauch als Energie über den aufgezeichneten Zeitraum.

        Für den Vergleich mit den Zählern taugt die Momentanleistung nicht:
        die Zähler melden aus dem Impulsabstand und reagieren sofort, diese
        Reihe hat nur alle `sample_interval_seconds` einen Wert. Über die
        Energie mittelt sich der Versatz heraus, und zwar umso besser, je
        länger aufgezeichnet wurde.

        Jeder Messpunkt gilt bis zum nächsten. Die Dauer wird auf das
        Doppelte des Abstands begrenzt: nach einem Ausfall stünde sonst ein
        einzelner Messpunkt für Stunden und würde die Summe verfälschen.

        Liefert (kwh, sekunden, messpunkte).
        """
        query = ('SELECT ts, consumption_w FROM balance')
        params: list = []
        if start_ts is not None:
            query += ' WHERE ts >= ?'
            params.append(start_ts)
        query += ' ORDER BY ts'

        with self.lock:
            rows = self.conn.execute(query, params).fetchall()

        if len(rows) < 2:
            return (0.0, 0.0, len(rows))

        abstaende = [rows[i + 1][0] - rows[i][0] for i in range(len(rows) - 1)]
        # Median statt Mittelwert: ein einzelner langer Ausfall soll die
        # Obergrenze nicht mit anheben.
        sortiert = sorted(abstaende)
        median = sortiert[len(sortiert) // 2]
        grenze = max(median * 2, 1.0)

        wh = 0.0
        sekunden = 0.0
        for i in range(len(rows) - 1):
            dauer = min(rows[i + 1][0] - rows[i][0], grenze)
            if dauer <= 0:
                continue
            wh += rows[i][1] * dauer / 3600.0
            sekunden += dauer

        return (wh / 1000.0, sekunden, len(rows))

    def series(self, feld: str, seit_ts: float, bis_ts: float,
               raster_s: float) -> list:
        """Verlauf einer Bilanzgröße, auf ein Zeitraster gemittelt.

        Erlaubt sind nur die Spaltennamen der Tabelle — der Feldname geht in
        den SQL-Text ein und darf deshalb nicht von außen frei wählbar sein.

        Liefert [(feld_start_ts, mittel, minimum, maximum), ...].
        """
        erlaubt = {'production_w', 'consumption_w', 'grid_w', 'battery_w',
                   'battery_percent'}
        if feld not in erlaubt:
            raise ValueError(f'unbekannte Größe: {feld}')

        with self.lock:
            return self.conn.execute(
                f'SELECT CAST(ts / ? AS INTEGER) * ? AS feld, '
                f'AVG({feld}), MIN({feld}), MAX({feld}) '
                f'FROM balance WHERE ts >= ? AND ts < ? '
                f'GROUP BY feld ORDER BY feld',
                (raster_s, raster_s, seit_ts, bis_ts)).fetchall()

    def gaps(self, start_ts: float, end_ts: float) -> list:
        """Vermerkte Lücken — die, bei denen der Dienst lief.

        Deckt nur den Fall ab, dass die *Quelle* ausfiel. War der Rechner
        selbst aus, hat niemand einen Eintrag geschrieben; dafür gibt es
        `detect_gaps()`.
        """
        with self.lock:
            return self.conn.execute(
                'SELECT start_ts, end_ts, reason FROM balance_gaps '
                'WHERE end_ts >= ? AND start_ts < ? ORDER BY start_ts',
                (start_ts, end_ts)).fetchall()

    def detect_gaps(self, start_ts: float, end_ts: float,
                    expected_interval: float | None = None,
                    faktor: float = 3.0) -> list:
        """Löcher aus den Zeitstempeln selbst erkennen.

        `note_gap()` schreibt nur, wenn der Dienst läuft und die Quelle
        ausfällt. Bei Stromausfall, Absturz oder einem Update ist der
        Rechner selbst weg — dann fehlen die Messpunkte, und es gibt
        niemanden, der das vermerken könnte.

        Rekonstruiert wird das aus dem Abstand aufeinanderfolgender
        Messpunkte: alles über dem `faktor`-fachen des erwarteten
        Abstands ist eine Lücke. Der Faktor 3 statt 2 lässt einen
        verschluckten Messpunkt durchgehen — ein einzelner Aussetzer beim
        Abruf ist kein Ausfall, und ein Gate, das dauernd meldet, wird
        abgeschaltet.

        Erkannt wird auch, wenn die Reihe erst *nach* `start_ts` beginnt
        oder *vor* `end_ts` endet: der Dienst kann mitten im Zeitraum
        gestartet oder gestoppt worden sein.

        Ohne `expected_interval` wird der Abstand aus den Daten selbst
        genommen (Median, wie in `consumption_energy()`). Das ist der
        robustere Weg: eine übergebene Zahl wäre eine zweite Quelle neben
        der Konfiguration und liefe auseinander, sobald jemand
        `sample_interval_seconds` ändert, ohne daran zu denken.

        Liefert [(start, ende, dauer_s), ...] chronologisch.
        """
        with self.lock:
            rows = self.conn.execute(
                'SELECT ts FROM balance WHERE ts >= ? AND ts < ? ORDER BY ts',
                (start_ts, end_ts)).fetchall()

        if not rows:
            return [(start_ts, end_ts, end_ts - start_ts)]

        if expected_interval is None:
            if len(rows) < 3:
                # Zu wenig Punkte für einen Median — ohne Anhaltspunkt
                # lieber nichts melden als eine erfundene Lücke.
                return []
            abstaende = sorted(rows[i + 1][0] - rows[i][0]
                               for i in range(len(rows) - 1))
            expected_interval = abstaende[len(abstaende) // 2]
        if expected_interval <= 0:
            return []
        grenze = expected_interval * faktor

        luecken = []
        if rows[0][0] - start_ts > grenze:
            luecken.append((start_ts, rows[0][0], rows[0][0] - start_ts))

        for i in range(len(rows) - 1):
            abstand = rows[i + 1][0] - rows[i][0]
            if abstand > grenze:
                luecken.append((rows[i][0], rows[i + 1][0], abstand))

        if end_ts - rows[-1][0] > grenze:
            luecken.append((rows[-1][0], end_ts, end_ts - rows[-1][0]))

        return luecken

    def iter_interval_energy(self, start_ts: float, end_ts: float,
                             interval_seconds: float):
        """Netzbezug je Intervall in kWh.

        Aus Momentanleistungen wird Energie, indem je Intervall der
        Mittelwert der Messpunkte gebildet und mit der Intervalldauer
        multipliziert wird. Intervalle ohne Messpunkt werden übersprungen
        und nicht interpoliert — eine erfundene Zahl wäre schlimmer als
        eine erkennbare Lücke.

        Liefert (intervall_start, netzbezug_kwh, pv_kwh, messpunkte).
        """
        with self.lock:
            rows = self.conn.execute(
                'SELECT ts, grid_w, production_w FROM balance '
                'WHERE ts >= ? AND ts < ? ORDER BY ts',
                (start_ts, end_ts)).fetchall()

        if not rows:
            return

        bucket_start = start_ts
        index = 0
        hours = interval_seconds / 3600.0

        while bucket_start < end_ts:
            bucket_end = bucket_start + interval_seconds
            grid_sum = 0.0
            pv_sum = 0.0
            samples = 0

            while index < len(rows) and rows[index][0] < bucket_end:
                grid_sum += max(0.0, rows[index][1])   # nur Bezug
                pv_sum += rows[index][2]
                samples += 1
                index += 1

            if samples:
                yield (bucket_start,
                       (grid_sum / samples) * hours / 1000.0,
                       (pv_sum / samples) * hours / 1000.0,
                       samples)

            bucket_start = bucket_end


class Allocator:
    """Verteilt den Netzbezug anteilig auf die Wohnungen.

    Ergebnisse werden zwischengespeichert, weil die Rechnung über lange
    Zeiträume teuer ist und die Anzeige sie mehrfach abruft. Der Schlüssel
    enthält den Datenstand (Impulszahl und letzter Bilanzzeitstempel), nicht
    die Uhrzeit: kommt ein Impuls dazu, passt der Schlüssel nicht mehr und
    es wird neu gerechnet. Ein Zeit-Cache würde für seine Dauer alte Zahlen
    ausliefern — bei einer Abrechnung ist das der falsche Kompromiss.
    """

    # So viele Ergebnisse werden behalten. Vier decken die üblichen
    # Zeiträume der Anzeige ab (Tag, Woche, Monat, Jahr); der älteste
    # fliegt raus. Klein genug, dass der Speicherbedarf auf einem Pi mit
    # 1 GB nicht auffällt.
    CACHE_EINTRAEGE = 4

    def __init__(self, balance_store: BalanceStore, pulse_db,
                 interval_seconds: float = 900.0,
                 model: str = 'proportional'):
        self.balance = balance_store
        self.db = pulse_db
        self.interval_seconds = interval_seconds
        if model not in MODELS:
            raise ValueError(
                f'Unbekanntes Verteilmodell {model!r}. '
                f'Bekannt: {", ".join(MODELS)}')
        self.model = model
        self._cache: dict = {}
        self._cache_lock = threading.Lock()

    def _datenstand(self) -> tuple:
        """Kennung des aktuellen Datenbestands.

        Ändert sich, sobald ein Impuls oder ein Bilanzmesspunkt dazukommt —
        und auch dann, wenn ein Zählerstand korrigiert wurde, weil das die
        Impulszahl je Zähler unberührt lässt, aber `pulses_per_kwh` ändern
        kann. Beides fließt ein.

        Zwei billige Abfragen: die Impulszahl steht als laufende Summe in
        `counters`, der letzte Bilanzzeitstempel ist ein Indexzugriff.
        """
        stände = self.db.all_states(include_retired=True)
        impulse = tuple(sorted(
            (m, s['pulses'], s['pulses_per_kwh'], s['offset_kwh'])
            for m, s in stände.items()))
        letzte = self.balance.latest() if self.balance is not None else None
        return (impulse, letzte[0] if letzte else None)

    def _pulses_per_interval(self, meter_ids: list, start_ts: float,
                             end_ts: float) -> dict:
        """Impulse je Wohnung und Intervall zählen.

        Die Zuordnung zum Intervall erfolgt über den Zeitstempel des
        Impulses, also über die Wanduhr. Bei einer Zeitumstellung kann ein
        Intervall dadurch doppelt oder gar nicht belegt sein — für eine
        Jahresbilanz ist das vernachlässigbar, für die Leistungsberechnung
        wäre es das nicht (dort läuft die monotone Uhr).

        Gezählt wird in SQLite (`pulses_per_bucket`) statt in einer Schleife
        über jeden einzelnen Impuls. Die frühere Fassung holte im
        Jahreszeitraum rund 7 Mio. Zeilen nach Python; daran und nicht an
        der Verteilrechnung hing die Laufzeit. `_pulses_per_interval_slow()`
        ist dieselbe Rechnung als Schleife und dient in den Tests als
        Referenz — beide müssen dasselbe liefern.
        """
        buckets = self.db.pulses_per_bucket(
            start_ts, end_ts, self.interval_seconds)
        # Zähler, die zwar in der Datenbank stehen, aber nicht zur Auswertung
        # gehören, bleiben draußen. Die SQL-Abfrage gruppiert über alle.
        erlaubt = set(meter_ids)
        return {bucket: {m: n for m, n in eintraege.items() if m in erlaubt}
                for bucket, eintraege in buckets.items()}

    def _pulses_per_interval_slow(self, meter_ids: list, start_ts: float,
                                  end_ts: float) -> dict:
        """Referenzfassung von `_pulses_per_interval` als Python-Schleife.

        Bewusst erhalten: sie ist die geprüfte Vorgängerimplementierung und
        damit der Maßstab, an dem sich die SQL-Fassung messen lassen muss.
        Wird im Betrieb nicht aufgerufen — für einen Jahreszeitraum dauert
        sie auf einem Pi 3B Minuten.
        """
        buckets: dict[float, dict[str, int]] = {}

        for meter_id in meter_ids:
            for _, _, ts in self.db.iter_pulses(meter_id, since=start_ts):
                if ts >= end_ts:
                    break
                bucket = start_ts + (
                    int((ts - start_ts) // self.interval_seconds)
                    * self.interval_seconds)
                buckets.setdefault(bucket, {})
                buckets[bucket][meter_id] = buckets[bucket].get(meter_id, 0) + 1

        return buckets

    def allocate(self, start_ts: float, end_ts: float,
                 use_cache: bool = True) -> dict:
        """Bilanz je Wohnung über einen Zeitraum.

        Liefert je Zähler den gemessenen Verbrauch und den anteilig
        zugerechneten Netzbezug, dazu Kennzahlen zur Belastbarkeit.

        Das Ergebnis wird zwischengespeichert und nur so lange verwendet,
        wie sich der Datenbestand nicht geändert hat. `use_cache=False`
        erzwingt eine Neuberechnung — für Tests und für den Vergleich
        zweier Rechenwege.
        """
        if not use_cache:
            return self._allocate_ungecacht(start_ts, end_ts)

        schluessel = (start_ts, end_ts, self.interval_seconds, self.model)
        stand = self._datenstand()
        with self._cache_lock:
            eintrag = self._cache.get(schluessel)
            if eintrag is not None and eintrag[0] == stand:
                return eintrag[1]

        # Bewusst außerhalb des Schlosses: die Rechnung dauert bei langen
        # Zeiträumen Sekunden, und solange soll kein anderer Abruf warten.
        # Rechnen im Doppel ist billiger als eine blockierte Anzeige.
        ergebnis = self._allocate_ungecacht(start_ts, end_ts)

        with self._cache_lock:
            self._cache[schluessel] = (stand, ergebnis)
            while len(self._cache) > self.CACHE_EINTRAEGE:
                self._cache.pop(next(iter(self._cache)))
        return ergebnis

    def _allocate_ungecacht(self, start_ts: float, end_ts: float) -> dict:
        """Die eigentliche Rechnung, ohne Zwischenspeicher."""
        # Stillgelegte Zähler gehören in einen Zeitraum, in dem sie noch
        # gezählt haben — sonst verschöbe sich deren Verbrauch nachträglich
        # auf die anderen Wohnungen. Ohne Impulse im Zeitraum fallen sie weg.
        meter_ids = self.db.meter_ids(include_retired=True)
        pulse_buckets = self._pulses_per_interval(meter_ids, start_ts, end_ts)
        stillgelegt = set(self.db.retired_meter_ids())
        if stillgelegt:
            mit_impulsen = {m for zaehler in pulse_buckets.values()
                            for m, n in zaehler.items() if n > 0}
            meter_ids = [m for m in meter_ids
                         if m not in stillgelegt or m in mit_impulsen]

        totals = {
            m: {'consumption_kwh': 0.0, 'grid_kwh': 0.0, 'solar_kwh': 0.0}
            for m in meter_ids
        }
        house_grid_kwh = 0.0
        house_pv_kwh = 0.0
        intervals_used = 0
        intervals_without_pulses = 0
        # Zwei verschiedene Ursachen, deshalb zwei Zahlen. Beide zusammen
        # in einem Posten wären für die Anzeige nicht erklärbar: sie könnte
        # nur einen Grund nennen und läge im anderen Fall daneben.
        #   idle:   die Wohnungen haben in diesem Intervall gar nichts
        #           verbraucht — der Bezug ging vollständig woandershin.
        #   capped: die Wohnungen haben verbraucht, aber weniger, als das
        #           Haus bezogen hat. Die Differenz gehört zu Verbrauchern
        #           hinter keinem Zähler (Anlagentechnik, Allgemeinstrom).
        unallocated_idle_kwh = 0.0
        unallocated_capped_kwh = 0.0

        ppk = {m: self.db.state(m)['pulses_per_kwh'] for m in meter_ids}

        for bucket, grid_kwh, pv_kwh, _ in self.balance.iter_interval_energy(
                start_ts, end_ts, self.interval_seconds):
            house_grid_kwh += grid_kwh
            house_pv_kwh += pv_kwh
            intervals_used += 1

            pulses = pulse_buckets.get(bucket, {})
            interval_kwh = {m: pulses.get(m, 0) / ppk[m] for m in meter_ids}
            interval_total = sum(interval_kwh.values())

            if interval_total <= 0:
                # In diesem Intervall hat keine Wohnung verbraucht. Der
                # Netzbezug ging an Verbraucher hinter keinem der drei
                # Zähler — er wird nicht verteilt, sondern ausgewiesen.
                intervals_without_pulses += 1
                unallocated_idle_kwh += grid_kwh
                continue

            shares = MODELS[self.model](interval_kwh, grid_kwh)

            for meter_id in meter_ids:
                anteil = shares[meter_id]
                totals[meter_id]['consumption_kwh'] += interval_kwh[meter_id]
                totals[meter_id]['grid_kwh'] += anteil
                totals[meter_id]['solar_kwh'] += max(
                    0.0, interval_kwh[meter_id] - anteil)

            # Netzbezug, der übrigbleibt, weil beide Modelle auf den
            # Wohnungsverbrauch deckeln. Tritt auf, sobald das Haus mehr
            # bezieht, als die Wohnungen zusammen verbraucht haben — bei
            # laufender Anlagentechnik ist das der Normalfall, nicht die
            # Ausnahme.
            unallocated_capped_kwh += max(
                0.0, grid_kwh - sum(shares.values()))

        expected = (end_ts - start_ts) / self.interval_seconds
        coverage = intervals_used / expected if expected > 0 else 0.0

        return {
            'start_ts': start_ts,
            'end_ts': end_ts,
            'interval_seconds': self.interval_seconds,
            'model': self.model,
            'model_label': MODEL_LABELS[self.model],
            'meters': totals,
            'house': {
                'grid_kwh': house_grid_kwh,
                'pv_kwh': house_pv_kwh,
                # Summe beider Ursachen — was die Abrechnung keiner Wohnung
                # zurechnet. Die Aufteilung steht daneben, damit die Anzeige
                # den richtigen Grund nennen kann.
                'unallocated_grid_kwh': (unallocated_idle_kwh
                                         + unallocated_capped_kwh),
                'unallocated_idle_kwh': unallocated_idle_kwh,
                'unallocated_capped_kwh': unallocated_capped_kwh,
            },
            'quality': {
                'intervals_used': intervals_used,
                'intervals_expected': int(expected),
                'coverage': coverage,
                'intervals_without_pulses': intervals_without_pulses,
                # Vermerkte Lücken: die Quelle fiel aus, der Dienst lief.
                'gaps': self.balance.gaps(start_ts, end_ts),
                # Erkannte Lücken: aus den Zeitstempeln rekonstruiert. Fängt
                # den Fall, dass der Rechner selbst weg war und niemand
                # etwas vermerken konnte — Stromausfall, Absturz, Update.
                'detected_gaps': [
                    {'start_ts': a, 'end_ts': b, 'seconds': d}
                    for a, b, d in self.balance.detect_gaps(
                        start_ts, end_ts)
                ],
            },
        }


class BalanceCollector:
    """Holt die Hausbilanz in festem Takt und legt sie ab.

    Läuft als eigener Thread neben der Impulserfassung. Ein Ausfall der
    Quelle darf die Impulserfassung nicht beeinträchtigen — deshalb wird
    hier alles abgefangen und nur protokolliert.
    """

    def __init__(self, source, store: BalanceStore,
                 interval_seconds: float = 60.0):
        self.source = source
        self.store = store
        self.interval_seconds = interval_seconds
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None

        self.collected = 0
        self.failed = 0
        self._failing_since: float | None = None

    def start(self) -> None:
        self.thread = threading.Thread(target=self._run, daemon=True,
                                       name='balance-collector')
        self.thread.start()
        LOG.info('Bilanzerfassung gestartet, alle %.0f s',
                 self.interval_seconds)

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join(timeout=5)

    def collect_once(self) -> bool:
        from .sources import SourceError
        try:
            reading = self.source.read()
        except SourceError as exc:
            self.failed += 1
            if self._failing_since is None:
                self._failing_since = time.time()
                LOG.warning('Hausbilanz nicht lesbar: %s', exc)
            return False

        if self._failing_since is not None:
            self.store.note_gap(self._failing_since, time.time(),
                                'Quelle nicht erreichbar')
            LOG.info('Hausbilanz wieder lesbar nach %.0f s',
                     time.time() - self._failing_since)
            self._failing_since = None

        self.store.add(reading)
        self.collected += 1
        return True

    def _run(self) -> None:
        self.collect_once()
        while not self.stop_event.wait(self.interval_seconds):
            self.collect_once()

    def statistics(self) -> dict:
        stats = {'collected': self.collected, 'failed': self.failed}
        if hasattr(self.source, 'statistics'):
            stats.update(self.source.statistics())
        return stats
