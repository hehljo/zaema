"""Persistenz und Zählerstände."""
import sqlite3

import pytest

from s0counter.db import Database


@pytest.fixture
def db(tmp_path):
    database = Database(str(tmp_path / 'test.sqlite3'))
    database.ensure_meter('eg', 'Strom EG', 1000)
    yield database
    database.close()


class TestImpulse:
    def test_leerer_zaehler(self, db):
        state = db.state('eg')
        assert state['pulses'] == 0
        assert state['energy_kwh'] == 0.0
        assert state['power_w'] == 0.0
        assert state['last_pulse'] is None

    def test_ein_impuls_ist_eine_wattstunde(self, db):
        db.add_pulse('eg', 1000.0, 1000.0)
        assert db.state('eg')['energy_kwh'] == pytest.approx(0.001)

    def test_tausend_impulse_sind_eine_kilowattstunde(self, db):
        for i in range(1000):
            db.add_pulse('eg', 1000.0 + i, 1000.0 + i)
        state = db.state('eg')
        assert state['pulses'] == 1000
        assert state['energy_kwh'] == pytest.approx(1.0)

    def test_unbekannter_zaehler(self, db):
        with pytest.raises(KeyError, match='xy'):
            db.state('xy')


class TestLeistung:
    def test_erst_ab_dem_zweiten_impuls(self, db):
        db.add_pulse('eg', 1000.0, 1000.0)
        assert db.state('eg')['power_w'] == 0.0

    def test_eine_wattstunde_pro_stunde_ist_ein_watt(self, db):
        db.add_pulse('eg', 0.0, 0.0)
        db.add_pulse('eg', 3600.0, 3600.0)
        assert db.state('eg')['power_w'] == pytest.approx(1.0)

    def test_ein_impuls_pro_sekunde_sind_3600_watt(self, db):
        db.add_pulse('eg', 0.0, 0.0)
        db.add_pulse('eg', 1.0, 1.0)
        assert db.state('eg')['power_w'] == pytest.approx(3600.0)

    def test_nur_die_letzten_beiden_impulse_zaehlen(self, db):
        db.add_pulse('eg', 0.0, 0.0)
        db.add_pulse('eg', 3600.0, 3600.0)   # 1 W
        db.add_pulse('eg', 3601.0, 3601.0)   # 3600 W
        assert db.state('eg')['power_w'] == pytest.approx(3600.0)

    def test_andere_impulszahl_je_kwh(self, db):
        db.ensure_meter('og', 'Strom OG', 2000)
        db.add_pulse('og', 0.0, 0.0)
        db.add_pulse('og', 1.0, 1.0)
        # 2000 Imp/kWh: ein Impuls ist 0,5 Wh, also 1800 W
        assert db.state('og')['power_w'] == pytest.approx(1800.0)


class TestOffset:
    def test_displaystand_uebernehmen(self, db):
        for i in range(500):
            db.add_pulse('eg', float(i), float(i))
        db.set_reading('eg', 1234.567)

        state = db.state('eg')
        assert state['energy_kwh'] == pytest.approx(1234.567)
        assert state['pulses'] == 500, 'Rohimpulse müssen erhalten bleiben'

    def test_impulse_nach_der_korrektur_zaehlen_weiter(self, db):
        db.set_reading('eg', 1000.0)
        for i in range(100):
            db.add_pulse('eg', float(i), float(i))
        assert db.state('eg')['energy_kwh'] == pytest.approx(1000.1)

    def test_korrektur_landet_im_audit_log(self, db):
        db.set_reading('eg', 42.0)
        log = db.audit_log()
        assert len(log) == 1
        assert log[0]['action'] == 'set-reading'
        assert log[0]['meter_id'] == 'eg'
        assert '42.0' in log[0]['details']

    def test_zweite_korrektur_ersetzt_die_erste(self, db):
        db.add_pulse('eg', 0.0, 0.0)
        db.set_reading('eg', 100.0)
        db.set_reading('eg', 200.0)
        assert db.state('eg')['energy_kwh'] == pytest.approx(200.0)
        assert len(db.audit_log()) == 2


class TestReparatur:
    def test_zaehlerstand_wird_aus_rohdaten_wiederhergestellt(self, tmp_path):
        """Nach einem Absturz zwischen den beiden Schreibvorgängen."""
        path = str(tmp_path / 'crash.sqlite3')
        db = Database(path)
        db.ensure_meter('eg', 'Strom EG', 1000)
        for i in range(10):
            db.add_pulse('eg', float(i), float(i))

        # Zwischenstand künstlich verfälschen, wie nach einem Absturz
        db.conn.execute('UPDATE counters SET pulses = 3 WHERE meter_id = "eg"')
        db.conn.commit()
        db.close()

        wieder = Database(path)
        assert wieder.state('eg')['pulses'] == 10
        aktionen = [e['action'] for e in wieder.audit_log()]
        assert 'counter-repair' in aktionen
        wieder.close()

    def test_migration_ohne_counters_tabelle(self, tmp_path):
        """Eine Datenbank der Vorversion muss weiterverwendbar sein."""
        path = str(tmp_path / 'alt.sqlite3')
        alt = sqlite3.connect(path)
        alt.executescript("""
            CREATE TABLE meters (meter_id TEXT PRIMARY KEY, name TEXT NOT NULL,
              pulses_per_kwh INTEGER NOT NULL, offset_kwh REAL NOT NULL DEFAULT 0);
            CREATE TABLE pulses (id INTEGER PRIMARY KEY AUTOINCREMENT,
              meter_id TEXT NOT NULL, ts REAL NOT NULL, mono REAL NOT NULL);
            INSERT INTO meters VALUES ('eg', 'Strom EG', 1000, 5.0);
        """)
        for i in range(250):
            alt.execute('INSERT INTO pulses(meter_id, ts, mono) VALUES(?,?,?)',
                        ('eg', float(i), float(i)))
        alt.commit()
        alt.close()

        neu = Database(path)
        state = neu.state('eg')
        assert state['pulses'] == 250
        assert state['energy_kwh'] == pytest.approx(5.25)
        neu.close()

    def test_reparatur_findet_die_beiden_juengsten_zeitstempel(self, tmp_path):
        """Die Werte selbst, nicht nur dass welche gesetzt wurden.

        `last_ts`/`prev_ts` gehen in die Leistungsberechnung. Ein Absturz
        darf dort nicht irgendwelche zwei Impulse hinterlassen, sondern muss
        die beiden jüngsten treffen.
        """
        path = str(tmp_path / 'juengste.sqlite3')
        db = Database(path)
        db.ensure_meter('eg', 'Strom EG', 1000)
        for i in range(20):
            db.add_pulse('eg', 1000.0 + i * 3.0, float(i))
        db.conn.execute('UPDATE counters SET pulses = 0 WHERE meter_id = "eg"')
        db.conn.commit()
        db.close()

        wieder = Database(path)
        zustand = wieder.conn.execute(
            'SELECT last_ts, prev_ts FROM counters WHERE meter_id = "eg"'
        ).fetchone()
        assert zustand == (1057.0, 1054.0)
        wieder.close()

    def test_reparatur_nutzt_den_vorhandenen_index(self, tmp_path):
        """Ohne Index läuft die Abfrage über die ganze Tabelle.

        Bei 6,3 Mio Impulsen im Jahr sind das gemessene 660 ms statt 0,04 ms.
        Der zweite Index dafür kostete 95 MB im Jahr; die Sortierung nach
        `ts` bedient stattdessen `idx_pulses_meter_ts` — aber nur, solange
        niemand wieder auf `ORDER BY id` umstellt.

        Geprüft wird die Abfrage, die `_repair_counters()` **tatsächlich
        absetzt**, nicht eine hier nochmal hingeschriebene: eine im Test
        wiederholte SQL-Zeile bleibt grün, wenn der Code daneben zurückgebaut
        wird — in der ersten Fassung dieses Tests war genau das der Fall und
        fiel erst bei der Sabotage-Gegenprobe auf.
        """
        path = str(tmp_path / 'plan.sqlite3')
        db = Database(path)
        db.ensure_meter('eg', 'Strom EG', 1000)
        for i in range(50):
            db.add_pulse('eg', float(i), float(i))
        db.conn.execute('UPDATE counters SET pulses = 0 WHERE meter_id = "eg"')
        db.conn.commit()
        db.close()

        # Jede Anweisung mitschneiden, die beim Start wirklich ausgeführt
        # wird. `set_trace_callback` greift in SQLite selbst, nicht an einer
        # Python-Hülle — was hier ankommt, ist tatsächlich gelaufen.
        gesehen = []
        wieder = Database(path)
        # Der Start hat die Summe bereits geradegezogen; für den Mitschnitt
        # muss sie erneut abweichen, sonst bricht die Reparatur vorher ab
        # und es käme gar keine Abfrage auf `pulses` zustande.
        wieder.conn.execute('UPDATE counters SET pulses = 0 WHERE meter_id = "eg"')
        wieder.conn.commit()
        wieder.conn.set_trace_callback(gesehen.append)
        wieder._repair_counters()
        wieder.conn.set_trace_callback(None)

        kandidaten = [s for s in gesehen
                      if 'FROM pulses' in s and 'ORDER BY' in s]
        assert kandidaten, gesehen

        # Der Mitschnitt liefert die Anweisung mit bereits eingesetzten
        # Werten — sie lässt sich direkt erklären, ohne eigene Parameter.
        for sql in kandidaten:
            plan = wieder.conn.execute('EXPLAIN QUERY PLAN ' + sql).fetchall()
            text = ' '.join(str(zeile[-1]) for zeile in plan)
            assert 'idx_pulses_meter_ts' in text, f'{sql} -> {text}'
            # Kein Nachsortieren und kein Tabellendurchlauf: der Index
            # liefert die Reihenfolge bereits mit.
            assert 'TEMP B-TREE' not in text.upper(), f'{sql} -> {text}'
            assert 'SCAN pulses' not in text, f'{sql} -> {text}'
        wieder.close()

    def test_kein_zweiter_index_auf_pulses(self, tmp_path):
        """`pulses` ist die größte Tabelle — jeder Index darauf verdoppelt sie fast."""
        path = str(tmp_path / 'indizes.sqlite3')
        db = Database(path)
        db.ensure_meter('eg', 'Strom EG', 1000)
        namen = sorted(r[0] for r in db.conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='index' AND tbl_name='pulses' AND name NOT LIKE 'sqlite_%'"))
        assert namen == ['idx_pulses_meter_ts'], namen
        db.close()


class TestExportUndSicherung:
    def test_export_liefert_alle_impulse(self, db):
        for i in range(5):
            db.add_pulse('eg', float(i), float(i))
        assert len(list(db.iter_pulses())) == 5

    def test_export_nach_zaehler_gefiltert(self, db):
        db.ensure_meter('og', 'Strom OG', 1000)
        db.add_pulse('eg', 1.0, 1.0)
        db.add_pulse('og', 2.0, 2.0)
        db.add_pulse('og', 3.0, 3.0)
        assert len(list(db.iter_pulses('og'))) == 2

    def test_export_nach_zeitpunkt_gefiltert(self, db):
        for i in range(10):
            db.add_pulse('eg', float(i), float(i))
        assert len(list(db.iter_pulses(since=5.0))) == 5

    def test_sicherung_enthaelt_die_daten(self, db, tmp_path):
        for i in range(20):
            db.add_pulse('eg', float(i), float(i))
        ziel = str(tmp_path / 'backup.sqlite3')
        db.backup_to(ziel)

        kopie = Database(ziel)
        assert kopie.state('eg')['pulses'] == 20
        kopie.close()


class TestMehrereZaehler:
    def test_zaehler_beeinflussen_sich_nicht(self, db):
        db.ensure_meter('og', 'Strom OG', 1000)
        db.ensure_meter('dg', 'Strom DG', 1000)

        for _ in range(10):
            db.add_pulse('eg', 1.0, 1.0)
        for _ in range(20):
            db.add_pulse('og', 1.0, 1.0)

        states = db.all_states()
        assert states['eg']['pulses'] == 10
        assert states['og']['pulses'] == 20
        assert states['dg']['pulses'] == 0

    def test_meter_ids_sind_sortiert(self, db):
        db.ensure_meter('og', 'OG', 1000)
        db.ensure_meter('dg', 'DG', 1000)
        assert db.meter_ids() == ['dg', 'eg', 'og']


class TestSicherungAufNetzlaufwerk:
    """Der Fall vom 27.08.2026: SQLite direkt auf eine SMB-Freigabe.

    `sqlite3.connect()` legte die Datei an, das Schreiben hing dann in den
    Dateisperren — 0 Bytes auf der Synology, 42 Minuten Wartezeit. Die
    Reparatur schreibt lokal und kopiert danach als Datenstrom.
    """

    def test_zielordner_wird_angelegt(self, tmp_path):
        db = Database(str(tmp_path / 'q.sqlite3'))
        db.add_pulse('eg', 1.0, 1)
        ziel = tmp_path / 'gibt' / 'es' / 'noch' / 'nicht' / 'b.sqlite3'
        db.backup_to(str(ziel))
        assert ziel.exists() and ziel.stat().st_size > 100

    def test_sicherung_ist_lesbar_und_enthaelt_die_daten(self, tmp_path):
        db = Database(str(tmp_path / 'q.sqlite3'))
        for i in range(5):
            db.add_pulse('eg', 100.0 + i, i + 1)
        ziel = str(tmp_path / 'ziel' / 'b.sqlite3')
        db.backup_to(ziel)

        # Nicht nur "Datei da", sondern der geschriebene Inhalt.
        con = sqlite3.connect(ziel)
        try:
            anzahl = con.execute('SELECT COUNT(*) FROM pulses').fetchone()[0]
        finally:
            con.close()
        assert anzahl == 5

    def test_keine_zwischendatei_bleibt_liegen(self, tmp_path):
        db = Database(str(tmp_path / 'q.sqlite3'))
        db.add_pulse('eg', 1.0, 1)
        zielordner = tmp_path / 'ziel'
        db.backup_to(str(zielordner / 'b.sqlite3'))

        reste = [p.name for p in zielordner.iterdir()
                 if p.name.startswith('.')]
        assert reste == [], f'Zwischendateien liegengeblieben: {reste}'
        quell_reste = [p.name for p in tmp_path.iterdir()
                       if p.name.startswith('.backup-')]
        assert quell_reste == [], f'lokale Reste: {quell_reste}'

    def test_bei_fehlschlag_kein_torso_am_ziel(self, tmp_path, monkeypatch):
        """Ein halb kopierter Stand darf nicht unter dem Zielnamen stehen.

        Die Rotation zählte ihn sonst als gültige Sicherung mit — und beim
        Zurückspielen fiele erst dann auf, dass er abgeschnitten ist.
        """
        db = Database(str(tmp_path / 'q.sqlite3'))
        db.add_pulse('eg', 1.0, 1)
        ziel = tmp_path / 'ziel' / 'b.sqlite3'

        import shutil as _sh

        def kaputt(quelle, ziel_pfad, *a, **k):
            # Schreibt etwas und bricht dann ab — wie ein Netzausfall
            # mitten in der Übertragung.
            with open(ziel_pfad, 'wb') as f:
                f.write(b'halb')
            raise OSError('Verbindung weg')

        monkeypatch.setattr(_sh, 'copyfile', kaputt)
        with pytest.raises(OSError):
            db.backup_to(str(ziel))

        assert not ziel.exists(), 'Torso steht unter dem endgültigen Namen'
        reste = [p.name for p in (tmp_path / 'ziel').iterdir()]
        assert reste == [], f'Reste am Ziel: {reste}'

    def test_sqlite_kopieren_fuer_fremde_datenbanken(self, tmp_path):
        # Der Weg, den backup.sh für Hausbilanz und Verlauf nimmt.
        from s0counter.db import sqlite_kopieren

        quelle = str(tmp_path / 'fremd.sqlite3')
        con = sqlite3.connect(quelle)
        con.execute('CREATE TABLE t (x INTEGER)')
        con.execute('INSERT INTO t VALUES (42)')
        con.commit()
        con.close()

        ziel = str(tmp_path / 'weg' / 'kopie.sqlite3')
        sqlite_kopieren(quelle, ziel)

        con = sqlite3.connect(ziel)
        try:
            assert con.execute('SELECT x FROM t').fetchone()[0] == 42
        finally:
            con.close()
