"""Prüft die laufende Anlage und sagt in Klartext, was fehlt.

Gedacht für zwei Momente: nach der Inbetriebnahme, und wenn man die Anlage
nach Monaten wieder anfasst und nicht mehr weiß, was eingerichtet war.

Drei Ausgänge statt zwei, weil eine fehlgeschlagene Prüfung sonst
stillschweigend als bestanden durchgeht:

    OK        geprüft, in Ordnung
    FEHLT     geprüft, nicht in Ordnung — mit dem Befehl, der es behebt
    UNKLAR    nicht messbar (kein Zugriff, Werkzeug fehlt, Dienst aus)

`UNKLAR` ist kein Fehler und macht den Gesamtlauf nicht rot. Es ist die
ehrliche Antwort auf „das konnte ich hier nicht feststellen" — etwa wenn
`doctor` ohne root läuft und systemd-Zustände nicht lesbar sind.

Der Doctor **ändert nichts**. Er nennt zu jedem Befund den Befehl, der ihn
behebt; ausgeführt wird er vom Menschen. Ein Werkzeug, das ungefragt an der
Konfiguration einer Abrechnungsanlage dreht, wäre die falsche Bauform.
"""
import os
import shutil
import socket
import subprocess
import time

from . import ordnung
from urllib.parse import urlparse

OK = 'OK'
FEHLT = 'FEHLT'
UNKLAR = 'UNKLAR'


class Befund:
    """Ein Prüfergebnis mit dem Weg zur Behebung."""

    def __init__(self, bereich: str, titel: str, stufe: str,
                 detail: str = '', hilfe: str = ''):
        self.bereich = bereich
        self.titel = titel
        self.stufe = stufe
        self.detail = detail
        self.hilfe = hilfe

    def __repr__(self):                      # pragma: no cover - Diagnose
        return f'<Befund {self.stufe} {self.bereich}/{self.titel}>'


def _systemctl(*args) -> tuple[int, str]:
    """systemctl aufrufen. Rückgabe (code, ausgabe).

    Code 127 heißt: systemctl gibt es hier nicht — auf dem
    Entwicklungsrechner der Normalfall, und ein Grund für UNKLAR statt FEHLT.
    """
    if shutil.which('systemctl') is None:
        return 127, ''
    try:
        fertig = subprocess.run(['systemctl', *args], capture_output=True,
                                text=True, timeout=10)
        return fertig.returncode, (fertig.stdout or '').strip()
    except (OSError, subprocess.SubprocessError):
        return 127, ''


def _erreichbar(host: str, port: int, timeout: float = 3.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


# --- Einzelprüfungen -----------------------------------------------------
#
# Jede liefert eine Liste von Befunden. Keine wirft: eine Prüfung, die den
# Doctor abbricht, verhindert alle folgenden — und gerade wenn viel im Argen
# liegt, will man die vollständige Liste sehen.

def _unit_fehlt(zustand: str) -> bool:
    """Ist die Unit gar nicht installiert?

    `not-found` heißt: hier läuft der Dienst nicht, weil er hier nie
    eingerichtet wurde — der Normalfall auf dem Entwicklungsrechner. Das ist
    UNKLAR, nicht FEHLT. Ein Doctor, der dort dauerhaft rot meldet, wird
    abgeschaltet und schützt danach gar nichts mehr.
    """
    return zustand in ('not-found', 'inactive-not-found', '')


def pruefe_dienst(config) -> list:
    befunde = []
    code, aktiv = _systemctl('is-active', 's0counter')
    _, aktiviert = _systemctl('is-enabled', 's0counter')

    if code == 127:
        return [Befund('Dienst', 'läuft', UNKLAR,
                       'systemctl nicht verfügbar — vermutlich kein Pi')]
    if _unit_fehlt(aktiviert):
        return [Befund('Dienst', 'eingerichtet', UNKLAR,
                       's0counter.service ist auf diesem Rechner nicht '
                       'installiert')]

    if aktiv == 'active':
        befunde.append(Befund('Dienst', 'läuft', OK))
    else:
        befunde.append(Befund(
            'Dienst', 'läuft', FEHLT, f'Zustand: {aktiv or "unbekannt"}',
            'systemctl start s0counter && journalctl -u s0counter -n 30'))

    if aktiviert == 'enabled':
        befunde.append(Befund('Dienst', 'startet nach Neustart', OK))
    else:
        befunde.append(Befund(
            'Dienst', 'startet nach Neustart', FEHLT,
            f'Zustand: {aktiviert}', 'systemctl enable s0counter'))

    return befunde


def pruefe_erfassung(db, config) -> list:
    """Kommen Impulse an?

    Ein Zähler, der seit Stunden schweigt, ist der teuerste Fehler dieser
    Anlage: er fällt sonst erst bei der Abrechnung auf. Die Schwelle ist
    bewusst großzügig — nachts verbraucht eine Wohnung wenig, und bei
    1000 Impulsen je kWh bedeuten 6 Stunden Stille unter 1 Watt Dauerlast.
    """
    befunde = []
    zustaende = db.all_states()
    if not zustaende:
        return [Befund('Erfassung', 'Zähler angelegt', FEHLT,
                       'kein Zähler in der Datenbank',
                       'Konfiguration prüfen, Dienst neu starten')]

    jetzt = time.time()
    for meter_id, zustand in sorted(
            zustaende.items(),
            key=lambda paar: ordnung.sortierschluessel(paar[0])):
        letzter = zustand.get('last_pulse')
        if letzter is None:
            befunde.append(Befund(
                'Erfassung', f'{meter_id}: Impulse', FEHLT,
                'noch nie ein Impuls',
                'Verdrahtung prüfen: s0counter stoppen, dann '
                'test_gpio.py laufen lassen'))
            continue
        alter = jetzt - letzter
        if alter > 6 * 3600:
            befunde.append(Befund(
                'Erfassung', f'{meter_id}: Impulse', FEHLT,
                f'letzter Impuls vor {alter / 3600:.1f} h',
                'Verdrahtung und Sicherung der Wohnung prüfen'))
        else:
            befunde.append(Befund(
                'Erfassung', f'{meter_id}: Impulse', OK,
                f'letzter vor {alter:.0f} s, Stand '
                f'{zustand["energy_kwh"]:.3f} kWh'))

    return befunde


def pruefe_bilanzquelle(config) -> list:
    """Antwortet die Quelle der Hausbilanz?

    Ohne sie rechnet die Abrechnung nicht — die Wohnungszahlen stimmen
    weiter, aber die Aufteilung in PV und Netz entfällt.
    """
    if not config.balance.enabled:
        return [Befund('Hausbilanz', 'eingeschaltet', UNKLAR,
                       'in der Konfiguration abgeschaltet')]

    befunde = [Befund('Hausbilanz', 'eingeschaltet', OK)]

    quelle = getattr(config, 'sonnenbatterie', None)
    if quelle is not None and quelle.enabled:
        host = quelle.host.replace('http://', '').replace('https://', '')
        host = host.split('/')[0].split(':')[0]
        if _erreichbar(host, 80):
            befunde.append(Befund('Hausbilanz', f'Sonnenbatterie {host}', OK))
        else:
            befunde.append(Befund(
                'Hausbilanz', f'Sonnenbatterie {host}', FEHLT,
                'antwortet nicht auf Port 80',
                f'ping {host} — Adresse in der Konfiguration prüfen'))
    else:
        befunde.append(Befund('Hausbilanz', 'Quelle', UNKLAR,
                              'keine Sonnenbatterie konfiguriert'))

    return befunde


def pruefe_homeassistant(config) -> list:
    settings = config.homeassistant
    if not settings.enabled:
        return [Befund('Home Assistant', 'eingeschaltet', UNKLAR,
                       'abgeschaltet; die Erfassung braucht ihn nicht')]

    befunde = []
    if not settings.token:
        befunde.append(Befund(
            'Home Assistant', 'Token hinterlegt', FEHLT,
            'leeres Token-Feld',
            'Token in /etc/s0counter/config.toml eintragen (nie ins Repo)'))
    else:
        befunde.append(Befund('Home Assistant', 'Token hinterlegt', OK))

    ziel = urlparse(settings.url)
    if ziel.hostname:
        port = ziel.port or (443 if ziel.scheme == 'https' else 80)
        if _erreichbar(ziel.hostname, port):
            befunde.append(Befund(
                'Home Assistant', f'{ziel.hostname}:{port}', OK))
        else:
            befunde.append(Befund(
                'Home Assistant', f'{ziel.hostname}:{port}', FEHLT,
                'antwortet nicht', 's0ctl ha-test'))

    return befunde


def pruefe_sicherung(config, ziel: str, ersatz: str) -> list:
    """Die wichtigste Prüfung der Anlage.

    Nicht „ist ein Timer aktiv", sondern „liegt eine Sicherung, ist sie
    jung genug, und liegt sie außerhalb dieses Geräts". Ein Timer, der
    nachts scheitert, ist genau der Fall, der hier auffallen muss — er hat
    diese Anlage einen Tag lang ohne Sicherung laufen lassen.
    """
    befunde = []

    code, zustand = _systemctl('is-enabled', 's0counter-backup.timer')
    if code == 127 or _unit_fehlt(zustand):
        befunde.append(Befund(
            'Sicherung', 'Timer eingerichtet', UNKLAR,
            'systemctl nicht verfügbar' if code == 127
            else 's0counter-backup.timer ist hier nicht installiert'))
    elif zustand == 'enabled':
        befunde.append(Befund('Sicherung', 'Timer aktiv', OK))
    else:
        befunde.append(Befund(
            'Sicherung', 'Timer aktiv', FEHLT, f'Zustand: {zustand}',
            'systemctl enable --now s0counter-backup.timer'))

    eingebunden = os.path.ismount(ziel) or os.path.ismount(
        os.path.dirname(ziel.rstrip('/')))
    if eingebunden:
        befunde.append(Befund('Sicherung', f'Ziel {ziel} eingebunden', OK))
        ort = ziel
    else:
        befunde.append(Befund(
            'Sicherung', f'Ziel {ziel} eingebunden', FEHLT,
            'Netzlaufwerk fehlt — es wird lokal gesichert, das schützt '
            'NICHT gegen einen Ausfall der Speicherkarte',
            'sudo s0ctl nas-setup  (führt durch die Einrichtung; '
            's0ctl doctor --nas-hilfe zeigt die Schritte zum Selbermachen)'))
        ort = ersatz

    # Eine Sicherung, die es gibt, ist noch keine, die aktuell ist — und
    # eine Datei, die es gibt, ist noch keine Sicherung. Am 27.08.2026 stand
    # eine 0-Byte-Datei auf der Synology und wurde hier als „Sicherung
    # aktuell" gemeldet: `sqlite3.connect()` hatte sie angelegt, das
    # Schreiben über CIFS hing dann in den Dateisperren. Deshalb wird die
    # Größe geprüft, nicht bloß die Existenz.
    juengste, anzahl, leere = None, 0, []
    if os.path.isdir(ort):
        for eintrag in os.listdir(ort):
            if not eintrag.startswith('impulse-') \
                    or not eintrag.endswith('.sqlite3'):
                continue
            pfad = os.path.join(ort, eintrag)
            try:
                stat = os.stat(pfad)
            except OSError:
                continue
            # Eine SQLite-Datei beginnt mit einem 100 Byte langen Kopf. Was
            # kleiner ist, kann keine Datenbank sein.
            if stat.st_size < 100:
                leere.append(eintrag)
                continue
            anzahl += 1
            if juengste is None or stat.st_mtime > juengste:
                juengste = stat.st_mtime

    if leere:
        befunde.append(Befund(
            'Sicherung', 'Sicherungen sind lesbar', FEHLT,
            f'{len(leere)} leere Datei(en) in {ort} — angelegt, aber nie '
            'beschrieben; typisch für SQLite direkt auf eine SMB-Freigabe',
            f'sudo rm {os.path.join(ort, leere[0])} && '
            'sudo systemctl start s0counter-backup.service'))

    if juengste is None:
        befunde.append(Befund(
            'Sicherung', 'Sicherung vorhanden', FEHLT,
            f'keine brauchbare Datei in {ort}',
            'systemctl start s0counter-backup.service'))
    else:
        alter_h = (time.time() - juengste) / 3600
        wann = time.strftime('%d.%m. %H:%M', time.localtime(juengste))
        # Der Timer läuft nachts; 48 h Toleranz deckt einen ausgefallenen
        # Lauf ab, ohne bei jedem Nachmittag Alarm zu schlagen.
        if alter_h > 48:
            befunde.append(Befund(
                'Sicherung', 'Sicherung aktuell', FEHLT,
                f'jüngste vom {wann}, {alter_h / 24:.1f} Tage alt',
                'journalctl -u s0counter-backup -n 30'))
        else:
            befunde.append(Befund(
                'Sicherung', 'Sicherung aktuell', OK,
                f'{anzahl} Stück, jüngste vom {wann}'))

    return befunde


def pruefe_speicher(config) -> list:
    """Die Datenbank wächst, die Karte nicht."""
    befunde = []
    pfad = os.path.dirname(config.database) or '/'
    try:
        st = os.statvfs(pfad)
    except OSError as exc:
        return [Befund('Speicher', pfad, UNKLAR, str(exc))]

    frei = st.f_bavail * st.f_frsize
    gesamt = st.f_blocks * st.f_frsize
    belegt_pct = (1 - frei / gesamt) * 100 if gesamt else 0

    # Gemessen wachsen Datenbank und Indizes um rund 334 MB im Jahr. Die
    # Schwelle nennt deshalb die verbleibende Zeit, nicht nur den Prozentwert.
    jahre = frei / 334e6 if frei > 0 else 0
    detail = (f'{frei / 1e9:.1f} GB frei ({belegt_pct:.0f} % belegt) — '
              f'reicht bei 334 MB/Jahr für rund {jahre:.0f} Jahre')
    if belegt_pct > 85:
        befunde.append(Befund('Speicher', pfad, FEHLT, detail,
                              'alte Sicherungen und Journal aufräumen'))
    else:
        befunde.append(Befund('Speicher', pfad, OK, detail))

    return befunde


def pruefe_aggregat(db) -> list:
    """Deckt sich das Minutenaggregat mit den Rohimpulsen?

    Beim Start gleicht der Dienst nur die Summen ab. Diese Prüfung geht
    Minute für Minute und findet auch Verschiebungen bei gleicher Summe —
    zu langsam für den Startvorgang, richtig für ein Prüfwerkzeug.
    """
    try:
        abweichungen = db.verify_pulse_minutes()
    except Exception as exc:                  # noqa: BLE001
        return [Befund('Datenbank', 'Aggregat stimmt', UNKLAR, str(exc))]

    if abweichungen:
        return [Befund(
            'Datenbank', 'Aggregat stimmt', FEHLT,
            f'{len(abweichungen)} Minuten weichen ab',
            'Dienst neu starten — der Startvorgang baut das Aggregat neu auf')]
    return [Befund('Datenbank', 'Aggregat stimmt', OK,
                   'Minutenaggregat deckt sich mit den Rohimpulsen')]


NAS_HILFE = """\
Sicherung auf die Synology einrichten
=====================================

Aktuell wird lokal nach /var/backups/s0counter gesichert. Das schützt gegen
eine kaputte Datenbank und gegen Fehlgriffe — NICHT gegen einen Ausfall der
Speicherkarte, denn beides liegt auf derselben Karte.

Auf dem Pi als root:

  1. Zugangsdaten anlegen (Modus 600, sie stehen im Klartext):

       install -m 600 /dev/null /etc/s0counter/nas.cred
       cat > /etc/s0counter/nas.cred <<'EOF'
       username=DEIN_NAS_BENUTZER
       password=DEIN_NAS_PASSWORT
       EOF

  2. Einhängepunkt und fstab-Eintrag. FREIGABE durch den Namen der
     Synology-Freigabe ersetzen (in DSM unter Systemsteuerung → Freigegebene
     Ordner). Der Eintrag muss auf EINER Zeile stehen:

       mkdir -p /mnt/synology
       printf '%s %s %s %s 0 0\\n' \\
         '//192.168.0.40/FREIGABE' '/mnt/synology' 'cifs' \\
         'credentials=/etc/s0counter/nas.cred,uid=root,gid=root,vers=3.0,_netdev,nofail' \\
         >> /etc/fstab

  3. Einbinden und prüfen. `mountpoint` ist die eigentliche Probe — `mount -a`
     meldet auch dann Erfolg, wenn nichts eingebunden wurde:

       mount -a
       mountpoint /mnt/synology && touch /mnt/synology/.schreibtest \\
         && rm /mnt/synology/.schreibtest && echo "Schreiben geht"

     Geht das Einbinden schief, nennt `dmesg | tail -5` den Grund. Häufig:
     falsches Passwort (Status 13), Freigabename falsch (Status 2), oder
     die Synology verlangt eine andere SMB-Fassung — dann `vers=3.0` auf
     `vers=2.1` ändern.

  4. Einen Lauf erzwingen und nachsehen, dass Dateien entstehen:

       systemctl start s0counter-backup.service
       ls -la /mnt/synology/s0counter/

  5. Zur Kontrolle nochmal:  s0ctl doctor

Hinweise:

  * `nofail` ist wichtig: ohne den Eintrag bleibt der Pi beim Booten hängen,
    wenn die NAS gerade aus ist.
  * Die gesicherte Konfiguration enthält das Home-Assistant-Token. Den
    Freigabeordner deshalb nicht öffentlich freigeben.
  * Die lokale Sicherung bleibt bestehen und wird nach 14 Tagen rotiert.
    Sie ist der Rückfall, wenn die NAS einmal nicht da ist.
"""


def alle_pruefungen(config, db, ziel: str, ersatz: str) -> list:
    """Alle Prüfungen der Reihe nach. Keine bricht die Kette ab."""
    befunde = []
    for pruefung in (
            lambda: pruefe_dienst(config),
            lambda: pruefe_erfassung(db, config),
            lambda: pruefe_bilanzquelle(config),
            lambda: pruefe_homeassistant(config),
            lambda: pruefe_sicherung(config, ziel, ersatz),
            lambda: pruefe_speicher(config),
            lambda: pruefe_aggregat(db),
    ):
        try:
            befunde.extend(pruefung())
        except Exception as exc:              # noqa: BLE001
            # Eine Prüfung, die den Doctor abbricht, verhindert alle
            # folgenden — gerade wenn viel im Argen liegt, will man die
            # vollständige Liste sehen.
            befunde.append(Befund('Doctor', 'Prüfung abgebrochen', UNKLAR,
                                  str(exc)))
    return befunde
