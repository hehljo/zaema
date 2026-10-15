"""Anzeigepanel — gegen das simulierte Display, ohne Hardware.

Nutzt `edip320.fake.FakeDisplay` aus dem Schwesterprojekt. Ist es nicht
erreichbar, werden diese Tests übersprungen statt zu scheitern: die beiden
Projekte sind getrennt und sollen einzeln lauffähig bleiben.
"""
import sys
import time
from pathlib import Path

import pytest

DISPLAY_PROJECT = Path(__file__).parent.parent.parent / 'edip320-display-test'
if DISPLAY_PROJECT.exists():
    sys.path.insert(0, str(DISPLAY_PROJECT))

edip = pytest.importorskip('edip320',
                           reason='Displayprojekt nicht daneben')

from edip320.fake import FakeDisplay          # noqa: E402
from edip320.transport import Display         # noqa: E402

from s0counter.balance import BalanceStore    # noqa: E402
from s0counter.config import from_dict        # noqa: E402
from s0counter.db import Database             # noqa: E402
from s0counter.main import Service            # noqa: E402
from s0counter.panel import (PAGE_COUNT, PAGE_DAY, PAGE_FLATS, PAGE_HOUSE,
                             TOUCH_NEXT, TOUCH_PREV, Panel)  # noqa: E402
from s0counter.sources import Reading         # noqa: E402


@pytest.fixture
def service(tmp_path):
    config = from_dict({
        'general': {'database': str(tmp_path / 'p.sqlite3'), 'http_port': 0},
        'meters': {
            'eg': {'name': 'Wohnung EG', 'gpio': 17},
            'og': {'name': 'Wohnung OG', 'gpio': 27},
            'dg': {'name': 'Wohnung DG', 'gpio': 22},
        },
    })
    instance = Service(config)
    instance.balance_store = BalanceStore(str(tmp_path / 'b.sqlite3'))
    yield instance
    instance.balance_store.close()
    instance.db.close()


@pytest.fixture
def panel(service):
    fake = FakeDisplay()
    return Panel(Display(fake, retries=1, retry_delay=0), service,
                 blank_after_seconds=60.0)


def shown(panel) -> str:
    """Alle bisher gesendeten Befehle als ein Text."""
    return '\n'.join(panel.display.port.decoded_commands())


def add_balance(service, production=1850.0, consumption=430.0,
                grid=-28.0, battery=-1390.0, percent=18.0, ts=None):
    service.balance_store.add(Reading(
        ts=ts if ts is not None else time.time(),
        production_w=production, consumption_w=consumption, grid_w=grid,
        battery_w=battery, battery_percent=percent, source='test'))


class TestAufbau:
    def test_alle_befehle_werden_quittiert(self, panel):
        panel.draw(full=True)
        stats = panel.display.stats
        assert stats.packets_sent > 0
        assert stats.timeouts == 0
        assert stats.naks == 0

    def test_rahmen_und_blaetterflaechen(self, panel):
        panel.draw(full=True)
        text = shown(panel)
        assert '#DL' in text
        assert '#GR2,2,317,237,' in text
        assert f'#AT2,214,60,237,{TOUCH_PREV},0,C<' in text
        assert f'#AT259,214,317,237,{TOUCH_NEXT},0,C>' in text

    def test_touchabfrage_wird_eingeschaltet(self, panel):
        panel.draw(full=True)
        assert '#AA1,' in shown(panel)

    def test_kein_befehl_ueberschreitet_die_paketgrenze(self, panel):
        add_balance(panel.service)
        for seite in range(PAGE_COUNT):
            panel.page = seite
            panel.draw(full=True)
        assert all(len(c) <= 128 for c in panel.display.port.commands)


class TestSeiteHaus:
    def test_ohne_daten_erscheint_ein_hinweis(self, panel):
        panel.page = PAGE_HOUSE
        panel.draw(full=True)
        assert 'Keine Hausdaten' in shown(panel)

    def test_einspeisung_wird_benannt(self, panel):
        add_balance(panel.service, grid=-28.0)
        panel.page = PAGE_HOUSE
        panel.draw(full=True)
        text = shown(panel)
        assert 'Einspeisung 28 W' in text
        assert 'Netz ' not in text.replace('Netz 28', '')

    def test_netzbezug_wird_benannt(self, panel):
        add_balance(panel.service, grid=468.0)
        panel.page = PAGE_HOUSE
        panel.draw(full=True)
        assert 'Netz 468 W' in shown(panel)

    def test_akkurichtung(self, panel):
        add_balance(panel.service, battery=-1390.0)
        panel.page = PAGE_HOUSE
        panel.draw(full=True)
        assert 'Akku laedt' in shown(panel)

        panel.display.port.commands.clear()
        add_balance(panel.service, battery=800.0)
        panel.draw(full=True)
        assert 'Akku entlaedt' in shown(panel)

    def test_grosse_leistung_in_kilowatt(self, panel):
        add_balance(panel.service, grid=12400.0)
        panel.page = PAGE_HOUSE
        panel.draw(full=True)
        assert 'Netz 12.4 kW' in shown(panel)

    def test_alte_daten_werden_gekennzeichnet(self, panel):
        add_balance(panel.service, ts=time.time() - 1800)
        panel.page = PAGE_HOUSE
        panel.draw(full=True)
        assert 'min alt' in shown(panel)

    def test_frische_daten_ohne_hinweis(self, panel):
        add_balance(panel.service)
        panel.page = PAGE_HOUSE
        panel.draw(full=True)
        assert 'min alt' not in shown(panel)


class TestSeiteWohnungen:
    def test_alle_drei_erscheinen(self, panel):
        panel.page = PAGE_FLATS
        panel.draw(full=True)
        text = shown(panel)
        for name in ('EG', 'OG', 'DG'):
            assert name in text

    def test_tagesverbrauch_aus_impulsen(self, panel):
        mitternacht = Panel._midnight()
        for i in range(500):
            panel.service.db.add_pulse('eg', mitternacht + i, mitternacht + i)

        panel.page = PAGE_FLATS
        panel.draw(full=True)
        assert '0.5 kWh' in shown(panel)

    def test_impulse_von_gestern_zaehlen_nicht(self, panel):
        gestern = Panel._midnight() - 3600
        for i in range(500):
            panel.service.db.add_pulse('eg', gestern + i, gestern + i)

        panel.page = PAGE_FLATS
        panel.draw(full=True)
        assert '0.5 kWh' not in shown(panel)


class TestSeiteTag:
    def test_eigendeckung_wird_gerechnet(self, panel):
        mitternacht = Panel._midnight()
        for minute in range(60):
            add_balance(panel.service, production=2000.0, grid=0.0,
                        ts=mitternacht + minute * 60)
        for i in range(1000):
            panel.service.db.add_pulse('eg', mitternacht + i, mitternacht + i)

        panel.page = PAGE_DAY
        panel.draw(full=True)
        text = shown(panel)
        assert 'Eigendeckung' in text
        assert '100 %' in text

    def test_ohne_verbrauch_keine_eigendeckung(self, panel):
        panel.page = PAGE_DAY
        panel.draw(full=True)
        assert 'Eigendeckung' not in shown(panel)


class TestBedienung:
    def test_blaettern_vorwaerts(self, panel):
        assert panel.page == PAGE_HOUSE
        panel.handle_touch(TOUCH_NEXT)
        assert panel.page == PAGE_FLATS
        panel.handle_touch(TOUCH_NEXT)
        assert panel.page == PAGE_DAY

    def test_blaettern_ist_ringfoermig(self, panel):
        panel.page = PAGE_DAY
        panel.handle_touch(TOUCH_NEXT)
        assert panel.page == PAGE_HOUSE

    def test_blaettern_rueckwaerts(self, panel):
        panel.handle_touch(TOUCH_PREV)
        assert panel.page == PAGE_DAY

    def test_taster_weckt_ohne_zu_blaettern(self, panel):
        panel.blank()
        panel.handle_button()
        assert panel.awake
        assert panel.page == PAGE_HOUSE

    def test_taster_blaettert_wenn_wach(self, panel):
        panel.handle_button()
        assert panel.page == PAGE_FLATS

    def test_abschalten_und_einschalten(self, panel):
        panel.blank()
        assert '#DA' in shown(panel)
        assert not panel.awake

        panel.display.port.commands.clear()
        panel.wake()
        assert '#DE' in shown(panel)
        assert panel.awake

    def test_abschaltung_nach_untaetigkeit(self, panel):
        panel.last_input = time.time() - 120
        panel.tick()
        assert not panel.awake

    def test_bedienung_verschiebt_die_abschaltung(self, panel):
        panel.last_input = time.time() - 120
        panel.handle_button()
        panel.tick()
        assert panel.awake

    def test_abgeschaltet_wird_nicht_gezeichnet(self, panel):
        panel.blank()
        panel.display.port.commands.clear()
        panel.draw()
        assert panel.display.port.commands == []

    def test_touch_aus_dem_puffer_wird_verarbeitet(self, panel):
        panel.display.port.press(TOUCH_NEXT)
        panel.tick()
        assert panel.page == PAGE_FLATS


class TestRobustheit:
    def test_stummes_display_stoppt_den_dienst_nicht(self, service):
        from edip320.fake import FakeDisplay as Fake
        panel = Panel(Display(Fake(answer_nothing=True), retries=0,
                              retry_delay=0), service)
        panel.draw(full=True)      # darf nicht werfen
        assert panel.display.stats.timeouts > 0

    def test_umlaute_werden_ersetzt_statt_zu_werfen(self, panel, service):
        service.db.ensure_meter('xx', 'Wohnung Süd ✓', 1000)
        panel.page = PAGE_FLATS
        panel.draw(full=True)      # darf nicht werfen
        assert panel.display.stats.packets_sent > 0
