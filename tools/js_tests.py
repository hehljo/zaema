#!/usr/bin/env python3
"""Führt die JavaScript-Tests der Anzeige aus.

Eigener Runner statt eines nackten `node --test`, weil ein Testrunner, der
nichts findet, Erfolg meldet: Exit-Code 0 bei null gelaufenen Tests sieht
grün aus und prüft nichts. Deshalb drei Ausgänge statt zwei:

    0   Tests gelaufen und bestanden
    1   Tests gelaufen, mindestens einer gefallen
    2   NICHT GELAUFEN — keine Datei gefunden, node fehlt, oder null Tests

Aufruf:

    python3 tools/js_tests.py

Die Python-Suite bleibt davon unberührt: node ist auf dem Pi nicht nötig,
die Anzeige läuft im Browser. Dieser Runner gehört auf den
Entwicklungsrechner und in die Prüfliste vor dem Ausrollen.
"""
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

WURZEL = Path(__file__).resolve().parent.parent
MUSTER = 'test_*.mjs'
# Sekunden für den GANZEN Lauf. Siehe Begründung bei subprocess.run unten.
ZEITLIMIT = int(os.environ.get('S0_JS_TIMEOUT', '20'))


def main() -> int:
    dateien = sorted((WURZEL / 'tests').glob(MUSTER))
    if not dateien:
        print(f'NICHT GELAUFEN: keine Datei nach {MUSTER} in tests/',
              file=sys.stderr)
        return 2

    if shutil.which('node') is None:
        print('NICHT GELAUFEN: node ist nicht installiert', file=sys.stderr)
        return 2

    # Zeitlimit, sonst blockiert eine Endlosschleife in der geprüften Logik
    # den ganzen Lauf, statt einen Fehler zu melden. Belegt am 26.08.2026:
    # eine Sabotage an der Wasserfall-Schleife ließ den Runner hängen — er
    # war weder grün noch rot, sondern gar nichts.
    #
    # Das Limit gehört hierher und nicht in die Testdatei: eine synchrone
    # Endlosschleife blockiert den Event-Loop, node:test kommt mit seinem
    # eigenen Timeout gar nicht mehr dran. Die Tests brauchen zusammen unter
    # einer Sekunde; 20 s sind auch auf einem Pi reichlich und halten den
    # Fehlerfall kurz.
    befehl = ['node', '--test', *[str(d) for d in dateien]]
    try:
        fertig = subprocess.run(befehl, capture_output=True, text=True,
                                cwd=str(WURZEL), timeout=ZEITLIMIT)
    except subprocess.TimeoutExpired:
        print(f'NICHT GELAUFEN: node hat das Zeitlimit von {ZEITLIMIT} s '
              'gerissen — vermutlich eine Endlosschleife in der geprüften '
              'Logik', file=sys.stderr)
        return 2
    ausgabe = (fertig.stdout or '') + (fertig.stderr or '')
    print(ausgabe, end='')

    # Die Zusammenfassung von node:test wird ausgewertet, nicht nur der
    # Exit-Code: „0 Tests, Code 0" ist genau der Fall, der hier auffallen
    # muss.
    bestanden = _zahl(ausgabe, 'pass')
    gefallen = _zahl(ausgabe, 'fail')

    if bestanden is None or gefallen is None:
        print('NICHT GELAUFEN: node hat keine Zusammenfassung ausgegeben',
              file=sys.stderr)
        return 2
    if bestanden + gefallen == 0:
        print('NICHT GELAUFEN: null Tests ausgeführt', file=sys.stderr)
        return 2
    if gefallen or fertig.returncode != 0:
        print(f'{gefallen} von {bestanden + gefallen} gefallen',
              file=sys.stderr)
        return 1

    print(f'{bestanden} JavaScript-Tests bestanden')
    return 0


def _zahl(ausgabe: str, feld: str):
    r"""Wert hinter `pass`/`fail` aus der Zusammenfassung von node:test.

    Das Präfix ist nicht `#`, sondern `ℹ` — und es hat schon einmal
    gewechselt. Deshalb `\S*`, also ein beliebiges Zeichen am Zeilenanfang:
    sonst meldet der Runner „nicht gelaufen", während die Tests längst
    laufen. Genau das ist am 26.08.2026 passiert und nur durch die
    Sabotage-Gegenprobe aufgefallen.

    Nicht `\W*`: `ℹ` ist ein Unicode-Buchstabe und zählt in Python als
    `\w`. Der naheliegende Ausdruck greift also gerade nicht.
    """
    treffer = re.search(rf'^\S*\s*{feld}\s+(\d+)\s*$', ausgabe, re.MULTILINE)
    return int(treffer.group(1)) if treffer else None


if __name__ == '__main__':
    sys.exit(main())
