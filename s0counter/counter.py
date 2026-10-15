"""Impulsannahme und Plausibilitätsprüfung.

Getrennt von der GPIO-Anbindung, damit sich die Entprellung ohne Hardware
testen lässt.
"""
import logging
import os
import threading
import time

LOG = logging.getLogger(__name__)


class PulseCounter:
    """Nimmt Impulse entgegen, verwirft Prellen und offensichtlichen Unsinn.

    Zwei Filter greifen nacheinander:

    1. `bounce_ms` — der harte Mindestabstand. Der Optokoppler und die
       mechanische Kontaktgabe im Zähler erzeugen Flanken im
       Millisekundenbereich; ohne Filter wird ein Impuls mehrfach gezählt.

    2. Plausibilität — ein Impulsabstand, der einer Leistung jenseits von
       60 kW entspräche, kann bei einem 3x80-A-Zähler kein echter Verbrauch
       sein. Solche Impulse werden verworfen und gezählt, damit
       Einstreuungen sichtbar werden statt still den Zählerstand zu
       verfälschen.

    Der Zeitstempel kommt aus `time.time()` für die Ablage und aus
    `time.monotonic()` für die Abstandsmessung — eine Zeitumstellung oder
    ein NTP-Sprung darf die Leistungsberechnung nicht verfälschen.
    """

    def __init__(self, meter_config, database, on_pulse=None):
        self.config = meter_config
        self.db = database
        self.on_pulse = on_pulse
        self.lock = threading.Lock()

        self._last_accepted_mono: float | None = None
        self.accepted = 0
        self.bounced = 0
        self.implausible = 0

    @property
    def meter_id(self) -> str:
        return self.config.meter_id

    def _plausibility_limit_seconds(self) -> float:
        """Kürzester Impulsabstand, der noch als echter Verbrauch durchgeht.

        Liegt der Wert unter `bounce_ms`, greift allein die Entprellung —
        die Plausibilitätsprüfung darf den vom Benutzer eingestellten
        Mindestabstand nicht heimlich verschärfen.
        """
        wh_per_pulse = self.config.wh_per_pulse
        max_power = self.config.max_plausible_power_w
        return (wh_per_pulse * 3600.0) / max_power

    def handle_pulse(self, wall_time: float | None = None,
                     mono_time: float | None = None) -> bool:
        """Einen Impuls verarbeiten. Gibt zurück, ob er gezählt wurde."""
        now_wall = time.time() if wall_time is None else wall_time
        now_mono = time.monotonic() if mono_time is None else mono_time

        with self.lock:
            previous = self._last_accepted_mono

            if previous is not None:
                gap = now_mono - previous
                # Beide Grenzen sind einschließend: ein Abstand von exakt
                # bounce_ms gilt als angenommen. Die Toleranz fängt ab, dass
                # 1000.010 - 1000.0 in Fließkomma knapp unter 0,010 liegt.
                tolerance = 1e-9
                bounce_limit = self.config.bounce_ms / 1000.0

                if gap < bounce_limit - tolerance:
                    self.bounced += 1
                    LOG.debug('%s: Impuls nach %.1f ms verworfen (Prellen)',
                              self.meter_id, gap * 1000)
                    return False

                # Die Plausibilitätsprüfung greift nur oberhalb der
                # Entprellung. Wer bounce_ms hochsetzt, filtert bewusst
                # gröber und soll nicht zusätzlich Warnungen bekommen.
                plausibility_limit = self._plausibility_limit_seconds()
                if bounce_limit < gap < plausibility_limit - tolerance:
                    self.implausible += 1
                    power = 3_600_000.0 / (self.config.pulses_per_kwh * gap)
                    LOG.warning(
                        '%s: Impuls nach %.1f ms verworfen — entspräche %.0f W. '
                        'Verkabelung oder Einstreuung prüfen.',
                        self.meter_id, gap * 1000, power)
                    return False

            self._last_accepted_mono = now_mono
            self.accepted += 1

        self.db.add_pulse(self.meter_id, now_wall, now_mono)

        if self.on_pulse is not None:
            self.on_pulse(self.meter_id)
        return True

    def statistics(self) -> dict:
        with self.lock:
            return {
                'accepted': self.accepted,
                'bounced': self.bounced,
                'implausible': self.implausible,
            }


def use_lgpio() -> None:
    """Legt die Pin-Factory auf lgpio fest, bevor gpiozero sie errät.

    Ohne diese Festlegung probiert gpiozero der Reihe nach durch und
    nimmt die erste Factory, die sich bauen lässt. Schlägt lgpio fehl —
    etwa weil das Arbeitsverzeichnis nicht beschreibbar ist und die
    Notify-Datei `.lgd-nfy*` nicht angelegt werden kann —, wird der
    Fehler verschluckt und `native` verwendet. Die arbeitet über
    /sys/class/gpio, das es ab Raspberry Pi OS Trixie nicht mehr gibt;
    der Aufruf endet dann in »OSError: [Errno 22] Invalid argument«, aus
    dem die eigentliche Ursache nicht hervorgeht.

    Wird nur gesetzt, wenn die Umgebung nichts vorgibt: eine bewusst
    gesetzte GPIOZERO_PIN_FACTORY hat Vorrang, damit sich zum Suchen
    eines Fehlers weiterhin eine andere Factory erzwingen lässt.
    """
    os.environ.setdefault('GPIOZERO_PIN_FACTORY', 'lgpio')


class GpioBinding:
    """Verbindet die GPIO-Eingänge mit den Zählern.

    `gpiozero` wird erst hier importiert, damit sich alles andere auf einem
    Rechner ohne GPIO testen lässt.
    """

    def __init__(self, counters: dict):
        self.counters = counters
        self.devices = []

    def setup(self) -> None:
        use_lgpio()
        from gpiozero import DigitalInputDevice

        for meter_id, counter in self.counters.items():
            config = counter.config

            # pull_up=True zieht die Leitung auf 3,3 V — die aktive Flanke
            # ist dann die fallende. Genau das liefert ein
            # Open-Collector-Ausgang, der während des Impulses gegen GND zieht.
            device = DigitalInputDevice(
                config.gpio,
                pull_up=config.active_low,
                bounce_time=config.bounce_ms / 1000.0,
            )
            device.when_activated = self._make_handler(counter)
            self.devices.append(device)

            LOG.info('%s (%s) an BCM GPIO%d, active_%s, Entprellung %.0f ms',
                     meter_id, config.name, config.gpio,
                     'low' if config.active_low else 'high', config.bounce_ms)

    @staticmethod
    def _make_handler(counter):
        def handler():
            counter.handle_pulse()
        return handler

    def close(self) -> None:
        for device in self.devices:
            device.close()
        self.devices.clear()
