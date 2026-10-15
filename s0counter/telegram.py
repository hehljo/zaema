"""Berichte und Abfragen über Telegram.

Zwei Richtungen:

  senden    Der Jahresbericht wird als PDF geschickt, ausgelöst durch einen
            systemd-Timer am 31.12.
  abfragen  Der aktuelle Stand auf Zuruf, über `s0ctl telegram-poll` oder
            einen Timer.

Kein Fremdpaket — die Bot-API ist reines HTTPS mit JSON, und der Upload
einer Datei ist ein multipart/form-data-Body, den man von Hand bauen kann.
Auf einem Pi, der jahrelang läuft, ist jede nicht installierte Abhängigkeit
eine Fehlerquelle weniger.
"""
import json
import logging
import os
import urllib.error
import urllib.request
import uuid

from . import ordnung

LOG = logging.getLogger(__name__)

API = 'https://api.telegram.org/bot'


class TelegramError(Exception):
    """Der Versand ist fehlgeschlagen."""


class TelegramBot:
    def __init__(self, token: str, chat_id: str, timeout: float = 30.0):
        self.token = token
        self.chat_id = str(chat_id)
        self.timeout = timeout

    # --- Transport -------------------------------------------------------

    def _call(self, method: str, payload: dict) -> dict:
        url = f'{API}{self.token}/{method}'
        data = json.dumps(payload).encode('utf-8')
        request = urllib.request.Request(
            url, data=data, method='POST',
            headers={'Content-Type': 'application/json'})
        return self._send(request, method)

    def _send(self, request, method: str) -> dict:
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as resp:
                answer = json.loads(resp.read().decode('utf-8'))
        except urllib.error.HTTPError as exc:
            detail = ''
            try:
                detail = json.loads(exc.read().decode()).get('description', '')
            except Exception:
                pass
            if exc.code == 401:
                raise TelegramError(
                    'Token abgelehnt. Bot-Token bei @BotFather prüfen.') from exc
            if exc.code == 400 and 'chat not found' in detail.lower():
                raise TelegramError(
                    f'Chat {self.chat_id} nicht gefunden. Wurde der Bot im '
                    f'Chat schon einmal angeschrieben?') from exc
            raise TelegramError(
                f'{method}: HTTP {exc.code} {detail or exc.reason}') from exc
        except urllib.error.URLError as exc:
            raise TelegramError(
                f'Telegram nicht erreichbar: {exc.reason}') from exc

        if not answer.get('ok'):
            raise TelegramError(
                f'{method}: {answer.get("description", "unbekannter Fehler")}')
        return answer.get('result', {})

    # --- Senden ----------------------------------------------------------

    def check(self) -> dict:
        """Bot-Zugang prüfen, gibt die Bot-Daten zurück."""
        return self._call('getMe', {})

    def send_message(self, text: str, markdown: bool = False) -> dict:
        payload = {'chat_id': self.chat_id, 'text': text}
        if markdown:
            payload['parse_mode'] = 'MarkdownV2'
        return self._call('sendMessage', payload)

    def send_document(self, path: str, caption: str = '') -> dict:
        """Eine Datei hochladen, etwa den Jahresbericht."""
        if not os.path.exists(path):
            raise TelegramError(f'Datei nicht gefunden: {path}')

        boundary = uuid.uuid4().hex
        filename = os.path.basename(path)
        with open(path, 'rb') as handle:
            content = handle.read()

        body = bytearray()

        def field(name: str, value: str) -> None:
            body.extend(f'--{boundary}\r\n'.encode())
            body.extend(
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
                .encode())
            body.extend(value.encode('utf-8'))
            body.extend(b'\r\n')

        field('chat_id', self.chat_id)
        if caption:
            field('caption', caption)

        body.extend(f'--{boundary}\r\n'.encode())
        body.extend(
            f'Content-Disposition: form-data; name="document"; '
            f'filename="{filename}"\r\n'.encode())
        body.extend(b'Content-Type: application/pdf\r\n\r\n')
        body.extend(content)
        body.extend(f'\r\n--{boundary}--\r\n'.encode())

        request = urllib.request.Request(
            f'{API}{self.token}/sendDocument',
            data=bytes(body), method='POST',
            headers={
                'Content-Type': f'multipart/form-data; boundary={boundary}',
                'Content-Length': str(len(body)),
            })
        return self._send(request, 'sendDocument')

    # --- Empfangen -------------------------------------------------------

    def get_updates(self, offset: int | None = None) -> list:
        payload = {'timeout': 0}
        if offset is not None:
            payload['offset'] = offset
        return self._call('getUpdates', payload)


def format_status(states: dict, balance_row=None,
                  allocation: dict | None = None) -> str:
    """Kurzer Statustext für die Abfrage zwischendurch."""
    lines = ['Stromzähler', '']

    for meter_id in ordnung.sortiert(states):
        state = states[meter_id]
        lines.append(f'{state["name"]}')
        lines.append(f'  {state["energy_kwh"]:.3f} kWh gesamt')
        if state['power_w'] > 0:
            lines.append(f'  {state["power_w"]:.0f} W gerade')

    if balance_row is not None:
        _, production, consumption, grid, battery, percent, _ = balance_row
        lines.append('')
        lines.append('Haus')
        lines.append(f'  PV {production:.0f} W')
        lines.append(f'  Verbrauch {consumption:.0f} W')
        if grid > 0:
            lines.append(f'  Netzbezug {grid:.0f} W')
        else:
            lines.append(f'  Einspeisung {-grid:.0f} W')
        lines.append(f'  Akku {percent:.0f} %')

    if allocation:
        lines.append('')
        lines.append(f'Netzbezug seit Jahresbeginn')
        for meter_id in ordnung.sortiert(allocation['meters']):
            values = allocation['meters'][meter_id]
            name = states.get(meter_id, {}).get('name', meter_id.upper())
            lines.append(f'  {name}: {values["grid_kwh"]:.1f} kWh '
                         f'von {values["consumption_kwh"]:.1f} kWh')

    return '\n'.join(lines)
