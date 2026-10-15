"""Lokales Anzeigepanel auf dem EA eDIP320.

Drei Seiten, die per Touch oder Taster durchgeblättert werden:

  1  Haus       PV, Verbrauch, Netz, Akku
  2  Wohnungen  EG, OG, DG mit Leistung und Tagesverbrauch
  3  Tag        Bilanz seit Mitternacht

Der Taster an GPIO23 weckt das Display und schaltet es nach einer Weile
ohne Bedienung wieder ab — ein Panel im Hausflur muss nachts nicht leuchten.

Das Display ist 320x240 Pixel und monochrom. Das begrenzt die Menge: pro
Seite etwa acht Zeilen, die aus zwei Metern lesbar sind. Deshalb große
Schrift für die eine Zahl, auf die es ankommt, und kleine für den Rest.
"""
import logging
import threading
import time

from . import ordnung

LOG = logging.getLogger(__name__)

WIDTH = 320
HEIGHT = 240

# Touch-Codes der Blätterflächen
TOUCH_NEXT = 10
TOUCH_PREV = 11

PAGE_HOUSE = 0
PAGE_FLATS = 1
PAGE_DAY = 2
PAGE_COUNT = 3

PAGE_TITLES = ('Haus', 'Wohnungen', 'Heute')


def _watt(value: float) -> str:
    """Leistung kurz und ohne Nachkommastellen — auf zwei Meter lesbar."""
    if abs(value) >= 10000:
        return f'{value / 1000:.1f} kW'
    return f'{value:.0f} W'


def _kwh(value: float) -> str:
    return f'{value:.1f} kWh'


class Panel:
    """Zeichnet die Anzeigeseiten und wertet Touch und Taster aus.

    Die Verbindung zum Display kommt von außen — dadurch lässt sich das
    Panel gegen das simulierte Display aus dem Schwesterprojekt testen,
    ohne dass Hardware nötig wäre.
    """

    def __init__(self, display, service, blank_after_seconds: float = 120.0,
                 refresh_seconds: float = 5.0):
        self.display = display
        self.service = service
        self.blank_after = blank_after_seconds
        self.refresh_seconds = refresh_seconds

        self.page = PAGE_HOUSE
        self.awake = True
        self.last_input = time.time()
        self.stop_event = threading.Event()
        self.thread = None
        self._drawn_page = None

    # --- Befehle ---------------------------------------------------------

    def _send(self, payload: bytes) -> bool:
        result = self.display.send(payload)
        if not result.ok:
            LOG.debug('Display antwortet nicht: %s', result.message)
        return result.ok

    def _cmd(self, text: str, terminator: bytes = b'') -> bool:
        return self._send(text.encode('latin-1') + terminator)

    def _text(self, x: int, y: int, value: str, align: str = 'L') -> bool:
        """Text ausgeben. Zeichen ausserhalb von Latin-1 werden ersetzt."""
        safe = value.encode('latin-1', 'replace').decode('latin-1')
        code = {'L': 'ZL', 'C': 'ZC', 'R': 'ZR'}[align]
        return self._cmd(f'#{code}{x},{y},{safe}', b'\r')

    def _font(self, number: int) -> bool:
        return self._cmd(f'#ZF{number},')

    # --- Aufbau ----------------------------------------------------------

    def _frame(self) -> None:
        """Rahmen und Kopfzeile — bleibt beim Seitenwechsel stehen."""
        self._cmd('#DL')
        self._cmd('#TA')
        self._cmd('#ZV4,')
        self._cmd('#AA1,')
        self._cmd('#AL0,1,')

        self._cmd(f'#GR2,2,{WIDTH - 3},{HEIGHT - 3},')
        self._cmd(f'#GD2,26,{WIDTH - 3},26,')

        # Blätterflächen links und rechts unten, ohne sichtbaren Rahmen
        self._cmd('#AE0,')
        self._cmd(f'#AT2,{HEIGHT - 26},60,{HEIGHT - 3},'
                  f'{TOUCH_PREV},0,C<', b'\r')
        self._cmd(f'#AT{WIDTH - 61},{HEIGHT - 26},{WIDTH - 3},{HEIGHT - 3},'
                  f'{TOUCH_NEXT},0,C>', b'\r')
        self._cmd('#AE1,')

    def _header(self) -> None:
        self._cmd(f'#RL4,4,{WIDTH - 5},24,')
        self._font(4)
        self._text(10, 8, PAGE_TITLES[self.page])
        self._font(3)
        self._text(WIDTH - 10, 10, time.strftime('%H:%M'), 'R')

    def _clear_body(self) -> None:
        self._cmd(f'#RL4,28,{WIDTH - 5},{HEIGHT - 28},')

    # --- Seiten ----------------------------------------------------------

    def _page_house(self) -> None:
        balance = self.service.house_balance()
        if balance is None:
            self._font(4)
            self._text(WIDTH // 2, 110, 'Keine Hausdaten', 'C')
            self._font(3)
            self._text(WIDTH // 2, 138, 'Bilanzerfassung inaktiv', 'C')
            return

        # Die eine Zahl, auf die es ankommt: Netz. Groß und mittig.
        grid = balance['grid_w']
        self._font(6)
        if grid > 0:
            self._text(WIDTH // 2, 44, f'Netz {_watt(grid)}', 'C')
        else:
            self._text(WIDTH // 2, 44, f'Einspeisung {_watt(-grid)}', 'C')

        self._font(4)
        rows = [
            ('PV', _watt(balance['production_w'])),
            ('Verbrauch', _watt(balance['consumption_w'])),
        ]

        battery = balance['battery_w']
        if battery > 0:
            rows.append(('Akku entlaedt', _watt(battery)))
        elif battery < 0:
            rows.append(('Akku laedt', _watt(-battery)))
        else:
            rows.append(('Akku', 'ruht'))
        rows.append(('Ladestand', f'{balance["battery_percent"]:.0f} %'))

        y = 92
        for label, value in rows:
            self._text(16, y, label)
            self._text(WIDTH - 16, y, value, 'R')
            y += 26

        age = time.time() - balance['ts']
        if age > 300:
            self._font(3)
            self._text(WIDTH // 2, HEIGHT - 44,
                       f'Daten {age / 60:.0f} min alt', 'C')

    def _page_flats(self) -> None:
        states = self.service.states()
        today = self._today_consumption()

        self._font(3)
        self._text(16, 36, 'Wohnung')
        self._text(202, 36, 'jetzt', 'R')
        self._text(WIDTH - 16, 36, 'heute', 'R')
        self._cmd(f'#GD10,52,{WIDTH - 11},52,')

        self._font(4)
        y = 62
        for meter_id in ordnung.sortiert(states):
            state = states[meter_id]
            name = state['name'].replace('Strom ', '').replace('Wohnung ', '')
            self._text(16, y, name)
            self._text(202, y, _watt(state['power_w']), 'R')
            self._text(WIDTH - 16, y,
                       _kwh(today.get(meter_id, 0.0)), 'R')
            y += 30

        gesamt = sum(s['power_w'] for s in states.values())
        self._cmd(f'#GD10,{y - 6},{WIDTH - 11},{y - 6},')
        self._text(16, y + 2, 'Summe')
        self._text(202, y + 2, _watt(gesamt), 'R')
        self._text(WIDTH - 16, y + 2, _kwh(sum(today.values())), 'R')

    def _page_day(self) -> None:
        start = self._midnight()
        today = self._today_consumption()
        verbrauch = sum(today.values())

        grid = pv = 0.0
        store = getattr(self.service, 'balance_store', None)
        if store is not None:
            for _, grid_kwh, pv_kwh, _ in store.iter_interval_energy(
                    start, time.time(), 900.0):
                grid += grid_kwh
                pv += pv_kwh

        self._font(4)
        rows = [
            ('Wohnungen', _kwh(verbrauch)),
            ('PV erzeugt', _kwh(pv)),
            ('aus dem Netz', _kwh(grid)),
        ]
        if verbrauch > 0:
            anteil = max(0.0, 1.0 - grid / verbrauch) * 100
            rows.append(('Eigendeckung', f'{anteil:.0f} %'))

        y = 50
        for label, value in rows:
            self._text(16, y, label)
            self._text(WIDTH - 16, y, value, 'R')
            y += 30

        self._font(3)
        self._text(WIDTH // 2, HEIGHT - 44,
                   f'seit {time.strftime("%d.%m.", time.localtime(start))} '
                   f'00:00', 'C')

    # --- Hilfsgrößen -----------------------------------------------------

    @staticmethod
    def _midnight() -> float:
        now = time.localtime()
        return time.mktime((now.tm_year, now.tm_mon, now.tm_mday,
                            0, 0, 0, 0, 0, -1))

    def _today_consumption(self) -> dict:
        """Verbrauch je Wohnung seit Mitternacht, aus den Rohimpulsen."""
        start = self._midnight()
        result = {}
        for meter_id in self.service.db.meter_ids():
            ppk = self.service.db.state(meter_id)['pulses_per_kwh']
            count = sum(1 for _ in self.service.db.iter_pulses(
                meter_id, since=start))
            result[meter_id] = count / ppk
        return result

    # --- Ablauf ----------------------------------------------------------

    def draw(self, full: bool = False) -> None:
        if not self.awake:
            return
        if full or self._drawn_page != self.page:
            self._frame()
            self._drawn_page = self.page
        else:
            self._clear_body()

        self._header()
        {
            PAGE_HOUSE: self._page_house,
            PAGE_FLATS: self._page_flats,
            PAGE_DAY: self._page_day,
        }[self.page]()

    def wake(self) -> None:
        if not self.awake:
            self.awake = True
            self._cmd('#DE')
            LOG.info('Anzeige eingeschaltet')
        self.last_input = time.time()
        self.draw(full=True)

    def blank(self) -> None:
        if self.awake:
            self.awake = False
            self._cmd('#DA')
            self._drawn_page = None
            LOG.info('Anzeige abgeschaltet')

    def next_page(self, step: int = 1) -> None:
        self.page = (self.page + step) % PAGE_COUNT
        self.last_input = time.time()
        if not self.awake:
            self.wake()
        else:
            self.draw()

    def handle_touch(self, code: int) -> None:
        if code == TOUCH_NEXT:
            self.next_page(1)
        elif code == TOUCH_PREV:
            self.next_page(-1)
        else:
            self.wake()

    def handle_button(self) -> None:
        """Taster: weckt die Anzeige, blättert wenn sie schon wach ist."""
        if self.awake:
            self.next_page(1)
        else:
            self.wake()

    def tick(self) -> None:
        """Einmal turnusmäßig auffrischen und die Abschaltung prüfen."""
        for code, _ in self.display.poll_touch():
            self.handle_touch(code)

        if self.awake:
            if self.blank_after > 0 and \
                    time.time() - self.last_input > self.blank_after:
                self.blank()
            else:
                self.draw()

    def run(self) -> None:
        self.wake()
        while not self.stop_event.wait(self.refresh_seconds):
            try:
                self.tick()
            except Exception as exc:
                LOG.warning('Anzeige: %s', exc)

    def start(self) -> None:
        self.thread = threading.Thread(target=self.run, daemon=True,
                                       name='panel')
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join(timeout=5)
        try:
            self._cmd('#DL')
            self._cmd('#DA')
        except Exception:
            pass


class PanelButton:
    """Der Taster an GPIO23.

    Getrennt von der Anzeige, damit die Anzeige ohne GPIO testbar bleibt.
    """

    def __init__(self, gpio: int, panel: Panel, bounce_ms: float = 50.0):
        self.gpio = gpio
        self.panel = panel
        self.bounce_ms = bounce_ms
        self.device = None

    def setup(self) -> None:
        from gpiozero import Button

        # Schließer gegen GND, interner Pull-up. Kein externer Widerstand.
        self.device = Button(self.gpio, pull_up=True,
                             bounce_time=self.bounce_ms / 1000.0)
        self.device.when_pressed = self.panel.handle_button
        LOG.info('Taster an BCM GPIO%d', self.gpio)

    def close(self) -> None:
        if self.device is not None:
            self.device.close()
            self.device = None
