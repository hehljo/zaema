"""Zeitreihe der Rechnerwerte samt Verdichtung."""
import pytest

from s0counter.history import HistoryCollector, HistoryStore

MINUTE = 60.0
QUARTER = 900.0
HOUR = 3600.0
TAG = 86400.0


@pytest.fixture
def store(tmp_path):
    s = HistoryStore(str(tmp_path / 'history.sqlite3'))
    yield s
    s.close()


def fuelle(store, ab, anzahl, abstand=MINUTE, temp=50.0, last=10.0):
    """Messpunkte im festen Takt ablegen."""
    for i in range(anzahl):
        store.add({
            'temperature_c': temp,
            'load_percent': last,
            'memory_used_percent': 20.0,
            'disk_used_percent': 40.0,
        }, ts=ab + i * abstand)


class TestAblage:
    def test_messpunkt_speichern(self, store):
        store.add({'temperature_c': 47.5, 'load_percent': 3.0,
                   'memory_used_percent': 22.0,
                   'disk_used_percent': 41.0}, ts=1000.0)
        reihe = store.series('temperature_c', 3600.0, jetzt=1010.0)
        assert len(reihe['points']) == 1
        assert reihe['points'][0]['avg'] == 47.5

    def test_fehlender_wert_bleibt_none(self, store):
        """Ein nicht gemessener Wert darf nicht als Nulllinie erscheinen."""
        store.add({'temperature_c': None, 'load_percent': 5.0,
                   'memory_used_percent': 20.0,
                   'disk_used_percent': 40.0}, ts=1000.0)
        reihe = store.series('temperature_c', 3600.0, jetzt=1010.0)
        assert reihe['points'][0]['avg'] is None

    def test_unbekannte_groesse_wird_abgelehnt(self, store):
        with pytest.raises(ValueError):
            store.series('gibtsnicht', 3600.0)

    def test_gleicher_zeitstempel_ersetzt(self, store):
        store.add({'temperature_c': 40.0}, ts=1000.0)
        store.add({'temperature_c': 60.0}, ts=1000.0)
        assert store.count('raw') == 1
        reihe = store.series('temperature_c', 100.0, jetzt=1050.0)
        assert reihe['points'][0]['avg'] == 60.0


class TestStufenwahl:
    def test_kurzer_zeitraum_nimmt_rohdaten(self, store):
        assert store.stufe_fuer(HOUR) == 'raw'
        assert store.stufe_fuer(4 * HOUR) == 'raw'

    def test_mittlerer_zeitraum_nimmt_viertelstunden(self, store):
        # 24 h = 1440 Rohpunkte, zu viel; 96 Viertelstunden, passt.
        assert store.stufe_fuer(24 * HOUR) == 'quarter'

    def test_langer_zeitraum_nimmt_stunden(self, store):
        assert store.stufe_fuer(30 * TAG) == 'hour'
        assert store.stufe_fuer(365 * TAG) == 'hour'


class TestVerdichtung:
    def test_mittelwert_ueber_die_viertelstunde(self, store):
        """15 Minutenwerte werden zu einem Viertelstundenpunkt."""
        # Wechselnde Temperatur, Mittel muss 50 sein.
        for i in range(15):
            store.add({'temperature_c': 40.0 + (i % 2) * 20.0},
                      ts=i * MINUTE)
        # Alles ist älter als 7 Tage -> wird verdichtet.
        ergebnis = store.compact(jetzt=8 * TAG)
        assert ergebnis['quarter'] == 1
        assert store.count('raw') == 0

        reihe = store.series('temperature_c', 9 * TAG, jetzt=8 * TAG)
        punkt = reihe['points'][0]
        # 8x 40 + 7x 60 = 740 / 15
        assert punkt['avg'] == pytest.approx(740 / 15)

    def test_extremwerte_bleiben_erhalten(self, store):
        """Eine kurze Spitze darf im Mittel nicht verschwinden.

        Ohne min/max sagt ein Stundenmittel von 52 °C nichts darüber, ob
        zwischendrin 78 °C anlagen — und genau dafür schaut man hin.
        """
        for i in range(15):
            store.add({'temperature_c': 78.0 if i == 7 else 50.0},
                      ts=i * MINUTE)
        store.compact(jetzt=8 * TAG)
        punkt = store.series('temperature_c', 9 * TAG,
                             jetzt=8 * TAG)['points'][0]
        assert punkt['max'] == 78.0
        assert punkt['min'] == 50.0
        assert punkt['avg'] < 55.0        # das Mittel verbirgt die Spitze

    def test_laufendes_rasterfeld_bleibt_unangetastet(self, store):
        """Frische Werte dürfen nicht verdichtet werden.

        Sonst entstünde ein Mittel aus einer halben Viertelstunde, das
        später nicht mehr ergänzt werden kann.
        """
        jetzt = 100 * TAG
        fuelle(store, jetzt - 10 * MINUTE, 10)
        ergebnis = store.compact(jetzt=jetzt)
        assert ergebnis['quarter'] == 0
        assert store.count('raw') == 10

    def test_rohdaten_werden_nach_frist_geloescht(self, store):
        jetzt = 100 * TAG
        fuelle(store, jetzt - 10 * TAG, 30)      # 30 min, 10 Tage alt
        fuelle(store, jetzt - 2 * TAG, 30)       # 30 min, 2 Tage alt
        store.compact(jetzt=jetzt)
        # Nur die frischen bleiben roh liegen.
        assert store.count('raw') == 30
        assert store.count('quarter') > 0

    def test_viertelstunden_werden_zu_stunden(self, store):
        jetzt = 200 * TAG
        # 4 Stunden Messwerte, 100 Tage alt -> ueber beide Fristen hinaus.
        fuelle(store, jetzt - 100 * TAG, 240)
        store.compact(jetzt=jetzt)
        assert store.count('raw') == 0
        assert store.count('quarter') == 0
        assert store.count('hour') == 4

    def test_gewichtetes_mittel_beim_zweiten_schritt(self, store):
        """Ein Viertelstundenfeld mit weniger Messpunkten wiegt weniger.

        Ungewichtet käme (60+40)/2 = 50 heraus. Richtig sind
        (60*15 + 40*5) / 20 = 55.
        """
        jetzt = 200 * TAG
        basis = jetzt - 100 * TAG
        basis = basis - (basis % HOUR)           # auf volle Stunde legen
        for i in range(15):                      # erstes Viertel: 15 Punkte
            store.add({'temperature_c': 60.0}, ts=basis + i * MINUTE)
        for i in range(5):                       # zweites Viertel: 5 Punkte
            store.add({'temperature_c': 40.0}, ts=basis + QUARTER + i * MINUTE)

        store.compact(jetzt=jetzt)
        punkte = store.series('temperature_c', 150 * TAG,
                              jetzt=jetzt)['points']
        assert len(punkte) == 1
        assert punkte[0]['avg'] == pytest.approx(55.0)
        assert punkte[0]['max'] == 60.0
        assert punkte[0]['min'] == 40.0


class TestAbruf:
    def test_zeitraum_begrenzt_die_punkte(self, store):
        jetzt = 100 * TAG
        fuelle(store, jetzt - 120 * MINUTE, 120)
        reihe = store.series('temperature_c', 30 * MINUTE, jetzt=jetzt)
        assert len(reihe['points']) == 30

    def test_leere_grobe_stufe_faellt_auf_rohdaten_zurueck(self, store):
        """Frisch gestartet gibt es nur Rohdaten — 24 h darf nicht leer sein."""
        jetzt = 100 * TAG
        fuelle(store, jetzt - 60 * MINUTE, 60)
        reihe = store.series('temperature_c', 24 * HOUR, jetzt=jetzt)
        assert reihe['level'] == 'raw'
        assert len(reihe['points']) == 60

    def test_punkte_sind_chronologisch(self, store):
        jetzt = 100 * TAG
        fuelle(store, jetzt - 10 * MINUTE, 10)
        punkte = store.series('temperature_c', HOUR, jetzt=jetzt)['points']
        zeiten = [p['t'] for p in punkte]
        assert zeiten == sorted(zeiten)


class TestSammler:
    def test_holt_werte_aus_der_quelle(self, store):
        quelle = lambda: {
            'temperature_c': 55.0,
            'load': {'load_percent': 12.5},
            'memory': {'used_percent': 33.0},
            'disk': {'used_percent': 44.0},
        }
        sammler = HistoryCollector(store, quelle)
        assert sammler.collect_once() is True
        assert sammler.collected == 1
        assert store.count('raw') == 1

    def test_fehlende_teilwerte_werden_zu_none(self, store):
        """Fehlt ein Block, darf der Rest trotzdem gespeichert werden."""
        quelle = lambda: {'temperature_c': 50.0, 'load': None,
                          'memory': None, 'disk': None}
        sammler = HistoryCollector(store, quelle)
        assert sammler.collect_once() is True
        reihe = store.series('load_percent', HOUR)
        assert reihe['points'][0]['avg'] is None

    def test_fehler_der_quelle_stoppt_nicht(self, store):
        """Ein Ausfall hier darf die Impulserfassung nie beeinträchtigen."""
        def kaputt():
            raise RuntimeError('Quelle weg')
        sammler = HistoryCollector(store, kaputt)
        assert sammler.collect_once() is False
        assert sammler.failed == 1
        assert sammler.collected == 0
