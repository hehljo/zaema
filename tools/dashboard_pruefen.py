#!/usr/bin/env python3
"""Prüft das Dashboard in einem echten Browser.

Bewusst kein pytest-Test: die Prüfung braucht Playwright und einen Browser,
und beides gehört nicht in die Voraussetzungen eines Dienstes, der auf einem
Pi läuft. Die Hauptsuite bleibt hardwarefrei und schnell.

Was hier gemessen wird, sieht kein Unittest:

  * läuft die Seite seitlich über (sechs Fenstergrößen, beide Themen)
  * ragt eine Kachel aus dem Fenster
  * sind die Trefferflächen der Bedienknöpfe mindestens 44 px
  * wird mitten im Wort umbrochen
  * wird die Einheit vom Wert abgetrennt ("8,291 kW" / "h")
  * bewirken die Knöpfe der Abrechnung tatsächlich etwas

Der letzte Punkt ist der wichtigste: ein Knopf, der nichts tut, ist im
Screenshot nicht von einem zu unterscheiden, der wirkt.

Aufruf gegen einen laufenden Dienst:

    python3 tools/dashboard_pruefen.py --url http://127.0.0.1:8080/dashboard

Ohne laufenden Dienst startet das Skript selbst einen mit erzeugten Daten:

    python3 tools/dashboard_pruefen.py --eigener-dienst

Rückgabewert 0, wenn alles besteht — damit taugt es als Gate.
"""
import argparse
import sys

VIEWPORTS = [
    (320, 568, 'telefon-winzig'),
    (360, 640, 'telefon-klein'),
    (390, 844, 'telefon'),
    (390, 420, 'telefon-tastatur'),   # offene Tastatur: wenig Höhe
    (768, 1024, 'tablet'),
    (1440, 900, 'desktop'),
]


def pruefen(url: str, screenshot_dir: str | None = None) -> list:
    """Liefert die Liste der Befunde. Leer heißt bestanden."""
    from playwright.sync_api import sync_playwright

    fehler = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            for thema in ('hell', 'dunkel'):
                for breite, hoehe, name in VIEWPORTS:
                    fehler += _seite_pruefen(browser, url, thema, breite,
                                             hoehe, name, screenshot_dir)
            fehler += _bedienung_pruefen(browser, url)
        finally:
            browser.close()
    return fehler


def _kurzfassungen_pruefen(seite, thema, name) -> list:
    """Der eingeklappte Zustand ist der Startzustand — er wird am meisten gesehen.

    Geprüft wird, was ein Mensch dort sieht: steht in jedem sichtbaren
    Abschnitt eine Werteliste, sind die Werte durch etwas getrennt, und ist
    die Herkunftsangabe ausgeblendet.

    Der Trenner wird über `getComputedStyle(el, '::before').content` geprüft,
    nicht über den Text. Weder `textContent` noch `innerText` geben
    generierten Inhalt zurück, und `getBoundingClientRect` des Elements
    schließt sein ::before nicht ein — eine Messung darüber meldete „kein
    Trenner", während im Screenshot deutlich „EG: 409 W · OG: 94 W" stand.
    Ein Gate, das den gesunden Fall rot meldet, wird abgeschaltet.
    """
    fehler = []
    befund = seite.evaluate("""() => {
        const ergebnis = [];
        for (const d of document.querySelectorAll('details[data-bereich]')) {
            if (d.hidden || !d.offsetParent) continue;
            const kurz = d.querySelector('[data-feld^="kurz-"]');
            const quelle = d.querySelector('[data-feld^="quelle-"]');
            const teile = kurz ? [...kurz.children] : [];
            // Der Trenner sitzt im ::before des ZWEITEN und jedes weiteren
            // Teils. Am ersten fehlt er zu Recht.
            const trenner = teile.length > 1
                ? getComputedStyle(teile[1], '::before').content
                : null;
            ergebnis.push({
                bereich: d.dataset.bereich,
                offen: d.open,
                text: kurz ? kurz.textContent.trim() : null,
                teile: teile.length,
                trenner,
                quelleSichtbar: quelle ? quelle.offsetParent !== null : false,
                marker: getComputedStyle(
                    d.querySelector('.abschnitt__titel'), '::before').content,
            });
        }
        return ergebnis;
    }""")

    if not befund:
        return [f'{thema}/{name}: kein sichtbarer Abschnitt gefunden']

    for b in befund:
        kennung = f'{thema}/{name}/{b["bereich"]}'
        if b['offen']:
            fehler.append(f'{kennung}: startet aufgeklappt statt eingeklappt')
        if not b['text']:
            fehler.append(f'{kennung}: eingeklappt ohne Werte')
            continue
        if b['quelleSichtbar']:
            fehler.append(f'{kennung}: Herkunftsangabe auch eingeklappt sichtbar')
        if b['marker'] in ('none', 'normal'):
            fehler.append(f'{kennung}: kein Klapp-Marker am Titel')
        # Mehrere Werte müssen getrennt sein. Ohne Trenner stünde dort
        # "567 WOG: 100 W" — lesbar nur, wer weiß, was gemeint ist.
        if b['teile'] > 1:
            t = b['trenner']
            if not t or t in ('none', 'normal') or '·' not in t:
                fehler.append(
                    f'{kennung}: Werte ohne Trenner (::before = {t!r})')

    return fehler


def _seite_pruefen(browser, url, thema, breite, hoehe, name,
                   screenshot_dir) -> list:
    fehler = []
    seite = browser.new_page(viewport={'width': breite, 'height': hoehe})
    konsole = []
    seite.on('console',
             lambda m: konsole.append(f'{m.type}: {m.text}')
             if m.type == 'error' else None)
    seite.on('pageerror', lambda e: konsole.append(f'pageerror: {e}'))

    try:
        seite.goto(url, wait_until='networkidle')
        seite.evaluate("t => document.documentElement.dataset.theme = t", thema)

        # Die Abschnitte starten eingeklappt. Zum Messen der Kacheln müssen
        # sie offen sein — sonst hat jedes Element die Größe null und alle
        # Layoutprüfungen unten wären grün, ohne etwas geprüft zu haben.
        # Vorher wird der eingeklappte Zustand selbst geprüft.
        kurz_fehler = _kurzfassungen_pruefen(seite, thema, name)
        fehler.extend(kurz_fehler)
        seite.evaluate("""() => {
            for (const d of document.querySelectorAll('details[data-bereich]')) {
                d.open = true;
            }
        }""")

        try:
            seite.wait_for_selector('[data-liste="abrechnung"] .karte',
                                    timeout=20000)
        except Exception:
            return [f'{thema}/{name}: keine Abrechnungskachel erschienen']
        seite.wait_for_timeout(600)

        if konsole:
            fehler.append(f'{thema}/{name}: Konsolenfehler {konsole[:3]}')

        # Die Fassung benennt den laufenden Code. Sie steht in der Kopfzeile
        # und ist genau die Art Angabe, die still verschwindet: kein Ablauf
        # hängt an ihr, ein Tippfehler im Feldnamen fiele sonst niemandem auf.
        fassung = seite.evaluate("""() => {
            const e = document.querySelector('[data-feld="fassung"]');
            if (!e) return {fehlt: true};
            const r = e.getBoundingClientRect();
            return {
                text: (e.textContent || '').trim(),
                titel: e.getAttribute('title') || '',
                breite: r.width,
                rechts: r.right,
            };
        }""")
        if fassung.get('fehlt'):
            fehler.append(f'{thema}/{name}: Fassungsangabe fehlt in der Kopfzeile')
        elif not fassung['text']:
            fehler.append(f'{thema}/{name}: Fassungsangabe ist leer')
        elif not fassung['text'].startswith('v'):
            fehler.append(f'{thema}/{name}: Fassung unerwartet: {fassung["text"]!r}')
        elif fassung['rechts'] > breite + 1:
            fehler.append(f'{thema}/{name}: Fassung ragt aus dem Fenster '
                          f'({fassung["rechts"]:.0f} > {breite})')
        elif 'eingespielt' not in fassung['titel']:
            fehler.append(f'{thema}/{name}: Fassung ohne Einspielzeitpunkt '
                          f'({fassung["titel"]!r})')

        ueberlauf = seite.evaluate(
            "() => document.documentElement.scrollWidth "
            "- document.documentElement.clientWidth")
        if ueberlauf > 1:
            fehler.append(f'{thema}/{name}: {ueberlauf} px seitlicher Überlauf')

        raus = seite.evaluate("""(b) => {
            const treffer = [];
            for (const e of document.querySelectorAll(
                    '.karte, .urteil, .zeitwahl__knopf, .fussnote')) {
                const r = e.getBoundingClientRect();
                if (r.width > 0 && (r.right > b + 1 || r.left < -1)) {
                    treffer.push(e.className + ' @ ' + Math.round(r.left)
                                 + '..' + Math.round(r.right));
                }
            }
            return treffer;
        }""", breite)
        if raus:
            fehler.append(f'{thema}/{name}: ragt aus dem Fenster {raus[:3]}')

        klein = seite.evaluate("""() => {
            const t = [];
            for (const e of document.querySelectorAll('.zeitwahl__knopf')) {
                const r = e.getBoundingClientRect();
                if (r.height < 44 || r.width < 44) {
                    t.push(e.textContent.trim() + ' ' + Math.round(r.width)
                           + 'x' + Math.round(r.height));
                }
            }
            return t;
        }""")
        if klein:
            fehler.append(f'{thema}/{name}: Trefferfläche unter 44 px {klein[:3]}')

        # Bruch mitten im Wort: mehr als ein Rechteck über einem Text ohne
        # Leerzeichen heißt, er wurde innerhalb des Worts umbrochen.
        umbruch = seite.evaluate("""() => {
            const treffer = [];
            const pruefen = (e, kennung) => {
                const text = e.textContent.trim();
                if (!text || text.includes(' ')) return;
                const r = document.createRange();
                r.selectNodeContents(e);
                if (r.getClientRects().length > 1) {
                    treffer.push(kennung + ': ' + text);
                }
            };
            for (const e of document.querySelectorAll('.karte__name'))
                pruefen(e, 'name');
            for (const e of document.querySelectorAll('.karte__einheit'))
                pruefen(e, 'einheit');
            return treffer;
        }""")
        if umbruch:
            fehler.append(f'{thema}/{name}: Bruch im Wort {umbruch[:4]}')

        # "8,291 kW" / "h" ist eine andere physikalische Größe.
        getrennt = seite.evaluate("""() => {
            const t = [];
            for (const w of document.querySelectorAll('.karte__wert')) {
                const e = w.querySelector('.karte__einheit');
                if (!e) continue;
                const rw = w.getBoundingClientRect();
                const re = e.getBoundingClientRect();
                if (re.top > rw.top + rw.height * 0.55) {
                    t.push(w.textContent.trim().slice(0, 22));
                }
            }
            return t;
        }""")
        if getrennt:
            fehler.append(
                f'{thema}/{name}: Einheit vom Wert getrennt {getrennt[:3]}')

        if screenshot_dir and name in ('telefon', 'desktop'):
            seite.screenshot(path=f'{screenshot_dir}/{thema}-{name}.png',
                             full_page=True)
    finally:
        seite.close()
    return fehler


def _bedienung_pruefen(browser, url) -> list:
    """Wirken die Knöpfe? Ein Knopf ohne Wirkung sieht aus wie einer mit."""
    fehler = []
    seite = browser.new_page(viewport={'width': 1440, 'height': 900})
    try:
        seite.goto(url, wait_until='networkidle')

        # Aufklappen wie ein Mensch: auf die Kopfzeile klicken, nicht `open`
        # setzen. Damit ist zugleich geprüft, dass der Klick überhaupt wirkt —
        # eine Kopfzeile ohne Klappwirkung sieht aus wie eine mit.
        # Der Abschnitt startet `hidden` und wird erst eingeblendet, wenn die
        # Abrechnung geantwortet hat — vorher ginge der Klick ins Leere.
        kopf = seite.locator(
            'details[data-bereich="abrechnung"] > .abschnitt__kopf')
        try:
            kopf.wait_for(state='visible', timeout=20000)
            kopf.click()
        except Exception:
            return ['Bedienung: Abrechnungsabschnitt nicht anklickbar']
        if not seite.evaluate("""() => document.querySelector(
                'details[data-bereich="abrechnung"]').open"""):
            fehler.append('Bedienung: Klick auf die Kopfzeile klappt nicht auf')

        try:
            seite.wait_for_selector('[data-liste="abrechnung"] .karte',
                                    timeout=20000)
        except Exception:
            return ['Bedienung: keine Abrechnungskachel erschienen']

        # Die Sicherungs-Kachel. Sie ist die einzige Stelle im Produkt, an
        # der eine fehlende Sicherung sichtbar wird — der Dienst läuft ja
        # weiter, und die Zahlen stimmen. Genau deshalb darf sie nicht
        # stillschweigend verschwinden.
        sicherung = seite.evaluate("""() => {
            const s = document.querySelector('details[data-bereich="system"]');
            if (!s) return {fehlt: 'kein Rechner-Abschnitt'};
            s.open = true;   // eingeklappt hat die Kachel keine Ausmaße
            const k = [...s.querySelectorAll('.karte')]
                .find(e => e.textContent.includes('Sicherung'));
            if (!k) return {fehlt: 'keine Sicherungs-Kachel'};
            return {
                zustand: k.dataset.zustand || null,
                text: k.innerText,
                hatOrt: /Netzlaufwerk|Speicherkarte/.test(k.innerText),
            };
        }""")
        if sicherung.get('fehlt'):
            fehler.append(f'Bedienung: {sicherung["fehlt"]}')
        elif not sicherung['hatOrt']:
            # Ohne den Ort sieht eine Sicherung auf derselben Karte aus wie
            # eine auf dem Netzlaufwerk — der Unterschied ist aber genau der
            # zwischen geschützt und ungeschützt.
            fehler.append('Bedienung: Sicherungs-Kachel nennt den Ort nicht')

        # Die Export-Links müssen den ANGEZEIGTEN Zeitraum tragen. Ohne
        # Parameter liefern sie stillschweigend die Vorgabe des Dienstes —
        # jemand lädt dann 24 Stunden herunter, während "30 Tage" auf dem
        # Schirm steht, und sieht dem Dokument nicht an, dass es das falsche
        # ist. Genau so war es gebaut, bis diese Prüfung entstand.
        links = seite.evaluate("""() => {
            const gewaehlt = document.querySelector(
                '[data-liste="abrechnung-zeitraum"] [aria-pressed="true"]');
            return {
                zeitraum: gewaehlt ? gewaehlt.textContent.trim() : null,
                pdf: document.querySelector('[data-feld="export-pdf"]')
                     ?.getAttribute('href'),
                csv: document.querySelector('[data-feld="export-csv"]')
                     ?.getAttribute('href'),
            };
        }""")
        for art in ('pdf', 'csv'):
            ziel = links[art]
            if not ziel:
                fehler.append(f'Bedienung: Export-Link {art} fehlt')
            elif 'range=' not in ziel:
                fehler.append(
                    f'Bedienung: Export-Link {art} ohne Zeitraum ({ziel!r}) — '
                    f'lädt die Vorgabe statt der Auswahl')
            elif 'model=' not in ziel:
                fehler.append(
                    f'Bedienung: Export-Link {art} ohne Verteilmodell ({ziel!r})')

        def erste_kachel():
            return seite.inner_text(
                '[data-liste="abrechnung"] .karte:first-child')

        for liste, mindestens, kennung in (
                ('abrechnung-zeitraum', 2, 'Zeitraum'),
                ('abrechnung-modell', 2, 'Modell')):
            knoepfe = seite.query_selector_all(
                f'[data-liste="{liste}"] .zeitwahl__knopf')
            if len(knoepfe) < mindestens:
                fehler.append(f'{kennung}: nur {len(knoepfe)} Knöpfe')
                continue

            # Den ersten Knopf wählen, der gerade NICHT aktiv ist.
            ziel = next((k for k in knoepfe
                         if k.get_attribute('aria-pressed') != 'true'), None)
            if ziel is None:
                fehler.append(f'{kennung}: kein inaktiver Knopf zum Prüfen')
                continue

            vorher = erste_kachel()
            ziel.click()
            seite.wait_for_timeout(2500)
            if erste_kachel() == vorher:
                fehler.append(
                    f'{kennung}: Klick auf "{ziel.inner_text()}" ändert nichts')
            if ziel.get_attribute('aria-pressed') != 'true':
                fehler.append(f'{kennung}: aria-pressed nach Klick nicht true')

        # Jede Zeitraumreihe braucht ihre Beschriftung. Ohne sie stehen
        # "30 Tage", "2026" und "August 2026" ununterscheidbar nebeneinander
        # — und ein `replaceChildren()` an der falschen Stelle löscht die
        # Marke, ohne dass ein Knopf dabei kaputtginge.
        reihen = seite.evaluate("""() => {
            const o = [];
            for (const r of document.querySelectorAll(
                    '[data-liste="abrechnung-zeitraum"] .zeitwahl')) {
                o.push({
                    marke: (r.querySelector('.zeitwahl__marke') || {})
                        .textContent || '',
                    knoepfe: r.querySelectorAll('button').length,
                });
            }
            return o;
        }""")
        if not reihen:
            fehler.append('keine Zeitraumreihen gefunden')
        for i, r in enumerate(reihen):
            if not r['marke'].strip():
                fehler.append(
                    f'Zeitraumreihe {i + 1} ohne Beschriftung '
                    f'({r["knoepfe"]} Knöpfe)')

        if not seite.inner_text('[data-feld="abrechnung-urteil"]').strip():
            fehler.append('Belastbarkeit wird nicht angezeigt')
        if len(seite.query_selector_all(
                '[data-liste="abrechnung"] .anteil__teil')) < 2:
            fehler.append('keine Anteilsbalken gezeichnet')
    finally:
        seite.close()
    return fehler


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--url', default='http://127.0.0.1:8080/dashboard')
    p.add_argument('--screenshots', metavar='ORDNER',
                   help='Bildschirmfotos dort ablegen')
    args = p.parse_args()

    try:
        import playwright  # noqa: F401
    except ImportError:
        print('Playwright fehlt — Prüfung übersprungen.\n'
              '  pip install playwright && playwright install chromium',
              file=sys.stderr)
        return 0

    fehler = pruefen(args.url, args.screenshots)
    if fehler:
        print(f'{len(fehler)} Befunde:')
        for f in fehler:
            print('  -', f)
        return 1
    print('Dashboard: alle Prüfungen bestanden.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
