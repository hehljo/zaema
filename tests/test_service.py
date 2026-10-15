"""Zusammenspiel von Erfassung, Zustand und HTTP-Schnittstelle."""
import json
import time
import urllib.error
import urllib.request

import pytest

from s0counter import __version__
from s0counter import main
from s0counter.config import from_dict
from s0counter.main import Service


@pytest.fixture
def service(tmp_path):
    config = from_dict({
        'general': {
            'database': str(tmp_path / 'service.sqlite3'),
            'http_host': '127.0.0.1',
            'http_port': 0,
            'power_timeout_seconds': 180,
        },
        'meters': {
            'eg': {'name': 'Strom EG', 'gpio': 17},
            'og': {'name': 'Strom OG', 'gpio': 27},
            'dg': {'name': 'Strom DG', 'gpio': 22},
        },
    })
    instance = Service(config)
    yield instance
    instance.db.close()


class TestEinrichtung:
    def test_alle_zaehler_angelegt(self, service):
        assert set(service.db.meter_ids()) == {'dg', 'eg', 'og'}

    def test_je_zaehler_ein_impulszaehler(self, service):
        assert set(service.counters) == {'eg', 'og', 'dg'}

    def test_startzustand(self, service):
        for state in service.states().values():
            assert state['pulses'] == 0
            assert state['energy_kwh'] == 0.0


class TestZustand:
    def test_impuls_erhoeht_den_stand(self, service):
        service.counters['eg'].handle_pulse()
        assert service.states()['eg']['pulses'] == 1

    def test_leistung_faellt_nach_dem_timeout_auf_null(self, service):
        """Ohne Impulse darf keine Leistung stehenbleiben."""
        alt = 1000.0
        service.db.add_pulse('eg', alt, alt)
        service.db.add_pulse('eg', alt + 1, alt + 1)

        # Beide Impulse liegen weit in der Vergangenheit
        assert service.db.state('eg')['power_w'] > 0
        assert service.states()['eg']['power_w'] == 0.0

    def test_leistung_bleibt_bei_frischen_impulsen(self, service):
        import time
        now = time.time()
        service.db.add_pulse('eg', now - 2, now - 2)
        service.db.add_pulse('eg', now - 1, now - 1)
        assert service.states()['eg']['power_w'] == pytest.approx(3600.0)

    def test_statusbericht_ist_vollstaendig(self, service):
        service.counters['eg'].handle_pulse()
        status = service.status()

        assert status['service'] == 's0counter'
        assert set(status['meters']) == {'eg', 'og', 'dg'}
        assert status['counters']['eg']['accepted'] == 1
        assert status['homeassistant']['enabled'] is False


class TestFassung:
    """Die Anzeige soll den laufenden Code benennen, nicht eine gepflegte Zahl."""

    def test_status_nennt_die_fassung(self, service):
        f = service.status()['fassung']
        assert f['version'] == __version__
        assert len(f['build']) == 12
        assert f['dateien'] >= 10
        assert f['stand_ts'] > 0

    def test_fingerabdruck_folgt_dem_code(self, tmp_path, monkeypatch):
        """Der Kern der Sache: eine geänderte Datei muss den Wert ändern.

        Ein Fingerabdruck, der bei geändertem Code gleich bleibt, ist
        schlimmer als keiner — er behauptet, der Stand sei unverändert.
        """
        paket = tmp_path / 'paket'
        paket.mkdir()
        (paket / 'a.py').write_text('x = 1\n')
        (paket / 'b.js').write_text('const y = 2;\n')
        monkeypatch.setattr(main, '__file__', str(paket / 'main.py'))

        main.laufende_fassung.cache_clear()
        vorher = main.laufende_fassung()['build']

        (paket / 'a.py').write_text('x = 2\n')
        main.laufende_fassung.cache_clear()
        nachher = main.laufende_fassung()['build']

        assert vorher != nachher, 'Fingerabdruck folgt dem Code nicht'

    def test_fingerabdruck_ignoriert_fremde_dateien(self, tmp_path, monkeypatch):
        """Sonst schlüge er bei jeder Sicherungskopie daneben an."""
        paket = tmp_path / 'paket'
        paket.mkdir()
        (paket / 'a.py').write_text('x = 1\n')
        monkeypatch.setattr(main, '__file__', str(paket / 'main.py'))

        main.laufende_fassung.cache_clear()
        vorher = main.laufende_fassung()['build']

        (paket / 'a.py.bak').write_text('alter Stand\n')
        (paket / 'notiz.txt').write_text('egal\n')
        main.laufende_fassung.cache_clear()
        assert main.laufende_fassung()['build'] == vorher

    def test_merker_bleibt_unveraendert(self, service):
        """`lru_cache` reicht dasselbe Objekt heraus — der Status kriegt eine Kopie."""
        erste = service.status()['fassung']
        erste['version'] = 'MANIPULIERT'
        assert service.status()['fassung']['version'] == __version__


class TestAbrechnungExport:
    """CSV und PDF müssen dieselben Zahlen tragen wie der Bildschirm."""

    def _daten(self, bilanz_service):
        return bilanz_service.allocation('24h')

    def test_csv_traegt_dieselben_zahlen_wie_die_anzeige(self, bilanz_service):
        daten = self._daten(bilanz_service)
        text = bilanz_service.allocation_csv(daten).decode('utf-8-sig')

        for mid, m in daten['meters'].items():
            if m['consumption_kwh'] <= 0:
                continue
            erwartet = f'{m["consumption_kwh"]:.3f}'.replace('.', ',')
            assert erwartet in text, f'{mid}: {erwartet} fehlt in der CSV'

    def test_csv_ist_fuer_excel_lesbar(self, bilanz_service):
        """BOM und Semikolon — ohne beides zerlegt Excel die Datei."""
        roh = bilanz_service.allocation_csv(self._daten(bilanz_service))
        assert roh.startswith(b'\xef\xbb\xbf'), 'BOM fehlt, Umlaute brechen'
        text = roh.decode('utf-8-sig')
        assert ';' in text
        # Dezimalkomma, nicht Punkt: sonst liest Excel die Zahlen als Text.
        zeilen = [z for z in text.splitlines() if z.startswith('eg;')]
        assert zeilen, text
        assert '.' not in zeilen[0], f'Dezimalpunkt in {zeilen[0]!r}'

    def test_csv_nennt_zeitraum_modell_und_urteil(self, bilanz_service):
        """Eine Tabelle wird weitergereicht — ohne das weiß niemand, was es ist."""
        daten = self._daten(bilanz_service)
        text = bilanz_service.allocation_csv(daten).decode('utf-8-sig')
        assert 'Zeitraum von' in text
        assert daten['model_label'] in text
        assert daten['quality']['verdict']['level'] in text

    def test_pdf_ist_ein_pdf(self, bilanz_service):
        roh = bilanz_service.allocation_pdf(self._daten(bilanz_service))
        assert roh.startswith(b'%PDF-'), roh[:20]
        assert roh.rstrip().endswith(b'%%EOF'), roh[-20:]
        assert len(roh) > 1000

    def test_export_rechnet_nicht_selbst(self, bilanz_service):
        """Beide Wege bekommen die Zuteilung übergeben, statt neu zu rechnen.

        Sonst entsteht eine zweite Wahrheit, die eines Tages von der Anzeige
        abweicht. Geprüft, indem eine manipulierte Zuteilung übergeben wird:
        steht der veränderte Wert im Dokument, wurde er übernommen und nicht
        neu berechnet.
        """
        daten = self._daten(bilanz_service)
        mid = next(iter(daten['meters']))
        daten['meters'][mid]['consumption_kwh'] = 123.456
        text = bilanz_service.allocation_csv(daten).decode('utf-8-sig')
        assert '123,456' in text


class TestHttp:
    def test_status_liefert_json(self, service):
        service.setup_http()
        port = service.http_server.server_address[1]
        try:
            service.counters['eg'].handle_pulse()
            with urllib.request.urlopen(
                    f'http://127.0.0.1:{port}/status', timeout=5) as response:
                assert response.status == 200
                daten = json.loads(response.read())

            assert daten['meters']['eg']['pulses'] == 1
            assert daten['meters']['eg']['energy_kwh'] == pytest.approx(0.001)
        finally:
            service.http_server.shutdown()

    def test_health(self, service):
        service.setup_http()
        port = service.http_server.server_address[1]
        try:
            with urllib.request.urlopen(
                    f'http://127.0.0.1:{port}/health', timeout=5) as response:
                assert json.loads(response.read()) == {'ok': True}
        finally:
            service.http_server.shutdown()

    def test_audit_ueber_http(self, service):
        service.db.set_reading('eg', 500.0)
        service.setup_http()
        port = service.http_server.server_address[1]
        try:
            with urllib.request.urlopen(
                    f'http://127.0.0.1:{port}/audit', timeout=5) as response:
                daten = json.loads(response.read())
            assert daten['audit'][0]['action'] == 'set-reading'
        finally:
            service.http_server.shutdown()

    def test_unbekannter_pfad(self, service):
        import urllib.error
        service.setup_http()
        port = service.http_server.server_address[1]
        try:
            with pytest.raises(urllib.error.HTTPError) as fehler:
                urllib.request.urlopen(f'http://127.0.0.1:{port}/gibtsnicht',
                                       timeout=5)
            assert fehler.value.code == 404
        finally:
            service.http_server.shutdown()


class TestVerlauf:
    """Abruf der Verlaufsdaten über /history."""

    def abruf(self, service, pfad):
        service.setup_http()
        port = service.http_server.server_address[1]
        try:
            with urllib.request.urlopen(
                    f'http://127.0.0.1:{port}{pfad}', timeout=5) as antwort:
                return antwort.status, json.loads(antwort.read())
        except urllib.error.HTTPError as fehler:
            return fehler.code, json.loads(fehler.read())
        finally:
            service.http_server.shutdown()

    def test_zaehlerverlauf_zaehlt_impulse(self, service):
        """Die Leistung kommt aus der Impulszahl je Feld, nicht aus dem
        Abstand zweier Impulse — nur so ist sie mit der Bilanz vergleichbar."""
        import time
        jetzt = time.time()
        # 10 Impulse in der letzten Minute, alle im selben Rasterfeld.
        for i in range(10):
            service.db.add_pulse('eg', jetzt - 30 + i * 0.5, 0.0)

        verlauf = service.history('meter.eg', 300.0)
        assert verlauf['unit'] == 'W'
        assert verlauf['points'], 'kein Punkt im Verlauf'
        # 10 Impulse à 1 Wh in einem 60-s-Feld -> 600 W
        gesamt = sum(p['avg'] for p in verlauf['points'])
        assert gesamt == pytest.approx(600.0, rel=0.01)

    def test_feld_ohne_impuls_fehlt_statt_null(self, service):
        """Kein Impuls heißt nicht 0 W, sondern 'noch nicht genug für den
        nächsten Impuls'. Eine Nulllinie wäre eine erfundene Aussage."""
        import time
        jetzt = time.time()
        service.db.add_pulse('eg', jetzt - 10, 0.0)
        verlauf = service.history('meter.eg', 600.0)
        # 600 s / 60 s Raster = 10 Felder, aber nur eines hat einen Impuls.
        assert len(verlauf['points']) == 1

    def test_unbekannter_zaehler(self, service):
        with pytest.raises(ValueError):
            service.history('meter.keller', 3600.0)

    def test_unbekannte_kennung(self, service):
        with pytest.raises(ValueError):
            service.history('gibtsnicht', 3600.0)

    def test_abgeschaltete_quelle_meldet_lookup(self, service):
        """Bilanz ist in dieser Konfiguration aus — das ist kein 404."""
        with pytest.raises(LookupError):
            service.history('house.consumption', 3600.0)

    def test_keys_nennen_nur_verfuegbares(self, service):
        keys = {k['key'] for k in service.history_keys()['keys']}
        # Bilanz und Rechnerreihe sind hier aus, Zähler gibt es.
        assert 'meter.eg' in keys
        assert 'house.consumption' not in keys
        assert 'system.temperature' not in keys

    def test_http_liefert_verlauf(self, service):
        import time
        service.db.add_pulse('eg', time.time() - 10, 0.0)
        status, daten = self.abruf(service, '/history?key=meter.eg&seconds=600')
        assert status == 200
        assert daten['key'] == 'meter.eg'
        assert len(daten['points']) == 1

    def test_http_lehnt_unsinnige_zeitraeume_ab(self, service):
        """Ein Jahr in Rohauflösung wären 525.000 Zeilen — im selben Prozess,
        der die Impulse erfasst."""
        status, daten = self.abruf(
            service, '/history?key=meter.eg&seconds=99999999')
        assert status == 400
        assert 'seconds' in daten['error']

    def test_http_lehnt_zu_viele_punkte_ab(self, service):
        status, _ = self.abruf(
            service, '/history?key=meter.eg&seconds=3600&points=999999')
        assert status == 400

    def test_http_lehnt_text_statt_zahl_ab(self, service):
        status, _ = self.abruf(
            service, '/history?key=meter.eg&seconds=viele')
        assert status == 400

    def test_http_unbekannte_kennung_ist_404(self, service):
        status, _ = self.abruf(service, '/history?key=gibtsnicht')
        assert status == 404

    def test_http_abgeschaltete_quelle_ist_503(self, service):
        status, _ = self.abruf(service, '/history?key=house.consumption')
        assert status == 503

    def test_status_pfad_mit_query_funktioniert(self, service):
        """Der Pfad wird vor dem Fragezeichen abgeschnitten — sonst liefe
        jeder Aufruf mit Parametern in den 404."""
        status, daten = self.abruf(service, '/status?x=1')
        assert status == 200
        assert daten['service'] == 's0counter'


class TestNeustart:
    def test_staende_ueberleben_einen_neustart(self, tmp_path):
        """Nach einem Neustart muss der Zählerstand erhalten sein."""
        settings = {
            'general': {'database': str(tmp_path / 'neustart.sqlite3'),
                        'http_port': 0},
            'meters': {'eg': {'name': 'Strom EG', 'gpio': 17}},
        }

        erst = Service(from_dict(settings))
        # Zeitstempel im Sekundenabstand, sonst greift die Entprellung
        for i in range(250):
            erst.counters['eg'].handle_pulse(float(i), float(i))
        erst.db.set_reading('eg', 1000.0)
        erst.db.close()

        zweit = Service(from_dict(settings))
        try:
            state = zweit.states()['eg']
            assert state['pulses'] == 250
            assert state['energy_kwh'] == pytest.approx(1000.0)
        finally:
            zweit.db.close()


@pytest.fixture
def bilanz_service(tmp_path):
    """Dienst mit eingeschalteter Hausbilanz und gefüllten Reihen.

    Die Quelle wird nicht abgefragt: der Sammler bleibt aus, die Messpunkte
    werden direkt eingetragen. So hängt der Test an keiner Uhr und an
    keinem Netz.
    """
    import time

    from s0counter.balance import MODELS, Allocator, BalanceStore

    config = from_dict({
        'general': {'database': str(tmp_path / 'dienst.sqlite3'),
                    'http_port': 0},
        'meters': {'eg': {'name': 'Strom EG', 'gpio': 17},
                   'og': {'name': 'Strom OG', 'gpio': 27}},
        'balance': {'enabled': True,
                    'database': str(tmp_path / 'bilanz.sqlite3'),
                    'allocation_interval_seconds': 900},
        'sonnenbatterie': {'enabled': True, 'host': '127.0.0.1'},
    })
    dienst = Service(config)
    dienst.balance_store = BalanceStore(config.balance.database)
    dienst.allocators = {
        name: Allocator(dienst.balance_store, dienst.db, 900.0, name)
        for name in MODELS
    }

    # Zwei volle Tage, auf dem Abrechnungsraster. Die Zeilen werden am
    # Stück eingetragen statt über add()/add_pulse(): die schreiben je
    # Zeile ein eigenes commit und brauchten für zwei Tage über 20 s.
    # Danach gleicht `_repair_pulse_minutes()` das Aggregat ab — derselbe
    # Weg, den auch eine von Hand eingespielte Datenbank nimmt.
    ende = (time.time() // 900) * 900
    start = ende - 2 * 86400

    bilanz = dienst.balance_store.conn
    bilanz.executemany(
        'INSERT INTO balance(ts, production_w, consumption_w, grid_w, '
        'battery_w, battery_percent, source) VALUES(?,?,?,?,?,?,?)',
        ((start + i * 60.0, 600.0, 1800.0, 1200.0, 0.0, 50.0, 'test')
         for i in range(int((ende - start) / 60))))
    bilanz.commit()

    # Impulse: EG dreimal so viel wie OG.
    roh = dienst.db.conn
    for meter_id, abstand in (('eg', 12.0), ('og', 36.0)):
        roh.executemany(
            'INSERT INTO pulses(meter_id, ts, mono) VALUES(?,?,?)',
            ((meter_id, start + i * abstand, start + i * abstand)
             for i in range(int((ende - start) / abstand))))
    roh.commit()
    dienst.db._repair_counters()

    yield dienst
    dienst.balance_store.close()
    dienst.db.close()


class TestAbrechnung:
    """Die Verteilung des Netzbezugs über /allocation."""

    def test_wohnungen_bekommen_verbrauch_und_herkunft(self, bilanz_service):
        ergebnis = bilanz_service.allocation('24h')
        for meter_id in ('eg', 'og'):
            werte = ergebnis['meters'][meter_id]
            assert werte['consumption_kwh'] > 0
            assert werte['grid_kwh'] >= 0
            assert werte['solar_kwh'] >= 0
            # Der gemessene Verbrauch teilt sich vollständig in Netz und PV.
            assert werte['grid_kwh'] + werte['solar_kwh'] == pytest.approx(
                werte['consumption_kwh'], abs=1e-9)

    def test_niemand_bekommt_mehr_netz_als_verbrauch(self, bilanz_service):
        for modell in ('proportional', 'waterfall'):
            ergebnis = bilanz_service.allocation('24h', modell)
            for meter_id, werte in ergebnis['meters'].items():
                assert werte['grid_kwh'] <= werte['consumption_kwh'] + 1e-9, \
                    f'{modell}/{meter_id}'

    def test_groesserer_verbraucher_bekommt_mehr_netzbezug(self, bilanz_service):
        ergebnis = bilanz_service.allocation('24h')
        assert ergebnis['meters']['eg']['grid_kwh'] > \
            ergebnis['meters']['og']['grid_kwh']

    def test_laengerer_zeitraum_ergibt_mehr_energie(self, bilanz_service):
        kurz = bilanz_service.allocation('24h')
        lang = bilanz_service.allocation('7d')
        assert lang['meters']['eg']['consumption_kwh'] > \
            kurz['meters']['eg']['consumption_kwh']

    def test_zeitraum_liegt_auf_dem_abrechnungsraster(self, bilanz_service):
        """Angeschnittene Intervalle würden die Anteile verfälschen."""
        ergebnis = bilanz_service.allocation('24h')
        raster = ergebnis['interval_seconds']
        assert ergebnis['range']['start_ts'] % raster == 0
        assert ergebnis['range']['end_ts'] % raster == 0

    def test_laufendes_intervall_bleibt_draussen(self, bilanz_service):
        """Sonst erschiene die angebrochene Viertelstunde als Einbruch."""
        import time
        ergebnis = bilanz_service.allocation('24h')
        assert ergebnis['range']['end_ts'] <= time.time()
        assert time.time() - ergebnis['range']['end_ts'] < 900 + 1

    def test_beide_modelle_liefern_ein_ergebnis(self, bilanz_service):
        anteilig = bilanz_service.allocation('24h', 'proportional')
        wasserfall = bilanz_service.allocation('24h', 'waterfall')
        assert anteilig['model'] == 'proportional'
        assert wasserfall['model'] == 'waterfall'

    def test_unbekannter_zeitraum_wird_abgelehnt(self, bilanz_service):
        with pytest.raises(ValueError):
            bilanz_service.allocation('alles')

    def test_unbekanntes_modell_wird_abgelehnt(self, bilanz_service):
        with pytest.raises(ValueError):
            bilanz_service.allocation('24h', 'nach-sympathie')

    def test_ohne_bilanz_kein_ergebnis(self, service):
        with pytest.raises(LookupError):
            service.allocation('24h')

    def test_auswahl_kommt_aus_dem_dienst(self, bilanz_service):
        """Die Anzeige soll ihre Auswahllisten nicht selbst führen —
        eine zweite Liste liefe irgendwann auseinander."""
        ergebnis = bilanz_service.allocation('24h')
        angeboten = {r['key'] for r in ergebnis['ranges']}
        # Die rollierenden Zeiträume müssen vollständig dabei sein; dazu
        # kommen die Kalendereinträge, die der Datenbestand hergibt.
        assert set(bilanz_service.ABRECHNUNGSZEITRAEUME) <= angeboten
        assert {m['key'] for m in ergebnis['models']} == \
            set(bilanz_service.allocators)


class TestAbrechnungKalender:
    """Echte Kalenderjahre und -monate, nicht nur rollierende Fenster.

    Für eine Nebenkostenabrechnung braucht es das Jahr 2026, nicht die
    letzten 365 Tage ab heute.
    """

    def test_jahr_wird_erkannt(self, bilanz_service):
        import time
        jahr = time.localtime().tm_year
        start, ende, label = bilanz_service._zeitraum_aufloesen(
            str(jahr), time.time())
        assert label == str(jahr)
        assert time.localtime(start).tm_mon == 1
        assert time.localtime(start).tm_mday == 1
        assert time.localtime(start).tm_hour == 0

    def test_monat_wird_erkannt(self, bilanz_service):
        import time
        jetzt = time.time()
        heute = time.localtime(jetzt)
        kennung = f'{heute.tm_year}-{heute.tm_mon:02d}'
        start, ende, label = bilanz_service._zeitraum_aufloesen(kennung, jetzt)
        assert time.localtime(start).tm_mday == 1
        assert time.localtime(start).tm_mon == heute.tm_mon
        assert str(heute.tm_year) in label

    def test_dezember_laeuft_ins_folgejahr(self, bilanz_service):
        """Der Monatswechsel über die Jahresgrenze — der klassische Fehler."""
        import time
        jahr, monat = bilanz_service._kalender_zerlegen('2024-12')
        assert (jahr, monat) == (2024, 12)
        start, ende, _ = bilanz_service._zeitraum_aufloesen(
            '2024-12', time.time())
        assert time.localtime(ende).tm_year == 2025
        assert time.localtime(ende).tm_mon == 1
        assert time.localtime(ende).tm_mday == 1

    def test_jahr_endet_am_jahreswechsel(self, bilanz_service):
        import time
        start, ende, _ = bilanz_service._zeitraum_aufloesen(
            '2024', time.time())
        assert time.localtime(start).tm_year == 2024
        assert time.localtime(ende).tm_year == 2025
        assert time.localtime(ende).tm_mon == 1

    def test_schaltjahr_hat_366_tage(self, bilanz_service):
        """2024 war ein Schaltjahr. Über mktime gerechnet, nicht über 365."""
        import time
        start, ende, _ = bilanz_service._zeitraum_aufloesen(
            '2024', time.time())
        assert (ende - start) / 86400 == pytest.approx(366, abs=0.05)

    def test_laufendes_jahr_wird_bei_jetzt_abgeschnitten(self, bilanz_service):
        """Sonst stünde das laufende Jahr mit 60 % Abdeckung da, obwohl
        kein einziger Messpunkt fehlt."""
        import time
        jetzt = time.time()
        jahr = time.localtime(jetzt).tm_year
        _, ende, _ = bilanz_service._zeitraum_aufloesen(str(jahr), jetzt)
        assert ende == pytest.approx(jetzt, abs=1)

    def test_zukuenftiger_zeitraum_wird_abgelehnt(self, bilanz_service):
        import time
        with pytest.raises(ValueError):
            bilanz_service._zeitraum_aufloesen('2199', time.time())

    def test_unsinn_wird_abgelehnt(self, bilanz_service):
        for kennung in ('20xx', '2026-13', '2026-00', '1999',
                        '2026-08-15', 'letztes-jahr', ''):
            with pytest.raises(ValueError):
                bilanz_service._zeitraum_aufloesen(kennung, 1_787_600_000.0)

    def test_abrechnung_ueber_das_jahr_rechnet(self, bilanz_service):
        import time
        jahr = str(time.localtime().tm_year)
        ergebnis = bilanz_service.allocation(jahr)
        assert ergebnis['range']['key'] == jahr
        assert ergebnis['range']['label'] == jahr
        assert ergebnis['meters']

    def test_angebotene_zeitraeume_richten_sich_nach_den_daten(
            self, bilanz_service):
        """Ein Knopf für ein Jahr ohne Messpunkte hat keinen Zweck."""
        import time
        angebote = bilanz_service._kalenderzeitraeume(time.time())
        assert angebote, 'keine Kalenderzeiträume angeboten'
        erster = bilanz_service.balance_store.first_ts()
        for eintrag in angebote:
            start, _, _ = bilanz_service._zeitraum_aufloesen(
                eintrag['key'], time.time())
            # Der Zeitraum muss wenigstens bis in die Daten hineinreichen.
            assert start <= time.time()
        # Die Fixture legt zwei Tage an — mehr als ein Jahr kann es nicht
        # geben, egal wie viele angeboten werden dürften.
        jahre = [e for e in angebote if e['kind'] == 'jahr']
        assert len(jahre) <= bilanz_service.JAHRE_RUECKWAERTS

    def test_laufender_monat_wird_angeboten(self, bilanz_service):
        """Auch wenn die Aufzeichnung erst mitten im Monat begann.

        Gemessen wird das Monatsende gegen den ersten Messpunkt, nicht der
        Monatsanfang — sonst fiele genau der Monat heraus, für den die
        meisten Daten vorliegen.
        """
        import time
        heute = time.localtime()
        erwartet = f'{heute.tm_year}-{heute.tm_mon:02d}'
        angebote = bilanz_service._kalenderzeitraeume(time.time())
        assert erwartet in {e['key'] for e in angebote}

    def test_laufendes_jahr_wird_angeboten(self, bilanz_service):
        import time
        jahr = str(time.localtime().tm_year)
        angebote = bilanz_service._kalenderzeitraeume(time.time())
        assert jahr in {e['key'] for e in angebote}

    def test_monate_ohne_daten_werden_nicht_angeboten(self, bilanz_service):
        """Die Gegenrichtung: ein Knopf ins Leere ist schlimmer als keiner.

        Die Fixture legt zwei Tage an — zwölf Monatsknöpfe wären elf
        Rechnungen ohne einen einzigen Messpunkt.
        """
        import time
        angebote = bilanz_service._kalenderzeitraeume(time.time())
        monate = [e for e in angebote if e['kind'] == 'monat']
        assert 1 <= len(monate) <= 2, [e['key'] for e in monate]

    def test_jeder_angebotene_zeitraum_ist_abrufbar(self, bilanz_service):
        """Die Anzeige baut ihre Knöpfe aus dieser Liste — jeder davon muss
        auch beantwortet werden, sonst führt ein Knopf ins Leere."""
        ergebnis = bilanz_service.allocation('24h')
        for eintrag in ergebnis['ranges']:
            geprueft = bilanz_service.allocation(eintrag['key'])
            assert geprueft['range']['key'] == eintrag['key']


class TestAbrechnungBelastbarkeit:
    """Eine Rechnung über zwei Stunden darf nicht aussehen wie eine über
    ein Jahr. Der dritte Ausgang verhindert das stille Durchwinken."""

    def test_voller_zeitraum_ist_belastbar(self, bilanz_service):
        ergebnis = bilanz_service.allocation('24h')
        assert ergebnis['quality']['verdict']['level'] == 'belastbar'
        assert ergebnis['quality']['coverage'] > 0.99

    def test_kurzer_zeitraum_ist_eingeschraenkt(self, bilanz_service):
        """Unter einem Tag ist die PV-Verteilung nicht aussagekräftig."""
        urteil = bilanz_service._abrechnung_bewerten(
            {'coverage': 1.0, 'intervals_without_pulses': 0, 'gaps': []},
            0.0, 3600.0)
        assert urteil['level'] == 'eingeschraenkt'
        assert any('kürzer als ein Tag' in g for g in urteil['reasons'])

    def test_luecken_druecken_die_bewertung(self, bilanz_service):
        urteil = bilanz_service._abrechnung_bewerten(
            {'coverage': 0.7, 'intervals_without_pulses': 0, 'gaps': []},
            0.0, 7 * 86400.0)
        assert urteil['level'] == 'eingeschraenkt'

    def test_kaum_daten_ist_unbrauchbar(self, bilanz_service):
        urteil = bilanz_service._abrechnung_bewerten(
            {'coverage': 0.2, 'intervals_without_pulses': 0, 'gaps': []},
            0.0, 7 * 86400.0)
        assert urteil['level'] == 'unbrauchbar'
        assert urteil['reasons']

    def test_fehlender_zeitraum_wird_als_zeitraum_gemeldet(self, bilanz_service):
        """Sieben Tage ohne Daten sind keine Abrechnung über sieben Tage."""
        ergebnis = bilanz_service.allocation('365d')
        assert ergebnis['quality']['verdict']['level'] == 'unbrauchbar'

    def test_nicht_zugeordneter_bezug_wird_genannt(self, bilanz_service):
        urteil = bilanz_service._abrechnung_bewerten(
            {'coverage': 1.0, 'intervals_without_pulses': 12, 'gaps': []},
            0.0, 2 * 86400.0)
        assert any('nicht zugeordnet' in g for g in urteil['reasons'])


class TestAbrechnungHttp:
    def abruf(self, service, pfad):
        service.setup_http()
        port = service.http_server.server_address[1]
        try:
            with urllib.request.urlopen(
                    f'http://127.0.0.1:{port}{pfad}', timeout=10) as antwort:
                return antwort.status, json.loads(antwort.read())
        except urllib.error.HTTPError as fehler:
            return fehler.code, json.loads(fehler.read())
        finally:
            service.http_server.shutdown()

    def test_abruf_liefert_die_verteilung(self, bilanz_service):
        status, daten = self.abruf(bilanz_service, '/allocation?range=24h')
        assert status == 200
        assert daten['meters']['eg']['consumption_kwh'] > 0
        assert daten['quality']['verdict']['level']

    def test_modell_kommt_aus_der_anfrage(self, bilanz_service):
        status, daten = self.abruf(
            bilanz_service, '/allocation?range=24h&model=waterfall')
        assert status == 200
        assert daten['model'] == 'waterfall'

    def test_unbekannter_zeitraum_gibt_400(self, bilanz_service):
        status, daten = self.abruf(bilanz_service, '/allocation?range=ewig')
        assert status == 400
        assert 'error' in daten

    def test_ohne_bilanz_gibt_503(self, service):
        status, daten = self.abruf(service, '/allocation?range=24h')
        assert status == 503
        assert 'error' in daten


class TestAnzeigeAusliefern:
    """Jedes JS-Modul der Anzeige muss auch ausgeliefert werden.

    Ein fehlender Eintrag in der Zuordnung erzeugt keinen Fehler im Dienst,
    sondern eine leere Seite im Browser: der Import scheitert still. Die
    Liste wird aus den `import`-Zeilen von dashboard.js GEPARST, nicht hier
    gepflegt — eine handgeführte Liste bliebe genau dort stehen, wo sie
    angelegt wurde.
    """

    def module(self):
        import re
        from pathlib import Path

        ordner = Path(__file__).resolve().parent.parent / 's0counter'
        quelle = (ordner / 'dashboard.js').read_text(encoding='utf-8')
        # Nur relative Importe: eine Bibliothek von außen gäbe es hier
        # ohnehin nicht, und ein absoluter Pfad wäre ein eigener Befund.
        return re.findall(r"^import\s.*?from\s+'\./([^']+)'",
                          quelle, re.MULTILINE)

    def test_jedes_importierte_modul_wird_ausgeliefert(self, service):
        gefunden = self.module()
        # Findet der Parser gar nichts, ist das rot und nicht grün: dann
        # prüft dieser Test seit einer Umbenennung nichts mehr.
        assert gefunden, 'keine Importe in dashboard.js gefunden'

        for name in gefunden:
            status, _ = self.roh(service, f'/{name}')
            assert status == 200, f'/{name} wird nicht ausgeliefert'

    def test_dashboard_selbst_kommt(self, service):
        status, _ = self.roh(service, '/dashboard')
        assert status == 200

    def roh(self, service, pfad):
        """Rohe Bytes statt JSON — hier werden Skriptdateien abgeholt."""
        service.setup_http()
        port = service.http_server.server_address[1]
        try:
            with urllib.request.urlopen(
                    f'http://127.0.0.1:{port}{pfad}', timeout=5) as antwort:
                return antwort.status, antwort.read()
        except urllib.error.HTTPError as fehler:
            return fehler.code, fehler.read()
        finally:
            service.http_server.shutdown()


class TestTimeoutJeZaehler:
    """Der Timeout muss je Zähler aus dessen Impulsrate kommen.

    Ein gemeinsamer Wert für alle wäre nur richtig, solange alle Zähler
    dieselbe Rate haben. Bei 100 Imp/kWh entsprechen die 180 s der
    Hausanlage nicht mehr 20 W, sondern 200 W — eine Wohnung mit
    Grundlast stünde dauerhaft auf 0 W.
    """

    @pytest.fixture
    def gemischt(self, tmp_path):
        config = from_dict({
            'general': {
                'database': str(tmp_path / 'gemischt.sqlite3'),
                'http_host': '127.0.0.1',
                'http_port': 0,
                'power_timeout_seconds': 180,
            },
            'meters': {
                # feiner Zähler wie im Haus: Timeout 180 s
                'fein': {'name': 'Fein', 'gpio': 17,
                         'pulses_per_kwh': 1000},
                # grober Zähler: dieselben 20 W brauchen 1800 s
                'grob': {'name': 'Grob', 'gpio': 27,
                         'pulses_per_kwh': 100},
            },
        })
        instance = Service(config)
        yield instance
        instance.db.close()

    def test_grober_zaehler_meldet_nach_180s_noch_leistung(self, gemischt):
        """Die Wirkung, nicht die Nebenwirkung: der Wert bleibt stehen.

        Beide Zähler bekommen denselben Impulsabstand. Nach 300 s ohne
        weiteren Impuls muss der feine auf 0 stehen (Timeout 180 s) und
        der grobe seine Leistung behalten (Timeout 1800 s).
        """
        jetzt = time.time()
        vor_300s = jetzt - 300.0

        for meter_id in ('fein', 'grob'):
            # zwei Impulse mit 10 s Abstand, der letzte vor 300 s
            gemischt.db.add_pulse(meter_id, vor_300s - 10.0, 1000.0)
            gemischt.db.add_pulse(meter_id, vor_300s, 1010.0)

        states = gemischt.states()

        assert states['fein']['power_w'] == 0.0, (
            'feiner Zähler: 300 s > 180 s Timeout, muss auf 0 stehen')
        assert states['grob']['power_w'] > 0.0, (
            'grober Zähler: 300 s < 1800 s Timeout, Leistung muss bleiben')

    def test_beide_zaehler_verfallen_nach_ihrem_eigenen_timeout(self, gemischt):
        """Gegenprobe nach grün: der grobe verfällt auch, nur später."""
        jetzt = time.time()
        vor_2000s = jetzt - 2000.0

        for meter_id in ('fein', 'grob'):
            gemischt.db.add_pulse(meter_id, vor_2000s - 10.0, 1000.0)
            gemischt.db.add_pulse(meter_id, vor_2000s, 1010.0)

        states = gemischt.states()

        assert states['fein']['power_w'] == 0.0
        assert states['grob']['power_w'] == 0.0, (
            '2000 s > 1800 s: auch der grobe Zähler muss verfallen')

    def test_zaehler_ohne_konfiguration_nutzt_den_globalen_wert(self, gemischt):
        """Ein abgebauter Zähler steht noch in der Datenbank.

        Ohne Rückfall gäbe es dort einen KeyError im Statusabruf — das
        Dashboard wäre für alle Zähler tot, wegen eines entfernten.
        """
        gemischt.db.ensure_meter('alt', 'Abgebaut', 1000)
        jetzt = time.time()
        gemischt.db.add_pulse('alt', jetzt - 300.0, 1000.0)

        states = gemischt.states()
        assert states['alt']['power_w'] == 0.0
