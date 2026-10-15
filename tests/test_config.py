"""Prüfung der Konfiguration."""
import pytest

from s0counter.config import ConfigError, from_dict, load


def minimal(**meters):
    return {
        'general': {'database': '/tmp/x.sqlite3'},
        'meters': meters or {'eg': {'name': 'EG', 'gpio': 17}},
    }


class TestGrundlagen:
    def test_minimale_konfiguration(self):
        config = from_dict(minimal())
        assert config.database == '/tmp/x.sqlite3'
        assert len(config.meters) == 1
        assert config.meters['eg'].gpio == 17

    def test_vorgaben_greifen(self):
        config = from_dict(minimal())
        meter = config.meters['eg']
        assert meter.pulses_per_kwh == 1000
        assert meter.active_low is True
        assert meter.bounce_ms == 10.0
        assert config.http_port == 8080

    def test_datenbank_fehlt(self):
        with pytest.raises(ConfigError, match='database'):
            from_dict({'meters': {'eg': {'gpio': 17}}})

    def test_kein_zaehler(self):
        with pytest.raises(ConfigError, match='Kein Zähler'):
            from_dict({'general': {'database': '/tmp/x'}})

    def test_datei_nicht_gefunden(self):
        with pytest.raises(ConfigError, match='nicht gefunden'):
            load('/gibt/es/nicht.toml')


class TestGpioPruefung:
    def test_doppelte_gpio_nummer(self):
        """Zwei Zähler auf derselben Leitung würden gemeinsam zählen."""
        with pytest.raises(ConfigError, match='doppelt vergeben'):
            from_dict(minimal(eg={'gpio': 17}, og={'gpio': 17}))

    def test_reservierte_hat_pins(self):
        with pytest.raises(ConfigError, match='HAT-Erkennung'):
            from_dict(minimal(eg={'gpio': 0}))

    def test_i2c_pins_werden_abgelehnt(self):
        with pytest.raises(ConfigError, match='I2C'):
            from_dict(minimal(eg={'gpio': 2}))

    def test_nummer_ausserhalb(self):
        with pytest.raises(ConfigError, match='nutzbare BCM'):
            from_dict(minimal(eg={'gpio': 99}))

    def test_gpio_fehlt(self):
        with pytest.raises(ConfigError, match='gpio fehlt'):
            from_dict(minimal(eg={'name': 'EG'}))

    def test_gpio_ist_keine_zahl(self):
        with pytest.raises(ConfigError, match='muss eine Zahl'):
            from_dict(minimal(eg={'gpio': 'siebzehn'}))

    def test_drei_zaehler_der_vorlage(self):
        config = from_dict(minimal(
            eg={'gpio': 17}, og={'gpio': 27}, dg={'gpio': 22}))
        assert [m.gpio for m in config.meters.values()] == [17, 27, 22]


class TestWerte:
    def test_impulszahl_muss_positiv_sein(self):
        with pytest.raises(ConfigError, match='positiv'):
            from_dict(minimal(eg={'gpio': 17, 'pulses_per_kwh': 0}))

    def test_entprellung_darf_nicht_negativ_sein(self):
        with pytest.raises(ConfigError, match='negativ'):
            from_dict(minimal(eg={'gpio': 17, 'bounce_ms': -5}))

    def test_wattstunden_je_impuls(self):
        config = from_dict(minimal(eg={'gpio': 17, 'pulses_per_kwh': 1000}))
        assert config.meters['eg'].wh_per_pulse == 1.0

        config = from_dict(minimal(eg={'gpio': 17, 'pulses_per_kwh': 2000}))
        assert config.meters['eg'].wh_per_pulse == 0.5


class TestHomeAssistant:
    def test_ausgeschaltet_braucht_nichts(self):
        raw = minimal()
        raw['homeassistant'] = {'enabled': False}
        assert from_dict(raw).homeassistant.enabled is False

    def test_eingeschaltet_ohne_url(self):
        raw = minimal()
        raw['homeassistant'] = {'enabled': True, 'token': 'abc'}
        with pytest.raises(ConfigError, match='url fehlt'):
            from_dict(raw)

    def test_eingeschaltet_ohne_token(self):
        raw = minimal()
        raw['homeassistant'] = {'enabled': True, 'url': 'http://x:8123'}
        with pytest.raises(ConfigError, match='token fehlt'):
            from_dict(raw)

    def test_vollstaendig(self):
        raw = minimal()
        raw['homeassistant'] = {
            'enabled': True, 'url': 'http://192.168.0.30:8123',
            'token': 'abc', 'prefix': 's0'}
        ha = from_dict(raw).homeassistant
        assert ha.enabled and ha.prefix == 's0'


class TestMitgelieferteVorlage:
    def test_config_toml_des_projekts_ist_gueltig(self):
        """Die ausgelieferte Vorlage muss die eigene Prüfung bestehen."""
        from pathlib import Path
        vorlage = Path(__file__).parent.parent / 'config.toml'
        config = load(str(vorlage))

        assert set(config.meters) == {'eg', 'og', 'dg'}
        assert all(m.pulses_per_kwh == 1000 for m in config.meters.values())


class TestImpulsrateJeZaehler:
    """Fremde Zähler haben andere Impulsraten als die Eltako im Haus.

    EN 62053-31 gibt die Rate nicht vor — sie ist Herstellersache. Neben
    den verbreiteten 1000 sind 10, 100, 500 und 2000 im Feld. Timeout und
    Plausibilitätsgrenze müssen deshalb je Zähler gelten, nicht global.
    """

    def test_vorgaben_entsprechen_der_hausanlage(self):
        """Die Vorgabe muss exakt das ergeben, was vorher fest eingebaut war.

        Vorher stand `power_timeout_seconds = 180` in der Datei. Wäre die
        Ableitung anders kalibriert, änderte sich das Verhalten der
        laufenden Anlage still mit dem Update.
        """
        meter = from_dict(minimal()).meters['eg']
        assert meter.pulses_per_kwh == 1000
        assert meter.max_plausible_power_w == 60_000.0
        assert meter.min_power_w == 20.0
        assert meter.power_timeout_seconds == pytest.approx(180.0)

    def test_timeout_skaliert_mit_der_impulsrate(self):
        """Bei 100 Imp/kWh braucht dieselbe Grenzleistung das Zehnfache.

        Mit dem alten festen Wert von 180 s hätte ein solcher Zähler alles
        unter 200 W als 0 W gemeldet — eine Wohnung mit Grundlast stünde
        dauerhaft auf null.
        """
        config = from_dict(minimal(
            eg={'name': 'EG', 'gpio': 17, 'pulses_per_kwh': 100},
            og={'name': 'OG', 'gpio': 27, 'pulses_per_kwh': 2000},
        ))
        assert config.meters['eg'].power_timeout_seconds == pytest.approx(1800.0)
        assert config.meters['og'].power_timeout_seconds == pytest.approx(90.0)

    def test_gemischte_zaehler_behalten_eigene_werte(self):
        config = from_dict(minimal(
            eg={'name': 'EG', 'gpio': 17, 'pulses_per_kwh': 1000},
            og={'name': 'OG', 'gpio': 27, 'pulses_per_kwh': 100,
                'max_plausible_power_w': 8000, 'min_power_w': 50},
        ))
        assert config.meters['eg'].max_plausible_power_w == 60_000.0
        assert config.meters['og'].max_plausible_power_w == 8_000.0
        assert config.meters['og'].min_power_w == 50.0

    def test_zu_niedrige_plausibilitaetsgrenze_wird_abgelehnt(self):
        """Verdeckt die Entprellung die Plausibilitätsprüfung, ist sie tot.

        Beide Stufen sind bewusst getrennt (docs/BEFUNDE.md). Liegt
        bounce_ms über der Grenze, verwirft die erste Stufe alles, was die
        zweite prüfen sollte — die Prüfung liefe leer, ohne Fehlermeldung.
        """
        with pytest.raises(ConfigError, match='Entprellung'):
            from_dict(minimal(eg={
                'name': 'EG', 'gpio': 17,
                'pulses_per_kwh': 2000,
                'bounce_ms': 100,
                'max_plausible_power_w': 30_000,
            }))

    def test_niedrige_grenze_bei_grober_impulsrate_ist_erlaubt(self):
        """Gegenprobe nach grün: der gesunde Fall darf nicht rot werden.

        Ein einphasiger Zähler mit 100 Imp/kWh und 8 kW Nennleistung ist
        eine völlig normale Anlage. Eine Prüfung, die hier anschlägt,
        würde abgeschaltet und schützte danach gar nichts mehr.
        """
        meter = from_dict(minimal(eg={
            'name': 'EG', 'gpio': 17,
            'pulses_per_kwh': 100,
            'max_plausible_power_w': 8_000,
        })).meters['eg']
        assert meter.max_plausible_power_w == 8_000.0

    def test_grenzwert_der_hausanlage_bleibt_erlaubt(self):
        """60000 W bei 1000 Imp/kWh ergibt exakt 60 ms — die eigene Anlage."""
        meter = from_dict(minimal(eg={
            'name': 'EG', 'gpio': 17,
            'pulses_per_kwh': 1000,
            'max_plausible_power_w': 60_000,
        })).meters['eg']
        assert meter.max_plausible_power_w == 60_000.0

    def test_timeout_unter_der_entprellung_wird_abgelehnt(self):
        with pytest.raises(ConfigError, match='Entprellung'):
            from_dict(minimal(eg={
                'name': 'EG', 'gpio': 17,
                'pulses_per_kwh': 1000,
                'min_power_w': 900_000,
            }))

    def test_unbrauchbare_werte(self):
        with pytest.raises(ConfigError, match='max_plausible_power_w'):
            from_dict(minimal(eg={'name': 'EG', 'gpio': 17,
                                  'max_plausible_power_w': 0}))
        with pytest.raises(ConfigError, match='min_power_w'):
            from_dict(minimal(eg={'name': 'EG', 'gpio': 17,
                                  'min_power_w': -1}))
