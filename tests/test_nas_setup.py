"""Der NAS-Assistent schreibt an /etc — diese Tests prüfen, dass er das
richtig macht und nach einem Fehlschlag nichts zurücklässt.

Kein einziger Test fasst das echte /etc/fstab an: jede Funktion nimmt den
Pfad als Parameter. Ein Test, der die echte fstab beschreibt, macht den
Entwicklungsrechner unbootbar — das ist keine hypothetische Sorge.
"""
import os

import pytest

from s0counter import nas_setup as nas


@pytest.fixture
def fstab(tmp_path):
    pfad = tmp_path / 'fstab'
    pfad.write_text('/dev/sda1 / ext4 defaults 0 1\n', encoding='utf-8')
    return str(pfad)


class TestZugangsdaten:
    def test_modus_600_und_inhalt(self, tmp_path):
        ziel = str(tmp_path / 'unter' / 'nas.cred')
        nas.zugangsdaten_schreiben('willi', 'geheim', ziel)

        assert os.stat(ziel).st_mode & 0o777 == 0o600
        inhalt = open(ziel, encoding='utf-8').read()
        assert 'username=willi' in inhalt
        assert 'password=geheim' in inhalt

    def test_passwort_mit_sonderzeichen_bleibt_unveraendert(self, tmp_path):
        # cifs liest die Datei zeilenweise und roh. Ein Passwort mit
        # Gleichheitszeichen oder Doppelpunkt darf nicht angefasst werden.
        ziel = str(tmp_path / 'nas.cred')
        nas.zugangsdaten_schreiben('willi', 'a=b:c#d', ziel)
        assert 'password=a=b:c#d\n' in open(ziel, encoding='utf-8').read()

    def test_ueberschreiben_behaelt_modus(self, tmp_path):
        ziel = str(tmp_path / 'nas.cred')
        nas.zugangsdaten_schreiben('a', 'b', ziel)
        nas.zugangsdaten_schreiben('c', 'd', ziel)
        assert os.stat(ziel).st_mode & 0o777 == 0o600
        assert 'username=c' in open(ziel, encoding='utf-8').read()

    def test_zu_offener_modus_bricht_ab(self, tmp_path, monkeypatch):
        """Die Nachprüfung des Modus muss wirklich zuschlagen.

        Ohne diesen Test ist sie unerreichbar: `mkstemp` legt von sich aus
        mit 0600 an, und `fchmod` lässt sich abschalten, ohne dass ein Test
        rot wird. Die Prüfung ist trotzdem nötig — sie ist die Zusicherung
        für jeden Weg, der die Datei anders erzeugt. Belegt am 26.08.2026
        durch eine Sabotage, die grün blieb.
        """
        ziel = str(tmp_path / 'nas.cred')
        echtes_fchmod = os.fchmod
        monkeypatch.setattr(os, 'fchmod', lambda *a: None)
        # Der Umweg über chmod nach dem Umbenennen bildet nach, was ein
        # Dateisystem ohne Rechteverwaltung täte.
        monkeypatch.setattr(os, 'replace', lambda a, b: (
            echtes_fchmod(os.open(a, os.O_RDONLY), 0o644),
            os.rename(a, b))[-1])

        with pytest.raises(nas.SetupFehler, match='600'):
            nas.zugangsdaten_schreiben('willi', 'geheim', ziel)


class TestFstab:
    def test_zeile_hat_die_pflichtoptionen(self):
        zeile = nas.fstab_zeile('192.168.0.40', 'daten', '/mnt/nas', '3.0')
        # Ohne nofail bleibt der Pi beim Booten hängen, wenn die NAS aus
        # ist. Ohne _netdev wird vor dem Netzwerk eingebunden.
        assert 'nofail' in zeile
        assert '_netdev' in zeile
        assert 'vers=3.0' in zeile
        assert f'credentials={nas.CRED}' in zeile
        assert zeile.startswith('//192.168.0.40/daten /mnt/nas cifs ')
        assert zeile.count('\n') == 1 and zeile.endswith('\n')

    def test_findet_eigenen_eintrag(self, fstab):
        nas.fstab_ergaenzen(
            nas.fstab_zeile('h', 'f', '/mnt/nas', '3.0'), fstab)
        assert nas.fstab_eintrag_finden('/mnt/nas', fstab) is not None

    def test_findet_fremden_eintrag_am_selben_punkt(self, fstab):
        # Ein Eintrag mit anderem Host ist derselbe Konflikt: verglichen
        # wird der Einhängepunkt, nicht die ganze Zeile.
        with open(fstab, 'a', encoding='utf-8') as f:
            f.write('//alt/share /mnt/nas cifs defaults 0 0\n')
        assert nas.fstab_eintrag_finden('/mnt/nas', fstab) is not None

    def test_schraegstrich_am_ende_ist_derselbe_punkt(self, fstab):
        nas.fstab_ergaenzen(
            nas.fstab_zeile('h', 'f', '/mnt/nas', '3.0'), fstab)
        assert nas.fstab_eintrag_finden('/mnt/nas/', fstab) is not None

    def test_kommentar_zaehlt_nicht_als_eintrag(self, fstab):
        with open(fstab, 'a', encoding='utf-8') as f:
            f.write('# //alt/share /mnt/nas cifs defaults 0 0\n')
        assert nas.fstab_eintrag_finden('/mnt/nas', fstab) is None

    def test_gruen_ohne_eintrag(self, fstab):
        # Gegenprobe nach GRÜN: findet die Suche überall etwas, wäre der
        # Assistent nicht benutzbar — er würde immer nach Ersetzen fragen.
        assert nas.fstab_eintrag_finden('/mnt/nas', fstab) is None

    def test_fehlender_zeilenumbruch_verschmilzt_nicht(self, tmp_path):
        pfad = str(tmp_path / 'fstab')
        with open(pfad, 'w', encoding='utf-8') as f:
            f.write('/dev/sda1 / ext4 defaults 0 1')   # ohne \n
        nas.fstab_ergaenzen(
            nas.fstab_zeile('h', 'f', '/mnt/nas', '3.0'), pfad)

        zeilen = open(pfad, encoding='utf-8').read().splitlines()
        assert len(zeilen) == 2
        assert zeilen[0] == '/dev/sda1 / ext4 defaults 0 1'

    def test_sicherungskopie_wird_angelegt(self, fstab):
        nas.fstab_ergaenzen(
            nas.fstab_zeile('h', 'f', '/mnt/nas', '3.0'), fstab)
        assert os.path.exists(f'{fstab}.s0counter-backup')

    def test_entfernen_laesst_den_rest_stehen(self, fstab):
        zeile = nas.fstab_zeile('h', 'f', '/mnt/nas', '3.0')
        nas.fstab_ergaenzen(zeile, fstab)
        nas.fstab_zeile_entfernen(zeile, fstab)

        inhalt = open(fstab, encoding='utf-8').read()
        assert '/mnt/nas' not in inhalt
        assert '/dev/sda1 / ext4' in inhalt


class TestProbeeinbinden:
    """Die Fassungen werden durchprobiert und Fehlschläge zurückgebaut."""

    def test_erfolg_bei_der_ersten_fassung(self, fstab, monkeypatch):
        monkeypatch.setattr(nas, 'einbinden', lambda p: (True, ''))

        fassung, _ = nas.probeeinbinden('h', 'f', '/mnt/nas', fstab)
        assert fassung == nas.SMB_FASSUNGEN[0]
        assert f'vers={fassung}' in open(fstab, encoding='utf-8').read()

    def test_faellt_auf_aeltere_fassung_zurueck(self, fstab, monkeypatch):
        versuche = []

        def einbinden(_pfad):
            versuche.append(len(versuche))
            return (len(versuche) == 3, 'protocol not supported')

        monkeypatch.setattr(nas, 'einbinden', einbinden)
        fassung, _ = nas.probeeinbinden('h', 'f', '/mnt/nas', fstab)

        assert fassung == nas.SMB_FASSUNGEN[2]
        inhalt = open(fstab, encoding='utf-8').read()
        # Genau EIN Eintrag, nicht drei: die gescheiterten müssen weg sein.
        assert inhalt.count('/mnt/nas') == 1
        assert f'vers={nas.SMB_FASSUNGEN[2]}' in inhalt

    def test_kompletter_fehlschlag_laesst_nichts_zurueck(self, fstab,
                                                        monkeypatch):
        # Das ist der wichtige Fall: ein Eintrag, der nach einem
        # gescheiterten Versuch stehenbliebe, zeigt beim nächsten Neustart
        # ins Leere.
        monkeypatch.setattr(nas, 'einbinden',
                            lambda p: (False, 'permission denied'))

        fassung, ausgabe = nas.probeeinbinden('h', 'f', '/mnt/nas', fstab)
        assert fassung is None
        assert 'permission denied' in ausgabe
        assert '/mnt/nas' not in open(fstab, encoding='utf-8').read()


class TestSchreibprobe:
    def test_beschreibbarer_ordner_besteht(self, tmp_path):
        ok, grund = nas.schreibprobe(str(tmp_path))
        assert ok, grund
        # Die Probedatei darf nicht liegenbleiben.
        assert not os.path.exists(
            os.path.join(str(tmp_path), '.s0counter-schreibtest'))

    def test_schreibgeschuetzter_ordner_faellt_durch(self, tmp_path):
        # Eingebunden heißt nicht beschreibbar — eine schreibgeschützte
        # Freigabe ließe die Sicherung still ins Leere laufen.
        ordner = tmp_path / 'ro'
        ordner.mkdir()
        os.chmod(ordner, 0o500)
        try:
            ok, grund = nas.schreibprobe(str(ordner))
            if os.geteuid() == 0:
                pytest.skip('als root sind Rechte wirkungslos')
            assert not ok
            assert grund
        finally:
            os.chmod(ordner, 0o700)

    def test_fehlender_ordner_faellt_durch(self, tmp_path):
        ok, _ = nas.schreibprobe(str(tmp_path / 'gibtsnicht'))
        assert not ok


class TestFehlergrund:
    """Aus der mount-Ausgabe muss ein brauchbarer Satz werden."""

    @pytest.mark.parametrize('ausgabe, erwartet', [
        ('mount error(13): Permission denied', 'Passwort'),
        ('mount error(2): No such file or directory', 'Freigabename'),
        ('mount error(112): Host is down', 'antwortet nicht'),
        ('mount error: Protocol not supported', 'SMB-Fassung'),
    ])
    def test_haeufige_gruende_werden_uebersetzt(self, ausgabe, erwartet):
        assert erwartet in nas.fehlergrund(ausgabe)

    def test_unbekannter_grund_wird_durchgereicht(self):
        # Nicht verschlucken: eine unbekannte Meldung ist besser als ein
        # erfundener Grund.
        assert 'irgendwas seltsames' in nas.fehlergrund('irgendwas seltsames')

    def test_leere_ausgabe_sagt_das(self):
        assert nas.fehlergrund('') == 'kein Grund genannt'


class TestFreigabenAuflisten:
    def test_ohne_smbclient_leere_liste_statt_ausnahme(self, monkeypatch):
        monkeypatch.setattr(nas.shutil, 'which', lambda _n: None)
        assert nas.freigaben_auflisten('h', 'u', 'p') == []

    def test_verwaltungsfreigaben_fliegen_raus(self, monkeypatch):
        monkeypatch.setattr(nas.shutil, 'which', lambda _n: '/usr/bin/smbclient')

        class Fertig:
            stdout = ('\n\tSharename       Type      Comment\n'
                      '\t---------       ----      -------\n'
                      '\tdaten           Disk      \n'
                      '\tvideo           Disk      \n'
                      '\tIPC$            IPC       IPC Service\n'
                      '\thomes$          Disk      \n')
            stderr = ''
            returncode = 0

        monkeypatch.setattr(nas.subprocess, 'run',
                            lambda *a, **k: Fertig())
        assert nas.freigaben_auflisten('h', 'u', 'p') == ['daten', 'video']

    def test_smbclient_vorhanden_meldet_das_fehlen(self, monkeypatch):
        monkeypatch.setattr(nas.shutil, 'which', lambda _n: None)
        assert nas.smbclient_vorhanden() is False
        monkeypatch.setattr(nas.shutil, 'which',
                            lambda _n: '/usr/bin/smbclient')
        assert nas.smbclient_vorhanden() is True

    def test_passwort_steht_nicht_in_der_kommandozeile(self, monkeypatch):
        # Sonst läse es jeder mit `ps aux` mit, solange der Aufruf läuft.
        monkeypatch.setattr(nas.shutil, 'which', lambda _n: '/usr/bin/smbclient')
        gesehen = {}

        class Fertig:
            stdout = ''
            stderr = ''
            returncode = 0

        def run(befehl, **kwargs):
            gesehen['befehl'] = befehl
            gesehen['env'] = kwargs.get('env', {})
            return Fertig()

        monkeypatch.setattr(nas.subprocess, 'run', run)
        nas.freigaben_auflisten('h', 'willi', 'streng-geheim')

        assert 'streng-geheim' not in ' '.join(gesehen['befehl'])
        assert gesehen['env'].get('PASSWD') == 'streng-geheim'


class TestFreigabenamePruefen:
    """Der Fall vom 27.08.2026: eingegeben wurde /volume2/Backup/Raspberry
    statt Backup. Das Einbinden scheiterte danach mit einer Meldung über die
    SMB-Fassung — also an einer Stelle, die mit der Ursache nichts zu tun
    hat.
    """

    def test_pfad_wird_beanstandet(self):
        grund = nas.freigabename_pruefen('/volume2/Backup/Raspberry')
        assert grund is not None
        assert 'Schrägstriche' in grund

    def test_beanstandung_nennt_den_richtigen_namen(self):
        # Ohne den Vorschlag muss der Mensch selbst draufkommen, welcher
        # Teil des Pfades die Freigabe ist.
        grund = nas.freigabename_pruefen('/volume2/Backup/Raspberry')
        assert 'volume2' in grund

    def test_rueckwaertsschraegstrich_zaehlt_auch(self):
        assert nas.freigabename_pruefen('\\\\nas\\Backup') is not None

    def test_datentraeger_statt_freigabe(self):
        grund = nas.freigabename_pruefen('volume2')
        assert grund is not None
        assert 'Datenträger' in grund

    # Gegenprobe in Richtung grün: ein Gate, das den gesunden Fall rot
    # meldet, wird abgeschaltet und schützt danach gar nichts mehr.
    @pytest.mark.parametrize('name', [
        'Backup',           # der tatsächliche Name in dieser Anlage
        'daten',
        'Media',
        'home',
        'Proxmox',
        'surveillance',
        'Backup-2026',      # Bindestriche sind erlaubt
        'Backup_Pi',        # Unterstriche auch
        'Ordner mit Leerzeichen',
        'volumen',          # kein Datenträger: keine Ziffer dahinter
        'volume',           # ebenfalls nicht
    ])
    def test_gesunde_namen_bleiben_unbeanstandet(self, name):
        assert nas.freigabename_pruefen(name) is None

    def test_leere_eingabe_ist_kein_namensfehler(self):
        # Die fängt der Aufrufer eigene Meldung ab ("ohne geht es nicht").
        assert nas.freigabename_pruefen('') is None
