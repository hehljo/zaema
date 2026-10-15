"""Aufzeichnung der Hausbilanz und Verteilung des Netzbezugs."""
import pytest

from s0counter.balance import (Allocator, BalanceCollector, BalanceStore,
                               share_proportional, share_waterfall)
from s0counter.db import Database
from s0counter.sources import Reading, SourceError

HOUR = 3600.0
QUARTER = 900.0


def reading(ts, production=0.0, consumption=0.0, grid=0.0, battery=0.0,
            percent=50.0, source='test'):
    return Reading(ts=ts, production_w=production, consumption_w=consumption,
                   grid_w=grid, battery_w=battery, battery_percent=percent,
                   source=source)


@pytest.fixture
def store(tmp_path):
    balance = BalanceStore(str(tmp_path / 'balance.sqlite3'))
    yield balance
    balance.close()


@pytest.fixture
def db(tmp_path):
    database = Database(str(tmp_path / 'pulses.sqlite3'))
    for meter_id, name in [('eg', 'Strom EG'), ('og', 'Strom OG'),
                           ('dg', 'Strom DG')]:
        database.ensure_meter(meter_id, name, 1000)
    yield database
    database.close()


class TestAblage:
    def test_messpunkt_speichern_und_lesen(self, store):
        store.add(reading(1000.0, production=1850.0, grid=-28.0))
        row = store.latest()
        assert row[0] == 1000.0
        assert row[1] == 1850.0
        assert row[3] == -28.0

    def test_gleicher_zeitstempel_ersetzt(self, store):
        store.add(reading(1000.0, production=100.0))
        store.add(reading(1000.0, production=200.0))
        assert store.count() == 1
        assert store.latest()[1] == 200.0

    def test_luecke_festhalten(self, store):
        store.note_gap(1000.0, 2000.0, 'Quelle nicht erreichbar')
        gaps = store.gaps(0.0, 5000.0)
        assert len(gaps) == 1
        assert gaps[0][2] == 'Quelle nicht erreichbar'

    def test_luecken_ausserhalb_des_zeitraums(self, store):
        store.note_gap(1000.0, 2000.0, 'alt')
        assert store.gaps(5000.0, 9000.0) == []


class TestVerbrauchsenergie:
    """Hausverbrauch als Energie — Grundlage des Langzeitvergleichs.

    Die Momentanleistungen von Zählern und Bilanz sind nicht vergleichbar:
    ein Zähler meldet aus dem Impulsabstand, die Bilanz alle 60 s. Über die
    Energie mittelt sich das heraus.
    """

    def test_konstante_last_ueber_eine_stunde(self, store):
        # 1000 W, alle 60 s ein Messpunkt, eine Stunde lang.
        for i in range(61):
            store.add(reading(1000.0 + i * 60.0, consumption=1000.0))
        kwh, sekunden, punkte = store.consumption_energy()
        assert punkte == 61
        assert sekunden == pytest.approx(HOUR)
        assert kwh == pytest.approx(1.0)

    def test_wechselnde_last(self, store):
        # 30 min bei 2000 W, danach 30 min bei 0 W -> 1,0 kWh.
        for i in range(30):
            store.add(reading(1000.0 + i * 60.0, consumption=2000.0))
        for i in range(30, 61):
            store.add(reading(1000.0 + i * 60.0, consumption=0.0))
        kwh, _, _ = store.consumption_energy()
        assert kwh == pytest.approx(1.0)

    def test_einzelner_messpunkt_ergibt_nichts(self, store):
        store.add(reading(1000.0, consumption=5000.0))
        kwh, sekunden, punkte = store.consumption_energy()
        assert (kwh, sekunden, punkte) == (0.0, 0.0, 1)

    def test_leere_reihe(self, store):
        assert store.consumption_energy() == (0.0, 0.0, 0)

    def test_ausfall_wird_gedeckelt(self, store):
        """Nach einem Ausfall darf ein Messpunkt nicht stundenlang gelten.

        Ohne Deckel stünde der Wert vor der Lücke für die gesamte Ausfall-
        dauer und würde die Summe weit nach oben ziehen.
        """
        for i in range(10):
            store.add(reading(1000.0 + i * 60.0, consumption=1000.0))
        # Sechs Stunden Lücke, dann geht es weiter.
        store.add(reading(1000.0 + 9 * 60.0 + 6 * HOUR, consumption=1000.0))
        for i in range(1, 10):
            store.add(reading(1000.0 + 9 * 60.0 + 6 * HOUR + i * 60.0,
                              consumption=1000.0))

        kwh, sekunden, _ = store.consumption_energy()
        # 18 echte Abstände à 60 s plus der gedeckelte Ausfall (2 × Median).
        assert sekunden == pytest.approx(18 * 60.0 + 120.0)
        # Ohne Deckel wären es 6 h × 1 kW = 6,0 kWh mehr.
        assert kwh < 0.4

    def test_start_begrenzt_den_zeitraum(self, store):
        for i in range(21):
            store.add(reading(1000.0 + i * 60.0, consumption=1000.0))
        ganz, _, _ = store.consumption_energy()
        teil, _, _ = store.consumption_energy(start_ts=1000.0 + 10 * 60.0)
        assert teil == pytest.approx(ganz / 2, rel=0.01)

    def test_erster_zeitstempel(self, store):
        assert store.first_ts() is None
        store.add(reading(5000.0, consumption=100.0))
        store.add(reading(1000.0, consumption=100.0))
        assert store.first_ts() == 1000.0


class TestIntervallEnergie:
    def test_konstante_leistung_ergibt_energie(self, store):
        """1000 W über eine Viertelstunde sind 0,25 kWh."""
        for i in range(15):
            store.add(reading(i * 60.0, grid=1000.0))

        results = list(store.iter_interval_energy(0.0, QUARTER, QUARTER))
        assert len(results) == 1
        _, grid_kwh, _, samples = results[0]
        assert grid_kwh == pytest.approx(0.25)
        assert samples == 15

    def test_einspeisung_zaehlt_nicht_als_bezug(self, store):
        """Negativer grid_w ist Einspeisung und darf nicht mitzählen."""
        for i in range(15):
            store.add(reading(i * 60.0, grid=-500.0))

        results = list(store.iter_interval_energy(0.0, QUARTER, QUARTER))
        assert results[0][1] == pytest.approx(0.0)

    def test_gemischtes_intervall(self, store):
        """Die Hälfte der Zeit Bezug, die Hälfte Einspeisung."""
        for i in range(10):
            store.add(reading(i * 60.0, grid=1000.0))
        for i in range(10, 20):
            store.add(reading(i * 60.0, grid=-1000.0))

        results = list(store.iter_interval_energy(0.0, 20 * 60.0, 20 * 60.0))
        # Mittelwert nur über die Bezugsanteile: 10 × 1000 W / 20 Punkte
        assert results[0][1] == pytest.approx(500.0 * (20 / 60) / 1000.0)

    def test_intervall_ohne_messpunkt_wird_uebersprungen(self, store):
        """Fehlende Daten werden nicht interpoliert."""
        for i in range(15):
            store.add(reading(i * 60.0, grid=1000.0))
        # zweites Intervall bleibt leer, drittes wieder belegt
        for i in range(30, 45):
            store.add(reading(i * 60.0, grid=1000.0))

        results = list(store.iter_interval_energy(0.0, 3 * QUARTER, QUARTER))
        assert len(results) == 2
        assert [r[0] for r in results] == [0.0, 2 * QUARTER]


class TestVerteilmodelle:
    """Die beiden Modelle an dem Beispiel, das wir durchgesprochen haben.

    Haus 1900 W, PV 1000 W, also 900 W Netzbezug. In einer Viertelstunde
    sind das 0,225 kWh. EG verbraucht 1400 W (0,35 kWh), OG 300 W
    (0,075 kWh), DG 200 W (0,05 kWh).
    """

    VERBRAUCH = {'eg': 0.350, 'og': 0.075, 'dg': 0.050}
    NETZ = 0.225

    def test_anteilig_gleicher_prozentsatz_fuer_alle(self):
        anteile = share_proportional(self.VERBRAUCH, self.NETZ)

        assert sum(anteile.values()) == pytest.approx(self.NETZ)
        # Jede Wohnung deckt denselben Anteil ihres Verbrauchs aus dem Netz
        quoten = {m: anteile[m] / self.VERBRAUCH[m] for m in self.VERBRAUCH}
        assert quoten['eg'] == pytest.approx(quoten['og'])
        assert quoten['og'] == pytest.approx(quoten['dg'])
        assert quoten['eg'] == pytest.approx(0.4737, abs=0.001)

    def test_wasserfall_deckelt_bei_kleinverbrauchern(self):
        """Wer weniger verbraucht als sein Drittel, gibt den Rest ab."""
        anteile = share_waterfall(self.VERBRAUCH, self.NETZ)

        assert sum(anteile.values()) == pytest.approx(self.NETZ)
        # DG und OG zahlen ihren vollen Verbrauch, EG den Rest
        assert anteile['dg'] == pytest.approx(0.050)
        assert anteile['og'] == pytest.approx(0.075)
        assert anteile['eg'] == pytest.approx(0.100)

    def test_die_modelle_laufen_deutlich_auseinander(self):
        anteilig = share_proportional(self.VERBRAUCH, self.NETZ)
        wasserfall = share_waterfall(self.VERBRAUCH, self.NETZ)

        # Der Großverbraucher zahlt im Wasserfall deutlich weniger
        assert wasserfall['eg'] < anteilig['eg'] * 0.7
        # Die Kleinverbraucher deutlich mehr
        assert wasserfall['dg'] > anteilig['dg'] * 1.5

    def test_niemand_bekommt_mehr_als_er_verbraucht_hat(self):
        """Die Eigenschaft, die beide Modelle einhalten müssen."""
        for modell in (share_proportional, share_waterfall):
            anteile = modell(self.VERBRAUCH, self.NETZ)
            for meter_id, wert in anteile.items():
                assert wert <= self.VERBRAUCH[meter_id] + 1e-9, modell.__name__

    def test_deckel_bei_netzbezug_ueber_dem_wohnungsverbrauch(self):
        """Wärmepumpe oder Allgemeinstrom laufen hinter keinem Zähler mit.

        Der Netzbezug des Hauses ist dann höher als die Summe der
        Wohnungsverbräuche. Keiner Wohnung darf mehr zugerechnet werden,
        als sie verbraucht hat — der Rest gehört zur Allgemeinheit und
        bleibt unverteilt.
        """
        gesamt = sum(self.VERBRAUCH.values())     # 0,475 kWh
        netz = 0.800                              # deutlich mehr

        for modell in (share_proportional, share_waterfall):
            anteile = modell(self.VERBRAUCH, netz)
            for meter_id, wert in anteile.items():
                assert wert <= self.VERBRAUCH[meter_id] + 1e-9, \
                    f'{modell.__name__}: {meter_id} bekommt {wert:.3f} kWh ' \
                    f'bei {self.VERBRAUCH[meter_id]:.3f} kWh Verbrauch'
            assert sum(anteile.values()) == pytest.approx(gesamt), \
                modell.__name__

    def test_voller_netzbezug_ohne_pv(self):
        """Nachts deckt jede Wohnung alles aus dem Netz, in beiden Modellen."""
        netz = sum(self.VERBRAUCH.values())
        for modell in (share_proportional, share_waterfall):
            anteile = modell(self.VERBRAUCH, netz)
            for meter_id, wert in anteile.items():
                assert wert == pytest.approx(self.VERBRAUCH[meter_id]), \
                    modell.__name__

    def test_kein_netzbezug(self):
        for modell in (share_proportional, share_waterfall):
            anteile = modell(self.VERBRAUCH, 0.0)
            assert sum(anteile.values()) == pytest.approx(0.0)

    def test_gleicher_verbrauch_ergibt_gleiche_anteile(self):
        gleich = {'eg': 0.1, 'og': 0.1, 'dg': 0.1}
        for modell in (share_proportional, share_waterfall):
            anteile = modell(gleich, 0.15)
            assert anteile['eg'] == pytest.approx(0.05), modell.__name__
            assert anteile['og'] == pytest.approx(0.05), modell.__name__
            assert anteile['dg'] == pytest.approx(0.05), modell.__name__

    def test_wasserfall_mehrere_ueberlaufrunden(self):
        """Zwei Wohnungen laufen nacheinander über."""
        verbrauch = {'a': 1.0, 'b': 0.05, 'c': 0.10}
        anteile = share_waterfall(verbrauch, 0.60)

        assert anteile['b'] == pytest.approx(0.05)
        assert anteile['c'] == pytest.approx(0.10)
        assert anteile['a'] == pytest.approx(0.45)
        assert sum(anteile.values()) == pytest.approx(0.60)

    def test_wasserfall_netzbezug_ueber_dem_verbrauch(self):
        """Mehr Netzbezug als Wohnungsverbrauch — der Rest bleibt übrig."""
        anteile = share_waterfall({'eg': 0.1, 'og': 0.1}, 0.5)
        assert sum(anteile.values()) == pytest.approx(0.2)

    def test_einzelne_wohnung(self):
        for modell in (share_proportional, share_waterfall):
            assert modell({'eg': 0.5}, 0.3)['eg'] == pytest.approx(0.3)

    def test_wohnung_ohne_verbrauch_bekommt_nichts(self):
        verbrauch = {'eg': 0.3, 'og': 0.0}
        for modell in (share_proportional, share_waterfall):
            anteile = modell(verbrauch, 0.2)
            assert anteile['og'] == pytest.approx(0.0), modell.__name__
            assert anteile['eg'] == pytest.approx(0.2), modell.__name__


class TestVerteilung:
    def test_anteilig_nach_verbrauch(self, store, db):
        """Der Fall aus der Absprache: Haus 1900 W, PV 1000 W, Netz 900 W.

        EG verbraucht 1400 W, OG 300 W, DG 200 W. Erwartet wird eine
        Verteilung im Verhältnis der Verbräuche, nicht zu gleichen Teilen.
        """
        for i in range(15):
            store.add(reading(i * 60.0, production=1000.0,
                              consumption=1900.0, grid=900.0))

        # Impulse einer Viertelstunde: 1400 W entsprechen 350 Wh = 350 Impulse
        for i in range(350):
            db.add_pulse('eg', i * 2.0, i * 2.0)
        for i in range(75):
            db.add_pulse('og', i * 10.0, i * 10.0)
        for i in range(50):
            db.add_pulse('dg', i * 15.0, i * 15.0)

        result = Allocator(store, db, QUARTER).allocate(0.0, QUARTER)
        grid = {m: v['grid_kwh'] for m, v in result['meters'].items()}
        total = sum(grid.values())

        assert total == pytest.approx(0.225, abs=0.001)   # 900 W × 0,25 h
        # Anteile: 350/475, 75/475, 50/475
        assert grid['eg'] == pytest.approx(0.225 * 350 / 475, abs=0.001)
        assert grid['og'] == pytest.approx(0.225 * 75 / 475, abs=0.001)
        assert grid['dg'] == pytest.approx(0.225 * 50 / 475, abs=0.001)

        # Die entscheidende Eigenschaft: niemand bekommt mehr Netzbezug
        # zugerechnet, als er überhaupt verbraucht hat.
        for meter_id, values in result['meters'].items():
            assert values['grid_kwh'] <= values['consumption_kwh'] + 1e-9

    def test_ohne_netzbezug_wird_nichts_verteilt(self, store, db):
        """Volle PV-Deckung: alle Wohnungen bekommen 0 kWh Netzbezug."""
        for i in range(15):
            store.add(reading(i * 60.0, production=3000.0,
                              consumption=1000.0, grid=-2000.0))
        for i in range(250):
            db.add_pulse('eg', i * 3.0, i * 3.0)

        result = Allocator(store, db, QUARTER).allocate(0.0, QUARTER)
        assert result['meters']['eg']['grid_kwh'] == pytest.approx(0.0)
        assert result['meters']['eg']['consumption_kwh'] == pytest.approx(0.25)
        assert result['meters']['eg']['solar_kwh'] == pytest.approx(0.25)

    def test_netzbezug_ohne_wohnungsverbrauch_bleibt_unverteilt(self, store, db):
        """Verbraucher hinter keinem der drei Zähler.

        Wenn das Haus Strom bezieht, aber keine Wohnung einen Impuls
        liefert, gehört der Bezug zur Allgemeinheit — Wärmepumpe,
        Allgemeinstrom, Ladepunkt. Er darf nicht auf die Wohnungen
        umgelegt werden.
        """
        for i in range(15):
            store.add(reading(i * 60.0, grid=800.0))

        result = Allocator(store, db, QUARTER).allocate(0.0, QUARTER)

        assert result['house']['unallocated_grid_kwh'] == pytest.approx(0.2)
        assert result['quality']['intervals_without_pulses'] == 1
        for values in result['meters'].values():
            assert values['grid_kwh'] == pytest.approx(0.0)

    def test_intervalle_werden_getrennt_gerechnet(self, store, db):
        """Der Kern des Verfahrens: nachts anders als mittags.

        Erste Viertelstunde: nur EG verbraucht, voller Netzbezug.
        Zweite Viertelstunde: nur DG verbraucht, kein Netzbezug (PV).

        Eine Jahresrechnung über Summen würde beiden denselben Anteil
        geben. Intervallweise bekommt nur EG den Netzbezug.
        """
        for i in range(15):
            store.add(reading(i * 60.0, grid=1000.0))
        for i in range(15, 30):
            store.add(reading(i * 60.0, grid=-1000.0))

        for i in range(250):
            db.add_pulse('eg', i * 3.0, i * 3.0)
        for i in range(250):
            db.add_pulse('dg', QUARTER + i * 3.0, QUARTER + i * 3.0)

        result = Allocator(store, db, QUARTER).allocate(0.0, 2 * QUARTER)

        assert result['meters']['eg']['grid_kwh'] == pytest.approx(0.25)
        assert result['meters']['dg']['grid_kwh'] == pytest.approx(0.0)
        # Verbraucht haben beide gleich viel
        assert result['meters']['eg']['consumption_kwh'] == pytest.approx(0.25)
        assert result['meters']['dg']['consumption_kwh'] == pytest.approx(0.25)

    def test_summe_der_anteile_ergibt_den_hausbezug(self, store, db):
        for i in range(60):
            store.add(reading(i * 60.0, grid=500.0 + i * 10))
        for i in range(600):
            db.add_pulse('eg', i * 6.0, i * 6.0)
        for i in range(300):
            db.add_pulse('og', i * 12.0, i * 12.0)

        result = Allocator(store, db, QUARTER).allocate(0.0, HOUR)
        verteilt = sum(v['grid_kwh'] for v in result['meters'].values())
        unverteilt = result['house']['unallocated_grid_kwh']

        assert verteilt + unverteilt == pytest.approx(
            result['house']['grid_kwh'], abs=1e-9)


class TestNichtZugeordnet:
    """Zwei Ursachen, zwei Zahlen.

    Beide sehen in der Summe gleich aus, erklären sich aber verschieden.
    Wer sie zusammenwirft, kann in der Anzeige nur einen Grund nennen und
    liegt im anderen Fall daneben — genau das ist im Browsertest vom
    25.08.2026 aufgefallen.
    """

    def test_ohne_impulse_zaehlt_als_still(self, store, db):
        for i in range(15):
            store.add(reading(i * 60.0, grid=800.0))

        haus = Allocator(store, db, QUARTER).allocate(0.0, QUARTER)['house']
        assert haus['unallocated_idle_kwh'] == pytest.approx(0.2)
        assert haus['unallocated_capped_kwh'] == pytest.approx(0.0)

    def test_deckel_zaehlt_getrennt(self, store, db):
        """Wohnungen verbrauchen, aber weniger als das Haus bezieht."""
        for i in range(15):
            store.add(reading(i * 60.0, consumption=2000.0, grid=2000.0))
        for i in range(100):          # 0,1 kWh gegen 0,5 kWh Hausbezug
            db.add_pulse('eg', i * 9.0, i * 9.0)

        haus = Allocator(store, db, QUARTER).allocate(0.0, QUARTER)['house']
        assert haus['unallocated_idle_kwh'] == pytest.approx(0.0)
        assert haus['unallocated_capped_kwh'] == pytest.approx(0.4, abs=0.001)

    def test_summe_bleibt_die_alte_zahl(self, store, db):
        """Die Aufteilung darf den Gesamtposten nicht verändern."""
        for i in range(15):
            store.add(reading(i * 60.0, consumption=2000.0, grid=2000.0))
        for i in range(15):
            store.add(reading(QUARTER + i * 60.0, grid=800.0))
        for i in range(100):
            db.add_pulse('eg', i * 9.0, i * 9.0)

        haus = Allocator(store, db, QUARTER).allocate(0.0, 2 * QUARTER)['house']
        assert haus['unallocated_grid_kwh'] == pytest.approx(
            haus['unallocated_idle_kwh'] + haus['unallocated_capped_kwh'])
        assert haus['unallocated_idle_kwh'] > 0
        assert haus['unallocated_capped_kwh'] > 0

    def test_beide_modelle_deckeln(self, store, db):
        """Der Deckel ist kein Sonderfall des Wasserfallmodells."""
        for i in range(15):
            store.add(reading(i * 60.0, consumption=2000.0, grid=2000.0))
        for i in range(100):
            db.add_pulse('eg', i * 9.0, i * 9.0)

        for modell in ('proportional', 'waterfall'):
            haus = Allocator(store, db, QUARTER, modell).allocate(
                0.0, QUARTER)['house']
            assert haus['unallocated_capped_kwh'] > 0, modell


class TestAusfallErkennen:
    """Ein Ausfall des Rechners hinterlässt keinen Eintrag.

    `note_gap()` schreibt nur, wenn der Dienst läuft und die Quelle
    ausfällt. Bei Stromausfall, Absturz oder Update ist der Rechner selbst
    weg — dann fehlen die Messpunkte und niemand konnte etwas vermerken.
    `detect_gaps()` rekonstruiert das aus den Zeitstempeln.
    """

    def _reihe(self, store, bis_minuten, aussparen=()):
        for i in range(bis_minuten):
            ts = i * 60.0
            if any(a <= ts < b for a, b in aussparen):
                continue
            store.add(reading(ts, grid=1000.0))

    def test_luecke_ohne_eintrag_wird_gefunden(self, store):
        self._reihe(store, 240, aussparen=[(3600.0, 7200.0)])

        luecken = store.detect_gaps(0.0, 240 * 60.0)
        assert len(luecken) == 1
        start, ende, dauer = luecken[0]
        assert start == pytest.approx(3540.0)
        assert ende == pytest.approx(7200.0)
        assert dauer == pytest.approx(3660.0)

    def test_durchgehende_reihe_meldet_nichts(self, store):
        """Die Gegenrichtung: ein Gate, das gesunde Daten rot meldet,
        wird abgeschaltet und schützt danach gar nichts mehr."""
        self._reihe(store, 240)
        assert store.detect_gaps(0.0, 240 * 60.0) == []

    def test_einzelner_aussetzer_gilt_nicht_als_ausfall(self, store):
        """Ein verschluckter Messpunkt ist kein Ausfall — sonst meldete
        die Anzeige dauernd Lücken und würde ignoriert."""
        self._reihe(store, 240, aussparen=[(3600.0, 3660.0)])
        assert store.detect_gaps(0.0, 240 * 60.0) == []

    def test_mehrere_luecken(self, store):
        self._reihe(store, 600,
                    aussparen=[(3600.0, 7200.0), (18000.0, 21600.0)])
        assert len(store.detect_gaps(0.0, 600 * 60.0)) == 2

    def test_spaeter_start_ist_eine_luecke(self, store):
        """Der Dienst wurde mitten im Zeitraum gestartet."""
        for i in range(120):
            store.add(reading(7200.0 + i * 60.0, grid=1000.0))

        luecken = store.detect_gaps(0.0, 14400.0)
        assert luecken
        assert luecken[0][0] == pytest.approx(0.0)
        assert luecken[0][1] == pytest.approx(7200.0)

    def test_frueher_stopp_ist_eine_luecke(self, store):
        self._reihe(store, 60)
        luecken = store.detect_gaps(0.0, 14400.0)
        assert luecken
        assert luecken[-1][1] == pytest.approx(14400.0)

    def test_leerer_zeitraum_ist_eine_einzige_luecke(self, store):
        luecken = store.detect_gaps(0.0, 3600.0)
        assert luecken == [(0.0, 3600.0, 3600.0)]

    def test_zu_wenige_punkte_melden_nichts(self, store):
        """Ohne Anhaltspunkt für den Abstand lieber nichts behaupten."""
        store.add(reading(0.0, grid=100.0))
        store.add(reading(60.0, grid=100.0))
        assert store.detect_gaps(0.0, 3600.0) == []

    def test_abstand_wird_aus_den_daten_genommen(self, store):
        """Bei einem 5-Minuten-Takt ist eine 6-Minuten-Pause kein Ausfall."""
        for i in range(60):
            store.add(reading(i * 300.0, grid=1000.0))
        assert store.detect_gaps(0.0, 60 * 300.0) == []

    def test_luecke_erscheint_in_der_abrechnung(self, store, db):
        """Bis zum Ergebnis geprüft, nicht nur bis zur Hilfsfunktion."""
        for i in range(240):
            ts = i * 60.0
            if 3600.0 <= ts < 7200.0:
                continue
            store.add(reading(ts, consumption=1200.0, grid=1200.0))
        for i in range(400):
            db.add_pulse('eg', i * 30.0, i * 30.0)

        quality = Allocator(store, db, QUARTER).allocate(
            0.0, 240 * 60.0)['quality']
        assert len(quality['detected_gaps']) == 1
        assert quality['detected_gaps'][0]['seconds'] == pytest.approx(3660.0)
        # Der vermerkte Weg bleibt leer — genau das ist der Punkt.
        assert quality['gaps'] == []


class TestBelastbarkeit:
    def test_abdeckung_wird_ausgewiesen(self, store, db):
        """Nur zwei von vier Viertelstunden haben Daten."""
        for i in range(15):
            store.add(reading(i * 60.0, grid=100.0))
        for i in range(15):
            store.add(reading(2 * QUARTER + i * 60.0, grid=100.0))

        result = Allocator(store, db, QUARTER).allocate(0.0, HOUR)
        assert result['quality']['intervals_used'] == 2
        assert result['quality']['intervals_expected'] == 4
        assert result['quality']['coverage'] == pytest.approx(0.5)

    def test_luecken_erscheinen_im_ergebnis(self, store, db):
        store.add(reading(0.0, grid=100.0))
        store.note_gap(100.0, 200.0, 'Quelle nicht erreichbar')

        result = Allocator(store, db, QUARTER).allocate(0.0, HOUR)
        assert len(result['quality']['gaps']) == 1


class TestZaehlungGleichwertig:
    """Die SQL-Zählung muss dasselbe liefern wie die Schleife.

    `_pulses_per_interval()` zählt in SQLite, `_pulses_per_interval_slow()`
    ist die geprüfte Vorgängerfassung als Python-Schleife. Weicht eine von
    der anderen ab, sind die Abrechnungszahlen falsch — deshalb wird hier
    nicht die Laufzeit verglichen, sondern das Ergebnis, Feld für Feld.
    """

    def _vergleiche(self, db, start, ende, intervall):
        """Beide schnellen Wege gegen die Referenzschleife.

        `pulses_per_bucket` hat zwei Pfade — über das Minutenaggregat und
        über die Rohimpulse. Geprüft werden beide, sonst bliebe der
        Aggregatpfad ungetestet, sobald ein Testfall auf krummen Grenzen
        liegt (oder umgekehrt).
        """
        allocator = Allocator(None, db, intervall)
        meter_ids = db.meter_ids()
        putzen = lambda d: {b: e for b, e in d.items() if e}

        langsam = putzen(
            allocator._pulses_per_interval_slow(meter_ids, start, ende))
        schnell = putzen(
            allocator._pulses_per_interval(meter_ids, start, ende))
        assert schnell == langsam

        # Denselben Vergleich noch einmal, aber erzwungen über die
        # Rohimpulse — damit der Rückfallweg nicht unbemerkt kaputtgeht.
        roh = {}
        for meter_id, feld, anzahl in db._buckets_aus_rohimpulsen(
                start, ende, intervall):
            if meter_id in set(meter_ids):
                roh.setdefault(start + feld * intervall, {})[meter_id] = anzahl
        assert putzen(roh) == langsam, 'Rohimpuls-Pfad weicht ab'

        return schnell

    def test_gleiche_zaehlung_bei_gemischter_last(self, db):
        for i in range(350):
            db.add_pulse('eg', i * 2.0, i * 2.0)
        for i in range(75):
            db.add_pulse('og', i * 10.0, i * 10.0)
        for i in range(50):
            db.add_pulse('dg', 1800.0 + i * 15.0, i * 15.0)

        ergebnis = self._vergleiche(db, 0.0, 4 * QUARTER, QUARTER)
        assert ergebnis, 'Der Vergleich lief gegen leere Daten'

    def test_gleiche_zaehlung_ueber_viele_intervalle(self, db):
        """Ein voller Tag im 15-Minuten-Raster."""
        for i in range(2000):
            db.add_pulse('eg', i * 43.0, i * 43.0)
        for i in range(500):
            db.add_pulse('og', i * 170.0, i * 170.0)

        ergebnis = self._vergleiche(db, 0.0, 86400.0, QUARTER)
        assert len(ergebnis) > 50

    def test_impuls_auf_der_intervallgrenze(self, db):
        """Ein Impuls exakt auf der Grenze gehört ins spätere Intervall."""
        db.add_pulse('eg', QUARTER, 1.0)
        db.add_pulse('eg', QUARTER - 0.001, 2.0)

        ergebnis = self._vergleiche(db, 0.0, 2 * QUARTER, QUARTER)
        assert ergebnis[0.0]['eg'] == 1
        assert ergebnis[QUARTER]['eg'] == 1

    def test_impuls_genau_auf_start_und_ende(self, db):
        """`start_ts` zählt mit, `end_ts` nicht — sonst doppelte Zählung.

        Zwei aneinandergrenzende Abfragen dürfen denselben Impuls nicht
        beide erfassen, sonst taucht Energie zweimal in der Abrechnung auf.
        """
        db.add_pulse('eg', 0.0, 1.0)
        db.add_pulse('eg', QUARTER, 2.0)

        erste = self._vergleiche(db, 0.0, QUARTER, QUARTER)
        zweite = self._vergleiche(db, QUARTER, 2 * QUARTER, QUARTER)
        assert erste[0.0]['eg'] == 1
        assert zweite[QUARTER]['eg'] == 1

    def test_versetzter_start_kein_rasterversatz(self, db):
        """Der Raster-Ursprung ist `start_ts`, nicht 0.

        Läge er bei 0, würden Impulse gegen den Netzbezug eines anderen
        Zeitfensters verrechnet — die Verteilung wäre still falsch.
        """
        start = 1234.5
        for i in range(40):
            db.add_pulse('eg', start + i * 60.0, i * 60.0)

        ergebnis = self._vergleiche(db, start, start + 4 * QUARTER, QUARTER)
        assert min(ergebnis) == start

    def test_zeitraum_ohne_impulse(self, db):
        db.add_pulse('eg', 10.0, 10.0)
        assert self._vergleiche(db, 10_000.0, 20_000.0, QUARTER) == {}

    def test_impulse_ausserhalb_bleiben_draussen(self, db):
        """Impulse vor `start_ts` und ab `end_ts` gehören nicht dazu."""
        db.add_pulse('eg', 100.0, 1.0)            # davor
        db.add_pulse('eg', QUARTER + 100.0, 2.0)  # mittendrin
        db.add_pulse('eg', 10 * QUARTER, 3.0)     # danach

        ergebnis = self._vergleiche(db, QUARTER, 2 * QUARTER, QUARTER)
        assert sum(e['eg'] for e in ergebnis.values()) == 1

    def test_verteilung_stimmt_mit_beiden_zaehlungen_ueberein(self, store, db):
        """Der Vergleich bis zum Ende: gleiche kWh, nicht nur gleiche Impulse.

        Gleiche Zählung ist die Voraussetzung — was zählt, ist dass hinten
        dieselben Abrechnungszahlen herauskommen.
        """
        for i in range(60):
            store.add(reading(i * 60.0, production=800.0,
                              consumption=2000.0, grid=1200.0))
        for i in range(600):
            db.add_pulse('eg', i * 6.0, i * 6.0)
        for i in range(200):
            db.add_pulse('og', i * 18.0, i * 18.0)

        allocator = Allocator(store, db, QUARTER)
        schnell = allocator.allocate(0.0, HOUR)

        # Dieselbe Rechnung, aber mit der Referenzzählung.
        original = allocator._pulses_per_interval
        allocator._pulses_per_interval = allocator._pulses_per_interval_slow
        try:
            langsam = allocator.allocate(0.0, HOUR)
        finally:
            allocator._pulses_per_interval = original

        for meter_id, werte in schnell['meters'].items():
            for feld, wert in werte.items():
                assert wert == pytest.approx(
                    langsam['meters'][meter_id][feld], abs=1e-9), (
                    f'{meter_id}.{feld} weicht ab')
        assert schnell['house'] == pytest.approx(langsam['house'])


class TestAggregatPfad:
    """Das Minutenaggregat darf nur greifen, wo es exakt sein kann."""

    def test_viertelstunde_auf_minutengrenze_nutzt_aggregat(self, db):
        assert db._aggregat_nutzbar(0.0, 3600.0, 900.0)

    def test_krummes_intervall_faellt_zurueck(self, db):
        """90 s sind kein Vielfaches von 60 — eine Minute läge in zwei
        Intervallen und würde falsch einsortiert."""
        assert not db._aggregat_nutzbar(0.0, 3600.0, 90.0)

    def test_krummer_start_faellt_zurueck(self, db):
        assert not db._aggregat_nutzbar(30.0, 3630.0, 900.0)

    def test_intervall_unter_einer_minute_faellt_zurueck(self, db):
        assert not db._aggregat_nutzbar(0.0, 3600.0, 30.0)

    def test_fliesskommarest_kippt_nicht_auf_den_langsamen_weg(self, db):
        """Ein Rundungsrest darf die Pfadwahl nicht umwerfen.

        Der Startwert ist eine echte Minutengrenze (1787599980 = 29793333 × 60)
        mit aufgesetztem Rest. Ohne Toleranz wäre `% 60` ungleich 0 und der
        Aggregatpfad bliebe ungenutzt, obwohl er exakt wäre.
        """
        grenze = 29_793_333 * 60.0
        assert grenze % 60 == 0, 'Testwert ist keine Minutengrenze'
        assert db._aggregat_nutzbar(grenze + 1e-9, grenze + 3600 + 1e-9, 900.0)
        # Gegenrichtung: ein echter Versatz muss weiterhin zurückfallen.
        assert not db._aggregat_nutzbar(grenze + 30.0, grenze + 3630.0, 900.0)

    def test_krummes_raster_liefert_trotzdem_richtig(self, db):
        """Der Rückfallweg muss dasselbe rechnen wie die Referenz."""
        for i in range(120):
            db.add_pulse('eg', i * 5.0, i * 5.0)

        allocator = Allocator(None, db, 90.0)
        meter_ids = db.meter_ids()
        putzen = lambda d: {b: e for b, e in d.items() if e}
        assert putzen(allocator._pulses_per_interval(meter_ids, 0.0, 600.0)) \
            == putzen(allocator._pulses_per_interval_slow(
                meter_ids, 0.0, 600.0))


class TestAggregatBleibtWahr:
    """Das Aggregat ist abgeleitet — die Rohimpulse sind die Wahrheit."""

    def test_aggregat_folgt_den_impulsen(self, db):
        for i in range(150):
            db.add_pulse('eg', i * 1.0, i * 1.0)
        assert db.verify_pulse_minutes() == []

    def test_verfaelschtes_aggregat_wird_erkannt(self, db):
        for i in range(150):
            db.add_pulse('eg', i * 1.0, i * 1.0)
        db.conn.execute('UPDATE pulse_minutes SET pulses = pulses + 7 '
                        'WHERE minute = 0')
        db.conn.commit()

        abweichungen = db.verify_pulse_minutes()
        assert len(abweichungen) == 1
        minute, meter_id, aggregiert, roh = abweichungen[0]
        assert (minute, meter_id) == (0, 'eg')
        assert aggregiert - roh == 7

    def test_verschiebung_bei_gleicher_summe_wird_erkannt(self, db):
        """Der Fall, den der Summenvergleich beim Start nicht sieht."""
        for i in range(150):
            db.add_pulse('eg', i * 1.0, i * 1.0)
        db.conn.execute(
            'UPDATE pulse_minutes SET pulses = pulses - 5 WHERE minute = 0')
        db.conn.execute(
            'UPDATE pulse_minutes SET pulses = pulses + 5 WHERE minute = 1')
        db.conn.commit()

        assert len(db.verify_pulse_minutes()) == 2

    def test_aggregat_wird_beim_start_wiederhergestellt(self, db, tmp_path):
        """Migration und Absturzfall: fehlendes Aggregat baut sich neu auf."""
        for i in range(150):
            db.add_pulse('eg', i * 1.0, i * 1.0)
            db.add_pulse('og', i * 2.0, i * 2.0)
        db.conn.execute('DELETE FROM pulse_minutes')
        db.conn.commit()
        assert db.verify_pulse_minutes() != []

        db._repair_pulse_minutes()
        assert db.verify_pulse_minutes() == []

    def test_bestandsdatenbank_baut_aggregat_beim_oeffnen_auf(self, tmp_path):
        """Der Fall auf dem Pi: die Datenbank läuft seit Tagen ohne Aggregat.

        Geprüft wird über `Database(...)` — den Weg, den der Dienst beim
        Start wirklich nimmt. Ein direkter Aufruf von
        `_repair_pulse_minutes()` würde nicht zeigen, ob der Start die
        Reparatur überhaupt auslöst.
        """
        pfad = str(tmp_path / 'bestand.sqlite3')

        alt = Database(pfad)
        alt.ensure_meter('eg', 'Strom EG', 1000)
        for i in range(200):
            alt.add_pulse('eg', i * 3.0, i * 3.0)
        # Zustand einer Datenbank aus der Zeit vor dem Aggregat.
        alt.conn.execute('DROP TABLE pulse_minutes')
        alt.conn.commit()
        alt.close()

        neu = Database(pfad)
        try:
            assert neu.verify_pulse_minutes() == []
            assert neu.conn.execute(
                'SELECT SUM(pulses) FROM pulse_minutes').fetchone()[0] == 200
        finally:
            neu.close()

    def test_luecke_im_aggregat_schliesst_sich_beim_oeffnen(self, tmp_path):
        """Absturz zwischen Impuls und Fortschreibung, über den Startweg."""
        pfad = str(tmp_path / 'luecke.sqlite3')

        alt = Database(pfad)
        alt.ensure_meter('eg', 'Strom EG', 1000)
        for i in range(120):
            alt.add_pulse('eg', i * 2.0, i * 2.0)
        alt.conn.execute('DELETE FROM pulse_minutes WHERE minute = 1')
        alt.conn.commit()
        alt.close()

        neu = Database(pfad)
        try:
            assert neu.verify_pulse_minutes() == []
            aktionen = [e['action'] for e in neu.audit_log()]
            assert 'aggregat-repair' in aktionen
        finally:
            neu.close()

    def test_wiederherstellung_landet_im_audit(self, db):
        for i in range(60):
            db.add_pulse('eg', i * 1.0, i * 1.0)
        db.conn.execute('DELETE FROM pulse_minutes')
        db.conn.commit()
        db._repair_pulse_minutes()

        aktionen = [e['action'] for e in db.audit_log()]
        assert 'aggregat-repair' in aktionen

    def test_verteilung_stimmt_nach_wiederherstellung(self, store, db):
        """Bis zum Ergebnis durchgeprüft, nicht nur bis zur Impulszahl."""
        for i in range(60):
            store.add(reading(i * 60.0, production=500.0,
                              consumption=1500.0, grid=1000.0))
        for i in range(400):
            db.add_pulse('eg', i * 9.0, i * 9.0)
        for i in range(120):
            db.add_pulse('og', i * 30.0, i * 30.0)

        vorher = Allocator(store, db, QUARTER).allocate(0.0, HOUR)
        db.conn.execute('DELETE FROM pulse_minutes')
        db.conn.commit()
        db._repair_pulse_minutes()
        nachher = Allocator(store, db, QUARTER).allocate(0.0, HOUR)

        for meter_id, werte in vorher['meters'].items():
            for feld, wert in werte.items():
                assert nachher['meters'][meter_id][feld] == pytest.approx(
                    wert, abs=1e-9), f'{meter_id}.{feld} weicht ab'
        assert nachher['house'] == pytest.approx(vorher['house'])


class TestZwischenspeicher:
    """Der Cache darf schneller sein, aber nie älter."""

    def _fuellen(self, store, db):
        for i in range(30):
            store.add(reading(i * 60.0, production=500.0,
                              consumption=1500.0, grid=1000.0))
        for i in range(300):
            db.add_pulse('eg', i * 6.0, i * 6.0)

    def test_gleicher_zeitraum_liefert_gleiches_ergebnis(self, store, db):
        self._fuellen(store, db)
        allocator = Allocator(store, db, QUARTER)
        assert allocator.allocate(0.0, HOUR) == allocator.allocate(0.0, HOUR)

    def test_neuer_impuls_macht_den_cache_ungueltig(self, store, db):
        """Der wichtigste Fall: ein Impuls muss sofort durchschlagen."""
        self._fuellen(store, db)
        allocator = Allocator(store, db, QUARTER)
        vorher = allocator.allocate(0.0, HOUR)['meters']['eg']['consumption_kwh']

        # In ein Intervall, für das die Bilanzreihe Daten hat (0–1740 s).
        # Ein Impuls außerhalb bliebe zu Recht unberücksichtigt und würde
        # den Cache-Test unbemerkt wirkungslos machen.
        db.add_pulse('eg', 1700.0, 1700.0)
        nachher = allocator.allocate(0.0, HOUR)['meters']['eg']['consumption_kwh']

        assert nachher > vorher
        assert nachher == pytest.approx(vorher + 0.001)

    def test_neuer_bilanzpunkt_macht_den_cache_ungueltig(self, store, db):
        self._fuellen(store, db)
        allocator = Allocator(store, db, QUARTER)
        vorher = allocator.allocate(0.0, HOUR)['house']['grid_kwh']

        for i in range(30, 45):
            store.add(reading(i * 60.0, grid=1000.0))
        nachher = allocator.allocate(0.0, HOUR)['house']['grid_kwh']

        assert nachher > vorher

    def test_korrigierter_zaehlerstand_macht_den_cache_ungueltig(self, store, db):
        """`set-reading` ändert den Offset — das muss durchschlagen."""
        self._fuellen(store, db)
        allocator = Allocator(store, db, QUARTER)
        allocator.allocate(0.0, HOUR)

        db.set_reading('eg', 12345.0)
        stand = allocator._datenstand()
        assert allocator._cache[(0.0, HOUR, QUARTER, 'proportional')][0] != stand

    def test_anderer_zeitraum_wird_getrennt_gehalten(self, store, db):
        self._fuellen(store, db)
        allocator = Allocator(store, db, QUARTER)
        eine = allocator.allocate(0.0, QUARTER)
        ganze = allocator.allocate(0.0, HOUR)
        assert eine['meters']['eg']['consumption_kwh'] < \
            ganze['meters']['eg']['consumption_kwh']

    def test_anderes_modell_wird_getrennt_gehalten(self, store, db):
        """Sonst lieferte ein Modellwechsel das Ergebnis des anderen."""
        for i in range(30):
            store.add(reading(i * 60.0, grid=600.0))
        for i in range(500):
            db.add_pulse('eg', i * 3.5, i * 3.5)
        for i in range(50):
            db.add_pulse('og', i * 35.0, i * 35.0)

        anteilig = Allocator(store, db, QUARTER, 'proportional')
        wasserfall = Allocator(store, db, QUARTER, 'waterfall')
        a = anteilig.allocate(0.0, HOUR)['meters']['og']['grid_kwh']
        w = wasserfall.allocate(0.0, HOUR)['meters']['og']['grid_kwh']
        assert a != pytest.approx(w)

    def test_cache_waechst_nicht_unbegrenzt(self, store, db):
        self._fuellen(store, db)
        allocator = Allocator(store, db, QUARTER)
        for i in range(12):
            allocator.allocate(0.0, QUARTER * (i + 1))
        assert len(allocator._cache) <= Allocator.CACHE_EINTRAEGE

    def test_cache_und_neurechnung_stimmen_ueberein(self, store, db):
        """Der Cache darf nichts anderes liefern als die frische Rechnung."""
        self._fuellen(store, db)
        allocator = Allocator(store, db, QUARTER)
        gecacht = allocator.allocate(0.0, HOUR)
        frisch = allocator.allocate(0.0, HOUR, use_cache=False)
        assert gecacht == frisch


class TestSammler:
    def test_messpunkt_wird_abgelegt(self, store):
        class Quelle:
            name = 'test'
            def read(self):
                return reading(1000.0, production=500.0)

        collector = BalanceCollector(Quelle(), store)
        assert collector.collect_once()
        assert collector.collected == 1
        assert store.count() == 1

    def test_ausfall_wird_gezaehlt_nicht_geworfen(self, store):
        """Ein Quellenausfall darf die Impulserfassung nicht stoppen."""
        class Kaputt:
            name = 'kaputt'
            def read(self):
                raise SourceError('nicht erreichbar')

        collector = BalanceCollector(Kaputt(), store)
        assert collector.collect_once() is False
        assert collector.failed == 1
        assert store.count() == 0

    def test_luecke_wird_nach_erholung_vermerkt(self, store):
        class Wackelig:
            name = 'wackelig'
            def __init__(self):
                self.ok = False
            def read(self):
                if not self.ok:
                    raise SourceError('gerade nicht')
                return reading(1000.0)

        quelle = Wackelig()
        collector = BalanceCollector(quelle, store)
        collector.collect_once()
        quelle.ok = True
        collector.collect_once()

        assert len(store.gaps(0.0, 9_999_999_999.0)) == 1
