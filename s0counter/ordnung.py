"""Reihenfolge der Wohnungen — an EINER Stelle.

Alphabetisch sortiert ergibt sich dg, eg, og. Für Stockwerke ist das
sinnlos: gelesen wird von unten nach oben, also EG, OG, DG. Die Anzeige
macht das seit jeher richtig (`config.reihenfolge` in `dashboard.js`), die
Python-Seite nicht — dort stand an sechs Stellen ein blankes `sorted()`,
und die Jahresabrechnung listete das Dachgeschoss zuoberst.

Bewusst ein eigenes Modul ohne Abhängigkeiten: es wird von der Abrechnung,
dem Dienst, dem Panel, dem Doctor und der Telegram-Ausgabe verwendet. Läge
die Liste in einem davon, müssten die anderen es importieren — und die
zweite Kopie wäre nur eine Frage der Zeit. Wer eine duplizierte Liste
findet, sucht nach der dritten.

Zähler, die hier nicht stehen, hängen alphabetisch hinten an, statt zu
verschwinden. Das ist der Grund für den zweistufigen Schlüssel: eine
Sortierung, die unbekannte Namen wegwirft, verliert stillschweigend Daten.
"""

# Von unten nach oben. Muss mit `config.reihenfolge` in dashboard.js
# übereinstimmen — `test_ordnung.py` erzwingt das, indem es die Liste aus
# der JS-Datei parst statt sie abzuschreiben.
REIHENFOLGE = ('eg', 'og', 'dg')


def sortierschluessel(meter_id: str) -> tuple:
    """Sortierschlüssel für einen Zählernamen.

    Bekannte Namen kommen in der Reihenfolge der Stockwerke, unbekannte
    danach in alphabetischer Ordnung.
    """
    name = (meter_id or '').lower()
    try:
        return (0, REIHENFOLGE.index(name), name)
    except ValueError:
        return (1, 0, name)


def sortiert(meter_ids) -> list:
    """Zählernamen in Stockwerksreihenfolge.

    Nimmt alles Iterierbare — auch ein dict, dann werden die Schlüssel
    sortiert. Das entspricht dem Verhalten von `sorted()`, das hier überall
    ersetzt wird.
    """
    return sorted(meter_ids, key=sortierschluessel)
