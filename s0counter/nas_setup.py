"""SMB-Freigabe der Synology einrichten — geführt, mit Rückfrage vor jedem
Schreibschritt.

Der Doctor selbst ändert weiterhin nichts. Er darf am Ende anbieten, hierher
zu wechseln; ausgelöst wird der Wechsel vom Menschen mit einer Eingabe. Das
ist der Unterschied zwischen „ein Werkzeug schlägt vor" und „ein Werkzeug
dreht ungefragt an der Konfiguration einer Abrechnungsanlage".

Zwei Dinge macht dieses Modul anders als die alte Anleitung:

  * Es **prüft die Wirkung**, nicht den Rückgabewert. `mount -a` meldet auch
    dann Erfolg, wenn nichts eingebunden wurde — geprüft wird deshalb mit
    `mountpoint` und einer echten Schreibprobe.
  * Es **erkennt den fstab-Eintrag wieder** und schreibt ihn nicht zweimal.
    Ein doppelter Eintrag fällt beim nächsten Neustart auf die Füße.

Die Zugangsdaten stehen im Klartext in einer Datei — das verlangt cifs so.
Deshalb Modus 600 und root als Eigentümer, geprüft nach dem Schreiben.
"""
import os
import re
import shutil
import subprocess
import tempfile

FSTAB = '/etc/fstab'
CRED = '/etc/s0counter/nas.cred'

# Vorgaben passend zu dieser Anlage (docs/ANLAGE.md). Sie sind Vorschläge in
# der Abfrage, keine festen Werte.
STANDARD_HOST = '192.168.0.40'
STANDARD_MOUNT = '/mnt/synology'

# `nofail` ist nicht optional: ohne den Eintrag bleibt der Pi beim Booten
# hängen, wenn die NAS gerade aus ist. `_netdev` sorgt dafür, dass erst nach
# dem Netzwerk eingebunden wird.
OPTIONEN_FEST = 'uid=root,gid=root,_netdev,nofail'

SMB_FASSUNGEN = ('3.1.1', '3.0', '2.1')


class SetupFehler(Exception):
    """Abbruch mit einem Grund, den man dem Menschen zeigen kann."""


def _lauf(befehl: list, timeout: int = 30) -> tuple[int, str]:
    """Befehl ausführen, (code, stdout+stderr) zurückgeben."""
    try:
        fertig = subprocess.run(befehl, capture_output=True, text=True,
                                timeout=timeout)
    except FileNotFoundError:
        return 127, f'{befehl[0]} nicht gefunden'
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, str(exc)
    ausgabe = ((fertig.stdout or '') + (fertig.stderr or '')).strip()
    return fertig.returncode, ausgabe


def ist_root() -> bool:
    return os.geteuid() == 0


def cifs_vorhanden() -> bool:
    """Ist mount.cifs installiert?

    Ohne das Hilfsprogramm meldet `mount` einen Fehler, der nach einem
    Netzwerkproblem aussieht ("wrong fs type") — die Ursache ist aber ein
    fehlendes Paket. Das vorher zu prüfen erspart eine Fehlersuche an der
    falschen Stelle.
    """
    return (shutil.which('mount.cifs') is not None
            or os.path.exists('/sbin/mount.cifs')
            or os.path.exists('/usr/sbin/mount.cifs'))


def freigaben_auflisten(host: str, benutzer: str, passwort: str) -> list:
    """Freigaben der NAS abfragen, damit man den Namen nicht raten muss.

    Scheitert das (kein smbclient, NAS verweigert die Auskunft), ist das
    kein Fehler: der Name lässt sich auch von Hand eintragen. Deshalb eine
    leere Liste statt einer Ausnahme.
    """
    if not smbclient_vorhanden():
        return []
    umgebung = dict(os.environ, PASSWD=passwort)
    try:
        fertig = subprocess.run(
            ['smbclient', '-L', host, '-U', benutzer], capture_output=True,
            text=True, timeout=20, env=umgebung)
    except (OSError, subprocess.SubprocessError):
        return []

    namen = []
    for zeile in (fertig.stdout or '').splitlines():
        treffer = re.match(r'\s+(\S+)\s+Disk\s', zeile)
        if treffer and not treffer.group(1).endswith('$'):
            namen.append(treffer.group(1))
    return namen


def smbclient_vorhanden() -> bool:
    """Ist smbclient installiert?

    Ohne das Paket liefert `freigaben_auflisten` eine leere Liste — und der
    Mensch muss den Freigabenamen raten. Genau das ist am 27.08.2026
    passiert: die Eingabe wurde zu `/volume2/Backup/Raspberry` statt
    `Backup`, das Einbinden scheiterte, und die Meldung zeigte auf die
    SMB-Fassung statt auf die eigentliche Ursache.
    """
    return shutil.which('smbclient') is not None


def freigabename_pruefen(name: str) -> str | None:
    """Sieht der eingegebene Name nach einem Pfad statt einer Freigabe aus?

    Gibt den Grund zurück, wenn etwas nicht stimmt, sonst None.

    SMB kennt keine Pfade — nur Freigabenamen. Wer in DSM den Ordner
    `/volume2/Backup/Raspberry` sieht, tippt sehr naheliegend genau das ein;
    richtig ist `Backup`, der Rest gehört hinter den Einhängepunkt. Der
    Fehler fällt sonst erst beim Einbinden auf, und zwar mit einer Meldung
    über die SMB-Fassung — also an der falschen Stelle.

    Bewusst nur eine Warnung mit Rückfrage, kein hartes Nein: exotische
    Server erlauben Zeichen, die hier auffällig aussehen. Ein Gate, das den
    gesunden Fall blockiert, wird abgeschaltet.
    """
    if not name:
        return None
    if '/' in name or '\\' in name:
        erster = re.split(r'[/\\]+', name.strip('/\\'))[0]
        vorschlag = f' Gemeint ist vermutlich: {erster}' if erster else ''
        return ('Ein Freigabename enthält keine Schrägstriche — SMB kennt '
                'keine Pfade, nur Freigabenamen. Unterordner gehören hinter '
                f'den Einhängepunkt.{vorschlag}')
    if name.startswith('volume') and name[6:7].isdigit():
        return ('Das sieht nach einem DSM-Datenträger aus, nicht nach einer '
                'Freigabe. Der Freigabename ist der Ordnername darunter.')
    return None


def zugangsdaten_schreiben(benutzer: str, passwort: str,
                           pfad: str = CRED) -> None:
    """Zugangsdaten ablegen — Modus 600, und das danach nachprüfen.

    Geschrieben wird erst in eine temporäre Datei im selben Verzeichnis und
    dann umbenannt: sonst steht das Passwort einen Moment lang in einer Datei
    mit den Standardrechten.
    """
    ordner = os.path.dirname(pfad)
    os.makedirs(ordner, mode=0o750, exist_ok=True)

    fd, temp = tempfile.mkstemp(dir=ordner)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'w') as f:
            f.write(f'username={benutzer}\npassword={passwort}\n')
        os.replace(temp, pfad)
    except BaseException:
        if os.path.exists(temp):
            os.unlink(temp)
        raise

    # Wirkung prüfen, nicht den Rückgabewert: ein umask oder ein
    # Dateisystem ohne Rechteverwaltung könnte den Modus verändert haben.
    modus = os.stat(pfad).st_mode & 0o777
    if modus != 0o600:
        raise SetupFehler(
            f'{pfad} hat Modus {modus:o} statt 600 — das Passwort wäre für '
            'andere Benutzer lesbar. Abgebrochen.')


def fstab_zeile(host: str, freigabe: str, mountpunkt: str,
                fassung: str) -> str:
    quelle = f'//{host}/{freigabe}'
    optionen = f'credentials={CRED},{OPTIONEN_FEST},vers={fassung}'
    return f'{quelle} {mountpunkt} cifs {optionen} 0 0\n'


def fstab_eintrag_finden(mountpunkt: str, pfad: str = FSTAB) -> str | None:
    """Steht für diesen Einhängepunkt schon etwas in der fstab?

    Verglichen wird der Einhängepunkt, nicht die ganze Zeile: ein Eintrag mit
    anderem Host oder anderer SMB-Fassung ist derselbe Konflikt.
    """
    try:
        with open(pfad, encoding='utf-8') as f:
            inhalt = f.read()
    except OSError:
        return None

    ziel = mountpunkt.rstrip('/')
    for zeile in inhalt.splitlines():
        blank = zeile.strip()
        if not blank or blank.startswith('#'):
            continue
        felder = blank.split()
        if len(felder) >= 2 and felder[1].rstrip('/') == ziel:
            return zeile
    return None


def fstab_ergaenzen(zeile: str, pfad: str = FSTAB) -> None:
    """Zeile anhängen — mit Sicherungskopie und abschließendem Zeilenumbruch.

    Eine fstab ohne Zeilenumbruch am Ende verschmilzt sonst mit dem neuen
    Eintrag zu einer unlesbaren Zeile, und der Pi bootet nicht mehr sauber.
    """
    sicherung = f'{pfad}.s0counter-backup'
    if os.path.exists(pfad) and not os.path.exists(sicherung):
        shutil.copy2(pfad, sicherung)

    try:
        with open(pfad, encoding='utf-8') as f:
            alt = f.read()
    except OSError:
        alt = ''

    neu = alt
    if neu and not neu.endswith('\n'):
        neu += '\n'
    neu += zeile

    with open(pfad, 'w', encoding='utf-8') as f:
        f.write(neu)


def fstab_zeile_entfernen(zeile: str, pfad: str = FSTAB) -> None:
    """Die eigene Zeile wieder herausnehmen (Rückbau bei Fehlschlag).

    Wichtig, damit ein misslungener Versuch keinen fstab-Eintrag hinterlässt,
    der beim nächsten Neustart ins Leere zeigt.
    """
    try:
        with open(pfad, encoding='utf-8') as f:
            zeilen = f.readlines()
    except OSError:
        return
    behalten = [z for z in zeilen if z.strip() != zeile.strip()]
    with open(pfad, 'w', encoding='utf-8') as f:
        f.writelines(behalten)


def eingebunden(mountpunkt: str) -> bool:
    """Die eigentliche Probe. `mount` meldet auch Erfolg, wenn nichts kam."""
    code, _ = _lauf(['mountpoint', '-q', mountpunkt])
    if code == 127:
        return os.path.ismount(mountpunkt)
    return code == 0


def einbinden(mountpunkt: str) -> tuple[bool, str]:
    """`mount` aufrufen und danach nachsehen, ob wirklich etwas da ist."""
    os.makedirs(mountpunkt, exist_ok=True)
    code, ausgabe = _lauf(['mount', mountpunkt], timeout=60)
    if eingebunden(mountpunkt):
        return True, ausgabe
    if code == 0:
        # Genau der Fall, vor dem die alte Anleitung gewarnt hat.
        return False, (ausgabe or
                       'mount meldete Erfolg, es ist aber nichts eingebunden')
    return False, ausgabe or f'mount endete mit Code {code}'


def aushaengen(mountpunkt: str) -> None:
    _lauf(['umount', mountpunkt], timeout=30)


def schreibprobe(mountpunkt: str) -> tuple[bool, str]:
    """Eingebunden heißt nicht beschreibbar.

    Eine Freigabe kann schreibgeschützt hängen — dann läuft die Sicherung ins
    Leere und meldet trotzdem nichts. Deshalb eine echte Datei schreiben,
    lesen und wieder löschen.
    """
    probe = os.path.join(mountpunkt, '.s0counter-schreibtest')
    try:
        with open(probe, 'w', encoding='utf-8') as f:
            f.write('s0counter')
        with open(probe, encoding='utf-8') as f:
            if f.read() != 's0counter':
                return False, 'Zurückgelesener Inhalt weicht ab'
        os.unlink(probe)
        return True, ''
    except OSError as exc:
        try:
            os.unlink(probe)
        except OSError:
            pass
        return False, str(exc)


def fehlergrund(ausgabe: str) -> str:
    """Aus der mount-Ausgabe einen brauchbaren Satz machen.

    Die Meldungen von mount.cifs sind für Menschen unbrauchbar
    ("wrong fs type, bad option..."); die Ursache steht als Status im
    Kernel-Log. Die häufigen drei werden hier übersetzt.
    """
    text = ausgabe.lower()
    if 'permission denied' in text or 'status=13' in text:
        return ('Benutzername oder Passwort stimmt nicht — oder die Freigabe '
                'ist für diesen Benutzer nicht freigegeben.')
    if 'no such file or directory' in text or 'status=2' in text:
        return ('Der Freigabename stimmt nicht. In DSM unter '
                'Systemsteuerung → Freigegebene Ordner nachsehen.')
    if 'host is down' in text or 'connection' in text or 'unreachable' in text:
        return ('Die NAS antwortet nicht. Läuft sie, und ist SMB in DSM '
                'eingeschaltet?')
    if 'wrong fs type' in text and not cifs_vorhanden():
        return 'Das Paket cifs-utils fehlt: apt install cifs-utils'
    if 'protocol not supported' in text or 'invalid argument' in text:
        return 'Die NAS spricht diese SMB-Fassung nicht.'
    return ausgabe.strip() or 'kein Grund genannt'


def probeeinbinden(host: str, freigabe: str, mountpunkt: str,
                   pfad: str | None = None) -> tuple:
    """Fassungen der Reihe nach probieren, bis eine trägt.

    Rückgabe: (fassung oder None, letzte Ausgabe). Der fstab-Eintrag wird
    dafür jeweils gesetzt und bei Fehlschlag wieder entfernt — `mount` liest
    die Optionen aus der fstab, ein Probelauf ohne Eintrag wäre ein zweiter
    Weg mit anderen Optionen und damit keine gültige Probe.

    `pfad` wird durchgereicht statt aus dem Vorgabewert der aufgerufenen
    Funktionen zu kommen: ein Vorgabewert wird bei der Definition gebunden
    und ließe sich im Test nicht ersetzen — die Tests hätten dann die echte
    /etc/fstab beschrieben.
    """
    ziel = pfad or FSTAB
    letzte = ''
    for fassung in SMB_FASSUNGEN:
        zeile = fstab_zeile(host, freigabe, mountpunkt, fassung)
        fstab_ergaenzen(zeile, ziel)
        erfolg, ausgabe = einbinden(mountpunkt)
        if erfolg:
            return fassung, ausgabe
        letzte = ausgabe
        fstab_zeile_entfernen(zeile, ziel)
    return None, letzte
