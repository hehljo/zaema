"""Stilllegen eines Zählers: aus den laufenden Anzeigen, nicht aus der Historie.

Geschützt wird zweierlei. Erstens verschwindet ein stillgelegter Zähler aus
allem, was den aktuellen Stand zeigt. Zweitens bleibt sein Verbrauch in
Zeiträumen, in denen er gezählt hat, Teil der Aufteilung — sonst würde sein
Anteil am Netzbezug nachträglich auf die anderen Wohnungen wandern.
"""
import sqlite3
import time

import pytest

from s0counter import doctor
from s0counter.balance import Allocator, BalanceStore
from s0counter.config import from_dict
from s0counter.db import Database
from s0counter.sources import Reading

QUARTER = 900.0


@pytest.fixture
def db(tmp_path):
    database = Database(str(tmp_path / 'p.sqlite3'))
    for meter_id in ('eg', 'og', 'wp'):
        database.ensure_meter(meter_id, f'Strom {meter_id.upper()}', 1000)
    yield database
    database.close()


@pytest.fixture
def store(tmp_path):
    balance = BalanceStore(str(tmp_path / 'b.sqlite3'))
    yield balance
    balance.close()


def config_mit(*meters):
    gpio = iter((17, 27, 22, 5))
    return from_dict({
        'general': {'database': '/tmp/x.sqlite3'},
        'meters': {m: {'name': m, 'gpio': next(gpio)} for m in meters},
    })


class TestDatenbank:
    def test_stillgelegt_fehlt_in_laufenden_staenden(self, db):
        db.add_pulse('wp', 10.0, 10.0)
        db.retire('wp')
        assert db.meter_ids() == ['eg', 'og']
        assert set(db.all_states()) == {'eg', 'og'}

    def test_rueckblick_sieht_ihn_weiter(self, db):
        db.add_pulse('wp', 10.0, 10.0)
        db.retire('wp')
        assert db.meter_ids(include_retired=True) == ['eg', 'og', 'wp']
        assert db.all_states(include_retired=True)['wp']['pulses'] == 1
        assert db.retired_meter_ids() == ['wp']

    def test_nichts_wird_geloescht_und_alles_protokolliert(self, db):
        for i in range(5):
            db.add_pulse('wp', float(i), float(i))
        db.set_reading('wp', 100.0)
        db.retire('wp')
        stand = db.state('wp')
        assert stand['pulses'] == 5
        assert stand['energy_kwh'] == pytest.approx(100.0)
        assert db.audit_log()[0]['action'] == 'retire'

    def test_zurueck_in_der_konfiguration_ist_wieder_aktiv(self, db):
        db.retire('wp')
        db.ensure_meter('wp', 'Strom WP', 1000)
        assert 'wp' in db.meter_ids()
        assert db.audit_log()[0]['action'] == 'reactivate'

    def test_aktive_zaehler_erzeugen_kein_reaktivieren(self, db):
        db.ensure_meter('eg', 'Strom EG', 1000)
        assert all(e['action'] != 'reactivate' for e in db.audit_log())

    def test_alte_datenbank_ohne_spalte_wird_nachgeruestet(self, tmp_path):
        pfad = str(tmp_path / 'alt.sqlite3')
        alt = sqlite3.connect(pfad)
        alt.executescript("""
            CREATE TABLE meters (meter_id TEXT PRIMARY KEY, name TEXT NOT NULL,
                                 pulses_per_kwh INTEGER NOT NULL,
                                 offset_kwh REAL NOT NULL DEFAULT 0);
            INSERT INTO meters VALUES ('eg', 'Strom EG', 1000, 12.5);
        """)
        alt.commit()
        alt.close()

        database = Database(pfad)
        try:
            assert database.meter_ids() == ['eg']
            assert database.state('eg')['energy_kwh'] == pytest.approx(12.5)
            database.retire('eg')
            assert database.meter_ids() == []
        finally:
            database.close()


class TestAufteilung:
    @staticmethod
    def _viertelstunde(store, start):
        for i in range(15):
            store.add(Reading(ts=start + i * 60.0, production_w=0.0,
                              consumption_w=1200.0, grid_w=1200.0,
                              battery_w=0.0, battery_percent=50.0,
                              source='test'))

    def test_vergangener_zeitraum_behaelt_den_stillgelegten(self, store, db):
        self._viertelstunde(store, 0.0)
        for i in range(100):
            db.add_pulse('eg', i * 5.0, i * 5.0)
            db.add_pulse('wp', i * 5.0 + 1, i * 5.0 + 1)
        vorher = Allocator(store, db, QUARTER).allocate(0.0, QUARTER)

        db.retire('wp')
        nachher = Allocator(store, db, QUARTER).allocate(0.0, QUARTER)

        assert 'wp' in nachher['meters']
        for m in vorher['meters']:
            assert nachher['meters'][m]['grid_kwh'] == pytest.approx(
                vorher['meters'][m]['grid_kwh'])

    def test_zeitraum_ohne_seine_impulse_zeigt_ihn_nicht(self, store, db):
        self._viertelstunde(store, QUARTER)
        db.add_pulse('wp', 10.0, 10.0)
        for i in range(100):
            db.add_pulse('eg', QUARTER + i * 5.0, QUARTER + i * 5.0)
        db.retire('wp')

        ergebnis = Allocator(store, db, QUARTER).allocate(QUARTER, 2 * QUARTER)
        assert 'wp' not in ergebnis['meters']
        # Ein aktiver Zähler ohne Impulse bleibt dagegen sichtbar.
        assert 'og' in ergebnis['meters']


class TestBefehl:
    def _args(self, meter):
        return type('Args', (), {'meter': meter})()

    def test_verweigert_solange_er_konfiguriert_ist(self, db, capsys):
        import s0ctl
        assert s0ctl.cmd_retire(self._args('wp'), config_mit('eg', 'og', 'wp'), db) == 2
        assert 'wp' in db.meter_ids()
        assert 'Konfiguration' in capsys.readouterr().err

    def test_unbekannter_zaehler(self, db):
        import s0ctl
        assert s0ctl.cmd_retire(self._args('xx'), config_mit('eg'), db) == 2

    def test_legt_still_und_meldet_den_stand(self, db, capsys):
        import s0ctl
        db.add_pulse('wp', 1.0, 1.0)
        assert s0ctl.cmd_retire(self._args('wp'), config_mit('eg', 'og'), db) == 0
        assert db.retired_meter_ids() == ['wp']
        assert '1 Impulse bleiben gespeichert' in capsys.readouterr().out
        # Zweimal stilllegen ist kein Fehler.
        assert s0ctl.cmd_retire(self._args('wp'), config_mit('eg', 'og'), db) == 0


class TestDoctor:
    def test_aktiv_aber_nicht_konfiguriert_ist_ein_hinweis(self, db):
        db.add_pulse('wp', time.time() - 30, 30.0)
        befunde = doctor.pruefe_erfassung(db, config_mit('eg', 'og'))
        hinweis = [b for b in befunde if b.titel == 'wp: Konfiguration']
        assert hinweis and hinweis[0].stufe == doctor.UNKLAR
        assert 's0ctl retire wp' in hinweis[0].hilfe

    def test_gesund_ohne_hinweis(self, db):
        db.retire('wp')
        befunde = doctor.pruefe_erfassung(db, config_mit('eg', 'og'))
        assert not [b for b in befunde if b.titel.endswith('Konfiguration')]
