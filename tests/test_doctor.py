"""Der Doctor prüft die Anlage — diese Tests prüfen den Doctor.

Besonders wichtig ist hier die Richtung *grün*: Ein Prüfwerkzeug, das im
gesunden Fall meckert, wird abgeschaltet und schützt danach gar nichts mehr.
Deshalb steht zu jeder Fehlermeldung ein Test, der belegt, dass sie bei
gesunden Daten ausbleibt.
"""
import os
import time

import pytest

from s0counter import doctor
from s0counter.config import from_dict
from s0counter.db import Database


@pytest.fixture
def config(tmp_path):
    return from_dict({
        'general': {'database': str(tmp_path / 'd.sqlite3')},
        'meters': {'eg': {'name': 'Strom EG', 'gpio': 17}},
    })


@pytest.fixture
def db(config):
    datenbank = Database(config.database)
    datenbank.ensure_meter('eg', 'Strom EG', 1000)
    yield datenbank
    datenbank.close()


def stufen(befunde):
    return {b.titel: b.stufe for b in befunde}


class TestErfassung:
    def test_frischer_impuls_ist_in_ordnung(self, db, config):
        db.add_pulse('eg', time.time() - 30, 30.0)
        befunde = doctor.pruefe_erfassung(db, config)
        assert all(b.stufe == doctor.OK for b in befunde), stufen(befunde)

    def test_stiller_zaehler_faellt_auf(self, db, config):
        """Der teuerste Fehler dieser Anlage: fällt sonst erst der Abrechnung auf."""
        db.add_pulse('eg', time.time() - 9 * 3600, 1.0)
        befunde = doctor.pruefe_erfassung(db, config)
        assert any(b.stufe == doctor.FEHLT for b in befunde), stufen(befunde)

    def test_zaehler_ohne_impuls_faellt_auf(self, db, config):
        befunde = doctor.pruefe_erfassung(db, config)
        assert [b.stufe for b in befunde] == [doctor.FEHLT]
        assert 'nie' in befunde[0].detail

    def test_grenzfall_knapp_darunter_bleibt_gruen(self, db, config):
        """Nachts verbraucht eine Wohnung wenig — 5 h Stille sind normal.

        Die Gegenprobe in Richtung grün: eine zu enge Schwelle würde jeden
        Morgen Alarm schlagen und den Doctor unbrauchbar machen.
        """
        db.add_pulse('eg', time.time() - 5 * 3600, 1.0)
        befunde = doctor.pruefe_erfassung(db, config)
        assert all(b.stufe == doctor.OK for b in befunde), stufen(befunde)


class TestSicherung:
    def test_fehlende_sicherung_faellt_auf(self, config, tmp_path):
        befunde = doctor.pruefe_sicherung(
            config, str(tmp_path / 'nas'), str(tmp_path / 'lokal'))
        vorhanden = [b for b in befunde if b.titel == 'Sicherung vorhanden']
        assert vorhanden and vorhanden[0].stufe == doctor.FEHLT

    def test_frische_sicherung_ist_in_ordnung(self, config, tmp_path):
        lokal = tmp_path / 'lokal'
        lokal.mkdir()
        (lokal / 'impulse-2026-08-25_0300.sqlite3').write_bytes(b'SQLite format 3\0' + b'\0' * 200)
        befunde = doctor.pruefe_sicherung(
            config, str(tmp_path / 'nas'), str(lokal))
        aktuell = [b for b in befunde if b.titel == 'Sicherung aktuell']
        assert aktuell and aktuell[0].stufe == doctor.OK

    def test_alte_sicherung_faellt_auf(self, config, tmp_path):
        """Eine Datei, die es gibt, ist noch keine, die aktuell ist."""
        lokal = tmp_path / 'lokal'
        lokal.mkdir()
        datei = lokal / 'impulse-alt.sqlite3'
        datei.write_bytes(b'SQLite format 3\0' + b'\0' * 200)
        alt = time.time() - 5 * 86400
        os.utime(datei, (alt, alt))
        befunde = doctor.pruefe_sicherung(
            config, str(tmp_path / 'nas'), str(lokal))
        aktuell = [b for b in befunde if b.titel == 'Sicherung aktuell']
        assert aktuell and aktuell[0].stufe == doctor.FEHLT

    def test_leere_datei_zaehlt_nicht_als_sicherung(self, config, tmp_path):
        """Der Fall vom 27.08.2026: 0 Bytes auf der Synology.

        `sqlite3.connect()` legte die Datei an, das Schreiben hing dann in
        den CIFS-Dateisperren. Der Doctor meldete sie als „Sicherung
        aktuell — 1 Stück" und der Befund sah grün aus, obwohl gar nichts
        gesichert war.
        """
        lokal = tmp_path / 'lokal'
        lokal.mkdir()
        (lokal / 'impulse-2026-08-27_1125.sqlite3').write_bytes(b'')
        befunde = doctor.pruefe_sicherung(
            config, str(tmp_path / 'nas'), str(lokal))

        lesbar = [b for b in befunde if b.titel == 'Sicherungen sind lesbar']
        assert lesbar and lesbar[0].stufe == doctor.FEHLT
        # Und sie darf nicht zusätzlich als gültige Sicherung durchgehen.
        aktuell = [b for b in befunde if b.titel == 'Sicherung aktuell']
        assert not aktuell, 'leere Datei wurde als aktuelle Sicherung gezaehlt'
        vorhanden = [b for b in befunde if b.titel == 'Sicherung vorhanden']
        assert vorhanden and vorhanden[0].stufe == doctor.FEHLT

    def test_abgeschnittene_datei_zaehlt_nicht(self, config, tmp_path):
        # Ein paar Bytes sind noch kein SQLite-Kopf (der ist 100 Byte lang).
        lokal = tmp_path / 'lokal'
        lokal.mkdir()
        (lokal / 'impulse-halb.sqlite3').write_bytes(b'SQLite fo')
        befunde = doctor.pruefe_sicherung(
            config, str(tmp_path / 'nas'), str(lokal))
        lesbar = [b for b in befunde if b.titel == 'Sicherungen sind lesbar']
        assert lesbar and lesbar[0].stufe == doctor.FEHLT

    def test_gesunde_sicherung_loest_keinen_fehlalarm_aus(self, config,
                                                         tmp_path):
        """Gegenprobe nach GRÜN: ein Gate, das den Normalfall rot meldet,
        wird abgeschaltet und schützt danach gar nichts mehr."""
        lokal = tmp_path / 'lokal'
        lokal.mkdir()
        (lokal / 'impulse-2026-08-27_0300.sqlite3').write_bytes(
            b'SQLite format 3\0' + b'\0' * 4000)
        befunde = doctor.pruefe_sicherung(
            config, str(tmp_path / 'nas'), str(lokal))

        lesbar = [b for b in befunde if b.titel == 'Sicherungen sind lesbar']
        assert not lesbar, 'Fehlalarm auf eine gesunde Sicherung'
        aktuell = [b for b in befunde if b.titel == 'Sicherung aktuell']
        assert aktuell and aktuell[0].stufe == doctor.OK

    def test_leere_neben_gesunder_wird_trotzdem_gemeldet(self, config,
                                                        tmp_path):
        # Die gesunde Datei darf die kaputte nicht verdecken — sonst
        # bleibt der Torso liegen und die Rotation zaehlt ihn mit.
        lokal = tmp_path / 'lokal'
        lokal.mkdir()
        (lokal / 'impulse-gut.sqlite3').write_bytes(
            b'SQLite format 3\0' + b'\0' * 4000)
        (lokal / 'impulse-leer.sqlite3').write_bytes(b'')
        befunde = doctor.pruefe_sicherung(
            config, str(tmp_path / 'nas'), str(lokal))

        lesbar = [b for b in befunde if b.titel == 'Sicherungen sind lesbar']
        assert lesbar and lesbar[0].stufe == doctor.FEHLT
        aktuell = [b for b in befunde if b.titel == 'Sicherung aktuell']
        assert aktuell and aktuell[0].stufe == doctor.OK

    def test_fehlende_nas_nennt_den_hilfebefehl(self, config, tmp_path):
        befunde = doctor.pruefe_sicherung(
            config, str(tmp_path / 'nas'), str(tmp_path / 'lokal'))
        ziel = [b for b in befunde if 'eingebunden' in b.titel]
        assert ziel and 'nas-hilfe' in ziel[0].hilfe


class TestNichtGemessen:
    """Der dritte Ausgang: nicht messbar ist kein Fehler.

    Ohne ihn wäre der Doctor auf dem Entwicklungsrechner dauerhaft rot —
    und ein Werkzeug, das immer rot ist, wird abgeschaltet.
    """

    def test_fehlende_unit_ist_unklar_nicht_fehlt(self, config, monkeypatch):
        monkeypatch.setattr(doctor, '_systemctl',
                            lambda *a: (4, 'not-found'))
        befunde = doctor.pruefe_dienst(config)
        assert [b.stufe for b in befunde] == [doctor.UNKLAR]

    def test_ohne_systemctl_ist_unklar(self, config, monkeypatch):
        monkeypatch.setattr(doctor, '_systemctl', lambda *a: (127, ''))
        befunde = doctor.pruefe_dienst(config)
        assert all(b.stufe == doctor.UNKLAR for b in befunde)

    def test_laufender_dienst_ist_gruen(self, config, monkeypatch):
        """Gegenprobe: bei gesunder Anlage darf nichts rot sein."""
        def fake(*args):
            return (0, 'active' if args[0] == 'is-active' else 'enabled')
        monkeypatch.setattr(doctor, '_systemctl', fake)
        befunde = doctor.pruefe_dienst(config)
        assert all(b.stufe == doctor.OK for b in befunde), stufen(befunde)

    def test_abgeschaltete_bilanz_ist_unklar(self, config):
        befunde = doctor.pruefe_bilanzquelle(config)
        assert befunde[0].stufe == doctor.UNKLAR

    def test_unklar_macht_den_lauf_nicht_rot(self, db, config, monkeypatch):
        """Der Kern des dritten Ausgangs."""
        monkeypatch.setattr(doctor, '_systemctl', lambda *a: (127, ''))
        db.add_pulse('eg', time.time() - 10, 10.0)
        befunde = doctor.alle_pruefungen(config, db, '/nirgends', '/auch-nicht')
        assert any(b.stufe == doctor.UNKLAR for b in befunde)
        # Der Aufrufer wertet nur FEHLT als Fehler — hier prüfen wir, dass
        # die Sicherung der einzige echte Befund bleibt.
        #
        # Gefiltert wird nach Bereich, nicht nach Titel: der Titel der
        # Speicherprüfung ist der Pfad der Datenbank, und der liegt im Test
        # unter `tmp_path`. Auf einem Rechner mit über 85 % belegter Platte
        # meldet sie zu Recht FEHLT — mit einem Titel, der bei jedem Lauf
        # anders heißt. Eine Titelliste hätte damit die Belegung des
        # Testrechners mitgeprüft statt den dritten Ausgang.
        offen = {b.bereich for b in befunde if b.stufe == doctor.FEHLT}
        assert offen <= {'Sicherung', 'Speicher'}


class TestRobustheit:
    def test_eine_kaputte_pruefung_bricht_nicht_alles_ab(
            self, db, config, monkeypatch):
        """Gerade wenn viel im Argen liegt, will man die ganze Liste sehen."""
        def explodiert(*args, **kwargs):
            raise RuntimeError('absichtlich kaputt')

        monkeypatch.setattr(doctor, 'pruefe_speicher', explodiert)
        befunde = doctor.alle_pruefungen(config, db, '/x', '/y')
        assert any(b.titel == 'Prüfung abgebrochen' for b in befunde)
        # Und die Prüfungen dahinter laufen trotzdem:
        assert any(b.bereich == 'Datenbank' for b in befunde)

    def test_aggregat_abweichung_faellt_auf(self, db):
        db.add_pulse('eg', 1000.0, 1.0)
        db.conn.execute('DELETE FROM pulse_minutes')
        db.conn.commit()
        befunde = doctor.pruefe_aggregat(db)
        assert befunde[0].stufe == doctor.FEHLT

    def test_stimmiges_aggregat_ist_gruen(self, db):
        db.add_pulse('eg', 1000.0, 1.0)
        befunde = doctor.pruefe_aggregat(db)
        assert befunde[0].stufe == doctor.OK


class TestNasHilfe:
    def test_anleitung_nennt_die_wesentlichen_schritte(self):
        text = doctor.NAS_HILFE
        for stichwort in ('nas.cred', 'fstab', 'nofail', 'mountpoint',
                          's0counter-backup.service', 's0ctl doctor'):
            assert stichwort in text, stichwort

    def test_anleitung_warnt_vor_dem_token(self):
        """Die gesicherte Konfiguration enthält Zugangsdaten."""
        assert 'Token' in doctor.NAS_HILFE


class TestAngebotAmEnde:
    """Der Doctor darf am Ende anbieten, das Fehlende einzurichten.

    Zwei Dinge müssen dabei stimmen, und beide sind schon einmal
    schiefgegangen:

      * Ohne Terminal darf NICHTS gefragt werden — sonst hängt jeder
        Cron-Lauf und jede Pipe an einer Eingabeaufforderung.
      * Der genannte Befehl muss es wirklich geben. Ein Hinweis auf einen
        Unterbefehl, den niemand aufrufen kann, ist dieselbe Falle wie ein
        Werkzeug ohne Weg dorthin.
    """

    def test_ohne_terminal_wird_nichts_gefragt(self, db, config, monkeypatch,
                                               capsys):
        import s0ctl

        monkeypatch.setattr(s0ctl.sys.stdin, 'isatty', lambda: False)

        def nicht_fragen(*a, **k):
            raise AssertionError('Ohne Terminal darf nicht gefragt werden')

        monkeypatch.setattr('builtins.input', nicht_fragen)

        class Args:
            nas_hilfe = False
            backup_ziel = '/mnt/gibtsnicht/s0counter'
            backup_ersatz = '/mnt/auchnicht'
            nicht_fragen = False

        # Rot, weil die Sicherung fehlt — aber ohne Rückfrage.
        assert s0ctl.cmd_doctor(Args(), config, db) == 1
        assert 'nas-setup' in capsys.readouterr().out

    def test_nicht_fragen_unterdrueckt_das_angebot(self, db, config,
                                                   monkeypatch):
        import s0ctl

        monkeypatch.setattr(s0ctl.sys.stdin, 'isatty', lambda: True)
        monkeypatch.setattr('builtins.input', lambda *a: (_ for _ in ()).throw(
            AssertionError('--nicht-fragen wurde ignoriert')))

        class Args:
            nas_hilfe = False
            backup_ziel = '/mnt/gibtsnicht/s0counter'
            backup_ersatz = '/mnt/auchnicht'
            nicht_fragen = True

        assert s0ctl.cmd_doctor(Args(), config, db) == 1

    def test_mit_terminal_wird_angeboten(self, db, config, monkeypatch):
        # Gegenprobe nach GRÜN: unterdrückt der Doctor das Angebot immer,
        # ist die ganze Bequemlichkeit tot und niemand merkt es.
        import s0ctl

        monkeypatch.setattr(s0ctl.sys.stdin, 'isatty', lambda: True)
        gefragt = []

        def antwort(frage):
            gefragt.append(frage)
            return 'n'

        monkeypatch.setattr('builtins.input', antwort)

        class Args:
            nas_hilfe = False
            backup_ziel = '/mnt/gibtsnicht/s0counter'
            backup_ersatz = '/mnt/auchnicht'
            nicht_fragen = False

        s0ctl.cmd_doctor(Args(), config, db)
        assert gefragt, 'Es wurde nichts angeboten'
        assert any('NAS' in f for f in gefragt)

    def test_bei_ablehnung_steht_der_befehl_da(self, monkeypatch, capsys):
        """Wer ablehnt, muss den Befehl zum Selbermachen bekommen.

        Geprüft wird gezielt `_doctor_angebot`, nicht der ganze Doctor: der
        Doctor-Bericht nennt den Befehl ohnehin im Befund, und ein Test auf
        die Gesamtausgabe bliebe deshalb auch dann grün, wenn dieser Hinweis
        wegfiele. Belegt am 26.08.2026 mit einer Sabotage, die grün blieb.
        """
        import s0ctl

        monkeypatch.setattr('builtins.input', lambda *a: 'n')

        class Args:
            backup_ziel = '/mnt/gibtsnicht/s0counter'

        offen = [doctor.Befund('Sicherung', 'Ziel /x eingebunden',
                               doctor.FEHLT)]
        s0ctl._doctor_angebot(offen, Args())

        ausgabe = capsys.readouterr().out
        assert 'Später von Hand' in ausgabe
        assert 'sudo s0ctl nas-setup' in ausgabe

    def test_jeder_angebotene_befehl_existiert(self):
        """Der genannte Unterbefehl muss wirklich aufrufbar sein.

        Aus dem Parser GEPARST, nicht hier gepflegt: eine handgeführte
        Liste bliebe nach einer Umbenennung stehen.
        """
        import s0ctl

        assert s0ctl.ANGEBOTE, 'keine Angebote hinterlegt'
        for angebot in s0ctl.ANGEBOTE:
            teile = angebot['befehl'].split()
            # "sudo s0ctl nas-setup" -> "nas-setup"
            unterbefehl = teile[teile.index('s0ctl') + 1]
            assert unterbefehl in s0ctl.unterbefehle(), \
                f'{unterbefehl} gibt es nicht'
