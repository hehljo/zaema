#!/bin/bash
# Rendert stromlaufplan.html nach stromlaufplan.pdf, beides neben diesem Skript.
#
# Warum der Umweg über ein Arbeitsverzeichnis im Snap-Home: Chromium liegt auf
# diesem Rechner als Snap und darf nur dort schreiben. Ein --print-to-pdf direkt
# in das Repo schlägt fehl, ohne eine verwertbare Fehlermeldung zu liefern.
set -euo pipefail

HIER="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
QUELLE="$HIER/stromlaufplan.html"
ZIEL="$HIER/stromlaufplan.pdf"
ARBEIT="${PLAN_WORKDIR:-$HOME/snap/chromium/common/plan}"

command -v chromium-browser >/dev/null 2>&1 || {
  echo "FEHLER: chromium-browser nicht gefunden" >&2; exit 1; }
[ -f "$QUELLE" ] || { echo "FEHLER: $QUELLE fehlt" >&2; exit 1; }

mkdir -p "$ARBEIT"
cp "$QUELLE" "$ARBEIT/"

# Das Rauschen von D-Bus und dconf ist im Snap ohne Session-Bus unvermeidbar
# und sagt nichts über den Erfolg aus.
(cd "$ARBEIT" && chromium-browser \
    --headless --disable-gpu --no-sandbox --no-pdf-header-footer \
    --print-to-pdf="$ARBEIT/out.pdf" stromlaufplan.html) 2>&1 \
  | grep -iE "bytes written" \
  | grep -viE "dbus|dconf|NameHasOwner|bluez" || true

[ -s "$ARBEIT/out.pdf" ] || { echo "FEHLER: kein PDF erzeugt" >&2; exit 1; }

cp "$ARBEIT/out.pdf" "$ZIEL"
BLAETTER="$(pdfinfo "$ZIEL" 2>/dev/null | awk '/^Pages/{print $2}')"
echo "PDF: $ZIEL ($(du -h "$ZIEL" | cut -f1), ${BLAETTER:-?} Blätter)"
