"""Systemwerte des Rechners: Temperatur, Last, Speicher, Ablage.

Die Werte kommen aus /sys und /proc. Geprüft wird gegen untergeschobene
Dateien, damit die Tests auf jedem Rechner laufen — auch auf einem ohne
Thermalzone.
"""
import os

import pytest

from s0counter import system


class TestTemperatur:
    def test_waermste_zone_gewinnt(self, tmp_path, monkeypatch):
        """Eine kühle Zone darf eine heiße nicht verdecken."""
        for name, wert in (('thermal_zone0', 42000), ('thermal_zone1', 71500)):
            zone = tmp_path / name
            zone.mkdir()
            (zone / 'temp').write_text(str(wert))
        monkeypatch.setattr(system, '_THERMAL', str(tmp_path))
        assert system.temperatur_c() == 71.5

    def test_milligrad_werden_umgerechnet(self, tmp_path, monkeypatch):
        zone = tmp_path / 'thermal_zone0'
        zone.mkdir()
        (zone / 'temp').write_text('49926')
        monkeypatch.setattr(system, '_THERMAL', str(tmp_path))
        assert system.temperatur_c() == pytest.approx(49.926)

    def test_ohne_thermalzone_none(self, tmp_path, monkeypatch):
        """Kein Messwert ist None, nicht 0 — sonst sieht es kühl aus."""
        monkeypatch.setattr(system, '_THERMAL', str(tmp_path / 'gibtsnicht'))
        assert system.temperatur_c() is None

    def test_unlesbarer_wert_wird_uebersprungen(self, tmp_path, monkeypatch):
        gut = tmp_path / 'thermal_zone0'
        gut.mkdir()
        (gut / 'temp').write_text('55000')
        kaputt = tmp_path / 'thermal_zone1'
        kaputt.mkdir()
        (kaputt / 'temp').write_text('keine Zahl')
        monkeypatch.setattr(system, '_THERMAL', str(tmp_path))
        assert system.temperatur_c() == 55.0


class TestLast:
    def test_last_wird_auf_kerne_bezogen(self, monkeypatch):
        """3.0 ist auf vier Kernen entspannt, auf einem Überlast."""
        monkeypatch.setattr(os, 'getloadavg', lambda: (3.0, 2.0, 1.0))
        monkeypatch.setattr(os, 'cpu_count', lambda: 4)
        werte = system.last()
        assert werte['load_1'] == 3.0
        assert werte['cores'] == 4
        assert werte['load_percent'] == 75.0

    def test_einkerner(self, monkeypatch):
        monkeypatch.setattr(os, 'getloadavg', lambda: (3.0, 2.0, 1.0))
        monkeypatch.setattr(os, 'cpu_count', lambda: 1)
        assert system.last()['load_percent'] == 300.0


class TestSpeicher:
    MEMINFO = ('MemTotal:         926828 kB\n'
               'MemFree:           92788 kB\n'
               'MemAvailable:     720340 kB\n'
               'Buffers:           31000 kB\n'
               'SwapTotal:        102396 kB\n'
               'SwapFree:         102396 kB\n')

    def test_rechnet_gegen_available_nicht_free(self, tmp_path, monkeypatch):
        """MemFree ignoriert Cache und sieht dramatischer aus, als es ist."""
        datei = tmp_path / 'meminfo'
        datei.write_text(self.MEMINFO)
        monkeypatch.setattr(system, '_lies',
                            lambda p: datei.read_text()
                            if p == '/proc/meminfo' else None)
        werte = system.speicher()
        # Gegen MemAvailable: (926828-720340)/926828 = 22,3 %
        assert werte['used_percent'] == pytest.approx(22.3, abs=0.1)
        # Gegen MemFree wären es 90,0 % — der Wert darf hier nicht stehen.
        assert werte['used_percent'] < 50.0

    def test_swap_wird_mitgefuehrt(self, tmp_path, monkeypatch):
        datei = tmp_path / 'meminfo'
        datei.write_text(self.MEMINFO)
        monkeypatch.setattr(system, '_lies',
                            lambda p: datei.read_text()
                            if p == '/proc/meminfo' else None)
        werte = system.speicher()
        assert werte['swap_total_bytes'] == 102396 * 1024
        assert werte['swap_used_bytes'] == 0

    def test_unlesbar_ergibt_none(self, monkeypatch):
        monkeypatch.setattr(system, '_lies', lambda p: None)
        assert system.speicher() is None


class TestAblage:
    def test_prozent_gegen_belegt_plus_verfuegbar(self, monkeypatch):
        """Die root-Reserve darf die Anzeige nicht beschönigen.

        Gegen die Gesamtgröße gerechnet erreicht ein volles Dateisystem nie
        100 %, obwohl der Dienst nichts mehr schreiben kann.
        """
        class FakeStat:
            f_frsize = 4096
            f_blocks = 1000      # 4,0 MB gesamt
            f_bfree = 100        # 400 kB frei
            f_bavail = 50        # davon 200 kB für den Dienst

        monkeypatch.setattr(os, 'statvfs', lambda p: FakeStat())
        werte = system.ablage('/')
        # belegt = (1000-100)*4096, verfuegbar = 50*4096
        # -> 900/(900+50) = 94,7 %
        assert werte['used_percent'] == pytest.approx(94.7, abs=0.1)
        # Gegen f_blocks wären es 90,0 % — zu optimistisch.
        assert werte['used_percent'] > 92.0

    def test_unlesbar_ergibt_none(self, monkeypatch):
        def wirft(_):
            raise OSError('kein Zugriff')
        monkeypatch.setattr(os, 'statvfs', wirft)
        assert system.ablage('/') is None


class TestDrosselung:
    def test_bits_werden_getrennt(self, monkeypatch):
        # 0x50005: Unterspannung jetzt und seit Start, Drosselung jetzt.
        monkeypatch.setattr(system, '_lies', lambda p: '0x50005')
        werte = system.gedrosselt()
        assert werte['undervoltage_now'] is True
        assert werte['throttled_now'] is True
        assert werte['undervoltage_since_boot'] is True
        assert werte['throttled_since_boot'] is True

    def test_sauberer_zustand(self, monkeypatch):
        monkeypatch.setattr(system, '_lies', lambda p: '0x0')
        werte = system.gedrosselt()
        assert not any(v for k, v in werte.items() if k != 'raw')

    def test_nicht_lesbar_ist_none_nicht_gruen(self, monkeypatch):
        """Nicht gemessen darf nicht als 'alles in Ordnung' erscheinen."""
        monkeypatch.setattr(system, '_lies', lambda p: None)
        assert system.gedrosselt() is None


class TestStatus:
    def test_pruefit_das_dateisystem_der_datenbank(self, tmp_path, monkeypatch):
        """Die Datenbank kann auf einer anderen Partition liegen."""
        gemessen = []
        monkeypatch.setattr(system, 'ablage',
                            lambda p: gemessen.append(p) or {'path': p})
        system.status(str(tmp_path / 'unter' / 'db.sqlite3'))
        # Verzeichnis existiert nicht -> Rückfall auf /
        assert gemessen == ['/']

        gemessen.clear()
        vorhanden = tmp_path / 'da'
        vorhanden.mkdir()
        system.status(str(vorhanden / 'db.sqlite3'))
        assert gemessen == [str(vorhanden)]

    def test_liefert_alle_schluessel(self):
        werte = system.status()
        for schluessel in ('temperature_c', 'load', 'memory', 'disk',
                           'uptime_seconds', 'throttling', 'backup'):
            assert schluessel in werte


class TestSicherung:
    """Eine fehlende Sicherung macht sich sonst nirgends bemerkbar."""

    def test_leeres_verzeichnis(self, tmp_path):
        lokal = tmp_path / 'lokal'
        lokal.mkdir()
        s = system.sicherung(str(tmp_path / 'nas'), str(lokal))
        assert s['anzahl'] == 0
        assert s['letzte_ts'] is None
        assert s['extern'] is False

    def test_zaehlt_nur_datenbanken(self, tmp_path):
        """Der CSV-Export darf nicht als Sicherungsstand mitzählen."""
        lokal = tmp_path / 'lokal'
        lokal.mkdir()
        (lokal / 'impulse-2026-08-25_0300.sqlite3').write_bytes(b'xyz')
        (lokal / 'impulse-2026-08-25.csv').write_text('a;b\n')
        (lokal / 'config-2026-08-25_0300.toml').write_text('x = 1\n')
        s = system.sicherung(str(tmp_path / 'nas'), str(lokal))
        assert s['anzahl'] == 1
        assert s['bytes'] == 3

    def test_juengste_wird_gefunden(self, tmp_path):
        lokal = tmp_path / 'lokal'
        lokal.mkdir()
        alt = lokal / 'impulse-alt.sqlite3'
        neu = lokal / 'impulse-neu.sqlite3'
        alt.write_bytes(b'a')
        neu.write_bytes(b'b')
        os.utime(alt, (1000.0, 1000.0))
        os.utime(neu, (2000.0, 2000.0))
        s = system.sicherung(str(tmp_path / 'nas'), str(lokal))
        assert s['letzte_ts'] == 2000.0
        assert s['anzahl'] == 2

    def test_fehlendes_verzeichnis_meldet_unbekannt(self, tmp_path):
        """Nicht messbar darf nicht wie „null Sicherungen" aussehen."""
        s = system.sicherung(str(tmp_path / 'nas'), str(tmp_path / 'weg'))
        assert s['anzahl'] is None
        assert s['letzte_ts'] is None
