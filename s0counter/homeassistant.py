"""Zählerstände an Home Assistant melden.

Die Installation hat keinen MQTT-Broker (Stand 06.08.2026, geprüft auf
192.168.0.30, 192.168.0.40 und 192.168.0.50). Die Sensoren werden deshalb
über die REST-API gesetzt.

Grenze des Verfahrens: über die REST-API gesetzte Zustände sind nicht an eine
Integration gebunden. Nach einem Neustart von Home Assistant sind sie weg, bis
der Zähler das nächste Mal veröffentlicht — bei einem Veröffentlichungsintervall
von 30 Sekunden ist die Lücke kurz. Der Langzeitverlauf im Energie-Dashboard
bleibt erhalten, weil er aus der Recorder-Datenbank kommt.
"""
import json
import logging
import threading
import urllib.error
import urllib.request

LOG = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 10.0


class HomeAssistantError(Exception):
    """Die API war nicht erreichbar oder hat den Zustand abgelehnt."""


class HomeAssistantClient:
    """Setzt Sensorzustände über die REST-API.

    Fehler beim Veröffentlichen werden protokolliert, aber nicht
    weitergereicht: die Impulserfassung darf nicht stehenbleiben, nur weil
    Home Assistant gerade neu startet.
    """

    def __init__(self, base_url: str, token: str, prefix: str = 's0',
                 timeout: float = DEFAULT_TIMEOUT, verify_on_start: bool = True):
        self.base_url = base_url.rstrip('/')
        self.token = token
        self.prefix = prefix
        self.timeout = timeout
        self.lock = threading.Lock()
        self.failures = 0
        self.publishes = 0
        self._warned = False

        if verify_on_start:
            self.verify()

    # --- Transport -------------------------------------------------------

    def _request(self, method: str, path: str, payload: dict | None = None) -> dict:
        url = f'{self.base_url}{path}'
        data = json.dumps(payload).encode('utf-8') if payload is not None else None
        request = urllib.request.Request(url, data=data, method=method)
        request.add_header('Authorization', f'Bearer {self.token}')
        if data is not None:
            request.add_header('Content-Type', 'application/json')

        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read().decode('utf-8')
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                raise HomeAssistantError(
                    'Token abgelehnt. Neues Long-Lived Access Token in Home '
                    'Assistant unter Profil, Sicherheit anlegen.') from exc
            raise HomeAssistantError(
                f'HTTP {exc.code} von {url}: {exc.reason}') from exc
        except urllib.error.URLError as exc:
            raise HomeAssistantError(
                f'{url} nicht erreichbar: {exc.reason}') from exc

        return json.loads(body) if body else {}

    def verify(self) -> str:
        """Erreichbarkeit und Token prüfen, gibt die Version zurück."""
        self._request('GET', '/api/')
        config = self._request('GET', '/api/config')
        version = config.get('version', 'unbekannt')
        LOG.info('Home Assistant %s erreichbar unter %s', version, self.base_url)
        return version

    # --- Veröffentlichen -------------------------------------------------

    def entity_id(self, meter_id: str, suffix: str) -> str:
        return f'sensor.{self.prefix}_{meter_id}_{suffix}'

    def _set_state(self, entity_id: str, state, attributes: dict) -> None:
        self._request('POST', f'/api/states/{entity_id}',
                      {'state': state, 'attributes': attributes})

    def publish_meter(self, meter_id: str, state: dict) -> None:
        """Energie- und Leistungssensor eines Zählers aktualisieren."""
        name = state['name']

        self._set_state(
            self.entity_id(meter_id, 'energy'),
            f'{state["energy_kwh"]:.3f}',
            {
                'friendly_name': f'{name} Energie',
                'unit_of_measurement': 'kWh',
                'device_class': 'energy',
                'state_class': 'total_increasing',
                'icon': 'mdi:counter',
                'pulses': state['pulses'],
                'pulses_per_kwh': state['pulses_per_kwh'],
                'offset_kwh': round(state['offset_kwh'], 3),
                'last_pulse': state['last_pulse'],
            })

        self._set_state(
            self.entity_id(meter_id, 'power'),
            f'{state["power_w"]:.1f}',
            {
                'friendly_name': f'{name} Leistung',
                'unit_of_measurement': 'W',
                'device_class': 'power',
                'state_class': 'measurement',
                'icon': 'mdi:flash',
            })

    def publish_all(self, states: dict) -> bool:
        """Alle Zähler melden. Gibt zurück, ob es geklappt hat."""
        with self.lock:
            try:
                for meter_id, state in states.items():
                    self.publish_meter(meter_id, state)
            except HomeAssistantError as exc:
                self.failures += 1
                if not self._warned:
                    LOG.warning('Home Assistant nicht erreichbar: %s '
                                '(Erfassung läuft weiter)', exc)
                    self._warned = True
                return False

            if self._warned:
                LOG.info('Home Assistant wieder erreichbar')
                self._warned = False
            self.publishes += 1
            return True

    def remove_entities(self, meter_ids: list[str]) -> int:
        """Sensoren des Zählers aus Home Assistant entfernen."""
        removed = 0
        for meter_id in meter_ids:
            for suffix in ('energy', 'power'):
                try:
                    self._request('DELETE',
                                  f'/api/states/{self.entity_id(meter_id, suffix)}')
                    removed += 1
                except HomeAssistantError:
                    pass
        return removed
