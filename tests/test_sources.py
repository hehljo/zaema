"""Datenquellen für die Hausbilanz."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from s0counter.sources import (HomeAssistantSource, RedundantSource,
                               SonnenbatterieSource, SourceError)

# Echte Antwort einer Sonnenbatterie, aufgenommen am 06.08.2026.
# Bilanz geht auf: 1850 PV − 427 Verbrauch − 1394 Akku ≈ 29 Einspeisung.
ECHTE_ANTWORT = {
    "Apparent_output": 806, "BackupBuffer": "0",
    "BatteryCharging": True, "BatteryDischarging": False,
    "Consumption_Avg": 375, "Consumption_W": 427,
    "Fac": 50.033, "FlowConsumptionBattery": False,
    "FlowConsumptionGrid": False, "FlowConsumptionProduction": True,
    "FlowGridBattery": False, "FlowProductionBattery": True,
    "FlowProductionGrid": True, "GridFeedIn_W": 28.0,
    "IsSystemInstalled": 1, "OperatingMode": "2",
    "Pac_total_W": -1394, "Production_W": 1850, "RSOC": 18,
    "RemainingCapacity_Wh": 2935, "SystemStatus": "OnGrid",
    "Timestamp": "2026-08-06 09:52:37", "USOC": 8,
    "Uac": 229.0, "Ubat": 207.0,
}


class FakeGeraet:
    """Antwortet wie die Sonnenbatterie oder wie Home Assistant."""

    def __init__(self, payload=None, status=200, entities=None):
        self.payload = payload
        self.status = status
        self.entities = entities or {}
        self.requests = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                outer.requests.append(self.path)

                if outer.status != 200:
                    self.send_error(outer.status)
                    return

                if self.path.startswith('/api/states/'):
                    entity = self.path.rsplit('/', 1)[-1]
                    if entity not in outer.entities:
                        self.send_error(404)
                        return
                    body = json.dumps({
                        'entity_id': entity,
                        'state': outer.entities[entity],
                        'attributes': {},
                    }).encode()
                else:
                    body = json.dumps(outer.payload).encode()

                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def url(self):
        return f'http://127.0.0.1:{self.port}'

    def stop(self):
        self.server.shutdown()


@pytest.fixture
def geraet():
    server = FakeGeraet(ECHTE_ANTWORT)
    yield server
    server.stop()


class TestSonnenbatterie:
    def test_werte_werden_uebernommen(self, geraet):
        reading = SonnenbatterieSource(geraet.url).read()
        assert reading.production_w == 1850.0
        assert reading.consumption_w == 427.0
        # USOC (8), nicht RSOC (18) — siehe test_ladestand_ist_der_nutzerwert.
        assert reading.battery_percent == 8.0
        assert reading.source == 'sonnenbatterie'

    def test_ladestand_ist_der_nutzerwert(self, geraet):
        """USOC, nicht RSOC.

        Das Gerät meldet beide. RSOC ist der technische Stand über die
        Bruttokapazität und erreicht nie 0 oder 100 %; USOC dehnt den
        nutzbaren Bereich auf 0–100 % und ist der Wert, den die Sonnen-App
        und Home Assistant anzeigen. In der Anlage standen deshalb zwei
        verschiedene Zahlen für denselben Akku (23 % gegen 16 %).
        """
        reading = SonnenbatterieSource(geraet.url).read()
        assert reading.battery_percent == float(ECHTE_ANTWORT['USOC'])
        assert reading.battery_percent != float(ECHTE_ANTWORT['RSOC'])

    def test_ladestand_faellt_auf_rsoc_zurueck(self):
        """Eine Firmware ohne USOC soll weiterlaufen, nicht anhalten."""
        ohne = {k: v for k, v in ECHTE_ANTWORT.items() if k != 'USOC'}
        server = FakeGeraet(ohne)
        try:
            reading = SonnenbatterieSource(server.url).read()
            assert reading.battery_percent == float(ECHTE_ANTWORT['RSOC'])
        finally:
            server.stop()

    def test_ohne_beide_ladestaende_fehler(self):
        """Fehlen beide, ist die Antwort wirklich unbrauchbar."""
        ohne = {k: v for k, v in ECHTE_ANTWORT.items()
                if k not in ('USOC', 'RSOC')}
        server = FakeGeraet(ohne)
        try:
            with pytest.raises(SourceError, match='USOC'):
                SonnenbatterieSource(server.url).read()
        finally:
            server.stop()

    def test_einspeisung_wird_zu_negativem_netzwert(self, geraet):
        """Das Gerät meldet Einspeisung positiv, Reading erwartet Bezug positiv."""
        reading = SonnenbatterieSource(geraet.url).read()
        assert reading.grid_w == -28.0
        assert reading.grid_export_w == 28.0
        assert reading.grid_import_w == 0.0

    def test_netzbezug_wird_positiv(self):
        """Gegenprobe: GridFeedIn_W negativ bedeutet Bezug."""
        server = FakeGeraet({**ECHTE_ANTWORT, 'GridFeedIn_W': -468.0})
        try:
            reading = SonnenbatterieSource(server.url).read()
            assert reading.grid_w == 468.0
            assert reading.grid_import_w == 468.0
            assert reading.grid_export_w == 0.0
        finally:
            server.stop()

    def test_akku_laden_bleibt_negativ(self, geraet):
        reading = SonnenbatterieSource(geraet.url).read()
        assert reading.battery_w == -1394.0

    def test_energiebilanz_der_echten_antwort_geht_auf(self, geraet):
        """PV minus Verbrauch minus Akkuladung ergibt die Einspeisung."""
        r = SonnenbatterieSource(geraet.url).read()
        rest = r.production_w - r.consumption_w + r.battery_w
        assert rest == pytest.approx(r.grid_export_w, abs=2.0)

    def test_host_ohne_schema_wird_ergaenzt(self):
        source = SonnenbatterieSource('192.168.0.50')
        assert source.host == 'http://192.168.0.50'

    def test_geraet_nicht_erreichbar(self):
        with pytest.raises(SourceError, match='nicht erreichbar'):
            SonnenbatterieSource('http://127.0.0.1:1', timeout=1).read()

    def test_fehlendes_feld_wird_gemeldet(self):
        unvollstaendig = {k: v for k, v in ECHTE_ANTWORT.items()
                          if k != 'GridFeedIn_W'}
        server = FakeGeraet(unvollstaendig)
        try:
            with pytest.raises(SourceError, match='GridFeedIn_W'):
                SonnenbatterieSource(server.url).read()
        finally:
            server.stop()


class TestHomeAssistant:
    def test_werte_und_vorzeichen(self):
        server = FakeGeraet(entities={
            'sensor.sonnenbatterie_123456_state_production': '1850',
            'sensor.sonnenbatterie_123456_state_consumption_current': '427',
            'sensor.sonnenbatterie_123456_state_grid_inout': '-468',
            'sensor.sonnenbatterie_123456_state_battery_inout': '-1394',
            'sensor.sonnenbatterie_123456_state_battery_percentage_user': '18',
        })
        try:
            reading = HomeAssistantSource(server.url, 'token', serial='123456').read()
            assert reading.production_w == 1850.0
            # grid_inout −468 bedeutet Import, Reading erwartet Import positiv
            assert reading.grid_w == 468.0
            assert reading.source == 'homeassistant'
        finally:
            server.stop()

    def test_unavailable_wird_gemeldet(self):
        server = FakeGeraet(entities={
            'sensor.sonnenbatterie_123456_state_production': 'unavailable',
        })
        try:
            with pytest.raises(SourceError, match='unavailable'):
                HomeAssistantSource(server.url, 'token', serial='123456').read()
        finally:
            server.stop()


class TestRedundanz:
    def test_erste_quelle_wird_bevorzugt(self, geraet):
        class Zweite:
            name = 'zweite'
            def read(self):
                raise AssertionError('darf nicht aufgerufen werden')

        source = RedundantSource([SonnenbatterieSource(geraet.url), Zweite()])
        assert source.read().source == 'sonnenbatterie'

    def test_umschalten_bei_ausfall(self, geraet):
        from s0counter.sources import Reading
        import time

        class Ersatz:
            name = 'ersatz'
            def read(self):
                return Reading(time.time(), 0, 0, 0, 0, 0, 'ersatz')

        source = RedundantSource([
            SonnenbatterieSource('http://127.0.0.1:1', timeout=1), Ersatz()])
        assert source.read().source == 'ersatz'
        assert source.statistics()['active'] == 'ersatz'

    def test_alle_quellen_tot(self):
        source = RedundantSource([
            SonnenbatterieSource('http://127.0.0.1:1', timeout=1)])
        with pytest.raises(SourceError, match='Keine Quelle'):
            source.read()

    def test_keine_quelle_konfiguriert(self):
        with pytest.raises(ValueError, match='Mindestens eine'):
            RedundantSource([])


class TestZweitquelleBrauchtSeriennummer:
    """Die Entitätsnamen in Home Assistant hängen an der Seriennummer des Geräts."""

    @staticmethod
    def _config(serial):
        from s0counter.config import from_dict
        return from_dict({
            'general': {'database': '/tmp/x.sqlite3'},
            'meters': {'eg': {'name': 'EG', 'gpio': 17}},
            'homeassistant': {'enabled': True, 'url': 'http://ha:8123', 'token': 't'},
            'sonnenbatterie': {'serial': serial},
        })

    def test_mit_seriennummer_entitaeten_daraus(self):
        from s0counter.sources import build
        quelle = build(self._config('987654'))
        assert [s.name for s in quelle.sources] == ['homeassistant']
        assert quelle.sources[0].entities['production'] == \
            'sensor.sonnenbatterie_987654_state_production'

    def test_ohne_seriennummer_keine_zweitquelle(self):
        from s0counter.sources import build
        assert build(self._config('')) is None
