#!/bin/bash
# Sichert Impulse, Hausbilanz und Berichte auf die Synology.
#
# Wird vom Timer s0counter-backup.timer nachts aufgerufen. Läuft auch von
# Hand. Der Zielordner muss eingebunden sein — siehe README, Abschnitt
# "Sicherung auf die Synology".
set -euo pipefail

CONFIG=${S0_CONFIG:-/etc/s0counter/config.toml}
TARGET=${S0_BACKUP_TARGET:-/mnt/synology/s0counter}
KEEP_DAYS=${S0_BACKUP_KEEP_DAYS:-90}
# Zweite Grenze, nach Anzahl. Die Altersgrenze allein deckelt den Platz
# nicht: mehrere Läufe am selben Tag fallen alle in dieselbe Frist. Der Wert
# gilt je Art (Impulse, Bilanz, Verlauf, Konfiguration) getrennt.
KEEP_COUNT=${S0_BACKUP_KEEP_COUNT:-14}
# Ausweichziel auf dem Pi selbst, falls die NAS nicht eingebunden ist. Eine
# Sicherung auf derselben SD-Karte schützt nicht gegen deren Ausfall — aber
# sehr wohl gegen eine kaputtgeschriebene Datenbank, einen Fehlgriff bei
# `set-reading` oder ein misslungenes Update. Und sie ist das, was den
# Unterschied macht zwischen „keine Sicherung" und „eine unvollständige".
FALLBACK=${S0_BACKUP_FALLBACK:-/var/backups/s0counter}
FALLBACK_KEEP_DAYS=${S0_BACKUP_FALLBACK_KEEP_DAYS:-14}
# Lokal enger als auf der NAS: hier liegt die Sicherung auf derselben Karte
# wie die Daten, und eine vollgeschriebene Karte hält die Erfassung an.
FALLBACK_KEEP_COUNT=${S0_BACKUP_FALLBACK_KEEP_COUNT:-3}
S0CTL=${S0CTL:-/usr/local/bin/s0ctl}
PYTHON=${S0_PYTHON:-/opt/s0counter/venv/bin/python}

DATE=$(date +%F)
STAMP=$(date +%F_%H%M)

log() { echo "$(date '+%F %T') $*"; }
fail() { log "FEHLER: $*" >&2; exit 1; }

# Ein nicht eingebundenes Ziel ist der häufigste Fehler: das Skript würde
# dann munter in ein lokales Verzeichnis schreiben, das beim nächsten
# Einbinden verdeckt wird — die Sicherung wäre unsichtbar weg.
if ! mountpoint -q "$(dirname "$TARGET")" 2>/dev/null && \
   ! mountpoint -q "$TARGET" 2>/dev/null; then
    # Früher brach das Skript hier ab. Das war richtig gedacht — schreiben in
    # ein nicht eingebundenes Ziel verdeckt die Sicherung beim nächsten
    # Einbinden — und in der Wirkung falsch: die Anlage lief einen Tag ganz
    # ohne Sicherung, weil der nächtliche Lauf lautlos fehlschlug. Statt
    # abzubrechen wird lokal gesichert und deutlich gewarnt.
    log "WARNUNG: $TARGET ist nicht eingebunden — NAS nicht erreichbar?"
    log "WARNUNG: sichere ersatzweise nach $FALLBACK (gleiche SD-Karte!)"
    log "WARNUNG: schützt NICHT gegen Kartenausfall. Siehe README, Abschnitt 14."
    TARGET="$FALLBACK"
    KEEP_DAYS="$FALLBACK_KEEP_DAYS"
    KEEP_COUNT="$FALLBACK_KEEP_COUNT"
    ERSATZ=1
else
    ERSATZ=0
fi

mkdir -p "$TARGET"

log "Sicherung nach $TARGET"

# Pfade aus dem Konfigurationsleser statt per grep aus der TOML-Datei.
#
# Grund: Diese Werte haben Vorgaben und stehen deshalb gar nicht in der
# Datei, solange sie niemand ändert. Beim Verlauf war genau das der Fall —
# ein grep auf `[history]` fand nichts und hätte ihn stillschweigend nie
# gesichert. Bei `balance` und `report` stand es nur zufällig drin, und ein
# gelöschter Vorgabewert hätte sie ebenso lautlos aus der Sicherung geworfen.
PFADE=$("$PYTHON" -c '
import sys
sys.path.insert(0, "/opt/s0counter")
from s0counter import config as c
cfg = c.load(sys.argv[1])
print(cfg.balance.database if cfg.balance.enabled else "")
print(cfg.history.database if cfg.history.enabled else "")
print(cfg.report.directory if cfg.report.enabled else "")
' "$CONFIG" 2>/dev/null || true)
BALANCE_DB=$(sed -n 1p <<<"$PFADE")
HISTORY_DB=$(sed -n 2p <<<"$PFADE")
REPORTS=$(sed -n 3p <<<"$PFADE")

# Datenbanken über die SQLite-Backup-API, transaktionssicher auch bei
# laufendem Dienst. Eine Dateikopie wäre bei aktivem WAL unbrauchbar.
"$S0CTL" -c "$CONFIG" backup "$TARGET/impulse-$STAMP.sqlite3"
log "  Impulse gesichert"

if [ -n "$BALANCE_DB" ] && [ -f "$BALANCE_DB" ]; then
    # Über Python statt über das sqlite3-Kommando: der Interpreter ist
    # ohnehin da, das Kommandozeilenwerkzeug muss es nicht sein.
    "$PYTHON" - "$BALANCE_DB" "$TARGET/bilanz-$STAMP.sqlite3" <<'PY'
import sys
sys.path.insert(0, '/opt/s0counter')
from s0counter.db import sqlite_kopieren
sqlite_kopieren(sys.argv[1], sys.argv[2])
PY
    log "  Hausbilanz gesichert"
fi

# CSV-Export der letzten 40 Tage. Vollständig wäre bei Millionen Zeilen
# unhandlich, und die Datenbank ist ohnehin dabei.
"$S0CTL" -c "$CONFIG" export "$TARGET/impulse-$DATE.csv" --days 40
log "  CSV geschrieben"

# Der Verlauf der Rechnerwerte. Nicht abrechnungsrelevant, aber die Quelle
# der Diagramme — und nach einem Kartenausfall nicht rekonstruierbar.
if [ -n "$HISTORY_DB" ] && [ -f "$HISTORY_DB" ]; then
    "$PYTHON" - "$HISTORY_DB" "$TARGET/verlauf-$STAMP.sqlite3" <<'PY'
import sys
sys.path.insert(0, '/opt/s0counter')
from s0counter.db import sqlite_kopieren
sqlite_kopieren(sys.argv[1], sys.argv[2])
PY
    log "  Verlauf gesichert"
fi

# Die Konfiguration. Sie enthält die Offsets der Zählerstände und das
# Home-Assistant-Token — nach einem Kartenausfall ist sie das, was sich am
# schwersten wiederbeschaffen lässt: die Datenbanken sind Zahlen, die
# Offsets sind abgelesene Displaystände von einem bestimmten Tag.
#
# Modus 600: die Kopie trägt dasselbe Geheimnis wie das Original. Auf einem
# Netzlaufwerk hängen die Rechte allerdings an dessen Einbindung — deshalb
# steht in der README, das Ziel nicht öffentlich freizugeben.
if [ -f "$CONFIG" ]; then
    install -m 600 "$CONFIG" "$TARGET/config-$STAMP.toml"
    log "  Konfiguration gesichert"
fi

# Die systemd-Units. Klein, aber ohne sie beginnt eine Neuinstallation mit
# Raten, welche Timer es gab und wie der Dienst gestartet wurde.
UNITS=$(ls /etc/systemd/system/s0counter*.service \
           /etc/systemd/system/s0counter*.timer 2>/dev/null || true)
if [ -n "$UNITS" ]; then
    mkdir -p "$TARGET/systemd"
    # shellcheck disable=SC2086
    cp -u $UNITS "$TARGET/systemd/" 2>/dev/null || true
    log "  systemd-Units übernommen"
fi

if [ -n "$REPORTS" ] && [ -d "$REPORTS" ]; then
    mkdir -p "$TARGET/berichte"
    cp -u "$REPORTS"/*.pdf "$TARGET/berichte/" 2>/dev/null || true
    log "  Berichte übernommen"
fi

# Aufräumen. Jede Datei, die oben mit -$STAMP angelegt wird, muss hier
# stehen — sonst wächst sie ewig weiter. `verlauf-` und `config-` fehlten
# beim Einbau prompt; deshalb steht das Muster jetzt in EINER Variablen und
# nicht zweimal im Skript.
#
# Mit Endung, nicht nur mit Präfix: `impulse-*` trifft auch den CSV-Export.
# Der soll hier mitrotieren, aber als eigener Eintrag — sonst zählt er bei
# der Anzahlgrenze weiter unten als Datenbank mit.
ROTIERT=(-name 'impulse-*.sqlite3' -o -name 'impulse-*.csv'
         -o -name 'bilanz-*.sqlite3' -o -name 'verlauf-*.sqlite3'
         -o -name 'config-*.toml')
# Berichte und die systemd-Units sind ausgenommen: die sollen bleiben, und
# die Units werden ohnehin überschrieben statt datiert.
DELETED=$(find "$TARGET" -maxdepth 1 -type f \
    \( "${ROTIERT[@]}" \) \
    -mtime "+$KEEP_DAYS" -print -delete | wc -l)
if [ "$DELETED" -gt 0 ]; then
    log "  $DELETED Sicherungen älter als $KEEP_DAYS Tage entfernt"
fi

# Zweite Grenze, nach Anzahl statt nach Alter. Sie deckelt den Platzbedarf
# hart — die Altersgrenze allein tut das nicht: bei mehreren Läufen am Tag
# (Timer plus ein paar Läufe von Hand) sammeln sich beliebig viele Stände
# innerhalb derselben Frist an.
#
# Gezählt wird je Art getrennt, nicht über alle Dateien: sonst verdrängen
# vier Impulssicherungen die zugehörigen Konfigurationskopien, und man hätte
# Datenbanken ohne die Offsets, die sie erst lesbar machen.
#
# Das Muster nennt Präfix UND Endung. Ein bloßes `impulse-*` traf auch den
# CSV-Export `impulse-JJJJ-MM-TT.csv` — der zählte als vierter Stand mit und
# verdrängte prompt eine echte Datenbank. Belegt beim ersten Lauf.
GEKAPPT=0
for MUSTER in 'impulse-*.sqlite3' 'bilanz-*.sqlite3' \
              'verlauf-*.sqlite3' 'config-*.toml' 'impulse-*.csv'; do
    # Neueste zuerst, ab Platz KEEP_COUNT+1 löschen. `find -printf` mit
    # Zeitstempel statt `ls -t`: Dateinamen mit Sonderzeichen brechen `ls`.
    while IFS= read -r datei; do
        [ -n "$datei" ] || continue
        rm -f -- "$datei" && GEKAPPT=$((GEKAPPT + 1))
    done < <(find "$TARGET" -maxdepth 1 -type f -name "$MUSTER" \
             -printf '%T@\t%p\n' 2>/dev/null \
             | sort -rn | tail -n "+$((KEEP_COUNT + 1))" | cut -f2-)
done
if [ "$GEKAPPT" -gt 0 ]; then
    log "  $GEKAPPT Sicherungen über die Grenze von $KEEP_COUNT je Art entfernt"
fi

SIZE=$(du -sh "$TARGET" 2>/dev/null | cut -f1)
log "Fertig. Belegt: $SIZE"

# Die Warnung am Ende wiederholen: Wer den Dienststatus ansieht, liest die
# letzte Zeile — die erste steht dann schon außer Sicht. Und der Rückgabewert
# bleibt 0, damit systemd den Lauf nicht als fehlgeschlagen führt: er ist
# gelungen, nur an einem schlechteren Ort.
if [ "$ERSATZ" -eq 1 ]; then
    log "ACHTUNG: nur lokal gesichert ($TARGET) — die NAS war nicht eingebunden."
fi
