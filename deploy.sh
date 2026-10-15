#!/bin/bash
# Bringt eine neue Fassung des Codes vom Arbeitsrechner auf den laufenden Pi.
#
# Abgrenzung zu install.sh: das legt die Anlage an (Pakete, Benutzer, venv,
# Dienst, Konfiguration) und läuft AUF dem Pi. Dieses Skript läuft auf dem
# Arbeitsrechner, setzt eine fertige Installation voraus und tauscht nur den
# Code aus. Konfiguration und Datenbank bleiben unberührt.
#
#   ./deploy.sh                 überträgt geänderte Dateien und startet neu
#   ./deploy.sh --dry-run       zeigt nur, was sich unterscheidet
#   ./deploy.sh --host devpi    anderer SSH-Host (Standard: devpi)
#
# Warum nicht `git pull` auf dem Pi: dort liegt bewusst kein Repository. Der
# Pi bekommt Code, keine Historie — und keinen Schreibzugriff auf das Remote.
#
# Warum `cat >` statt scp: derselbe Grund wie bei der Synology (siehe
# CLAUDE.md) — ein Weg für alle Ziele ist ein Weg weniger, der kaputtgehen
# kann, und `cat` braucht kein SFTP-Subsystem.
set -euo pipefail

HOST=devpi
DRY_RUN=0
ZIEL=/opt/s0counter/s0counter

# Welche Dateien überhaupt zum Code gehören. Bewusst EINE Liste: dieselbe
# Aufzählung stand hier zwischenzeitlich dreimal (Abgleich, Rechte,
# Nachkontrolle), und beim ersten Lauf fehlte `*.html` in zweien davon —
# genau die Datei, die den Abrechnungsabschnitt der Anzeige trägt.
#
# In Anführungszeichen, sonst expandiert bash die Muster schon hier gegen das
# Projektverzeichnis: `*.py` würde zu `s0ctl.py test_gpio.py` und damit
# ausgerechnet die Paketdateien verfehlen, um die es geht.
MUSTER=('*.py' '*.js' '*.html')
fern_muster() { printf "$ZIEL/%s " "${MUSTER[@]}"; }
lokal_muster() { printf "s0counter/%s " "${MUSTER[@]}"; }

# Dateien neben dem Paket, in /opt/s0counter statt /opt/s0counter/s0counter.
# Sie standen zunächst gar nicht im Abgleich: der Lauf meldete „keine
# Unterschiede", während backup.sh am Pi und im Repo auseinanderliefen.
# Einzeln aufgeführt, weil das Verzeichnis daneben auch venv/ und README
# enthält, die nicht übertragen werden sollen.
OBEN=(backup.sh s0ctl.py test_gpio.py)
OBEN_ZIEL=/opt/s0counter

# Ein Eintrag in GEAENDERT ist entweder ein Paketname (`db.py`) oder einer
# mit `../`-Präfix für die Ebene darüber (`../backup.sh`). Diese beiden
# Funktionen lösen das an EINER Stelle auf — sonst müsste jede der vier
# Verwendungsstellen dieselbe Fallunterscheidung tragen.
lokal_pfad() {
    case "$1" in ../*) echo "${1#../}" ;; *) echo "s0counter/$1" ;; esac
}
fern_pfad() {
    case "$1" in ../*) echo "$OBEN_ZIEL/${1#../}" ;; *) echo "$ZIEL/$1" ;; esac
}

while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run) DRY_RUN=1 ;;
        --host) shift; HOST="${1:?--host braucht einen Wert}" ;;
        *) echo "Unbekannte Option: $1" >&2
           echo "Aufruf: $0 [--dry-run] [--host NAME]" >&2
           exit 2 ;;
    esac
    shift
done

BASE="$(cd "$(dirname "$0")" && pwd)"
cd "$BASE"

sshq() { ssh -o BatchMode=yes -o ConnectTimeout=15 "$HOST" "$@"; }

echo "== Verbindung =="
if ! sshq true 2>/dev/null; then
    echo "Keine SSH-Verbindung zu '$HOST'." >&2
    echo "Bei Tailscale-SSH verlangt der Zugang periodisch eine Bestätigung im" >&2
    echo "Browser; der Link steht in der Ausgabe von: ssh -v $HOST true" >&2
    exit 1
fi
echo "$HOST erreichbar: $(sshq hostname)"

# --- Unterschiede ermitteln ------------------------------------------------
# Verglichen wird über Prüfsummen statt über Zeitstempel: eine frisch
# ausgecheckte Datei ist neuer als die laufende, ohne sich zu unterscheiden.
echo
echo "== Abgleich =="
LOKAL=$(eval md5sum $(lokal_muster) 2>/dev/null \
        | sed 's| s0counter/| |' | sort -k2)
FERN=$(sshq "md5sum $(fern_muster) 2>/dev/null \
        | sed 's| $ZIEL/| |' | sort -k2" || true)

GEAENDERT=()
while read -r summe datei; do
    [ -n "${datei:-}" ] || continue
    if ! grep -qx "$summe  *$datei" <<<"$FERN"; then
        GEAENDERT+=("$datei")
    fi
done <<<"$LOKAL"

# Dieselbe Prüfsummenlogik für die Dateien neben dem Paket. Sie werden mit
# dem Präfix `../` geführt, damit die Ziele unterscheidbar bleiben — ohne
# das landete backup.sh im Paketverzeichnis.
for datei in "${OBEN[@]}"; do
    [ -f "$datei" ] || continue
    soll=$(md5sum "$datei" | cut -d' ' -f1)
    ist=$(sshq "md5sum $OBEN_ZIEL/$datei 2>/dev/null | cut -d' ' -f1" || true)
    [ "$soll" = "$ist" ] || GEAENDERT+=("../$datei")
done

# Dateien, die nur noch auf dem Pi liegen, sind Reste einer früheren Fassung.
# Sie werden gemeldet, aber nicht gelöscht: was hier automatisch verschwindet,
# verschwindet irgendwann auch, wenn es gebraucht wird.
while read -r _ datei; do
    [ -n "${datei:-}" ] || continue
    if [ ! -f "s0counter/$datei" ]; then
        echo "  nur auf dem Pi (bleibt liegen): $datei"
    fi
done <<<"$FERN"

if [ ${#GEAENDERT[@]} -eq 0 ]; then
    echo "  Keine Unterschiede — der Pi hat diesen Stand bereits."
    exit 0
fi

printf '  zu übertragen: %s\n' "${GEAENDERT[@]}"

if [ "$DRY_RUN" -eq 1 ]; then
    echo
    echo "(--dry-run: nichts übertragen)"
    exit 0
fi

# --- Sicherung -------------------------------------------------------------
# Vor dem Austausch der alte Codestand und die Datenbank. Die Datenbank über
# `.backup` statt `cp`: bei aktivem WAL ist eine Dateikopie kein konsistenter
# Stand, und genau die will man haben, wenn man sie je braucht.
STAND=$(date +%Y%m%d-%H%M%S)
SICHER=/root/s0-rollback-$STAND
echo
echo "== Sicherung nach $SICHER =="
QUELLEN=()
for f in "${GEAENDERT[@]}"; do QUELLEN+=("$(fern_pfad "$f")"); done
sshq "set -e
    mkdir -p $SICHER
    for f in ${QUELLEN[*]}; do
        [ -f \$f ] && cp -a \$f $SICHER/ || true
    done
    sudo -u s0counter sqlite3 /var/lib/s0counter/s0counter.sqlite3 \
        '.backup /tmp/s0-vor-deploy-$STAND.sqlite3'
    mv /tmp/s0-vor-deploy-$STAND.sqlite3 $SICHER/
    chown root:root $SICHER/*
    ls -la $SICHER/ | tail -n +2"

# --- Übertragen ------------------------------------------------------------
echo
echo "== Übertragung =="
for f in "${GEAENDERT[@]}"; do
    sshq "cat > $(fern_pfad "$f")" < "$(lokal_pfad "$f")"
    echo "  $f"
done

# Der Bytecode-Zwischenspeicher wird verworfen: er trägt den Zeitstempel der
# alten Datei, und eine per `cat` geschriebene Datei kann denselben Stempel
# und dieselbe Größe haben wie die überschriebene.
#
# Die Dateien neben dem Paket brauchen ihre eigenen Rechte: backup.sh und
# s0ctl.py werden ausgeführt, 644 machte sie unbrauchbar.
sshq "set -e
    chown root:root $(fern_muster)
    chmod 644 $(fern_muster)
    rm -rf $ZIEL/__pycache__"
for f in "${GEAENDERT[@]}"; do
    case "$f" in ../*) sshq "chown root:root $(fern_pfad "$f");
                             chmod 755 $(fern_pfad "$f")" ;;
    esac
done

# --- Nachkontrolle ---------------------------------------------------------
# Erst prüfen, ob die Übertragung angekommen ist, dann erst neu starten. Ein
# Dienst, der mit halb übertragenem Code startet, ist schwerer zu reparieren
# als einer, der noch mit dem alten läuft.
echo
echo "== Prüfsummen nach der Übertragung =="
# Jede Datei einzeln abgefragt statt aus einer Sammelliste gesucht: die
# Liste musste sonst beide Zielverzeichnisse kennen, und ein grep auf den
# Dateinamen träfe `main.py` auch in `s0ctl.py`-Zeilen.
FEHLER=0
for f in "${GEAENDERT[@]}"; do
    SOLL=$(md5sum "$(lokal_pfad "$f")" | cut -d' ' -f1)
    IST=$(sshq "md5sum $(fern_pfad "$f") 2>/dev/null | cut -d' ' -f1" || true)
    if [ "$SOLL" = "$IST" ]; then
        echo "  ok  $f"
    else
        echo "  ABWEICHUNG  $f (soll $SOLL, ist ${IST:-fehlt})" >&2
        FEHLER=1
    fi
done
[ "$FEHLER" -eq 0 ] || { echo "Übertragung unvollständig — kein Neustart." >&2; exit 1; }

echo
echo "== Konfigurationsprüfung mit dem neuen Code =="
sshq "sudo -u s0counter env PYTHONPATH=/opt/s0counter \
    /opt/s0counter/venv/bin/python -m s0counter.main \
    -c /etc/s0counter/config.toml --check-config" | head -3

# Zählerstände vor dem Neustart festhalten. Ein Neustart, der Impulse
# verliert, verliert sie unbemerkt — die Summe stünde danach einfach etwas
# niedriger da, ohne Fehlermeldung.
VORHER=$(sshq "curl -s --max-time 10 http://127.0.0.1:8080/status" \
         | python3 -c "import json,sys
d=json.load(sys.stdin)
print(' '.join(f\"{k}={v['pulses']}\" for k,v in sorted(d['meters'].items())))" 2>/dev/null || echo "")
[ -n "$VORHER" ] && echo "  Stand vor dem Neustart: $VORHER"

echo
echo "== Neustart =="
sshq "systemctl restart s0counter"
sleep 6
ZUSTAND=$(sshq "systemctl is-active s0counter" || true)
echo "  Dienst: $ZUSTAND"
if [ "$ZUSTAND" != "active" ]; then
    echo >&2
    echo "Dienst läuft nicht. Letzte Meldungen:" >&2
    sshq "journalctl -u s0counter --since '-2min' --no-pager | tail -20" >&2
    echo >&2
    echo "Zurück auf den alten Stand:" >&2
    # Jede Datei mit ihrem eigenen Ziel: die Sicherung liegt flach in einem
    # Ordner, die Dateien gehören aber in zwei verschiedene Verzeichnisse.
    # Ein pauschales `cp * $ZIEL/` würde backup.sh ins Paket legen — und die
    # mitgesicherte Datenbank gleich dazu.
    RUECK=''
    for f in "${GEAENDERT[@]}"; do
        RUECK+="cp -a $SICHER/${f#../} $(fern_pfad "$f"); "
    done
    echo "  ssh $HOST '$RUECK" >&2
    echo "             rm -rf $ZIEL/__pycache__; systemctl restart s0counter'" >&2
    exit 1
fi

# --- Wirkung, nicht Rückgabewert -------------------------------------------
# `systemctl is-active` sagt nur, dass ein Prozess lebt. Gezählt wird erst,
# wenn wieder Impulse ankommen und die Zählerstände nicht zurückgefallen sind.
echo
echo "== Nachkontrolle =="
sshq "curl -s --max-time 10 http://127.0.0.1:8080/status" | python3 -c "
import json, sys
vorher = dict(t.split('=') for t in '''$VORHER'''.split() if '=' in t)
d = json.load(sys.stdin)
schlecht = 0
for k, m in sorted(d['meters'].items()):
    alt = int(vorher.get(k, -1))
    if alt < 0:
        print(f'  {k}: {m[\"pulses\"]} Impulse, {m[\"energy_kwh\"]:.3f} kWh (kein Vergleichswert)')
    elif m['pulses'] < alt:
        print(f'  {k}: RÜCKFALL {alt} -> {m[\"pulses\"]} Impulse'); schlecht = 1
    else:
        print(f'  {k}: {m[\"pulses\"]} Impulse (+{m[\"pulses\"]-alt}), {m[\"energy_kwh\"]:.3f} kWh')
b = d.get('balance', {})
if b.get('enabled'):
    print(f'  Hausbilanz: {b.get(\"collected\", 0)} Abrufe, {b.get(\"failed\", 0)} Fehler')
sys.exit(schlecht)
"

echo
echo "== Aggregat gegen die Rohimpulse =="
sshq "sudo -u s0counter sqlite3 /var/lib/s0counter/s0counter.sqlite3 '
    SELECT p.meter_id || \": roh \" || p.n || \", aggregiert \" || COALESCE(a.n, 0)
    FROM (SELECT meter_id, COUNT(*) n FROM pulses GROUP BY 1) p
    LEFT JOIN (SELECT meter_id, SUM(pulses) n FROM pulse_minutes GROUP BY 1) a
      USING (meter_id);'" | sed 's/^/  /'

echo
echo "Fertig. Sicherung: $HOST:$SICHER"
echo "Anzeige: http://192.168.0.20:8080/dashboard"
