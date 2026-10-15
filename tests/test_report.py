"""PDF-Erzeugung und Jahresbericht."""
import re
import time
import zlib

import pytest

from s0counter import pdf
from s0counter.config import ReportConfig
from s0counter.report import YearReport, year_bounds


def jahr(y=2026):
    return year_bounds(y)


def beispiel_zuteilung(coverage=0.97, gaps=None, unallocated=271.168):
    start, end = jahr()
    return {
        'start_ts': start, 'end_ts': end, 'interval_seconds': 900.0,
        'meters': {
            'eg': {'consumption_kwh': 3241.882, 'grid_kwh': 1876.443,
                   'solar_kwh': 1365.439},
            'og': {'consumption_kwh': 1502.117, 'grid_kwh': 844.201,
                   'solar_kwh': 657.916},
            'dg': {'consumption_kwh': 987.554, 'grid_kwh': 521.088,
                   'solar_kwh': 466.466},
        },
        'house': {'grid_kwh': 3512.9, 'pv_kwh': 9840.2,
                  'unallocated_grid_kwh': unallocated},
        'quality': {
            'intervals_used': int(35040 * coverage),
            'intervals_expected': 35040,
            'coverage': coverage,
            'intervals_without_pulses': 412,
            'gaps': gaps if gaps is not None else [],
        },
    }


BEISPIEL_STAENDE = {
    'eg': {'name': 'Wohnung EG', 'energy_kwh': 8421.5, 'pulses': 3241882,
           'pulses_per_kwh': 1000},
    'og': {'name': 'Wohnung OG', 'energy_kwh': 3312.1, 'pulses': 1502117,
           'pulses_per_kwh': 1000},
    'dg': {'name': 'Wohnung DG', 'energy_kwh': 1456.8, 'pulses': 987554,
           'pulses_per_kwh': 1000},
}


def text_of(pdf_bytes: bytes) -> str:
    """Textinhalt aller Seiten aus den komprimierten Streams holen."""
    parts = []
    for match in re.finditer(rb'stream\n(.*?)\nendstream', pdf_bytes,
                             re.DOTALL):
        try:
            parts.append(zlib.decompress(match.group(1)).decode('latin-1'))
        except zlib.error:
            pass
    body = '\n'.join(parts)
    return '\n'.join(re.findall(r'\((.*?)\) Tj', body))


class TestPdfGrundlagen:
    def test_gueltige_struktur(self):
        doc = pdf.Document(title='Test')
        page = doc.new_page()
        page.text(50, 50, 'Hallo')
        data = doc.build()

        assert data.startswith(b'%PDF-1.4')
        assert data.rstrip().endswith(b'%%EOF')
        assert b'/Type /Catalog' in data
        assert b'/Type /Pages' in data
        assert b'/Type /Page ' in data

    def test_seitenbaum_zaehlt_richtig(self):
        doc = pdf.Document()
        for _ in range(3):
            doc.new_page().text(50, 50, 'x')
        data = doc.build()

        match = re.search(rb'/Type /Pages /Kids \[(.*?)\] /Count (\d+)', data)
        assert match is not None
        assert int(match.group(2)) == 3
        assert len(match.group(1).split(b' 0 R')) - 1 == 3

    def test_xref_stimmt_mit_den_objekten(self):
        """Ein falscher Querverweistabellen-Eintrag macht das PDF unlesbar."""
        doc = pdf.Document()
        doc.new_page().text(50, 50, 'Test')
        data = doc.build()

        size = int(re.search(rb'/Size (\d+)', data).group(1))
        # Die Einträge stehen lückenlos hintereinander und teilen sich das
        # trennende \n — ein Muster mit führendem \n findet nur jeden zweiten.
        eintraege = len(re.findall(rb'\d{10} 00000 n \n', data))
        assert eintraege == size - 1

        start = int(re.search(rb'startxref\n(\d+)', data).group(1))
        assert data[start:start + 4] == b'xref'

    def test_umlaute_ueberleben(self):
        doc = pdf.Document()
        doc.new_page().text(50, 50, 'Größe, Zähler, Öl')
        assert 'Größe, Zähler, Öl' in text_of(doc.build())

    def test_gedankenstrich_und_euro(self):
        """WinAnsi kennt Zeichen, die Latin-1 nicht hat."""
        doc = pdf.Document()
        doc.new_page().text(50, 50, 'A — B € C')
        data = doc.build()
        stream = zlib.decompress(
            re.search(rb'stream\n(.*?)\nendstream', data, re.DOTALL).group(1))
        assert b'\x97' in stream      # em dash
        assert b'\x80' in stream      # Euro
        assert b'?' not in stream

    def test_klammern_werden_maskiert(self):
        """Unmaskierte Klammern zerlegen die PDF-Syntax."""
        doc = pdf.Document()
        doc.new_page().text(50, 50, 'Test (wichtig) hier')
        data = doc.build()
        stream = zlib.decompress(
            re.search(rb'stream\n(.*?)\nendstream', data, re.DOTALL).group(1))
        assert rb'\(wichtig\)' in stream

    def test_textbreite_waechst_mit_der_groesse(self):
        schmal = pdf.text_width('Test', 10)
        breit = pdf.text_width('Test', 20)
        assert breit == pytest.approx(schmal * 2)

    def test_monospace_ist_gleichmaessig(self):
        assert pdf.text_width('iii', 10, 'mono') == pdf.text_width(
            'WWW', 10, 'mono')


class TestJahresbericht:
    def test_drei_seiten(self):
        doc = YearReport(beispiel_zuteilung(), {}, BEISPIEL_STAENDE).build()
        assert len(doc.pages) == 3

    def test_wohnungsnamen_erscheinen(self):
        report = YearReport(beispiel_zuteilung(),
                            {k: v['name'] for k, v in BEISPIEL_STAENDE.items()},
                            BEISPIEL_STAENDE)
        text = text_of(report.build().build())
        for name in ('Wohnung EG', 'Wohnung OG', 'Wohnung DG'):
            assert name in text

    def test_zahlen_im_deutschen_format(self):
        report = YearReport(beispiel_zuteilung(), {}, BEISPIEL_STAENDE)
        text = text_of(report.build().build())
        assert '3.241,882' in text
        assert '1.876,443' in text

    def test_verfahren_wird_erklaert(self):
        """Ein Beleg muss sagen, was gemessen und was gerechnet ist."""
        report = YearReport(beispiel_zuteilung(), {}, BEISPIEL_STAENDE)
        text = text_of(report.build().build())
        assert 'gemessen' in text
        assert 'gerechnet' in text
        assert 'Verteilschlüssel' in text.replace('VERTEILSCHLÜSSEL',
                                                  'Verteilschlüssel')

    def test_hinweis_auf_fehlende_eichfaehigkeit(self):
        report = YearReport(beispiel_zuteilung(), {}, BEISPIEL_STAENDE)
        text = text_of(report.build().build())
        assert 'eichfähig' in text

    def test_warnung_bei_luechenhafter_abdeckung(self):
        report = YearReport(beispiel_zuteilung(coverage=0.62), {},
                            BEISPIEL_STAENDE)
        text = text_of(report.build().build())
        assert 'Hinweis' in text
        assert '62,0' in text

    def test_keine_warnung_bei_guter_abdeckung(self):
        report = YearReport(beispiel_zuteilung(coverage=0.99), {},
                            BEISPIEL_STAENDE)
        text = text_of(report.build().build())
        assert 'Hinweis:' not in text

    def test_ausfaelle_werden_aufgelistet(self):
        start, _ = jahr()
        gaps = [(start + 1000, start + 4600, 'Quelle nicht erreichbar')]
        report = YearReport(beispiel_zuteilung(gaps=gaps), {},
                            BEISPIEL_STAENDE)
        text = text_of(report.build().build())
        assert 'Quelle nicht erreichbar' in text

    def test_nicht_zugeordneter_bezug_wird_erklaert(self):
        report = YearReport(beispiel_zuteilung(unallocated=271.0), {},
                            BEISPIEL_STAENDE)
        text = text_of(report.build().build())
        assert 'nicht zugeordnet' in text
        assert 'Allgemeinstrom' in text

    def test_kosten_nur_mit_preis(self):
        ohne = YearReport(beispiel_zuteilung(), {}, BEISPIEL_STAENDE)
        assert 'EUR' not in text_of(ohne.build().build())

        cfg = ReportConfig(enabled=True, price_ct_per_kwh=32.5)
        mit = YearReport(beispiel_zuteilung(), {}, BEISPIEL_STAENDE, cfg)
        assert 'EUR' in text_of(mit.build().build())

    def test_kostenrechnung_stimmt(self):
        cfg = ReportConfig(enabled=True, price_ct_per_kwh=30.0)
        report = YearReport(beispiel_zuteilung(), {}, BEISPIEL_STAENDE, cfg)
        text = text_of(report.build().build())
        # EG: 1876,443 kWh × 0,30 EUR = 562,93 EUR
        assert '562,93' in text

    def test_grundpreis_wird_aufgeteilt(self):
        cfg = ReportConfig(enabled=True, price_ct_per_kwh=30.0,
                           base_price_eur_per_year=120.0)
        report = YearReport(beispiel_zuteilung(), {}, BEISPIEL_STAENDE, cfg)
        text = text_of(report.build().build())
        # 120 EUR auf drei Wohnungen sind 40 EUR je Wohnung
        assert '40,00' in text

    def test_datei_wird_geschrieben(self, tmp_path):
        ziel = str(tmp_path / 'unterordner' / 'bericht.pdf')
        report = YearReport(beispiel_zuteilung(), {}, BEISPIEL_STAENDE)
        pfad = report.save(ziel)

        assert pfad == ziel
        with open(ziel, 'rb') as handle:
            assert handle.read(8) == b'%PDF-1.4'


class TestJahresgrenzen:
    def test_kalenderjahr(self):
        start, end = year_bounds(2026)
        assert time.localtime(start).tm_year == 2026
        assert time.localtime(start).tm_mon == 1
        assert time.localtime(start).tm_mday == 1
        assert time.localtime(end - 1).tm_year == 2026
        assert time.localtime(end - 1).tm_mon == 12

    def test_schaltjahr_hat_366_tage(self):
        start, end = year_bounds(2028)
        assert (end - start) / 86400 == pytest.approx(366, abs=0.05)
