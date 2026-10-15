"""Die Wohnungen werden von unten nach oben gelistet, nicht alphabetisch.

Alphabetisch ergibt sich dg, eg, og — das Dachgeschoss stünde in der
Jahresabrechnung zuoberst. Gemeldet am 27.08.2026.

Die letzte Klasse prüft, dass die Python-Liste und die des Dashboards
übereinstimmen. Sie parst dazu `dashboard.js`, statt die Werte
abzuschreiben: eine abgeschriebene Vergleichszahl bleibt genau dort
stehen, wo sie angelegt wurde, und prüft danach nichts mehr.
"""
import os
import re

import pytest

from s0counter import ordnung

JS = os.path.join(os.path.dirname(__file__), '..', 's0counter', 'dashboard.js')


class TestReihenfolge:
    def test_von_unten_nach_oben(self):
        assert ordnung.sortiert(['dg', 'eg', 'og']) == ['eg', 'og', 'dg']

    def test_eingabereihenfolge_egal(self):
        for eingabe in (['og', 'dg', 'eg'], ['dg', 'og', 'eg'],
                        ['eg', 'dg', 'og'], ['og', 'eg', 'dg']):
            assert ordnung.sortiert(eingabe) == ['eg', 'og', 'dg']

    def test_dict_wird_nach_schluesseln_sortiert(self):
        # Die ersetzten Aufrufe übergaben teils ein dict an sorted().
        daten = {'dg': 1, 'eg': 2, 'og': 3}
        assert ordnung.sortiert(daten) == ['eg', 'og', 'dg']

    def test_grossschreibung_stoert_nicht(self):
        assert ordnung.sortiert(['DG', 'EG', 'OG']) == ['EG', 'OG', 'DG']

    def test_teilmenge_behaelt_die_ordnung(self):
        assert ordnung.sortiert(['dg', 'eg']) == ['eg', 'dg']
        assert ordnung.sortiert(['og', 'eg']) == ['eg', 'og']
        assert ordnung.sortiert(['dg', 'og']) == ['og', 'dg']


class TestUnbekannteZaehler:
    """Eine Sortierung, die unbekannte Namen wegwirft, verliert Daten."""

    def test_unbekannte_gehen_nicht_verloren(self):
        ergebnis = ordnung.sortiert(['og', 'keller', 'dg', 'eg', 'anbau'])
        assert set(ergebnis) == {'og', 'keller', 'dg', 'eg', 'anbau'}

    def test_unbekannte_haengen_hinten_an(self):
        ergebnis = ordnung.sortiert(['keller', 'dg', 'eg', 'anbau', 'og'])
        assert ergebnis == ['eg', 'og', 'dg', 'anbau', 'keller']

    def test_nur_unbekannte_bleiben_alphabetisch(self):
        assert ordnung.sortiert(['zeta', 'alpha']) == ['alpha', 'zeta']

    def test_leere_liste(self):
        assert ordnung.sortiert([]) == []


class TestGleichstandMitDerAnzeige:
    """Python und JavaScript müssen dieselbe Reihenfolge tragen.

    Sonst zeigt das Dashboard EG-OG-DG und die Abrechnung etwas anderes —
    derselbe Fehler, nur an einer Stelle repariert. Wer eine duplizierte
    Liste findet, sucht nach der dritten.
    """

    @staticmethod
    def _js_reihenfolge():
        with open(JS, encoding='utf-8') as f:
            inhalt = f.read()
        treffer = re.search(r'reihenfolge:\s*\[([^\]]*)\]', inhalt)
        assert treffer, 'config.reihenfolge nicht in dashboard.js gefunden'
        return tuple(re.findall(r"'([^']+)'", treffer.group(1)))

    def test_js_liste_ist_auffindbar(self):
        # Ohne diese Prüfung wäre ein Fehlschlag beim Parsen nicht von
        # einem echten Unterschied zu trennen.
        assert len(self._js_reihenfolge()) > 0

    def test_python_und_js_stimmen_ueberein(self):
        assert ordnung.REIHENFOLGE == self._js_reihenfolge()


class TestVerwenderNutzenDieOrdnung:
    """Kein blankes sorted() mehr auf Zählernamen.

    Diese Prüfung liest den Quelltext, weil sie sonst nur eine Kopie der
    Logik prüfen würde. Sie ist der Grund, warum eine siebte Fundstelle
    beim nächsten Mal auffällt, statt still alphabetisch zu bleiben.
    """

    DATEIEN = ('report.py', 'telegram.py', 'panel.py', 'doctor.py',
               'main.py')

    @pytest.mark.parametrize('datei', DATEIEN)
    def test_kein_blankes_sorted_auf_zaehlern(self, datei):
        pfad = os.path.join(os.path.dirname(__file__), '..', 's0counter',
                            datei)
        with open(pfad, encoding='utf-8') as f:
            inhalt = f.read()

        verboten = [
            r"sorted\(states\)",
            r"sorted\(meters\)",
            r"sorted\(allocation\['meters'\]\)",
            r"sorted\(self\.allocation\['meters'\]\)",
            r"sorted\(zustaende\.items\(\)\)",
            r"sorted\(daten\.get\('meters'",
        ]
        for muster in verboten:
            assert not re.search(muster, inhalt), (
                f'{datei}: {muster} sortiert alphabetisch — '
                'ordnung.sortiert() verwenden')

    @pytest.mark.parametrize('datei', DATEIEN)
    def test_ordnung_ist_importiert(self, datei):
        pfad = os.path.join(os.path.dirname(__file__), '..', 's0counter',
                            datei)
        with open(pfad, encoding='utf-8') as f:
            inhalt = f.read()
        assert 'from . import ordnung' in inhalt
        assert 'ordnung.sortiert' in inhalt or 'ordnung.sortierschluessel' \
            in inhalt


class TestAbrechnungImPdf:
    """Wirkungsprüfung am fertigen Dokument, nicht an einer Zwischenstufe.

    Dass `ordnung.sortiert()` richtig sortiert, sagt noch nicht, dass die
    Abrechnung es auch verwendet — geprüft wird deshalb der Text, der im
    PDF landet.
    """

    @staticmethod
    def _pdf_text(tmp_path):
        from tests.test_report import (BEISPIEL_STAENDE, beispiel_zuteilung)
        from s0counter.report import YearReport

        # Flach, wie `build_year_report` es aufbaut: {id: name}. Ein
        # verschachteltes dict landet als Python-Repr im PDF — genau das
        # ist beim Schreiben dieses Tests passiert.
        namen = {mid: stand['name']
                 for mid, stand in BEISPIEL_STAENDE.items()}
        bericht = YearReport(beispiel_zuteilung(), namen, BEISPIEL_STAENDE)
        ziel = str(tmp_path / 'jahr.pdf')
        bericht.save(ziel)
        with open(ziel, 'rb') as f:
            roh = f.read()

        # Die Inhaltsströme sind mit FlateDecode gepackt. Der Suchlauf
        # nimmt deshalb jeden Stream und versucht ihn zu entpacken; was
        # sich nicht entpacken lässt, wird übersprungen.
        import zlib
        text = []
        for treffer in re.finditer(rb'stream\r?\n', roh):
            anfang = treffer.end()
            schluss = roh.find(b'endstream', anfang)
            if schluss < 0:
                continue
            roh_text = roh[anfang:schluss]
            try:
                roh_text = zlib.decompress(roh_text)
            except zlib.error:
                continue
            text.extend(t.decode('latin-1', 'ignore')
                        for t in re.findall(rb'\((.*?)\)\s*Tj', roh_text))
        return text

    def test_wohnungen_stehen_von_unten_nach_oben(self, tmp_path):
        stuecke = self._pdf_text(tmp_path)
        gefunden = [s for s in stuecke
                    if s.strip() in ('Wohnung EG', 'Wohnung OG',
                                     'Wohnung DG')]
        assert gefunden, 'keine Wohnungsnamen im PDF gefunden'

        # Erstes Vorkommen je Wohnung entscheidet.
        reihenfolge = []
        for s in gefunden:
            k = s.strip()
            if k not in reihenfolge:
                reihenfolge.append(k)
        assert reihenfolge == ['Wohnung EG', 'Wohnung OG', 'Wohnung DG']
