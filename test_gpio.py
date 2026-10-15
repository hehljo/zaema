#!/usr/bin/env python3
"""Signaltest der S0-Eingänge, bevor der Dienst dauerhaft läuft.

Zeigt jeden erkannten Impuls sofort an, damit sich prüfen lässt, ob eine
Aktivierung genau ein Ereignis erzeugt. Mehrere Ereignisse je Aktivierung
bedeuten Prellen — dann `bounce_ms` erhöhen.

    sudo systemctl stop s0counter
    sudo -u s0counter /opt/s0counter/venv/bin/python /opt/s0counter/test_gpio.py
"""
import argparse
import os
import shutil
import sys
import tempfile
import time

from s0counter import config as config_module
from s0counter.counter import use_lgpio


def main() -> int:
    parser = argparse.ArgumentParser(description='S0-Eingänge prüfen')
    parser.add_argument('-c', '--config',
                        default='/etc/s0counter/config.toml')
    args = parser.parse_args()

    try:
        config = config_module.load(args.config)
    except config_module.ConfigError as exc:
        print(f'Konfigurationsfehler: {exc}', file=sys.stderr)
        return 2

    # lgpio legt seine Notify-Datei .lgd-nfy* über einen relativen Pfad an
    # und scheitert in einem Verzeichnis ohne Schreibrecht. Dieses Skript
    # wird von Hand aufgerufen — aus /opt/s0counter, aus dem Home des
    # Monteurs, aus irgendwoher. Deshalb wird für die Dauer des Tests in
    # ein sicher beschreibbares Verzeichnis gewechselt, statt sich auf das
    # Verzeichnis des Aufrufers zu verlassen.
    use_lgpio()
    workdir = tempfile.mkdtemp(prefix='s0-gpiotest-')
    os.chdir(workdir)

    try:
        from gpiozero import DigitalInputDevice
    except ImportError:
        print('gpiozero fehlt. Läuft dieses Skript wirklich auf dem Pi?',
              file=sys.stderr)
        return 2

    counts: dict[str, int] = {}
    last_seen: dict[str, float] = {}
    devices = []

    def make_handler(meter_id: str):
        def handler():
            now = time.monotonic()
            counts[meter_id] = counts.get(meter_id, 0) + 1
            gap = ''
            if meter_id in last_seen:
                delta = (now - last_seen[meter_id]) * 1000
                gap = f'  Abstand {delta:8.1f} ms'
                if delta < 50:
                    gap += '  <- sehr kurz, auf Prellen prüfen'
            last_seen[meter_id] = now
            print(f'{time.strftime("%H:%M:%S")}  IMPULS  {meter_id:5} '
                  f'Nr. {counts[meter_id]:5}{gap}', flush=True)
        return handler

    print('GPIO-Test. Jeden Kanal einzeln aktivieren. Ctrl+C beendet.\n')

    for meter_id, meter in config.meters.items():
        device = DigitalInputDevice(
            meter.gpio,
            pull_up=meter.active_low,
            bounce_time=meter.bounce_ms / 1000.0)
        device.when_activated = make_handler(meter_id)
        devices.append(device)

        ruhe = 'aktiv' if device.value else 'inaktiv'
        print(f'  {meter_id:5} {meter.name:20} BCM GPIO{meter.gpio:<3} '
              f'active_{"low" if meter.active_low else "high":5} '
              f'Ruhezustand: {ruhe}')

    print('\nErwartet wird im Ruhezustand "inaktiv". Steht ein Kanal dauerhaft '
          'auf "aktiv",\nist active_low vermutlich falsch herum '
          'oder der Eingang liegt fest an einem Pegel.\n')

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print('\n\nErgebnis:')
        for meter_id in config.meters:
            print(f'  {meter_id:5} {counts.get(meter_id, 0):5} Impulse')
        print('\nDanach: sudo systemctl start s0counter')
    finally:
        for device in devices:
            device.close()
        # Erst nach dem Schließen: lgpio räumt seine Notify-Datei selbst
        # weg, und das kann es nur, solange das Verzeichnis noch steht.
        os.chdir('/')
        shutil.rmtree(workdir, ignore_errors=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
