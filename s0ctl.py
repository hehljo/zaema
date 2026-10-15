#!/usr/bin/env python3
"""Verwaltung des S0-Zählers.

    s0ctl status
    s0ctl set-reading eg 1234.567
    s0ctl retire wp
    s0ctl export /tmp/export.csv
    s0ctl backup /mnt/nas/s0counter.sqlite3
    s0ctl audit
    s0ctl ha-test
    s0ctl ha-remove
    s0ctl balance
    s0ctl allocate --year 2026
    s0ctl report --year 2026 --send
    s0ctl telegram-test
    s0ctl doctor
    s0ctl nas-setup
"""
import argparse
import csv
import getpass
import os
import sys
import time

from s0counter import config as config_module
from s0counter.db import Database


def cmd_status(args, config, db) -> int:
    states = db.all_states()
    if not states:
        print('Kein Zähler in der Datenbank.')
        return 1

    print(f'{"ID":5} {"Name":20} {"Stand":>14} {"Impulse":>12} {"Leistung":>10}')
    print('-' * 66)
    for meter_id, state in states.items():
        age = ''
        if state['last_pulse']:
            seconds = time.time() - state['last_pulse']
            age = (f'  letzter Impuls vor {seconds:.0f} s'
                   if seconds < 3600
                   else f'  letzter Impuls vor {seconds / 3600:.1f} h')
        else:
            age = '  noch kein Impuls'

        print(f'{meter_id:5} {state["name"]:20} '
              f'{state["energy_kwh"]:11.3f} kWh '
              f'{state["pulses"]:12} '
              f'{state["power_w"]:8.1f} W{age}')
    return 0


def cmd_set_reading(args, config, db) -> int:
    """Softwarestand auf den abgelesenen Zählerstand setzen."""
    try:
        before = db.state(args.meter)
    except KeyError:
        print(f'Unbekannter Zähler: {args.meter}', file=sys.stderr)
        print(f'Bekannt sind: {", ".join(db.meter_ids())}', file=sys.stderr)
        return 2

    if args.kwh < 0:
        print('Ein Zählerstand kann nicht negativ sein.', file=sys.stderr)
        return 2

    difference = args.kwh - before['energy_kwh']
    if abs(difference) > 100 and not args.force:
        print(f'Der neue Stand weicht um {difference:+.3f} kWh vom bisherigen '
              f'({before["energy_kwh"]:.3f} kWh) ab.', file=sys.stderr)
        print('Bei einer Ersteinrichtung ist das normal — dann mit --force '
              'bestätigen.', file=sys.stderr)
        return 3

    offset = db.set_reading(args.meter, args.kwh)
    after = db.state(args.meter)
    print(f'{args.meter}: {before["energy_kwh"]:.3f} -> '
          f'{after["energy_kwh"]:.3f} kWh')
    print(f'  Offset {offset:+.3f} kWh, {after["pulses"]} Rohimpulse '
          f'unverändert erhalten')
    return 0


def cmd_export(args, config, db) -> int:
    since = None
    if args.days:
        since = time.time() - args.days * 86400

    written = 0
    with open(args.file, 'w', newline='', encoding='utf-8') as handle:
        writer = csv.writer(handle, delimiter=';')
        writer.writerow(['id', 'meter', 'timestamp_unix', 'timestamp_local'])
        for row_id, meter_id, ts in db.iter_pulses(args.meter, since):
            writer.writerow([
                row_id, meter_id, f'{ts:.6f}',
                time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(ts)),
            ])
            written += 1

    print(f'{written} Impulse nach {args.file} geschrieben')
    return 0


def cmd_backup(args, config, db) -> int:
    db.backup_to(args.file)
    print(f'Sicherung nach {args.file}')
    return 0


def cmd_audit(args, config, db) -> int:
    entries = db.audit_log(args.limit)
    if not entries:
        print('Keine Einträge im Audit-Log.')
        return 0
    for entry in entries:
        stamp = time.strftime('%Y-%m-%d %H:%M:%S',
                              time.localtime(entry['ts']))
        print(f'{stamp}  {entry["action"]:16} '
              f'{entry["meter_id"] or "-":5} {entry["details"]}')
    return 0


def cmd_ha_test(args, config, db) -> int:
    """Home-Assistant-Anbindung prüfen und einmal veröffentlichen."""
    from s0counter.homeassistant import HomeAssistantClient, HomeAssistantError

    settings = config.homeassistant
    if not settings.enabled:
        print('Home Assistant ist in der Konfiguration nicht aktiviert.',
              file=sys.stderr)
        return 2

    try:
        client = HomeAssistantClient(settings.url, settings.token,
                                     settings.prefix, settings.timeout)
    except HomeAssistantError as exc:
        print(f'Fehlgeschlagen: {exc}', file=sys.stderr)
        return 3

    states = db.all_states()
    if client.publish_all(states):
        print(f'{len(states) * 2} Sensoren aktualisiert:')
        for meter_id in states:
            print(f'  {client.entity_id(meter_id, "energy")}')
            print(f'  {client.entity_id(meter_id, "power")}')
        return 0

    print('Veröffentlichung fehlgeschlagen, siehe Log.', file=sys.stderr)
    return 3


def cmd_ha_remove(args, config, db) -> int:
    from s0counter.homeassistant import HomeAssistantClient, HomeAssistantError

    settings = config.homeassistant
    if not settings.url or not settings.token:
        print('url oder token fehlen in der Konfiguration.', file=sys.stderr)
        return 2

    try:
        client = HomeAssistantClient(settings.url, settings.token,
                                     settings.prefix, settings.timeout)
    except HomeAssistantError as exc:
        print(f'Fehlgeschlagen: {exc}', file=sys.stderr)
        return 3

    removed = client.remove_entities(db.meter_ids(include_retired=True))
    print(f'{removed} Sensoren entfernt')
    return 0


def cmd_retire(args, config, db) -> int:
    """Zähler stilllegen: aus den laufenden Anzeigen nehmen, nichts löschen."""
    meter = args.meter
    if meter in config.meters:
        print(f'{meter} steht noch in der Konfiguration. Erst den Block '
              f'[meters.{meter}] entfernen und den Dienst neu starten '
              f'(sudo systemctl restart s0counter), dann stilllegen.',
              file=sys.stderr)
        return 2
    if meter not in db.meter_ids(include_retired=True):
        print(f'Unbekannter Zähler: {meter}', file=sys.stderr)
        return 2
    if meter in db.retired_meter_ids():
        print(f'{meter} ist bereits stillgelegt.')
        return 0

    state = db.state(meter)
    db.retire(meter)
    print(f'{meter} stillgelegt. Letzter Stand {state["energy_kwh"]:.3f} kWh, '
          f'{state["pulses"]} Impulse bleiben gespeichert.')

    settings = config.homeassistant
    if settings.enabled and settings.url and settings.token:
        from s0counter.homeassistant import HomeAssistantClient, HomeAssistantError
        try:
            client = HomeAssistantClient(settings.url, settings.token,
                                         settings.prefix, settings.timeout)
            removed = client.remove_entities([meter])
            print(f'{removed} Sensoren aus Home Assistant entfernt.')
        except HomeAssistantError as exc:
            print(f'Home Assistant nicht erreicht ({exc}); die Sensoren von '
                  f'{meter} bleiben dort. Später mit sudo s0ctl ha-remove '
                  f'nachholen, die aktiven Zähler legt der Dienst danach '
                  f'von selbst wieder an.', file=sys.stderr)
    return 0


def _open_balance(config):
    from s0counter.balance import BalanceStore
    if not config.balance.enabled:
        print('Die Hausbilanz ist nicht aktiviert. [balance] enabled = true '
              'in der Konfiguration setzen.', file=sys.stderr)
        return None
    return BalanceStore(config.balance.database)


def cmd_balance(args, config, db) -> int:
    """Aktuellen Stand der Hausbilanz zeigen."""
    store = _open_balance(config)
    if store is None:
        return 2
    try:
        row = store.latest()
        if row is None:
            print('Noch keine Messpunkte aufgezeichnet.')
            return 1

        ts, production, consumption, grid, battery, percent, source = row
        alter = time.time() - ts
        print(f'Stand von {time.strftime("%d.%m.%Y %H:%M:%S", time.localtime(ts))}'
              f'  ({alter:.0f} s alt, Quelle {source})')
        print()
        print(f'  PV-Erzeugung      {production:9.0f} W')
        print(f'  Hausverbrauch     {consumption:9.0f} W')
        if grid > 0:
            print(f'  Netzbezug         {grid:9.0f} W')
        else:
            print(f'  Netzeinspeisung   {-grid:9.0f} W')
        if battery > 0:
            print(f'  Akku entlädt      {battery:9.0f} W')
        elif battery < 0:
            print(f'  Akku lädt         {-battery:9.0f} W')
        else:
            print(f'  Akku              {0:9.0f} W')
        print(f'  Ladestand         {percent:9.0f} %')
        print()
        print(f'  {store.count()} Messpunkte insgesamt')
        return 0
    finally:
        store.close()


def _print_allocation(result, db) -> None:
    print(f'Verteilmodell: {result["model_label"]}')
    print()
    print(f'{"Wohnung":20} {"Verbrauch":>12} {"davon Netz":>12} '
          f'{"davon PV":>12} {"aus Netz":>9}')
    print('-' * 70)

    for meter_id in sorted(result['meters']):
        values = result['meters'][meter_id]
        name = db.state(meter_id)['name']
        # Anteil des eigenen Verbrauchs, der aus dem Netz kam — das ist
        # die Zahl, an der sich die Modelle unterscheiden.
        quote = (values['grid_kwh'] / values['consumption_kwh'] * 100
                 if values['consumption_kwh'] > 0 else 0.0)
        print(f'{name:20} {values["consumption_kwh"]:9.3f} kWh '
              f'{values["grid_kwh"]:9.3f} kWh {values["solar_kwh"]:9.3f} kWh '
              f'{quote:7.1f} %')

    house = result['house']
    print('-' * 70)
    print(f'{"Haus Netzbezug":20} {house["grid_kwh"]:9.3f} kWh')
    print(f'{"nicht zugeordnet":20} {house["unallocated_grid_kwh"]:9.3f} kWh'
          f'   (Verbraucher hinter keinem Zähler)')


def cmd_allocate(args, config, db) -> int:
    """Verteilung des Netzbezugs für einen Zeitraum rechnen."""
    from s0counter.balance import MODELS, Allocator
    from s0counter.report import year_bounds

    store = _open_balance(config)
    if store is None:
        return 2

    try:
        year = args.year or time.localtime().tm_year
        start, end = year_bounds(year)
        end = min(end, time.time())

        modelle = list(MODELS) if args.compare else [args.model]
        ergebnisse = {}

        print(f'Netzbezug {year}, Stand '
              f'{time.strftime("%d.%m.%Y", time.localtime(end))}')
        print()

        for modell in modelle:
            result = Allocator(store, db,
                               config.balance.allocation_interval_seconds,
                               modell).allocate(start, end)
            ergebnisse[modell] = result
            _print_allocation(result, db)
            print()

        if args.compare and len(ergebnisse) > 1:
            print('Unterschied je Wohnung')
            print('-' * 70)
            anteilig = ergebnisse['proportional']['meters']
            wasserfall = ergebnisse['waterfall']['meters']
            for meter_id in sorted(anteilig):
                name = db.state(meter_id)['name']
                diff = wasserfall[meter_id]['grid_kwh'] - \
                    anteilig[meter_id]['grid_kwh']
                richtung = 'mehr' if diff > 0 else 'weniger'
                print(f'{name:20} {abs(diff):9.3f} kWh {richtung} '
                      f'im Wasserfallmodell')
            print()

        quality = next(iter(ergebnisse.values()))['quality']
        print(f'Abdeckung: {quality["coverage"] * 100:.1f} % '
              f'({quality["intervals_used"]} von '
              f'{quality["intervals_expected"]} Zeitfenstern)')
        if quality['gaps']:
            print(f'Ausfälle der Datenquelle: {len(quality["gaps"])}')
        if quality['coverage'] < 0.95:
            print()
            print('Achtung: Die Datengrundlage ist lückenhaft, der '
                  'zugeordnete Netzbezug entsprechend unvollständig.')
        return 0
    finally:
        store.close()


def cmd_report(args, config, db) -> int:
    """Jahresbericht als PDF erzeugen und optional verschicken."""
    from s0counter.balance import MODEL_LABELS, MODELS
    from s0counter.report import build_year_report

    store = _open_balance(config)
    if store is None:
        return 2

    try:
        year = args.year or time.localtime().tm_year
        modelle = list(MODELS) if args.both else [args.model]

        pfade = {}
        for modell in modelle:
            pfad = build_year_report(year, db, store, config,
                                     args.directory, modell)
            pfade[modell] = pfad
            print(f'{pfad}  ({os.path.getsize(pfad) / 1024:.1f} kB)  '
                  f'— {MODEL_LABELS[modell]}')

        if args.send:
            if not config.telegram.enabled:
                print('Telegram ist nicht aktiviert.', file=sys.stderr)
                return 3
            from s0counter.telegram import TelegramBot, TelegramError
            bot = TelegramBot(config.telegram.token, config.telegram.chat_id,
                              config.telegram.timeout)
            try:
                if len(pfade) > 1:
                    bot.send_message(
                        f'Stromabrechnung {year} — zwei Verteilmodelle zum '
                        f'Vergleich. Die Rohdaten sind dieselben, nur der '
                        f'Schlüssel unterscheidet sich.')
                for modell, pfad in pfade.items():
                    bot.send_document(
                        pfad,
                        f'Stromabrechnung {year} — {MODEL_LABELS[modell]}')
                print(f'{len(pfade)} Bericht(e) über Telegram verschickt.')
            except TelegramError as exc:
                print(f'Versand fehlgeschlagen: {exc}', file=sys.stderr)
                return 3
        return 0
    finally:
        store.close()


def cmd_telegram_test(args, config, db) -> int:
    """Telegram-Zugang prüfen und eine Nachricht schicken."""
    from s0counter.telegram import TelegramBot, TelegramError, format_status

    if not config.telegram.enabled:
        print('Telegram ist in der Konfiguration nicht aktiviert.',
              file=sys.stderr)
        return 2

    bot = TelegramBot(config.telegram.token, config.telegram.chat_id,
                      config.telegram.timeout)
    try:
        info = bot.check()
        print(f'Bot: @{info.get("username")} ({info.get("first_name")})')

        balance_row = None
        if config.balance.enabled:
            from s0counter.balance import BalanceStore
            store = BalanceStore(config.balance.database)
            try:
                balance_row = store.latest()
            finally:
                store.close()

        bot.send_message(format_status(db.all_states(), balance_row))
        print(f'Nachricht an Chat {config.telegram.chat_id} verschickt.')
        return 0
    except TelegramError as exc:
        print(f'Fehlgeschlagen: {exc}', file=sys.stderr)
        return 3


def _frage(text: str, vorgabe: str = '') -> str:
    """Eine Zeile abfragen. Leere Eingabe nimmt die Vorgabe."""
    hinweis = f' [{vorgabe}]' if vorgabe else ''
    antwort = input(f'{text}{hinweis}: ').strip()
    return antwort or vorgabe


def _ja(text: str, vorgabe: bool = False) -> bool:
    """Ja/Nein abfragen. Bei fehlendem Terminal gilt die Vorgabe.

    Ohne den EOFError-Zweig bricht der Assistent in einer Pipe mit einem
    Rückverfolgungsprotokoll ab statt mit einem Satz.
    """
    standard = 'J/n' if vorgabe else 'j/N'
    try:
        antwort = input(f'{text} [{standard}] ').strip().lower()
    except EOFError:
        return vorgabe
    if not antwort:
        return vorgabe
    return antwort in ('j', 'ja', 'y', 'yes')


def cmd_nas_setup(args, config, db) -> int:
    """Geführt die SMB-Freigabe der Synology einbinden.

    Jeder Schreibschritt wird vorher gezeigt und einzeln bestätigt. Bricht
    ein Schritt ab, wird das bis dahin Geschriebene wieder zurückgebaut —
    ein halb eingerichteter fstab-Eintrag zeigt sonst beim nächsten Neustart
    ins Leere.
    """
    from s0counter import nas_setup as nas

    print('SMB-Sicherung auf die Synology einrichten')
    print('=' * 62)
    print('Der Assistent fragt vor jedem Schreibschritt nach. Mit Strg-C')
    print('kommst du jederzeit raus, ohne dass etwas geändert bleibt.')
    print()

    if not nas.ist_root():
        print('Dieser Schritt braucht root: sudo s0ctl nas-setup',
              file=sys.stderr)
        return 2

    if not nas.cifs_vorhanden():
        print('Es fehlt das Paket cifs-utils. Ohne das kann Linux keine')
        print('SMB-Freigabe einbinden.')
        print()
        if not _ja('Jetzt "apt install -y cifs-utils" ausführen?', True):
            print('Abgebrochen — nichts geändert.')
            return 1
        code, ausgabe = nas._lauf(['apt-get', 'install', '-y', 'cifs-utils'],
                                  timeout=300)
        if code != 0 or not nas.cifs_vorhanden():
            print(f'Installation fehlgeschlagen:\n{ausgabe}', file=sys.stderr)
            return 3
        print('cifs-utils ist installiert.')
        print()

    try:
        host = _frage('Adresse der NAS', nas.STANDARD_HOST)
        benutzer = _frage('Benutzername auf der NAS')
        if not benutzer:
            print('Ohne Benutzernamen geht es nicht. Abgebrochen.')
            return 1
        passwort = getpass.getpass('Passwort (wird nicht angezeigt): ')
        if not passwort:
            print('Ohne Passwort geht es nicht. Abgebrochen.')
            return 1

        # Den Freigabenamen abfragen statt raten lassen. Fehlt smbclient,
        # wird es angeboten: ohne die Liste rät der Mensch den Namen, und
        # der Fehler fällt erst beim Einbinden auf — dort dann mit einer
        # Meldung über die SMB-Fassung, also an der falschen Stelle.
        print()
        if not nas.smbclient_vorhanden():
            print('Für die Liste der Freigaben fehlt das Paket smbclient.')
            print('Ohne die Liste musst du den Namen von Hand eintippen.')
            print()
            if _ja('Jetzt "apt install -y smbclient" ausführen?', True):
                code, ausgabe = nas._lauf(
                    ['apt-get', 'install', '-y', 'smbclient'], timeout=300)
                if code == 0 and nas.smbclient_vorhanden():
                    print('smbclient ist installiert.')
                else:
                    # Kein Abbruch: der Name lässt sich auch eintippen.
                    print(f'Installation fehlgeschlagen:\n{ausgabe}',
                          file=sys.stderr)
            print()

        print(f'Frage die Freigaben von {host} ab …')
        freigaben = nas.freigaben_auflisten(host, benutzer, passwort)
        if freigaben:
            print('Gefunden:')
            for nummer, name in enumerate(freigaben, 1):
                print(f'  {nummer}. {name}')
            wahl = _frage('Nummer oder Name der Freigabe', '1')
            if wahl.isdigit() and 1 <= int(wahl) <= len(freigaben):
                freigabe = freigaben[int(wahl) - 1]
            else:
                freigabe = wahl
        else:
            print('Keine Liste bekommen — die NAS gibt keine Auskunft oder')
            print('Benutzername/Passwort stimmen nicht. Der Name steht in DSM')
            print('unter Systemsteuerung → Freigegebene Ordner. Es ist nur')
            print('der Ordnername (z.B. "Backup"), kein Pfad.')
            freigabe = _frage('Name der Freigabe')
        if not freigabe:
            print('Ohne Freigabenamen geht es nicht. Abgebrochen.')
            return 1

        # Sieht die Eingabe nach einem Pfad aus, nachfragen statt sie in die
        # fstab zu schreiben und am Einbinden scheitern zu lassen.
        einwand = nas.freigabename_pruefen(freigabe)
        if einwand:
            print()
            print(f'⚠ {einwand}')
            print()
            if not _ja(f'Trotzdem "{freigabe}" verwenden?', False):
                freigabe = _frage('Name der Freigabe')
                if not freigabe:
                    print('Ohne Freigabenamen geht es nicht. Abgebrochen.')
                    return 1

        mountpunkt = _frage('Einhängepunkt auf dem Pi', nas.STANDARD_MOUNT)
    except (EOFError, KeyboardInterrupt):
        print('\nAbgebrochen — nichts geändert.')
        return 1

    vorhanden = nas.fstab_eintrag_finden(mountpunkt)
    if vorhanden:
        print()
        print(f'In {nas.FSTAB} steht für {mountpunkt} bereits ein Eintrag:')
        print(f'  {vorhanden.strip()}')
        if not _ja('Ersetzen?', False):
            print('Abgebrochen — nichts geändert.')
            return 1
        nas.aushaengen(mountpunkt)
        nas.fstab_zeile_entfernen(vorhanden)

    print()
    print('Geschrieben werden:')
    print(f'  {nas.CRED}  (Zugangsdaten, Modus 600)')
    print(f'  {nas.FSTAB}  (eine Zeile für //{host}/{freigabe})')
    print(f'  {mountpunkt}  (Ordner, falls er fehlt)')
    print()
    if not _ja('Weiter?', True):
        print('Abgebrochen — nichts geändert.')
        return 1

    try:
        nas.zugangsdaten_schreiben(benutzer, passwort)
    except (OSError, nas.SetupFehler) as exc:
        print(f'Zugangsdaten konnten nicht abgelegt werden: {exc}',
              file=sys.stderr)
        return 3
    print(f'✓ {nas.CRED} angelegt')

    # Die SMB-Fassung nicht raten: Synology-Geräte sprechen je nach DSM-
    # Einstellung 3.1.1, 3.0 oder 2.1. Ausprobiert wird von neu nach alt.
    print('Binde ein und probiere die SMB-Fassungen durch …')
    fassung, ausgabe = nas.probeeinbinden(host, freigabe, mountpunkt)
    if fassung is None:
        print(f'✗ Einbinden fehlgeschlagen: {nas.fehlergrund(ausgabe)}',
              file=sys.stderr)
        print()
        print('Nichts steht mehr in der fstab. Die Zugangsdaten bleiben')
        print(f'liegen ({nas.CRED}); bei falschem Passwort einfach den')
        print('Assistenten nochmal starten.')
        return 3
    print(f'✓ eingebunden mit SMB {fassung}')

    ok, grund = nas.schreibprobe(mountpunkt)
    if not ok:
        # Eingebunden, aber nicht beschreibbar: die Sicherung liefe ins
        # Leere und meldete trotzdem nichts. Also zurückbauen.
        print(f'✗ Schreiben auf {mountpunkt} geht nicht: {grund}',
              file=sys.stderr)
        print('  In DSM unter Systemsteuerung → Freigegebene Ordner →')
        print(f'  {freigabe} → Berechtigungen dem Benutzer "{benutzer}"')
        print('  Lesen/Schreiben geben.')
        nas.aushaengen(mountpunkt)
        nas.fstab_zeile_entfernen(
            nas.fstab_zeile(host, freigabe, mountpunkt, fassung))
        print('Der fstab-Eintrag wurde wieder entfernt.')
        return 3
    print('✓ Schreibprobe bestanden')

    ziel = os.path.join(mountpunkt, 's0counter')
    os.makedirs(ziel, exist_ok=True)
    print(f'✓ {ziel} angelegt')

    print()
    if _ja('Jetzt gleich eine Sicherung dorthin laufen lassen?', True):
        code, ausgabe = nas._lauf(
            ['systemctl', 'start', 's0counter-backup.service'], timeout=300)
        if code == 127:
            print('systemctl gibt es hier nicht — Sicherung übersprungen.')
        elif code != 0:
            print(f'Der Lauf meldete einen Fehler:\n{ausgabe}')
            print('Nachsehen mit: journalctl -u s0counter-backup -n 30')
        else:
            # Rückgabewert ist keine Wirkung: nachzählen, was wirklich liegt.
            dateien = [d for d in os.listdir(ziel)
                       if d.startswith('impulse-')]
            if dateien:
                print(f'✓ {len(dateien)} Sicherung(en) liegen in {ziel}')
            else:
                print(f'✗ Der Lauf meldete Erfolg, in {ziel} liegt aber')
                print('  nichts. Nachsehen mit:')
                print('  journalctl -u s0counter-backup -n 30')
                return 3

    print()
    print('Fertig. Zur Kontrolle:  s0ctl doctor')
    if ziel.rstrip('/') != args.backup_ziel.rstrip('/'):
        # Sonst prüft der Doctor weiter den alten Ort und meldet FEHLT,
        # obwohl gerade erfolgreich eingerichtet wurde.
        print()
        print(f'Hinweis: der Doctor erwartet die Sicherung unter')
        print(f'  {args.backup_ziel}')
        print(f'Du hast {ziel} gewählt. Entweder beim Aufruf mitgeben:')
        print(f'  s0ctl doctor --backup-ziel {ziel}')
        print('oder in backup.sh S0_BACKUP_TARGET entsprechend setzen.')
    return 0


def cmd_doctor(args, config, db) -> int:
    """Anlage durchprüfen und in Klartext sagen, was fehlt."""
    from s0counter import doctor

    if args.nas_hilfe:
        print(doctor.NAS_HILFE)
        return 0

    befunde = doctor.alle_pruefungen(
        config, db, args.backup_ziel, args.backup_ersatz)

    zeichen = {doctor.OK: '✓', doctor.FEHLT: '✗', doctor.UNKLAR: '?'}
    letzter_bereich = None
    for b in befunde:
        if b.bereich != letzter_bereich:
            print(f'\n{b.bereich}')
            letzter_bereich = b.bereich
        zeile = f'  {zeichen[b.stufe]} {b.titel}'
        if b.detail:
            zeile += f' — {b.detail}'
        print(zeile)
        if b.hilfe:
            print(f'      → {b.hilfe}')

    offen = [b for b in befunde if b.stufe == doctor.FEHLT]
    unklar = [b for b in befunde if b.stufe == doctor.UNKLAR]

    print()
    print('-' * 62)
    teile = [f'{len(befunde) - len(offen) - len(unklar)} in Ordnung']
    if offen:
        teile.append(f'{len(offen)} offen')
    if unklar:
        teile.append(f'{len(unklar)} nicht messbar')
    print(', '.join(teile))

    if offen:
        print()
        print('Offen:')
        for b in offen:
            print(f'  • {b.bereich}: {b.titel}')
            if b.hilfe:
                print(f'      {b.hilfe}')

    # Der Doctor selbst ändert weiterhin nichts. Er darf aber anbieten, in
    # den Assistenten zu wechseln — ausgelöst wird das vom Menschen. Ohne
    # das Angebot bleibt ein Befund ein Satz, den man später wiederfinden
    # müsste; das ist dieselbe Falle wie ein Hinweis, der nur im Fehlerfall
    # erscheint und deshalb nie gelesen wird.
    if offen and not args.nicht_fragen and sys.stdin.isatty():
        _doctor_angebot(offen, args)

    # `UNKLAR` macht den Lauf NICHT rot: es heißt „hier nicht feststellbar",
    # nicht „kaputt". Sonst wäre der Doctor auf dem Entwicklungsrechner
    # dauerhaft rot und würde dort abgeschaltet.
    return 1 if offen else 0


# Was der Doctor am Ende anbieten kann. Eine Stelle, damit ein neuer
# Assistent nicht an zwei Orten nachgetragen werden muss.
#
# `erkennen` bekommt die Liste der offenen Befunde und sagt, ob dieses
# Angebot passt. `befehl` ist das, was der Mensch sonst tippen müsste — es
# wird angezeigt, auch wenn er ablehnt.
ANGEBOTE = [
    {
        'passt': lambda offen: any(
            b.bereich == 'Sicherung' and 'eingebunden' in b.titel
            for b in offen),
        'frage': 'Die Sicherung liegt nur lokal. Jetzt die NAS einrichten?',
        'befehl': 'sudo s0ctl nas-setup',
        'handler': cmd_nas_setup,
    },
]


def _doctor_angebot(offen, args) -> None:
    """Für jeden passenden Befund fragen, ob gleich weitergemacht wird."""
    passende = [a for a in ANGEBOTE if a['passt'](offen)]
    if not passende:
        return

    print()
    print('-' * 62)
    print('Gleich weitermachen?')
    for angebot in passende:
        print()
        if _ja(angebot['frage'], False):
            print()
            # Der Assistent bringt seine eigenen Rückfragen mit; der Doctor
            # reicht nur durch. Ein Fehlschlag hier darf den Doctor nicht
            # abbrechen — er hat seinen Bericht schon abgeliefert.
            try:
                angebot['handler'](args, None, None)
            except KeyboardInterrupt:
                print('\nAbgebrochen.')
        else:
            print(f'  Später von Hand:  {angebot["befehl"]}')


def unterbefehle() -> set:
    """Welche Unterbefehle es gibt — aus dem Parser gelesen.

    Damit lässt sich prüfen, ob ein Befehl, auf den ein Hinweis verweist,
    wirklich existiert. Eine von Hand gepflegte Liste bliebe nach einer
    Umbenennung stehen und der Hinweis zeigte ins Leere.
    """
    for aktion in _parser()._actions:
        if isinstance(aktion, argparse._SubParsersAction):
            return set(aktion.choices)
    return set()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description='Verwaltung des S0-Zählers',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__)
    parser.add_argument('-c', '--config',
                        default='/etc/s0counter/config.toml')
    sub = parser.add_subparsers(dest='command', required=True)

    sub.add_parser('status', help='Zählerstände anzeigen')

    setter = sub.add_parser('set-reading',
                            help='auf den Zählerstand synchronisieren')
    setter.add_argument('meter')
    setter.add_argument('kwh', type=float)
    setter.add_argument('--force', action='store_true',
                        help='große Abweichung bestätigen')

    exporter = sub.add_parser('export', help='Rohimpulse als CSV')
    exporter.add_argument('file')
    exporter.add_argument('--meter', help='nur diesen Zähler')
    exporter.add_argument('--days', type=int, help='nur die letzten N Tage')

    backup = sub.add_parser('backup', help='Datenbank sichern')
    backup.add_argument('file')

    audit = sub.add_parser('audit', help='Korrekturen anzeigen')
    audit.add_argument('--limit', type=int, default=50)

    retire = sub.add_parser(
        'retire', help='Zähler stilllegen (Historie bleibt)')
    retire.add_argument('meter')

    sub.add_parser('ha-test', help='Home Assistant prüfen und veröffentlichen')
    sub.add_parser('ha-remove', help='Sensoren aus Home Assistant entfernen')

    sub.add_parser('balance', help='aktuelle Hausbilanz anzeigen')

    allocate = sub.add_parser(
        'allocate', help='Netzbezug auf die Wohnungen verteilen')
    allocate.add_argument('--year', type=int,
                          help='Kalenderjahr, sonst das laufende')
    allocate.add_argument('--model', default='proportional',
                          choices=('proportional', 'waterfall'),
                          help='Verteilmodell (Vorgabe: proportional)')
    allocate.add_argument('--compare', action='store_true',
                          help='beide Modelle nebeneinander zeigen')

    report = sub.add_parser('report', help='Jahresbericht als PDF')
    report.add_argument('--year', type=int,
                        help='Kalenderjahr, sonst das laufende')
    report.add_argument('--directory', help='Zielordner, sonst aus der Konfiguration')
    report.add_argument('--model', default='proportional',
                        choices=('proportional', 'waterfall'),
                        help='Verteilmodell (Vorgabe: proportional)')
    report.add_argument('--both', action='store_true',
                        help='beide Modelle als getrennte PDF erzeugen')
    report.add_argument('--send', action='store_true',
                        help='anschließend über Telegram verschicken')

    sub.add_parser('telegram-test',
                   help='Telegram prüfen und Status verschicken')

    arzt = sub.add_parser(
        'doctor', help='Anlage durchprüfen: was läuft, was fehlt')
    arzt.add_argument('--nas-hilfe', action='store_true',
                      help='Anleitung zum Einbinden der Synology ausgeben')
    arzt.add_argument('--backup-ziel', default='/mnt/synology/s0counter',
                      help='erwarteter Ort der Sicherung')
    arzt.add_argument('--backup-ersatz', default='/var/backups/s0counter',
                      help='lokales Ausweichziel')
    arzt.add_argument('--nicht-fragen', action='store_true',
                      help='am Ende nichts anbieten (für Skripte und Cron)')

    nas_cmd = sub.add_parser(
        'nas-setup',
        help='SMB-Freigabe der Synology geführt einbinden (braucht root)')
    nas_cmd.add_argument('--backup-ziel', default='/mnt/synology/s0counter',
                         help='Ort, den der Doctor anschließend erwartet')

    return parser


def main() -> int:
    args = _parser().parse_args()

    # nas-setup läuft bewusst ohne Konfiguration und ohne Datenbank: es wird
    # genau dann gebraucht, wenn die Anlage noch nicht steht. Ein
    # Konfigurationsfehler darf den Einrichtungsweg nicht versperren.
    if args.command == 'nas-setup':
        return cmd_nas_setup(args, None, None)

    try:
        config = config_module.load(args.config)
    except config_module.ConfigError as exc:
        print(f'Konfigurationsfehler: {exc}', file=sys.stderr)
        return 2

    db = Database(config.database)
    handlers = {
        'status': cmd_status,
        'set-reading': cmd_set_reading,
        'export': cmd_export,
        'backup': cmd_backup,
        'audit': cmd_audit,
        'ha-test': cmd_ha_test,
        'ha-remove': cmd_ha_remove,
        'retire': cmd_retire,
        'balance': cmd_balance,
        'allocate': cmd_allocate,
        'report': cmd_report,
        'telegram-test': cmd_telegram_test,
        'doctor': cmd_doctor,
    }
    try:
        return handlers[args.command](args, config, db)
    finally:
        db.close()


if __name__ == '__main__':
    sys.exit(main())
