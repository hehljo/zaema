"""Entprellung und Plausibilitätsprüfung der Impulsannahme."""
import pytest

from s0counter.config import MeterConfig
from s0counter.counter import PulseCounter
from s0counter.db import Database


@pytest.fixture
def db(tmp_path):
    database = Database(str(tmp_path / 'test.sqlite3'))
    database.ensure_meter('eg', 'Strom EG', 1000)
    yield database
    database.close()


@pytest.fixture
def counter(db):
    config = MeterConfig('eg', 'Strom EG', gpio=17, pulses_per_kwh=1000,
                         bounce_ms=10)
    return PulseCounter(config, db)


class TestAnnahme:
    def test_erster_impuls_wird_gezaehlt(self, counter):
        assert counter.handle_pulse(1000.0, 1000.0)
        assert counter.accepted == 1

    def test_impuls_landet_in_der_datenbank(self, counter, db):
        counter.handle_pulse(1000.0, 1000.0)
        assert db.state('eg')['pulses'] == 1

    def test_rueckmeldung_wird_aufgerufen(self, db):
        gemeldet = []
        config = MeterConfig('eg', 'Strom EG', gpio=17)
        counter = PulseCounter(config, db, on_pulse=gemeldet.append)
        counter.handle_pulse(1.0, 1.0)
        assert gemeldet == ['eg']


class TestEntprellung:
    def test_zu_schnelle_folge_wird_verworfen(self, counter):
        counter.handle_pulse(1000.0, 1000.0)
        assert not counter.handle_pulse(1000.005, 1000.005)  # 5 ms
        assert counter.accepted == 1
        assert counter.bounced == 1

    def test_abstand_genau_an_der_grenze_wird_angenommen(self, counter):
        counter.handle_pulse(1000.0, 1000.0)
        assert counter.handle_pulse(1000.010, 1000.010)  # 10 ms
        assert counter.accepted == 2

    def test_verworfener_impuls_verschiebt_das_fenster_nicht(self, counter):
        """Sonst könnte anhaltendes Prellen echte Impulse dauerhaft blockieren.

        Der dritte Impuls liegt 60 ms nach dem ersten und damit über beiden
        Grenzen — er muss ankommen, obwohl dazwischen einer verworfen wurde.
        """
        counter.handle_pulse(1000.0, 1000.0)
        counter.handle_pulse(1000.005, 1000.005)   # Prellen, verworfen
        assert counter.handle_pulse(1000.060, 1000.060)
        assert counter.accepted == 2

    def test_normale_last_wird_durchgelassen(self, counter):
        """3.600 W entsprechen einem Impuls je Sekunde."""
        for i in range(60):
            assert counter.handle_pulse(float(i), float(i))
        assert counter.accepted == 60
        assert counter.bounced == 0


class TestPlausibilitaet:
    def test_unmoeglich_hohe_leistung_wird_verworfen(self, counter):
        """60 ms Abstand entsprächen 60 kW — mehr als der Zähler führen kann."""
        counter.handle_pulse(1000.0, 1000.0)
        assert not counter.handle_pulse(1000.030, 1000.030)
        assert counter.implausible == 1
        assert counter.accepted == 1

    def test_hohe_aber_moegliche_last_wird_angenommen(self, counter):
        """20 kW dreiphasig sind realistisch: 180 ms Abstand."""
        counter.handle_pulse(1000.0, 1000.0)
        assert counter.handle_pulse(1000.180, 1000.180)
        assert counter.implausible == 0

    def test_grenze_haengt_an_der_impulszahl(self, db):
        """Bei 2000 Imp/kWh ist derselbe Abstand halb so viel Leistung."""
        config = MeterConfig('og', 'OG', gpio=27, pulses_per_kwh=2000,
                             bounce_ms=1)
        counter = PulseCounter(config, db)
        db.ensure_meter('og', 'OG', 2000)

        counter.handle_pulse(0.0, 0.0)
        # 40 ms bei 2000 Imp/kWh sind 45 kW — noch plausibel
        assert counter.handle_pulse(0.040, 0.040)

    def test_statistik_zaehlt_alle_kategorien(self, counter):
        counter.handle_pulse(0.0, 0.0)
        counter.handle_pulse(0.002, 0.002)    # Prellen
        counter.handle_pulse(0.030, 0.030)    # unplausibel
        counter.handle_pulse(1.0, 1.0)        # gut

        stats = counter.statistics()
        assert stats == {'accepted': 2, 'bounced': 1, 'implausible': 1}

    def test_grobe_entprellung_erzeugt_keine_falschen_warnungen(self, db):
        """Wer bounce_ms hochsetzt, filtert bewusst — das ist kein Fehler.

        Bei bounce_ms = 100 liegt die Entprellung über der
        Plausibilitätsgrenze von 60 ms. Verworfene Impulse müssen dann als
        Prellen zählen, nicht als unplausibel.
        """
        config = MeterConfig('eg', 'Strom EG', gpio=17, bounce_ms=100)
        counter = PulseCounter(config, db)

        counter.handle_pulse(0.0, 0.0)
        assert not counter.handle_pulse(0.050, 0.050)

        stats = counter.statistics()
        assert stats['bounced'] == 1
        assert stats['implausible'] == 0


class TestZeitbasis:
    def test_abstandsmessung_nutzt_die_monotone_uhr(self, counter, db):
        """Ein Sprung der Systemuhr darf keinen Impuls verwerfen.

        Die Wanduhr springt hier um eine Stunde zurück, die monotone Uhr
        läuft normal weiter. Der Impuls muss trotzdem gezählt werden.
        """
        counter.handle_pulse(10_000.0, 500.0)
        assert counter.handle_pulse(6_400.0, 501.0)
        assert counter.accepted == 2

    def test_wanduhr_wird_gespeichert(self, counter, db):
        counter.handle_pulse(1_700_000_000.0, 42.0)
        assert db.state('eg')['last_pulse'] == 1_700_000_000.0
