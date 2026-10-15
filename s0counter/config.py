"""Konfiguration laden und prüfen.

Ein Tippfehler in der GPIO-Nummer oder eine doppelt vergebene Leitung fällt
sonst erst auf, wenn Wochen später die Zählerstände nicht zusammenpassen.
Deshalb wird beim Start hart geprüft.
"""
import logging
import tomllib
from dataclasses import dataclass, field

LOG = logging.getLogger(__name__)

# BCM-Nummern, die auf dem Raspberry Pi 3B frei als GPIO nutzbar sind.
# 0 und 1 sind für die HAT-Erkennung reserviert, 2 und 3 haben feste
# Pull-ups für I2C und eignen sich schlecht für S0-Eingänge.
USABLE_GPIO = frozenset(range(4, 28))
I2C_GPIO = frozenset({2, 3})
RESERVED_GPIO = frozenset({0, 1})


class ConfigError(Exception):
    """Die Konfiguration ist unbrauchbar und würde falsche Werte liefern."""


# Kürzester Impulsabstand, den EN 62053-31 zulässt: mindestens 30 ms Impuls
# und mindestens 30 ms Pause. Kein normkonformer Zähler sendet schneller,
# alles darunter ist Prellen oder Einstreuung.
_S0_MIN_PERIODE_S = 0.060


@dataclass
class MeterConfig:
    meter_id: str
    name: str
    gpio: int
    pulses_per_kwh: int = 1000
    active_low: bool = True
    bounce_ms: float = 10.0
    max_plausible_power_w: float = 60_000.0
    min_power_w: float = 20.0

    @property
    def wh_per_pulse(self) -> float:
        return 1000.0 / self.pulses_per_kwh

    @property
    def power_timeout_seconds(self) -> float:
        """Zeit ohne Impuls, nach der die Leistung auf 0 gesetzt wird.

        Abgeleitet aus `min_power_w` statt fest eingetragen: Bei einer Last
        genau an dieser Grenze ist das gerade der Impulsabstand. Wer den
        Wert direkt einträgt, müsste ihn bei jeder anderen Impulsrate neu
        ausrechnen — bei 100 Imp/kWh entsprechen dieselben 180 s nicht mehr
        20 W, sondern 200 W, und eine Wohnung mit Grundlast stünde
        dauerhaft auf 0 W.
        """
        return (self.wh_per_pulse * 3600.0) / self.min_power_w


@dataclass
class MqttConfig:
    enabled: bool = False
    host: str = '127.0.0.1'
    port: int = 1883
    username: str = ''
    password: str = ''
    base_topic: str = 'haus/energie'
    homeassistant_discovery: bool = True


@dataclass
class HomeAssistantConfig:
    enabled: bool = False
    url: str = ''
    token: str = ''
    prefix: str = 's0'
    timeout: float = 10.0


@dataclass
class SonnenbatterieConfig:
    enabled: bool = False
    host: str = ''
    timeout: float = 10.0
    # Seriennummer des Geräts; Home Assistant bildet daraus die Entitätsnamen
    # (sensor.sonnenbatterie_<serial>_…). Nur für die Zweitquelle nötig.
    serial: str = ''


@dataclass
class BalanceConfig:
    """Aufzeichnung der Hausbilanz für die Zuordnung des Netzbezugs."""
    enabled: bool = False
    database: str = '/var/lib/s0counter/balance.sqlite3'
    sample_interval_seconds: float = 60.0
    allocation_interval_seconds: float = 900.0


@dataclass
class HistoryConfig:
    """Zeitreihe der Rechnerwerte für die Verlaufsdiagramme.

    Alte Werte werden verdichtet (siehe history.py), deshalb wächst die
    Datei nicht linear: rund 20 MB im Jahr statt 56 MB.
    """
    enabled: bool = True
    database: str = '/var/lib/s0counter/history.sqlite3'
    sample_interval_seconds: float = 60.0


@dataclass
class TelegramConfig:
    enabled: bool = False
    token: str = ''
    chat_id: str = ''
    timeout: float = 30.0


@dataclass
class PanelConfig:
    """Lokales Anzeigepanel am EA eDIP320."""
    enabled: bool = False
    port: str = '/dev/ttyUSB0'
    baud: int = 57600
    button_gpio: int = 23
    blank_after_seconds: float = 120.0
    refresh_seconds: float = 5.0


@dataclass
class ReportConfig:
    enabled: bool = False
    directory: str = '/var/lib/s0counter/berichte'
    price_ct_per_kwh: float = 0.0
    base_price_eur_per_year: float = 0.0
    landlord: str = ''


@dataclass
class Config:
    database: str
    http_host: str = '0.0.0.0'
    http_port: int = 8080
    publish_interval_seconds: float = 30.0
    power_timeout_seconds: float = 180.0
    meters: dict = field(default_factory=dict)
    mqtt: MqttConfig = field(default_factory=MqttConfig)
    homeassistant: HomeAssistantConfig = field(default_factory=HomeAssistantConfig)
    sonnenbatterie: SonnenbatterieConfig = field(default_factory=SonnenbatterieConfig)
    balance: BalanceConfig = field(default_factory=BalanceConfig)
    history: HistoryConfig = field(default_factory=HistoryConfig)
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    report: ReportConfig = field(default_factory=ReportConfig)
    panel: PanelConfig = field(default_factory=PanelConfig)


def _check_gpio(gpio: int, meter_id: str) -> None:
    if gpio in RESERVED_GPIO:
        raise ConfigError(
            f'Zähler {meter_id}: GPIO{gpio} ist für die HAT-Erkennung '
            f'reserviert und darf nicht belegt werden')
    if gpio in I2C_GPIO:
        raise ConfigError(
            f'Zähler {meter_id}: GPIO{gpio} hat einen festen Pull-up für I2C '
            f'und ist als S0-Eingang ungeeignet')
    if gpio not in USABLE_GPIO:
        raise ConfigError(
            f'Zähler {meter_id}: GPIO{gpio} ist keine nutzbare BCM-Nummer '
            f'(erlaubt: 4 bis 27)')


def load(path: str) -> Config:
    """Konfigurationsdatei lesen und vollständig prüfen."""
    try:
        with open(path, 'rb') as handle:
            raw = tomllib.load(handle)
    except FileNotFoundError:
        raise ConfigError(f'Konfiguration nicht gefunden: {path}') from None
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f'{path} ist kein gültiges TOML: {exc}') from exc

    return from_dict(raw)


def from_dict(raw: dict) -> Config:
    general = raw.get('general', {})
    if 'database' not in general:
        raise ConfigError('[general] database fehlt')

    meters_raw = raw.get('meters', {})
    if not meters_raw:
        raise ConfigError(
            'Kein Zähler konfiguriert. Mindestens ein [meters.<name>] '
            'Abschnitt wird gebraucht.')

    meters: dict[str, MeterConfig] = {}
    gpio_usage: dict[int, str] = {}

    for meter_id, entry in meters_raw.items():
        if 'gpio' not in entry:
            raise ConfigError(f'Zähler {meter_id}: gpio fehlt')

        try:
            gpio = int(entry['gpio'])
        except (TypeError, ValueError):
            raise ConfigError(
                f'Zähler {meter_id}: gpio muss eine Zahl sein, '
                f'nicht {entry["gpio"]!r}') from None

        _check_gpio(gpio, meter_id)

        if gpio in gpio_usage:
            raise ConfigError(
                f'GPIO{gpio} ist doppelt vergeben: {gpio_usage[gpio]} '
                f'und {meter_id}. Beide Zähler würden dieselben Impulse '
                f'zählen.')
        gpio_usage[gpio] = meter_id

        ppk = int(entry.get('pulses_per_kwh', 1000))
        if ppk <= 0:
            raise ConfigError(
                f'Zähler {meter_id}: pulses_per_kwh muss positiv sein')

        bounce = float(entry.get('bounce_ms', 10.0))
        if bounce < 0:
            raise ConfigError(
                f'Zähler {meter_id}: bounce_ms darf nicht negativ sein')

        max_power = float(entry.get('max_plausible_power_w', 60_000.0))
        if max_power <= 0:
            raise ConfigError(
                f'Zähler {meter_id}: max_plausible_power_w muss positiv sein')

        min_power = float(entry.get('min_power_w', 20.0))
        if min_power <= 0:
            raise ConfigError(
                f'Zähler {meter_id}: min_power_w muss positiv sein')

        meter = MeterConfig(
            meter_id=meter_id,
            name=entry.get('name', meter_id.upper()),
            gpio=gpio,
            pulses_per_kwh=ppk,
            active_low=bool(entry.get('active_low', True)),
            bounce_ms=bounce,
            max_plausible_power_w=max_power,
            min_power_w=min_power,
        )

        # Die Entprellung darf die Plausibilitätsgrenze nicht überholen.
        # Beide Stufen sind bewusst getrennt (siehe docs/BEFUNDE.md): die
        # Entprellung fängt Kontaktprellen ab, die Grenze darüber unplausibel
        # hohe Leistung. Liegt bounce_ms schon über der Grenze, verwirft die
        # erste Stufe alles, was die zweite prüfen sollte — die
        # Plausibilitätsprüfung wäre toter Code, ohne dass es auffällt.
        grenze_s = (meter.wh_per_pulse * 3600.0) / max_power
        if bounce / 1000.0 >= grenze_s:
            raise ConfigError(
                f'Zähler {meter_id}: bounce_ms = {bounce:.0f} ms erreicht '
                f'die Plausibilitätsgrenze von {grenze_s * 1000:.1f} ms '
                f'({max_power:.0f} W bei {ppk} Imp/kWh). Die Entprellung '
                f'würde die Plausibilitätsprüfung vollständig verdecken. '
                f'Entweder bounce_ms senken oder max_plausible_power_w '
                f'senken.')

        # Ein Zähler nach EN 62053-31 sendet nie schneller als 30 ms Impuls
        # plus 30 ms Pause. Eine Grenze oberhalb dieser Leistung kann per
        # Bauart nie greifen — das ist kein Fehler, aber der eingetragene
        # Wert tut dann nichts und wiegt in falscher Sicherheit.
        norm_grenzleistung = (meter.wh_per_pulse * 3600.0) / _S0_MIN_PERIODE_S
        if max_power > norm_grenzleistung:
            LOG.warning(
                '%s: max_plausible_power_w = %.0f W liegt über den %.0f W, '
                'die ein Zähler mit %d Imp/kWh nach EN 62053-31 überhaupt '
                'signalisieren kann. Die Plausibilitätsprüfung greift damit '
                'nie.', meter_id, max_power, norm_grenzleistung, ppk)

        if meter.power_timeout_seconds <= bounce / 1000.0:
            raise ConfigError(
                f'Zähler {meter_id}: min_power_w = {min_power:.0f} W ergibt '
                f'einen Timeout von {meter.power_timeout_seconds * 1000:.0f} ms, '
                f'der unter der Entprellung von {bounce:.0f} ms liegt. Die '
                f'Leistung fiele auf 0, bevor der nächste Impuls überhaupt '
                f'angenommen werden darf.')

        meters[meter_id] = meter

    mqtt_raw = raw.get('mqtt', {})
    mqtt = MqttConfig(
        enabled=bool(mqtt_raw.get('enabled', False)),
        host=mqtt_raw.get('host', '127.0.0.1'),
        port=int(mqtt_raw.get('port', 1883)),
        username=mqtt_raw.get('username', ''),
        password=mqtt_raw.get('password', ''),
        base_topic=mqtt_raw.get('base_topic', 'haus/energie'),
        homeassistant_discovery=bool(
            mqtt_raw.get('homeassistant_discovery', True)),
    )

    ha_raw = raw.get('homeassistant', {})
    homeassistant = HomeAssistantConfig(
        enabled=bool(ha_raw.get('enabled', False)),
        url=ha_raw.get('url', ''),
        token=ha_raw.get('token', ''),
        prefix=ha_raw.get('prefix', 's0'),
        timeout=float(ha_raw.get('timeout', 10.0)),
    )

    if homeassistant.enabled:
        if not homeassistant.url:
            raise ConfigError('[homeassistant] url fehlt')
        if not homeassistant.token:
            raise ConfigError('[homeassistant] token fehlt')

    sb_raw = raw.get('sonnenbatterie', {})
    sonnenbatterie = SonnenbatterieConfig(
        enabled=bool(sb_raw.get('enabled', False)),
        host=sb_raw.get('host', ''),
        timeout=float(sb_raw.get('timeout', 10.0)),
        serial=str(sb_raw.get('serial', '')).strip(),
    )
    if sonnenbatterie.enabled and not sonnenbatterie.host:
        raise ConfigError('[sonnenbatterie] host fehlt')

    balance_raw = raw.get('balance', {})
    balance = BalanceConfig(
        enabled=bool(balance_raw.get('enabled', False)),
        database=balance_raw.get(
            'database', '/var/lib/s0counter/balance.sqlite3'),
        sample_interval_seconds=float(
            balance_raw.get('sample_interval_seconds', 60.0)),
        allocation_interval_seconds=float(
            balance_raw.get('allocation_interval_seconds', 900.0)),
    )

    history_raw = raw.get('history', {})
    history = HistoryConfig(
        enabled=bool(history_raw.get('enabled', True)),
        database=history_raw.get(
            'database', '/var/lib/s0counter/history.sqlite3'),
        sample_interval_seconds=float(
            history_raw.get('sample_interval_seconds', 60.0)),
    )

    if balance.enabled:
        if not (sonnenbatterie.enabled or homeassistant.enabled):
            raise ConfigError(
                'Die Hausbilanz braucht mindestens eine Quelle. '
                '[sonnenbatterie] oder [homeassistant] aktivieren.')
        if balance.sample_interval_seconds <= 0:
            raise ConfigError(
                '[balance] sample_interval_seconds muss positiv sein')
        if balance.allocation_interval_seconds < balance.sample_interval_seconds:
            raise ConfigError(
                f'[balance] allocation_interval_seconds '
                f'({balance.allocation_interval_seconds:.0f} s) darf nicht '
                f'kleiner sein als sample_interval_seconds '
                f'({balance.sample_interval_seconds:.0f} s) — sonst bleiben '
                f'Abrechnungsintervalle ohne Messpunkt.')

    if history.enabled:
        if history.sample_interval_seconds <= 0:
            raise ConfigError(
                '[history] sample_interval_seconds muss positiv sein')
        # Unterhalb von 10 s wächst die Reihe schneller, als die Verdichtung
        # sie einholt: 8.640 Zeilen je Tag und Feld sind auf einem Pi 3B
        # schon spürbar, darunter läuft die Karte in Monaten voll.
        if history.sample_interval_seconds < 10.0:
            raise ConfigError(
                f'[history] sample_interval_seconds '
                f'({history.sample_interval_seconds:.0f} s) ist zu klein. '
                f'Mindestens 10 s — darunter wächst der Verlauf schneller, '
                f'als die Verdichtung ihn zusammenfassen kann.')
        if history.database == general.get('database'):
            raise ConfigError(
                '[history] database darf nicht dieselbe Datei sein wie die '
                'Impulsdatenbank — die Rohimpulse bleiben unangetastet.')

    tg_raw = raw.get('telegram', {})
    telegram = TelegramConfig(
        enabled=bool(tg_raw.get('enabled', False)),
        token=tg_raw.get('token', ''),
        chat_id=str(tg_raw.get('chat_id', '')),
        timeout=float(tg_raw.get('timeout', 30.0)),
    )
    if telegram.enabled:
        if not telegram.token:
            raise ConfigError('[telegram] token fehlt')
        if not telegram.chat_id:
            raise ConfigError('[telegram] chat_id fehlt')

    panel_raw = raw.get('panel', {})
    panel = PanelConfig(
        enabled=bool(panel_raw.get('enabled', False)),
        port=panel_raw.get('port', '/dev/ttyUSB0'),
        baud=int(panel_raw.get('baud', 57600)),
        button_gpio=int(panel_raw.get('button_gpio', 23)),
        blank_after_seconds=float(panel_raw.get('blank_after_seconds', 120.0)),
        refresh_seconds=float(panel_raw.get('refresh_seconds', 5.0)),
    )

    if panel.enabled:
        valid_baud = (1200, 2400, 4800, 9600, 19200, 38400, 57600, 115200)
        if panel.baud not in valid_baud:
            raise ConfigError(
                f'[panel] baud {panel.baud} ist keine der am Display per '
                f'Lötbrücke einstellbaren Raten: '
                f'{", ".join(str(b) for b in valid_baud)}')

        if panel.button_gpio >= 0:
            _check_gpio(panel.button_gpio, 'panel')
            if panel.button_gpio in gpio_usage:
                raise ConfigError(
                    f'[panel] button_gpio GPIO{panel.button_gpio} ist bereits '
                    f'von Zähler {gpio_usage[panel.button_gpio]} belegt. '
                    f'Jeder Tastendruck würde als Stromimpuls gezählt.')

        if panel.refresh_seconds <= 0:
            raise ConfigError('[panel] refresh_seconds muss positiv sein')

    report_raw = raw.get('report', {})
    report = ReportConfig(
        enabled=bool(report_raw.get('enabled', False)),
        directory=report_raw.get(
            'directory', '/var/lib/s0counter/berichte'),
        price_ct_per_kwh=float(report_raw.get('price_ct_per_kwh', 0.0)),
        base_price_eur_per_year=float(
            report_raw.get('base_price_eur_per_year', 0.0)),
        landlord=report_raw.get('landlord', ''),
    )
    if report.enabled and not balance.enabled:
        raise ConfigError(
            'Der Jahresbericht braucht die Hausbilanz. [balance] aktivieren.')
    if report.price_ct_per_kwh < 0:
        raise ConfigError('[report] price_ct_per_kwh darf nicht negativ sein')

    return Config(
        database=general['database'],
        http_host=general.get('http_host', '0.0.0.0'),
        http_port=int(general.get('http_port', 8080)),
        publish_interval_seconds=float(
            general.get('publish_interval_seconds', 30.0)),
        power_timeout_seconds=float(general.get('power_timeout_seconds', 180.0)),
        meters=meters,
        mqtt=mqtt,
        homeassistant=homeassistant,
        sonnenbatterie=sonnenbatterie,
        balance=balance,
        history=history,
        telegram=telegram,
        report=report,
        panel=panel,
    )
