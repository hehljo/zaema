"""MQTT-Anbindung mit Home-Assistant-Discovery.

Optional. In der aktuellen Installation läuft kein Broker (geprüft am
06.08.2026), die Anbindung an Home Assistant läuft deshalb über
`homeassistant.py`. Dieser Weg bleibt für den Fall, dass später ein Broker
dazukommt — dann ist er der bessere: Discovery legt echte Entitäten an, die
einen Neustart von Home Assistant überstehen.
"""
import json
import logging
import socket

LOG = logging.getLogger(__name__)


class MqttPublisher:
    def __init__(self, config, meters: dict):
        self.config = config
        self.meters = meters
        self.client = None
        self.node = socket.gethostname().replace('.', '_').replace('-', '_')

    def connect(self) -> None:
        import paho.mqtt.client as mqtt

        self.client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=f's0counter-{self.node}')

        if self.config.username:
            self.client.username_pw_set(self.config.username,
                                        self.config.password)

        availability = f'{self.config.base_topic}/availability'
        self.client.will_set(availability, 'offline', retain=True)
        self.client.connect(self.config.host, self.config.port, 60)
        self.client.loop_start()
        self.client.publish(availability, 'online', retain=True)

        LOG.info('MQTT verbunden mit %s:%d', self.config.host, self.config.port)

        if self.config.homeassistant_discovery:
            self.publish_discovery()

    def publish_discovery(self) -> None:
        """Entitäten bei Home Assistant anmelden."""
        base = self.config.base_topic
        device = {
            'identifiers': [f's0counter_{self.node}'],
            'name': f'S0 Zähler {self.node}',
            'manufacturer': 'Eigenbau',
            'model': 'Raspberry Pi S0 Counter',
        }

        sensors = [
            ('energy_kwh', 'Energie', 'kWh', 'energy', 'total_increasing'),
            ('power_w', 'Leistung', 'W', 'power', 'measurement'),
        ]

        for meter_id, meter in self.meters.items():
            for topic_suffix, label, unit, device_class, state_class in sensors:
                key = topic_suffix.split('_')[0]
                payload = {
                    'name': f'{meter.name} {label}',
                    'unique_id': f's0counter_{self.node}_{meter_id}_{key}',
                    'state_topic': f'{base}/{meter_id}/{topic_suffix}',
                    'availability_topic': f'{base}/availability',
                    'unit_of_measurement': unit,
                    'device_class': device_class,
                    'state_class': state_class,
                    'device': device,
                }
                self.client.publish(
                    f'homeassistant/sensor/s0counter_{self.node}/'
                    f'{meter_id}_{key}/config',
                    json.dumps(payload), retain=True)

    def publish_states(self, states: dict) -> None:
        if self.client is None:
            return
        base = self.config.base_topic
        for meter_id, state in states.items():
            values = {
                'energy_kwh': f'{state["energy_kwh"]:.3f}',
                'power_w': f'{state["power_w"]:.1f}',
                'pulses': str(state['pulses']),
                'last_pulse': str(state['last_pulse'] or ''),
            }
            for key, value in values.items():
                self.client.publish(f'{base}/{meter_id}/{key}', value,
                                    retain=True)

    def disconnect(self) -> None:
        if self.client is None:
            return
        self.client.publish(f'{self.config.base_topic}/availability',
                            'offline', retain=True)
        self.client.disconnect()
        self.client.loop_stop()
        self.client = None
