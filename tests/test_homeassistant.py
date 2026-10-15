"""Home-Assistant-Anbindung gegen einen lokalen Testserver."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from s0counter.homeassistant import HomeAssistantClient, HomeAssistantError

TOKEN = 'testtoken'


class FakeHomeAssistant:
    """Ein Server, der sich wie die REST-API von Home Assistant verhält."""

    def __init__(self, *, reject_token: bool = False, fail_states: bool = False):
        self.states: dict[str, dict] = {}
        self.deleted: list[str] = []
        self.reject_token = reject_token
        self.fail_states = fail_states
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def _authorized(self) -> bool:
                header = self.headers.get('Authorization', '')
                if outer.reject_token or header != f'Bearer {TOKEN}':
                    self.send_error(401, 'Unauthorized')
                    return False
                return True

            def _json(self, payload, code=200):
                body = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if not self._authorized():
                    return
                if self.path == '/api/':
                    self._json({'message': 'API running.'})
                elif self.path == '/api/config':
                    self._json({'version': '2025.10.3',
                                'location_name': 'Test'})
                else:
                    self.send_error(404)

            def do_POST(self):
                if not self._authorized():
                    return
                if outer.fail_states:
                    self.send_error(500, 'Internal Server Error')
                    return

                length = int(self.headers.get('Content-Length', 0))
                payload = json.loads(self.rfile.read(length))
                entity = self.path.rsplit('/', 1)[-1]
                outer.states[entity] = payload
                self._json({'entity_id': entity, **payload}, 201)

            def do_DELETE(self):
                if not self._authorized():
                    return
                entity = self.path.rsplit('/', 1)[-1]
                if entity in outer.states:
                    del outer.states[entity]
                    outer.deleted.append(entity)
                    self._json({})
                else:
                    self.send_error(404)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def url(self) -> str:
        return f'http://127.0.0.1:{self.port}'

    def stop(self):
        self.server.shutdown()


@pytest.fixture
def ha():
    server = FakeHomeAssistant()
    yield server
    server.stop()


def zustand(name='Strom EG', kwh=1234.567, watt=450.0, impulse=1234567):
    return {
        'id': 'eg', 'name': name, 'pulses_per_kwh': 1000,
        'offset_kwh': 0.0, 'pulses': impulse, 'energy_kwh': kwh,
        'last_pulse': 1_700_000_000.0, 'power_w': watt,
    }


class TestVerbindung:
    def test_erreichbarkeit_und_version(self, ha):
        client = HomeAssistantClient(ha.url, TOKEN)
        assert client.verify() == '2025.10.3'

    def test_falsches_token(self, ha):
        with pytest.raises(HomeAssistantError, match='Token abgelehnt'):
            HomeAssistantClient(ha.url, 'falsch')

    def test_server_nicht_erreichbar(self):
        with pytest.raises(HomeAssistantError, match='nicht erreichbar'):
            HomeAssistantClient('http://127.0.0.1:1', TOKEN, timeout=1)

    def test_pruefung_ueberspringen(self):
        """Der Dienst muss auch starten, wenn Home Assistant gerade neu lädt."""
        client = HomeAssistantClient('http://127.0.0.1:1', TOKEN,
                                     verify_on_start=False)
        assert client.failures == 0


class TestVeroeffentlichen:
    def test_zwei_sensoren_je_zaehler(self, ha):
        client = HomeAssistantClient(ha.url, TOKEN)
        assert client.publish_all({'eg': zustand()})
        assert set(ha.states) == {'sensor.s0_eg_energy', 'sensor.s0_eg_power'}

    def test_energiesensor_fuer_das_dashboard(self, ha):
        client = HomeAssistantClient(ha.url, TOKEN)
        client.publish_all({'eg': zustand(kwh=1234.567)})

        sensor = ha.states['sensor.s0_eg_energy']
        assert sensor['state'] == '1234.567'
        attribute = sensor['attributes']
        assert attribute['unit_of_measurement'] == 'kWh'
        assert attribute['device_class'] == 'energy'
        assert attribute['state_class'] == 'total_increasing'

    def test_leistungssensor(self, ha):
        client = HomeAssistantClient(ha.url, TOKEN)
        client.publish_all({'eg': zustand(watt=450.0)})

        sensor = ha.states['sensor.s0_eg_power']
        assert sensor['state'] == '450.0'
        assert sensor['attributes']['device_class'] == 'power'
        assert sensor['attributes']['state_class'] == 'measurement'

    def test_rohimpulse_stehen_als_attribut_dabei(self, ha):
        client = HomeAssistantClient(ha.url, TOKEN)
        client.publish_all({'eg': zustand(impulse=999)})
        assert ha.states['sensor.s0_eg_energy']['attributes']['pulses'] == 999

    def test_alle_drei_wohnungen(self, ha):
        client = HomeAssistantClient(ha.url, TOKEN)
        client.publish_all({
            'eg': zustand('Strom EG'),
            'og': zustand('Strom OG'),
            'dg': zustand('Strom DG'),
        })
        assert len(ha.states) == 6

    def test_eigener_praefix(self, ha):
        client = HomeAssistantClient(ha.url, TOKEN, prefix='haus')
        client.publish_all({'eg': zustand()})
        assert 'sensor.haus_eg_energy' in ha.states


class TestAusfall:
    def test_fehler_stoppt_die_erfassung_nicht(self, ha):
        """Ein Serverfehler darf nicht bis in die Impulsschleife durchschlagen."""
        ha.fail_states = True
        client = HomeAssistantClient(ha.url, TOKEN)

        assert client.publish_all({'eg': zustand()}) is False
        assert client.failures == 1

    def test_erholung_wird_erkannt(self, ha):
        ha.fail_states = True
        client = HomeAssistantClient(ha.url, TOKEN)
        client.publish_all({'eg': zustand()})

        ha.fail_states = False
        assert client.publish_all({'eg': zustand()}) is True
        assert client.publishes == 1


class TestAufraeumen:
    def test_sensoren_entfernen(self, ha):
        client = HomeAssistantClient(ha.url, TOKEN)
        client.publish_all({'eg': zustand(), 'og': zustand()})

        assert client.remove_entities(['eg', 'og']) == 4
        assert ha.states == {}
