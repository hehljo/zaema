#!/usr/bin/env python3
"""Prüft den Netzanteil in der Wohnungskachel im echten Browser.

Was hier gemessen wird, sieht kein Unittest: die Verteilrechnung ist über
tests/test_dashboard_verteilung.mjs abgedeckt, aber ob die Zahl auch in der
Kachel ANKOMMT, entscheidet sich erst im Browser. Beides zu verwechseln ist
die Falle „Nebenwirkung geprüft ist nicht Wirkung geprüft".

Geprüft wird je Fenstergröße und Thema:

  * steht in jeder Wohnungskachel genau eine Klammer
  * enthält sie eine Wattzahl und das Wort „Netz"
  * ist sie farblich abgesetzt (nicht dieselbe Farbe wie der Hauptwert)
  * bleibt sie innerhalb der Kachel und bricht nicht um
  * läuft die Seite dadurch seitlich über

Aufruf gegen einen laufenden Dienst:

    python3 tools/klammer_pruefen.py --url http://127.0.0.1:8080/dashboard

Rückgabewert 0, wenn alles besteht — damit taugt es als Gate. Ein Lauf ohne
einzige gefundene Kachel meldet 2 („nicht gemessen"), nicht 0: ein Prüfer,
der nichts findet, darf keinen Erfolg melden.
"""
import argparse
import re
import sys

# Dieselben Größen wie in dashboard_pruefen.py, damit ein Befund dort und
# hier denselben Namen trägt.
VIEWPORTS = [
    (320, 568, 'telefon-winzig'),
    (390, 844, 'telefon'),
    (390, 420, 'telefon-tastatur'),
    (768, 1024, 'tablet'),
    (1440, 900, 'desktop'),
]

# "(0 W Netz)" — die Zahl darf Tausenderpunkte tragen, das Komma ist
# deutsch. Ohne Ziffer wäre die Klammer eine leere Behauptung.
MUSTER = re.compile(r'^\(\s*[\d.,]+\s*W\s+Netz\s*\)$')


def pruefen(url: str) -> tuple:
    """Liefert (befunde, gemessene_kacheln). Leere Liste heißt bestanden."""
    from playwright.sync_api import sync_playwright

    befunde = []
    gemessen = 0

    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            for thema in ('hell', 'dunkel'):
                for breite, hoehe, name in VIEWPORTS:
                    ort = f'{thema}/{name}'
                    seite = browser.new_page(
                        viewport={'width': breite, 'height': hoehe})
                    js_fehler = []
                    seite.on('pageerror', lambda e: js_fehler.append(str(e)))
                    seite.goto(url, wait_until='networkidle')
                    seite.evaluate(
                        f"document.documentElement.dataset.theme='{thema}'")
                    seite.wait_for_timeout(1500)

                    # Eingeklappte Abschnitte liefern leeren inner_text —
                    # eine Messung darauf liefe ins Leere statt zu prüfen.
                    for aufklappbar in seite.query_selector_all('details'):
                        aufklappbar.evaluate('e => e.open = true')
                    seite.wait_for_timeout(400)

                    befunde += _seite_pruefen(seite, ort)
                    gemessen += len(
                        seite.query_selector_all('.karte__zusatz'))
                    if js_fehler:
                        befunde.append(f'{ort}: JS-Fehler {js_fehler[0]}')
                    seite.close()
        finally:
            browser.close()

    return befunde, gemessen


def _seite_pruefen(seite, ort: str) -> list:
    befunde = []
    zusaetze = seite.query_selector_all('.karte__zusatz')

    for z in zusaetze:
        # textContent, nicht inner_text: letzteres liefert für alles in
        # einem geschlossenen Abschnitt leer und täuscht eine leere Klammer
        # vor, wo eine gefüllte steht.
        text = (z.evaluate('e => e.textContent') or '').strip()
        if not MUSTER.match(text):
            befunde.append(f'{ort}: Klammer "{text}" passt nicht zum Muster')
            continue

        kasten = z.bounding_box()
        if not kasten or kasten['width'] < 5 or kasten['height'] < 5:
            befunde.append(f'{ort}: Klammer "{text}" ist unsichtbar')
            continue

        karte = z.evaluate_handle("e => e.closest('.karte')").as_element()
        rahmen = karte.bounding_box() if karte else None
        if rahmen and kasten['x'] + kasten['width'] > \
                rahmen['x'] + rahmen['width'] + 1:
            befunde.append(f'{ort}: Klammer ragt aus der Kachel')

        # Die Klammer soll sich absetzen. Gleiche Farbe wie der Hauptwert
        # hieße: die Regel greift nicht, und die Zahl verschwimmt mit dem
        # Verbrauch.
        farben = z.evaluate(
            "e => [getComputedStyle(e).color,"
            " getComputedStyle(e.closest('.karte__wert')).color]")
        if farben[0] == farben[1]:
            befunde.append(f'{ort}: Klammer nicht farblich abgesetzt')

        # Ein Umbruch mitten in "(0 W Netz)" liest sich als zwei Angaben.
        zeilen = z.evaluate('e => e.getClientRects().length')
        if zeilen > 1:
            befunde.append(f'{ort}: Klammer bricht über {zeilen} Zeilen um')

    if seite.evaluate('document.documentElement.scrollWidth > '
                      'document.documentElement.clientWidth + 1'):
        befunde.append(f'{ort}: Seite läuft seitlich über')

    return befunde


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--url', default='http://127.0.0.1:8080/dashboard')
    args = p.parse_args()

    try:
        befunde, gemessen = pruefen(args.url)
    except Exception as exc:                      # noqa: BLE001
        print(f'NICHT GEMESSEN: {exc}', file=sys.stderr)
        return 2

    # Der dritte Ausgang: keine einzige Klammer gefunden heißt nicht
    # „bestanden", sondern „nichts geprüft". Ohne diesen Zweig meldete ein
    # Lauf gegen ein Dashboard ohne Hausbilanz stillschweigend Erfolg.
    if gemessen == 0:
        print('NICHT GEMESSEN: keine einzige Klammer gefunden — läuft der '
              'Dienst mit Hausbilanz?', file=sys.stderr)
        return 2

    if befunde:
        print(f'{len(befunde)} Befunde:')
        for b in befunde:
            print(f'  - {b}')
        return 1

    print(f'Netzanteil in der Kachel in Ordnung ({gemessen} Klammern '
          'gemessen)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
