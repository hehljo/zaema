"""Quellen für die Hausbilanz — PV, Netz und Speicher.

Die S0-Zähler messen, *wie viel* jede Wohnung verbraucht. Für die Frage,
*woher* der Strom kam, braucht es eine zweite Quelle: den Netzbezug des
gesamten Hauses im selben Zeitraum.

Alle Quellen liefern denselben Messwert-Satz (`Reading`), damit sie
gegeneinander austauschbar sind. Aktuell gibt es zwei Implementierungen und
Platz für eine dritte:

  SonnenbatterieSource   direkt am Gerät, ohne Anmeldung, offline
  HomeAssistantSource    Zweitquelle, falls die Sonnenbatterie nicht antwortet
  (später) Lesekopf am geeichten Netzzähler über SML/D0 — die sauberste
  Quelle, weil direkt an der Messstelle. Dafür genügt eine weitere Klasse
  mit derselben `read()`-Methode; am Sammler ändert sich nichts.
"""
import json
import logging
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

LOG = logging.getLogger(__name__)


class SourceError(Exception):
    """Die Quelle war nicht erreichbar oder lieferte Unbrauchbares."""


@dataclass(frozen=True)
class Reading:
    """Eine Momentaufnahme der Hausbilanz.

    Vorzeichen sind hier vereinheitlicht, unabhängig davon, wie die Quelle
    sie liefert:

      grid_w    positiv = Bezug aus dem Netz, negativ = Einspeisung
      battery_w positiv = Entladung, negativ = Ladung
    """
    ts: float
    production_w: float
    consumption_w: float
    grid_w: float
    battery_w: float
    battery_percent: float
    source: str

    @property
    def grid_import_w(self) -> float:
        """Nur der Bezug, nie negativ."""
        return max(0.0, self.grid_w)

    @property
    def grid_export_w(self) -> float:
        """Nur die Einspeisung, nie negativ."""
        return max(0.0, -self.grid_w)


def _fetch_json(url: str, timeout: float, headers: dict | None = None) -> dict:
    request = urllib.request.Request(url)
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode('utf-8'))
    except urllib.error.HTTPError as exc:
        raise SourceError(f'HTTP {exc.code} von {url}: {exc.reason}') from exc
    except urllib.error.URLError as exc:
        raise SourceError(f'{url} nicht erreichbar: {exc.reason}') from exc
    except json.JSONDecodeError as exc:
        raise SourceError(f'{url} lieferte kein gültiges JSON: {exc}') from exc


class SonnenbatterieSource:
    """Liest die Sonnenbatterie direkt über ihre lokale HTTP-Schnittstelle.

    `/api/v2/status` antwortet im Heimnetz ohne Anmeldung. Die Endpunkte
    unter `/api/v2/latestdata` und `/api/v2/powermeter` verlangen einen
    Token aus der Weboberfläche — für die Bilanz werden sie nicht gebraucht.

    Vorzeichen laut Gerät, geprüft am 06.08.2026 gegen die Flow-Flags:
      GridFeedIn_W  positiv = Einspeisung ins Netz
                    negativ = Bezug aus dem Netz
      Pac_total_W   negativ = Akku wird geladen
                    positiv = Akku wird entladen
    Beide werden hier auf die Konvention von `Reading` gedreht.

    Ladestand: Das Gerät meldet **zwei** Werte, und sie unterscheiden sich
    deutlich (gemessen am 25.08.2026: RSOC 23, USOC 16).

      USOC  „User State of Charge" — der nutzbare Bereich auf 0–100 %
            gedehnt. Das ist der Wert, den die Sonnen-App und die
            Home-Assistant-Integration anzeigen.
      RSOC  „Relative State of Charge" — der technische Stand über die
            Bruttokapazität. Er erreicht nie 0 % und nie 100 %, weil der
            Akku in beide Richtungen eine Reserve behält.

    Genommen wird USOC: der Wert soll neben dem in Home Assistant stehen
    können, ohne dass jemand zwei verschiedene Zahlen für dasselbe sieht.
    RSOC stand hier bis zum 25.08.2026 und war der Grund für genau diese
    Verwirrung — siehe docs/BEFUNDE.md, F21.
    """

    name = 'sonnenbatterie'

    def __init__(self, host: str, timeout: float = 10.0):
        self.host = host.rstrip('/')
        self.timeout = timeout
        if not self.host.startswith('http'):
            self.host = f'http://{self.host}'

    def read(self) -> Reading:
        data = _fetch_json(f'{self.host}/api/v2/status', self.timeout)

        for key in ('Production_W', 'Consumption_W', 'GridFeedIn_W',
                    'Pac_total_W'):
            if key not in data:
                raise SourceError(
                    f'Antwort der Sonnenbatterie ohne Feld {key} — '
                    f'andere Firmware?')

        # USOC bevorzugt, RSOC als Rückfall: eine ältere Firmware, die nur
        # RSOC kennt, soll weiterlaufen statt den Dienst anzuhalten. Fehlen
        # beide, ist die Antwort wirklich unbrauchbar.
        if 'USOC' in data:
            ladestand = float(data['USOC'])
        elif 'RSOC' in data:
            ladestand = float(data['RSOC'])
        else:
            raise SourceError(
                'Antwort der Sonnenbatterie ohne USOC und ohne RSOC — '
                'andere Firmware?')

        return Reading(
            ts=time.time(),
            production_w=float(data['Production_W']),
            consumption_w=float(data['Consumption_W']),
            # Gerät meldet Einspeisung positiv, Reading erwartet Bezug positiv
            grid_w=-float(data['GridFeedIn_W']),
            # Gerät meldet Laden negativ, Reading erwartet Entladen positiv
            battery_w=float(data['Pac_total_W']),
            battery_percent=ladestand,
            source=self.name,
        )


class HomeAssistantSource:
    """Zweitquelle über die REST-API von Home Assistant.

    Greift auf dieselben Sonnenbatterie-Werte zu, nur über den Umweg der
    Integration. Nützlich, wenn das Gerät selbst nicht antwortet, Home
    Assistant aber noch läuft — etwa weil die Sonnenbatterie gerade ein
    Firmware-Update macht.
    """

    name = 'homeassistant'

    # Endungen der Entitäten der Integration `sonnenbatterie`; davor steht
    # sensor.sonnenbatterie_<Seriennummer>_.
    ENTITY_SUFFIXES = {
        'production': 'state_production',
        'consumption': 'state_consumption_current',
        'grid': 'state_grid_inout',
        'battery': 'state_battery_inout',
        'percent': 'state_battery_percentage_user',
    }

    def __init__(self, url: str, token: str, timeout: float = 10.0,
                 serial: str = '', entities: dict | None = None):
        self.url = url.rstrip('/')
        self.token = token
        self.timeout = timeout
        defaults = {key: f'sensor.sonnenbatterie_{serial}_{suffix}'
                    for key, suffix in self.ENTITY_SUFFIXES.items()}
        self.entities = {**defaults, **(entities or {})}

    def _state(self, entity_id: str) -> float:
        data = _fetch_json(
            f'{self.url}/api/states/{entity_id}', self.timeout,
            {'Authorization': f'Bearer {self.token}'})
        state = data.get('state')
        if state in (None, 'unknown', 'unavailable', ''):
            raise SourceError(f'{entity_id} liefert "{state}"')
        try:
            return float(state)
        except ValueError:
            raise SourceError(
                f'{entity_id} liefert keinen Zahlenwert: {state!r}') from None

    def read(self) -> Reading:
        values = {key: self._state(entity)
                  for key, entity in self.entities.items()}

        # grid_inout des Sensors: negativ = Import. Reading will Import positiv.
        # Gegenprobe am 06.08.2026: state_grid_in 468 W bei state_grid_inout -468.
        return Reading(
            ts=time.time(),
            production_w=values['production'],
            consumption_w=values['consumption'],
            grid_w=-values['grid'],
            battery_w=values['battery'],
            battery_percent=values['percent'],
            source=self.name,
        )


class RedundantSource:
    """Fragt mehrere Quellen der Reihe nach, bis eine antwortet.

    Die erste Quelle ist die bevorzugte. Fällt sie aus, übernimmt die
    nächste, und der Wechsel wird protokolliert — aber nur beim Wechsel,
    nicht bei jedem Abruf, sonst läuft das Log voll.
    """

    name = 'redundant'

    def __init__(self, sources: list):
        if not sources:
            raise ValueError('Mindestens eine Quelle wird gebraucht')
        self.sources = sources
        self.active_index = 0
        self.failures: dict[str, int] = {}

    def read(self) -> Reading:
        errors = []

        for offset in range(len(self.sources)):
            index = (self.active_index + offset) % len(self.sources)
            source = self.sources[index]
            try:
                reading = source.read()
            except SourceError as exc:
                self.failures[source.name] = self.failures.get(source.name, 0) + 1
                errors.append(f'{source.name}: {exc}')
                continue

            if index != self.active_index:
                previous = self.sources[self.active_index].name
                # Bei gleichnamigen Quellen — etwa zwei Sonnenbatterien unter
                # verschiedenen Adressen — sagt der Name allein nichts aus.
                if previous == source.name:
                    LOG.warning('Quelle %s gewechselt: Eintrag %d statt %d',
                                source.name, index + 1, self.active_index + 1)
                else:
                    LOG.warning('Quelle gewechselt: %s statt %s',
                                source.name, previous)
                self.active_index = index
            return reading

        raise SourceError('Keine Quelle antwortet — ' + '; '.join(errors))

    def statistics(self) -> dict:
        return {
            'active': self.sources[self.active_index].name,
            'available': [s.name for s in self.sources],
            'failures': dict(self.failures),
        }


def build(config) -> RedundantSource | None:
    """Quellen aus der Konfiguration zusammenstellen.

    Reihenfolge ist die Vorzugsreihenfolge: das Gerät selbst vor dem Umweg
    über Home Assistant.
    """
    sources = []

    if config.sonnenbatterie.enabled:
        sources.append(SonnenbatterieSource(
            config.sonnenbatterie.host, config.sonnenbatterie.timeout))

    if config.homeassistant.enabled and config.homeassistant.token:
        if config.sonnenbatterie.serial:
            sources.append(HomeAssistantSource(
                config.homeassistant.url, config.homeassistant.token,
                config.homeassistant.timeout, config.sonnenbatterie.serial))
        else:
            LOG.warning('Home Assistant als Zweitquelle braucht '
                        '[sonnenbatterie] serial, sonst sind die '
                        'Entitätsnamen unbekannt. Zweitquelle bleibt aus.')

    if not sources:
        return None
    return RedundantSource(sources)
