#!/bin/bash
# Installiert den S0-Zähler als systemd-Dienst.
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "Bitte mit sudo ausführen." >&2
    exit 1
fi

BASE="$(cd "$(dirname "$0")" && pwd)"
CONFIG=/etc/s0counter/config.toml

# Bei der Erstinbetriebnahme muss der Signaltest vor dem Dienst laufen: ein
# laufender s0counter belegt die GPIOs, test_gpio.py kommt dann nicht mehr
# dran. Mit --no-start wird alles installiert und geprüft, der Dienst aber
# weder gestartet noch aktiviert.
NO_START=0
for arg in "$@"; do
    case "$arg" in
        --no-start) NO_START=1 ;;
        *) echo "Unbekannte Option: $arg" >&2
           echo "Aufruf: $0 [--no-start]" >&2
           exit 2 ;;
    esac
done

echo "== Systempakete =="
apt-get update
apt-get install -y python3 python3-venv python3-pip python3-gpiozero \
    python3-lgpio python3-paho-mqtt sqlite3

echo
echo "== Benutzer und Verzeichnisse =="
id s0counter >/dev/null 2>&1 || \
    useradd --system --home /var/lib/s0counter --shell /usr/sbin/nologin s0counter

# Auf aktuellem Raspberry Pi OS legt das udev-Regelwerk die Gruppe gpio an.
# Fehlt sie, wird sie ergänzt, damit die Rechte auf /dev/gpiochip* passen.
getent group gpio >/dev/null || groupadd --system gpio
usermod -a -G gpio s0counter
# dialout für den USB-RS232-Adapter des Anzeigepanels
getent group dialout >/dev/null && usermod -a -G dialout s0counter

install -d -o root -g root /opt/s0counter /etc/s0counter
install -d -o s0counter -g s0counter -m 750 /var/lib/s0counter

echo
echo "== Programmdateien =="
rm -rf /opt/s0counter/s0counter
cp -r "$BASE/s0counter" /opt/s0counter/
install -m 755 "$BASE/s0ctl.py" "$BASE/test_gpio.py" "$BASE/backup.sh" \
    /opt/s0counter/
[ -f "$BASE/README.md" ] && install -m 644 "$BASE/README.md" /opt/s0counter/

# Das Anzeigepanel braucht die Bibliothek des Displayprojekts. Liegt sie
# daneben, wird sie mitinstalliert — sonst bleibt [panel] eben aus.
if [ -d "$BASE/../edip320-display-test/edip320" ]; then
    rm -rf /opt/s0counter/edip320
    cp -r "$BASE/../edip320-display-test/edip320" /opt/s0counter/
    echo "  Displaybibliothek edip320 übernommen"
else
    echo "  Displaybibliothek nicht gefunden — [panel] bleibt ungenutzt"
fi

python3 -m venv --system-site-packages /opt/s0counter/venv
/opt/s0counter/venv/bin/pip install --quiet --upgrade pip
/opt/s0counter/venv/bin/pip install --quiet -r "$BASE/requirements.txt"

echo
echo "== Konfiguration =="
if [ -f "$CONFIG" ]; then
    # Eine bestehende Konfiguration wird nie überschrieben — sie enthält
    # den Offset-Abgleich und gegebenenfalls das Home-Assistant-Token.
    echo "  $CONFIG bleibt unverändert."
    if ! diff -q "$BASE/config.toml" "$CONFIG" >/dev/null 2>&1; then
        install -m 640 -o root -g s0counter "$BASE/config.toml" "$CONFIG.neu"
        echo "  Neue Vorlage liegt als $CONFIG.neu — Unterschiede prüfen:"
        echo "    diff $CONFIG $CONFIG.neu"
    fi
else
    install -m 640 -o root -g s0counter "$BASE/config.toml" "$CONFIG"
    echo "  $CONFIG angelegt."
fi

install -m 644 "$BASE/s0counter.service" /etc/systemd/system/s0counter.service

# Timer für Jahresbericht und Sicherung mitinstallieren, aber nicht
# aktivieren — sie brauchen erst Konfiguration (Telegram-Token, NAS-Mount).
if [ -d "$BASE/systemd" ]; then
    install -m 644 "$BASE"/systemd/*.service "$BASE"/systemd/*.timer \
        /etc/systemd/system/
    echo "  Timer installiert, aber nicht aktiviert (siehe README)"
fi

cat >/usr/local/bin/s0ctl <<'WRAPPER'
#!/bin/sh
# PYTHONPATH, damit s0ctl aus jedem Verzeichnis heraus läuft — auch aus
# einem, in das der aufrufende Benutzer nicht schreiben darf.
exec env PYTHONPATH=/opt/s0counter \
    /opt/s0counter/venv/bin/python /opt/s0counter/s0ctl.py "$@"
WRAPPER
chmod 755 /usr/local/bin/s0ctl

chown -R root:root /opt/s0counter

echo
echo "== Konfiguration prüfen =="
if ! /opt/s0counter/venv/bin/python -m s0counter.main -c "$CONFIG" --check-config; then
    echo
    echo "Die Konfiguration ist fehlerhaft. Der Dienst wird nicht gestartet." >&2
    echo "Nach dem Korrigieren: sudo systemctl enable --now s0counter" >&2
    exit 1
fi

if [ "$NO_START" -eq 1 ]; then
    systemctl daemon-reload
    echo
    echo "== Dienst NICHT gestartet (--no-start) =="
    echo
    echo "Alles installiert und geprüft. Die GPIOs sind frei für den"
    echo "Signaltest. Reihenfolge der Erstinbetriebnahme:"
    echo
    echo "  1. Signaltest, jeden Kanal einzeln auslösen:"
    echo "       sudo -u s0counter /opt/s0counter/venv/bin/python /opt/s0counter/test_gpio.py"
    echo
    echo "  2. Erst wenn jeder Kanal genau ein Ereignis je Impuls zeigt:"
    echo "       sudo systemctl enable --now s0counter"
    echo
    exit 0
fi

echo
echo "== Dienst starten =="

# Lief der Dienst schon, muss er neu gestartet werden: "enable --now"
# lässt einen laufenden Dienst unangetastet. Nach einem Update liefen
# sonst weiter der alte Programmstand und die alte Unit — die neuen
# Dateien lägen bloß auf der Platte. Das fällt erst beim nächsten
# Neustart des Pi auf, also frühestens Wochen später.
if systemctl is-active --quiet s0counter; then
    RESTART=1
    echo "  Dienst läuft — wird mit dem neuen Stand neu gestartet."
else
    RESTART=0
fi

systemctl daemon-reload
systemctl enable --now s0counter
[ "$RESTART" -eq 1 ] && systemctl restart s0counter

# Nicht nur starten, sondern nachsehen, ob er auch oben bleibt. Ein
# fehlgeschlagener ExecStart hinter einem erfolgreichen ExecStartPre ist
# sonst nicht zu erkennen: systemctl enable --now meldet Erfolg, der
# Dienst hängt in der Neustartschleife, und die Installation behauptet,
# fertig zu sein. Restart=on-failure lässt "activating (auto-restart)"
# zu, deshalb wird bis zu 15 s auf einen stabilen Zustand gewartet.
for _ in $(seq 15); do
    STATE=$(systemctl is-active s0counter || true)
    [ "$STATE" = "activating" ] || break
    sleep 1
done

if [ "$(systemctl is-active s0counter || true)" != "active" ]; then
    echo
    echo "Der Dienst läuft nicht. Die letzten Meldungen:" >&2
    echo >&2
    journalctl -u s0counter -n 30 --no-pager >&2 || true
    echo >&2
    echo "Installation abgebrochen — nichts wurde entfernt." >&2
    echo "Nach dem Beheben:  sudo systemctl restart s0counter" >&2
    exit 1
fi

systemctl --no-pager --full status s0counter || true

echo
echo "Fertig. Nächste Schritte:"
echo
echo "  1. Signaltest, jeden Kanal einzeln auslösen:"
echo "       sudo systemctl stop s0counter"
echo "       sudo -u s0counter /opt/s0counter/venv/bin/python /opt/s0counter/test_gpio.py"
echo "       sudo systemctl start s0counter"
echo
echo "  2. Displaystände eintragen:"
echo "       sudo s0ctl set-reading eg 1234.567 --force"
echo
echo "  3. Status:  curl http://127.0.0.1:8080/status"
echo
echo "  Anleitung: $BASE/README.md"
