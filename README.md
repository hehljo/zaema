# Zaema

[![Tests](https://github.com/hehljo/zaema/actions/workflows/tests.yml/badge.svg)](https://github.com/hehljo/zaema/actions/workflows/tests.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/)
[![Lizenz: MIT](https://img.shields.io/badge/Lizenz-MIT-green)](LICENSE)
[![Plattform: Raspberry Pi OS](https://img.shields.io/badge/Plattform-Raspberry%20Pi%20OS%20Trixie-c51a4a)](https://www.raspberrypi.com/software/)

**S0-Impulserfassung dreier Wohnungszähler am Raspberry Pi** — mit Hausbilanz,
Jahresbericht und Web-Dashboard.

Im Einsatz mit drei Eltako DSZ15D-3x80A (EG, OG, DG) an einem
4-Kanal-PC817-Modul; jeder Zähler mit S0-Ausgang nach DIN EN 62053-31 passt.

> **Zaema?** *Zähl ma* — augschburgerisch für „zählen wir". Genau das tut es:
> zählen, was drei Wohnungen verbrauchen, und dazusagen, woher der Strom kam.

Der Name gilt für das Projekt, nicht für die Anlage: Dienst, Pfade und
Home-Assistant-Entitäten heißen weiterhin `s0counter` bzw. `sensor.s0_*`. Das
sind Verträge mit systemd, dem Dateisystem und dem Recorder-Verlauf — ein
Umbenennen dort wäre eine Migration, keine Textänderung.

- drei unabhängige S0-Kanäle, 1.000 Impulse je kWh, also 1 Wh je Impuls
- jeder Impuls einzeln in SQLite, Zählerstand jederzeit aus Rohdaten belegbar
- Momentanleistung aus dem Impulsabstand
- **Hausbilanz** direkt von der Sonnenbatterie (Home Assistant nur als
  Ausfallquelle), dadurch Zuordnung des Netzbezugs auf die einzelnen Wohnungen
- **Jahresbericht als PDF**, automatisch am 31.12., Versand über Telegram
- Anbindung an Home Assistant über die REST-API, MQTT optional
- Statusabfrage unter `http://<pi>:8080/status`
- nächtliche Sicherung auf ein Netzlaufwerk
- Start über systemd

> **⚠️ Die Verdrahtung im Zählerschrank gehört in die Hände einer
> Elektrofachkraft.** Die Software selbst läuft ohne. Wer was darf, steht
> weiter unten unter „Wer das nachbauen darf“.

> Der geeichte Zählerstand des Stromzählers bleibt die Abrechnungsgrundlage.
> Fällt Pi, Modul oder Software aus, lassen sich die Impulse dieser Zeit nicht
> nachträglich rekonstruieren.

---

## ⚠️ Wer das nachbauen darf

**Diese Anlage wurde von einer Elektrofachkraft geplant, aufgebaut und in
Betrieb genommen.** Alles, was hier an Verdrahtung dokumentiert ist, ist die
Beschreibung einer ausgeführten Arbeit — **keine Anleitung zum Nachmachen für
Laien.**

Der S0-Ausgang selbst ist ein potentialfreier Kontakt im SELV-Bereich und
harmlos. **Der Weg dorthin führt durch den Zählerschrank**, und der ist es
nicht:

| Bereich | Wer |
|---|---|
| Hausanschlusskasten (HAK), Vorzählerbereich | **Nur der Netzbetreiber** oder ein von ihm dazu beauftragtes Unternehmen. Hier liegt ungezählte Energie, die Plomben sind nicht deine |
| Zählerplatz, Verteilung, Zuleitungen | Ein **im Installateurverzeichnis eines Netzbetreibers eingetragenes Installationsunternehmen** (§ 13 NAV). Die Inbetriebsetzung der Anlage hinter der Trennvorrichtung ebenso (§ 14 NAV) |
| Arbeiten am Zähler selbst | Elektrofachkraft nach DIN VDE 1000-10, mit den fünf Sicherheitsregeln |
| Raspberry Pi, Software, Auswertung | Jeder, der mag — das ist die SELV-Seite und Gegenstand der meisten dieser README |

**Am HAK hat niemand ohne Auftrag des Netzbetreibers etwas zu suchen.** Dort
gibt es keinen vorgelagerten Schutz: Ein Kurzschluss wird nicht von einer
Sicherung begrenzt, sondern vom Netz — mit Lichtbogenenergien, gegen die
normale Kleidung nicht schützt. NH-Sicherungen werden mit Aufsteckgriff,
Visier und Störlichtbogen-Schutzkleidung gezogen, nicht mit der Zange.

Und die häufigste Fehleinschätzung: **„Ich hab schon mal eine Steckdose
gewechselt" ist keine Qualifikation.** DIN VDE 1000-10 stellt im Anhang A
ausdrücklich fest, dass es keine für alle Gebiete der Elektrotechnik
ausgebildete Elektrofachkraft gibt — wer für ein Gebiet ausgebildet ist, gilt
für andere als Laie. Das gilt auch in die andere Richtung, und es gilt auch
für Leute, die Software schreiben können.

**Wer diese Anlage nachbauen will und keine Elektrofachkraft ist:** Die
S0-Zähler, die Verdrahtung im Zählerschrank und die Inbetriebsetzung gehören
beauftragt. Der Pi, das Optokoppler-Modul und alles ab Klemme `-X2` sind
danach ungefährlich und lassen sich selbst machen. Der Stromlaufplan unter
`docs/anlage/` ist dafür gedacht, **einer Fachkraft** die Arbeit vorzugeben —
nicht, sie zu ersetzen.

> Diese Übersicht ist eine Einordnung, keine Rechtsberatung. Maßgeblich sind
> die Technischen Anschlussbedingungen deines Netzbetreibers.

---

## Warum REST und nicht MQTT

In der Installation läuft kein MQTT-Broker — geprüft am 06.08.2026 auf
`192.168.0.30` (Home Assistant), `192.168.0.40` (Synology) und `192.168.0.50`
(Sonnenbatterie), Port 1883 überall zu. Die Sensoren werden deshalb über die
REST-API gesetzt.

**Grenze des Verfahrens:** über REST gesetzte Zustände hängen an keiner
Integration. Nach einem Neustart von Home Assistant sind sie weg, bis der Pi
das nächste Mal veröffentlicht — bei 30 Sekunden Intervall eine kurze Lücke.
Der Langzeitverlauf im Energie-Dashboard bleibt erhalten, der kommt aus der
Recorder-Datenbank.

Sobald ein Broker da ist (Mosquitto-Add-on in Home Assistant), ist MQTT der
bessere Weg: Discovery legt echte Entitäten an, die Neustarts überstehen. Der
Code dafür liegt fertig in `s0counter/mqtt.py`, es reicht das Umschalten in der
Konfiguration.

---

## 1. Hardware

- Raspberry Pi 3B
- microSD ab 16 GB, besser eine Endurance-Karte
- stabiles 5-V-Netzteil
- JZK PC817 4-Kanal-Modul, Eingangsbereich 3,6 bis 24 V
- drei Eltako DSZ15D-3x80A
- berührungssicheres Gehäuse

### Andere Zähler

Es läuft jeder Zähler mit S0-Ausgang nach EN 62053-31, auch gemischt. Drei
Werte je Zähler in `config.toml`, alle mit Vorgaben für den DSZ15D:

| Wert | Vorgabe | wann ändern |
|---|---|---|
| `pulses_per_kwh` | 1000 | **immer prüfen**, steht auf dem Zähler |
| `max_plausible_power_w` | 60000 | bei kleineren Zählern |
| `min_power_w` | 20 | selten |

**Die Impulsrate ist nicht genormt.** EN 62053-31 legt sie nicht fest, sie ist
Sache des Herstellers. Neben den verbreiteten 1000 sind 10, 100, 500 und 2000
im Feld. Ein falscher Wert verfälscht jede Kilowattstunde — und die Eichung
sagt darüber nichts: MID-geeicht ist die Messung im Zähler, nicht der
Impulsausgang. Geeichte Zähler gibt es in allen genannten Raten.

`max_plausible_power_w` ist die Leistung, oberhalb derer ein Impulsabstand als
Einstreuung gilt. 60000 entspricht den rund 55 kW eines DSZ15D-3x80A. Bei
einem einphasigen 32-A-Zähler gehören dort eher 8000 hin, sonst laufen
Störimpulse als echter Verbrauch mit.

`min_power_w` ist die Dauerlast, unterhalb derer keine Momentanleistung mehr
angezeigt wird; daraus wird der Timeout berechnet. Bei 1000 Imp/kWh und 20 W
sind das 180 s, bei 100 Imp/kWh automatisch 1800 s. Der Wert wird bewusst
nicht als Sekundenzahl eingetragen — eine feste Zahl wäre nur für eine
Impulsrate richtig, und bei einem groben Zähler stünde eine Wohnung mit
Grundlast dauerhaft auf 0 W.

Beim Start abgelehnt wird eine Kombination, in der `bounce_ms` die
Plausibilitätsgrenze erreicht: die Entprellung verwürfe dann alles, was die
Plausibilitätsprüfung sehen sollte, und diese liefe wirkungslos mit.

## 2. Basis-Image

Raspberry Pi OS Lite 64-bit (Trixie), ohne Desktop. Im Imager Hostname,
Benutzer, Netz, Zeitzone `Europe/Berlin` und SSH vorkonfigurieren.

```bash
sudo apt update && sudo apt full-upgrade -y && sudo reboot
```

Python 3.11 oder neuer wird gebraucht — die Konfiguration wird mit `tomllib`
aus der Standardbibliothek gelesen. Trixie bringt 3.13 mit.

---

## 3. Verdrahtung

> ⚠️ **Der S0-Anschluss sitzt im Zählerschrank.** Was hier steht, beschreibt
> eine von einer Elektrofachkraft ausgeführte Arbeit. Freischalten, Sichern,
> Spannungsfreiheit feststellen, Erden und Kurzschließen, benachbarte Teile
> abdecken — die fünf Regeln gelten, bevor der erste Draht angefasst wird.
> Wer keine Elektrofachkraft ist: beauftragen, siehe [Wer das nachbauen
> darf](#️-wer-das-nachbauen-darf). Ab Klemme `-X2` (Pi-Seite) ist es SELV
> und unkritisch.

### Modul vorher prüfen

Die Beschriftung billiger PC817-Platinen ist nicht einheitlich. Vor dem
Anschluss an Pi und Zähler:

1. Eingangs- und Ausgangsseite am Platinenaufdruck bestimmen
2. spannungslos den Widerstand zwischen Eingangs-GND und Pi-GND messen —
   für echte galvanische Trennung darf da keine niederohmige Verbindung sein
3. einen Kanal zunächst mit 5 V testen
4. am Ausgang sicherstellen, dass gegen Pi-GND nie mehr als 3,3 V anliegen
5. Eingangsstrom bei 5 V messen, er muss unter dem S0-Maximum von 20 mA bleiben

### S0-Seite je Kanal

Der Eltako-S0-Ausgang ist ein Schalter in einer 5-V-Stromschleife:

```text
separate +5 V  ----  Eltako S0+
Eltako S0-     ----  Modul INx+
Modul INx-     ----  separate 0 V
```

Hat das Modul je Kanal nur `IN` und einen gemeinsamen Eingangs-GND:

```text
separate +5 V -> Eltako S0+ -> Eltako S0- -> INx
separate 0 V  --------------------------------> Eingangs-GND
```

Die Eingangsseite mit einer eigenen, geregelten 5-V-SELV-Versorgung speisen.
Beide Seiten über gemeinsame GND zu versorgen hebt die Trennung auf.

### Pi-Seite

Erwartet wird ein Transistorausgang, der während des Impulses gegen GND zieht,
also aktiv LOW:

```text
Modul Ausgangs-GND  ->  Pi GND,   physischer Pin 6
Modul OUT1          ->  GPIO17,   physischer Pin 11   EG
Modul OUT2          ->  GPIO27,   physischer Pin 13   OG
Modul OUT3          ->  GPIO22,   physischer Pin 15   DG
                        Kanal 4 bleibt Reserve
```

Bei reinem Open-Collector-Ausgang genügt der interne Pull-up des GPIO. Braucht
die Platine einen Ausgangs-VCC, dann an Pi 3,3 V, physischer Pin 1.

**Nie 5 V, 12 V oder 24 V an einen GPIO.** Vor dem Anschluss OUT gegen Pi-GND
messen.

---

## 4. Installation

```bash
git clone https://github.com/hehljo/zaema.git
cd zaema
sudo ./install.sh
```

Der Installer prüft die Konfiguration, bevor er den Dienst startet, und
überschreibt eine vorhandene `/etc/s0counter/config.toml` nie — eine geänderte
Vorlage landet als `config.toml.neu` daneben.

```bash
sudo systemctl status s0counter
sudo journalctl -u s0counter -f
curl http://127.0.0.1:8080/status
```

---

## 5. Signaltest vor dem Dauerbetrieb

```bash
sudo systemctl stop s0counter
sudo -u s0counter /opt/s0counter/venv/bin/python /opt/s0counter/test_gpio.py
```

Jeden Kanal einzeln aktivieren. **Pro Aktivierung darf genau ein Ereignis
erscheinen.** Mehrere bedeuten Prellen — dann `bounce_ms` erhöhen.

Das Skript zeigt beim Start den Ruhezustand jedes Kanals. Steht einer dauerhaft
auf `aktiv`, ist `active_low` falsch herum oder der Eingang hängt fest.

```bash
sudo systemctl start s0counter
```

---

## 6. Zählerstände übernehmen

Direkt nach erfolgreichem Signaltest die abgelesenen Stände eintragen:

```bash
sudo s0ctl set-reading eg 1234.567 --force
sudo s0ctl set-reading og 987.654 --force
sudo s0ctl set-reading dg 456.789 --force
sudo s0ctl status
```

`--force` ist bei der Ersteinrichtung nötig, weil die Abweichung groß ist. Das
ist Absicht: später schützt die Abfrage vor einem Zahlendreher.

`set-reading` setzt nur einen Offset. Die Rohimpulse bleiben unverändert, jede
Korrektur steht im Audit-Log:

```bash
sudo s0ctl audit
```

---

## 7. Home Assistant

Token anlegen: Home Assistant öffnen, unten links auf den eigenen Namen,
Reiter **Sicherheit**, ganz unten **Langlebige Zugriffstoken**.

```bash
sudo nano /etc/s0counter/config.toml
```

```toml
[homeassistant]
enabled = true
url = "http://192.168.0.30:8123"
token = "HIER_DAS_TOKEN"
prefix = "s0"
```

```bash
sudo systemctl restart s0counter
sudo s0ctl ha-test
```

Daraus werden je Zähler zwei Sensoren:

```text
sensor.s0_eg_energy    kWh, device_class energy, state_class total_increasing
sensor.s0_eg_power     W,   device_class power,  state_class measurement
```

Die Energiesensoren lassen sich im Energie-Dashboard unter *Individuelle
Geräte* eintragen. Entfernen mit `sudo s0ctl ha-remove`.

Die Datei enthält danach ein gültiges Token — sie gehört `root:s0counter` mit
Modus 640 und darf nicht in ein Repository.

### Was Home Assistant auch kennt

> **Nicht die Quelle dieses Projekts.** Die Hausbilanz holt sich der Dienst
> direkt am Gerät ab, nicht über Home Assistant — siehe Abschnitt 8. Die
> folgenden Entitäten stehen hier, weil sie in Home Assistant ohnehin
> existieren und beim Gegenrechnen helfen.

Die Sonnenbatterie ist dort über die Integration `sonnenbatterie`
(192.168.0.50) angebunden und zeigt die Hausseite:

```text
sensor.sonnenbatterie_<seriennummer>_state_production          PV-Erzeugung in W
sensor.sonnenbatterie_<seriennummer>_state_consumption_current Hausverbrauch in W
sensor.sonnenbatterie_<seriennummer>_state_grid_inout          Netz, Vorzeichen zeigt Richtung
sensor.sonnenbatterie_<seriennummer>_state_battery_inout       Akku, Vorzeichen zeigt Richtung
sensor.sonnebatterie_produktion                        Erzeugung gesamt in kWh
sensor.sonnebatterie_hausverbrauch                     Verbrauch gesamt in kWh
```

Eine SolarEdge-Integration gibt es nicht — die PV-Daten kommen vollständig über
die Sonnenbatterie.

Der S0-Zähler ergänzt das um die Aufteilung, die dort fehlt: **wie sich der
Hausverbrauch auf EG, OG und DG verteilt.** Die Summe der drei Wohnungszähler
sollte in der Größenordnung dem Hausverbrauch der Sonnenbatterie entsprechen;
eine Differenz sind Verbraucher hinter keinem der drei Zähler.

Woher der Strom kommt — Netz, PV oder Akku — lässt sich aus den S0-Impulsen
allein nicht bestimmen. Dafür braucht es die Sonnenbatterie-Sensoren dazu.

---

## 8. Hausbilanz und Netzbezug je Wohnung

Die S0-Zähler messen, **wie viel** jede Wohnung verbraucht. Für die Frage,
**woher** der Strom kam, braucht es den Netzbezug des Hauses im selben
Zeitraum. Den liefert die Sonnenbatterie.

### Woher die Bilanzdaten kommen

**Direkt vom Gerät, nicht über Home Assistant.** Der Dienst ruft
`http://192.168.0.50/api/v2/status` auf — im Heimnetz ohne Anmeldung, ohne
Token, ohne dass Home Assistant laufen muss.

Home Assistant ist die **Ausfallquelle**. `sources.build()` stellt beide in
Vorzugsreihenfolge auf, ein `RedundantSource` schaltet um, sobald die erste
nicht antwortet:

| Reihenfolge | Quelle | Weg |
|---|---|---|
| 1 | `SonnenbatterieSource` | direkt am Gerät, `/api/v2/status` |
| 2 | `HomeAssistantSource` | über die REST-API, braucht Token |

Der Umweg über Home Assistant hätte sonst zwei Abhängigkeiten hintereinander:
Die Sonnenbatterie müsste antworten **und** Home Assistant müsste laufen.
Fällt die VM auf der DiskStation aus, stünde die Bilanz still, obwohl das
Gerät im selben Netz erreichbar ist.

Beide Quellen liefern denselben `Reading`-Satz mit vereinheitlichten
Vorzeichen, sind also gegeneinander austauschbar. Ein Lesekopf am geeichten
Netzzähler wäre die sauberste dritte Quelle — dafür genügt eine weitere Klasse
mit derselben `read()`-Methode.

Welche gerade zieht, zeigt `sudo s0ctl balance` unter `active`.

```toml
[balance]
enabled = true

# Erste Wahl: direkt am Gerät.
[sonnenbatterie]
enabled = true
host = "192.168.0.50"

# Zweite Wahl. Greift nur, wenn die Sonnenbatterie nicht antwortet,
# und braucht ein gesetztes Token.
[homeassistant]
enabled = true
```

```bash
sudo systemctl restart s0counter
sudo s0ctl balance      # aktueller Stand
sudo s0ctl allocate     # Verteilung im laufenden Jahr
```

### Wie verteilt wird

In jedem Zeitfenster von 15 Minuten bekommt eine Wohnung denselben Anteil am
Netzbezug, den sie am Hausverbrauch hatte.

Beispiel: Haus 1.900 W, PV 1.000 W, also 900 W aus dem Netz. EG verbraucht
1.400 W, OG 300 W, DG 200 W:

| | Verbrauch | Anteil | Netzbezug |
|---|---|---|---|
| EG | 1.400 W | 73,7 % | 663 W |
| OG | 300 W | 15,8 % | 142 W |
| DG | 200 W | 10,5 % | 95 W |

Das ist ein **Verteilschlüssel, keine Messung**. Physikalisch lässt sich
nicht feststellen, welche Wattstunde aus dem Netz in welche Wohnung floss.
Die Eigenschaft, auf die es ankommt: niemand bekommt mehr Netzbezug
zugerechnet, als er überhaupt verbraucht hat.

Die Rechnung läuft fensterweise und nicht über Jahressummen — wer nachts
verbraucht, bezieht fast alles aus dem Netz; wer mittags verbraucht, deckt
viel aus der Photovoltaik. Eine Jahressumme würde beide gleich behandeln.

**Netzbezug in Fenstern ohne jeden Wohnungsverbrauch** wird nicht verteilt,
sondern getrennt ausgewiesen. Das sind Verbraucher hinter keinem der drei
Zähler: Allgemeinstrom, Wärmepumpe, Ladepunkt.

---

## 9. Jahresbericht

```toml
[report]
enabled = true
price_ct_per_kwh = 32.5          # 0 lässt die Kostenrechnung weg
base_price_eur_per_year = 120.0  # wird gleichmäßig aufgeteilt
landlord = "Musterstraße 1"
```

```bash
sudo s0ctl report --year 2026            # PDF erzeugen
sudo s0ctl report --year 2026 --send     # und über Telegram schicken
```

Drei Seiten: Übersicht mit Kosten, Erklärung des Verfahrens samt
Belastbarkeit der Daten, Zählerstände zum Abgleich.

Automatisch am 31.12. um 23:50:

```bash
sudo systemctl enable --now s0counter-report.timer
systemctl list-timers s0counter-report.timer
```

Der Timer holt einen verpassten Termin nach, falls der Pi zu der Zeit aus war.

---

## 10. Telegram

Bot anlegen: in Telegram **@BotFather** anschreiben, `/newbot`, Namen wählen,
Token merken. Danach den eigenen Bot **einmal anschreiben** — vorher darf er
nicht zurückschreiben.

Chat-ID: **@userinfobot** anschreiben, oder

```bash
curl "https://api.telegram.org/bot<TOKEN>/getUpdates"
```

```toml
[telegram]
enabled = true
token = "123456:ABC-DEF..."
chat_id = "987654321"
```

```bash
sudo s0ctl telegram-test
```

Schickt den aktuellen Stand aller Wohnungen und die Hausbilanz.

---

## 11. Bedienung

**Der erste Befehl, wenn etwas unklar ist** — oder wenn die Anlage nach
Monaten wieder angefasst wird:

```bash
sudo s0ctl doctor
```

Er prüft Dienst, Erfassung, Bilanzquelle, Home Assistant, Sicherung,
Speicherplatz und die Stimmigkeit der Datenbank, und nennt zu jedem offenen
Punkt den Befehl, der ihn behebt. Drei Ausgänge: `✓` in Ordnung, `✗` offen,
`?` hier nicht messbar. Der Rückgabewert ist nur bei `✗` ungleich null —
„nicht messbar" ist kein Fehler, sondern die ehrliche Antwort auf eine
Prüfung, die auf diesem Rechner nicht durchführbar war.

Der Doctor **ändert nichts**. Für das Einbinden der Synology gibt er die
vollständige Anleitung aus:

```bash
sudo s0ctl doctor --nas-hilfe
```

Alles Weitere:

```bash
sudo s0ctl status                        # Zählerstände
sudo s0ctl balance                       # Hausbilanz jetzt
sudo s0ctl allocate --year 2026          # Verteilung des Netzbezugs
sudo s0ctl report --year 2026 --send     # Jahresbericht
sudo s0ctl set-reading eg 1234.567
sudo s0ctl export /tmp/eg.csv --meter eg --days 30
sudo s0ctl backup /tmp/s0counter.sqlite3
sudo s0ctl audit
sudo s0ctl ha-test
sudo s0ctl telegram-test
```

Was `s0ctl doctor` prüft — und warum jeweils:

| Prüfung | Findet |
|---|---|
| Dienst läuft, startet nach Neustart | ein `enable` vergessen; nach einem Stromausfall bliebe die Anlage still |
| Impulse je Zähler | einen Zähler, der seit über 6 h schweigt — sonst fällt das erst der Abrechnung auf |
| Bilanzquelle erreichbar | eine geänderte IP der Sonnenbatterie; ohne sie entfällt die Aufteilung in PV und Netz |
| Home Assistant | leeres Token, nicht erreichbare Instanz |
| Sicherung: Timer, Ziel, **Alter** | den Fall, der diese Anlage einen Tag ungesichert ließ: Timer aktiv, Lauf scheitert nachts lautlos |
| Speicherplatz | die volle Karte, mit der verbleibenden Zeit statt nur einem Prozentwert |
| Aggregat gegen Rohimpulse | Verschiebungen, die der Start-Abgleich nicht sieht (dort nur Summen) |

HTTP-Schnittstelle:

```text
GET /status      Zählerstände, Hausbilanz, Panel, Zustand aller Anbindungen
GET /health      einfacher Lebendcheck
GET /audit       Korrekturen der Zählerstände
GET /dashboard   Live-Anzeige im Browser
GET /history     Verlauf einer Größe (?key=…&seconds=…&points=…)
GET /allocation  Abrechnung: Verbrauch je Wohnung, aufgeteilt in PV und Netz
```

### Abrechnung unterm Jahr

`/allocation` beantwortet die Frage, für die die Anlage gebaut wurde: was hat
jede Wohnung im gewählten Zeitraum verbraucht, und wie viel davon kam aus der
PV statt aus dem Netz. Bis dahin gab es diese Rechnung nur im Jahresbericht.

```bash
curl 'http://127.0.0.1:8080/allocation?range=7d'
curl 'http://127.0.0.1:8080/allocation?range=30d&model=waterfall'
```

| Parameter | Werte | Vorgabe |
|---|---|---|
| `range` | rollierend: `today`, `24h`, `7d`, `30d`, `365d` · Kalender: `2026`, `2026-08` | `24h` |
| `model` | `proportional`, `waterfall` | `proportional` |

**Rollierend oder Kalender.** `365d` sind die letzten 365 Tage ab jetzt —
richtig für „wie lief es zuletzt", falsch für eine Nebenkostenabrechnung.
Dafür gibt es echte Kalenderzeiträume:

```bash
curl 'http://127.0.0.1:8080/allocation?range=2026'      # Kalenderjahr
curl 'http://127.0.0.1:8080/allocation?range=2026-08'   # August 2026
```

Gerechnet wird in Ortszeit, Schaltjahre und Zeitumstellungen inbegriffen. Das
laufende Jahr endet beim aktuellen Zeitpunkt, nicht am 31. Dezember — sonst
stünde es im August mit 60 % Abdeckung da, obwohl nichts fehlt.

Angeboten werden nur Zeiträume, für die Daten vorliegen; die Liste steht als
`ranges` in jeder Antwort. Das Dashboard baut seine Knöpfe daraus und führt
keine eigene Liste.

Jede Antwort trägt unter `quality.verdict` eine Bewertung mit drei Ausgängen
— `belastbar`, `eingeschraenkt`, `unbrauchbar` — samt Begründung. Der dritte
Ausgang ist wichtig: eine Rechnung über zwei Stunden Daten liefert richtige
Zahlen, sagt aber nichts aus, und ohne diese Kennzeichnung sähe sie genauso
aus wie eine über ein Jahr.

### Ausfälle werden erkannt, auch die unbemerkten

Zwei Wege, weil sie verschiedene Fälle abdecken:

- `quality.gaps` — **vermerkte** Ausfälle. Die Quelle war nicht erreichbar,
  der Dienst lief weiter und hat es aufgeschrieben.
- `quality.detected_gaps` — **erkannte** Ausfälle. Aus den Zeitstempeln
  rekonstruiert, für den Fall, dass der Rechner selbst weg war:
  Stromausfall, Absturz, Update. Dann konnte niemand etwas vermerken, und
  die Impulse dieser Zeit fehlen ebenfalls.

Der zweite Weg ist der wichtigere. Ein Loch im Datenbestand ist nachträglich
nicht mehr zu füllen — die Impulse sind schlicht nicht gezählt worden. Die
Bewertung nennt Zeitpunkt und Dauer, damit man weiß, worauf sich die Zahlen
nicht stützen:

```text
2 Aufzeichnungslücken über zusammen 4 d 5 h — längste ab 18.08.2026 09:30
(4 d). In dieser Zeit lief die Erfassung nicht; die Impulse fehlen ebenfalls.
```

Als Lücke gilt erst das Dreifache des üblichen Messabstands. Ein einzelner
verschluckter Messpunkt ist kein Ausfall — ein Gate, das dauernd meldet,
wird ignoriert.

**Der nicht zugeordnete Netzbezug wird ausgewiesen, nicht verteilt.**
`house.unallocated_grid_kwh` ist Strom, den keine Wohnung zugerechnet bekommt,
aufgeschlüsselt nach zwei Ursachen:

- `unallocated_idle_kwh` — in diesen Intervallen lieferte keine Wohnung einen
  Impuls;
- `unallocated_capped_kwh` — das Haus bezog mehr, als die Wohnungen zusammen
  verbraucht haben (Anlagentechnik, Speicherverluste, Verbraucher hinter
  keinem Zähler).

Beides auf die Wohnungen umzulegen wäre eine Erfindung: welche Wohnung ihn
verursacht hat, ist nicht messbar. Wer ihn dennoch verteilen will, trifft
damit eine Fairness-Entscheidung — sie gehört als drittes Modell neben
`proportional` und `waterfall`, nicht in die Messung.

### Anzeige prüfen

Layout und Bedienung des Dashboards lassen sich nicht mit pytest messen —
dafür gibt es ein eigenes Werkzeug. Es fährt sechs Fenstergrößen in beiden
Themen ab und prüft Überlauf, Trefferflächen, Wortumbrüche und ob die Knöpfe
tatsächlich etwas bewirken:

```bash
python3 tools/dashboard_pruefen.py --url http://127.0.0.1:8080/dashboard
```

Der Netzanteil in der Wohnungskachel hat ein eigenes Werkzeug: dass die Zahl
*richtig gerechnet* wird, prüfen die JavaScript-Tests — dass sie in der
Kachel auch *ankommt*, sichtbar und farblich abgesetzt, entscheidet sich erst
im Browser:

```bash
python3 tools/klammer_pruefen.py --url http://127.0.0.1:8080/dashboard
```

Rückgabewert 2 heißt **nicht gemessen** (keine Klammer gefunden — läuft der
Dienst mit Hausbilanz?), nicht „bestanden".

Beide brauchen Playwright (`pip install playwright && playwright install
chromium`) — der Dienst auf dem Pi braucht es nicht.

---

## 12. Platzbedarf

Gemessen mit der Datendichte der Anlage — aus dem Live-Stand hochgerechnet:
rund 19.000 Impulse am Tag, 6,94 Mio. im Jahr.

| Was | pro Jahr |
|---|---|
| `pulses` (Rohimpulse) | 208 MB |
| Indizes darauf | 368 MB |
| `pulse_minutes` (Aggregat) | 25 MB |
| Bilanzreihe (eigene Datei) | 32 MB |
| **zusammen** | **rund 630 MB** |

Auf zehn Jahre gerechnet etwa 6,3 GB. Die Karte im Pi hat 14,7 GB, davon
waren bei der Messung 8,3 GB frei — es reicht, aber nicht beliebig lange.
Das Dashboard warnt ab 85 % Belegung.

**Auffällig ist das Verhältnis:** die beiden Indizes auf `pulses` brauchen
mehr Platz als die Daten selbst. `idx_pulses_meter_id` (209 MB im Jahr) wird
dabei nur von `_repair_counters()` beim Start benutzt. Ihn zu streichen
spräche für sich, ist aber ein Eingriff in bestehende Struktur und deshalb
hier nur vermerkt, nicht ausgeführt.

Das Minutenaggregat kostet mit 25 MB im Jahr rund 4 % Aufschlag und spart im
Gegenzug den Faktor 45 bei der Abrechnung — siehe F12 in `docs/BEFUNDE.md`.

Wenn der Platz knapp wird, ist der richtige Hebel die **Ausdünnung alter
Rohimpulse**: das Aggregat trägt die Abrechnung, die Einzelimpulse braucht
man nur für die Nachvollziehbarkeit. Eingebaut ist das nicht — es wäre der
erste Schritt, sobald die Karte an ihre Grenze kommt.

---

## 13. Sicherung auf ein Netzlaufwerk

Zwei Wege, beide führen zum selben Ergebnis.

**Geführt** — fragt Adresse, Benutzer, Passwort und Freigabe ab, legt die
Zugangsdaten und den fstab-Eintrag an, probiert die SMB-Fassungen durch,
macht eine echte Schreibprobe und lässt auf Wunsch gleich eine Sicherung
laufen:

```bash
sudo s0ctl nas-setup
```

Vor jedem Schreibschritt wird gefragt. Scheitert das Einbinden, wird der
fstab-Eintrag wieder entfernt — ein halb eingerichteter Eintrag zeigte beim
nächsten Neustart ins Leere. `s0ctl doctor` bietet den Assistenten am Ende
von selbst an, wenn die Sicherung nur lokal liegt.

**Von Hand** — die vollständige Anleitung steht im Werkzeug selbst:

```bash
sudo s0ctl doctor --nas-hilfe
```

Kurzfassung: `/etc/s0counter/nas.cred` (Modus 600) mit `username=` und
`password=`, dazu ein `cifs`-Eintrag in `/etc/fstab` mit `_netdev,nofail` —
ohne `nofail` bleibt der Pi beim Booten hängen, wenn die NAS gerade aus ist.
Danach `mount -a`, mit **`mountpoint`** gegenprüfen (`mount -a` meldet auch
dann Erfolg, wenn nichts eingebunden wurde), einen Lauf erzwingen und
`s0ctl doctor` zur Kontrolle.

Der Assistent braucht `cifs-utils` und für die Freigabenliste `smbclient`;
fehlt eines der Pakete, bietet er die Installation an.

**Der Freigabename ist kein Pfad.** In DSM steht der Ordner als
`/volume2/Backup/Raspberry` — die Freigabe heißt aber nur `Backup`, der Rest
gehört hinter den Einhängepunkt. Wird trotzdem ein Pfad eingegeben, fragt
der Assistent nach, statt einen fstab-Eintrag zu schreiben, der sich nicht
einbinden lässt. Ohne diese Rückfrage scheiterte das Einbinden am 27.08.2026
mit „Die NAS spricht diese SMB-Fassung nicht" — einer Meldung, die auf die
falsche Ursache zeigt.

### Was gesichert wird

| Datei | Warum |
|---|---|
| `impulse-*.sqlite3` | die Rohimpulse — die Abrechnungsgrundlage |
| `bilanz-*.sqlite3` | die Hausbilanz, ohne sie keine Aufteilung in PV und Netz |
| `verlauf-*.sqlite3` | die Rechnerwerte hinter den Diagrammen |
| `config-*.toml` | **Offsets und Token** — das am schwersten Wiederbeschaffbare |
| `systemd/*` | die Units, sonst beginnt eine Neuinstallation mit Raten |
| `impulse-*.csv` | Rohimpulse der letzten 40 Tage, auch ohne SQLite lesbar |
| `berichte/*.pdf` | Jahresberichte, werden nicht rotiert |

Die Datenbanken laufen über die SQLite-Backup-API, transaktionssicher auch
bei laufendem Dienst; eine Dateikopie wäre bei aktivem WAL unbrauchbar.

Geschrieben wird dabei **immer erst lokal**, danach wird die fertige Datei
ans Ziel kopiert. Der Umweg sieht überflüssig aus und ist es nicht: die
Backup-API braucht Dateisperren, die über SMB nicht funktionieren. Am
27.08.2026 stand deshalb eine 0-Byte-Datei auf der Synology und der Dienst
hing 42 Minuten darin fest (siehe `docs/BEFUNDE.md`, F25). Mit dem Umweg
dauert ein vollständiger Lauf rund fünf Sekunden.

Findet `s0ctl doctor` eine Sicherungsdatei unter 100 Byte, meldet er das als
eigenen Befund — eine Datei, die es gibt, ist noch keine Sicherung.

Die Konfigurationskopie trägt Modus 600, weil sie dasselbe Geheimnis enthält
wie das Original. Auf einem Netzlaufwerk hängen die Rechte allerdings an
dessen Einbindung — **den Freigabeordner deshalb nicht öffentlich freigeben.**

### Rotation

Zwei Grenzen, beide je Dateiart getrennt gezählt:

| | NAS | lokales Ausweichziel |
|---|---|---|
| nach Alter (`S0_BACKUP_KEEP_DAYS`) | 90 Tage | 14 Tage |
| nach Anzahl (`S0_BACKUP_KEEP_COUNT`) | 14 Stände | **3 Stände** |

Die Altersgrenze allein deckelt den Platz nicht: Mehrere Läufe am selben Tag
fallen alle in dieselbe Frist. Die Anzahlgrenze tut es — lokal enger, weil
die Sicherung dort auf derselben Karte liegt wie die Daten und eine
vollgeschriebene Karte die Erfassung anhält.

Getrennt je Art, damit nicht vier Impulssicherungen die zugehörigen
Konfigurationskopien verdrängen — man hätte sonst Datenbanken ohne die
Offsets, die sie erst lesbar machen. Berichte und Units werden nicht rotiert.

Der Stand ist im Dashboard sichtbar (Abschnitt *Rechner*, Kachel *Letzte
Sicherung*): Alter, Anzahl, belegter Platz und **wo** sie liegt. Liegt sie
auf der Speicherkarte oder ist sie älter als zwei Tage, warnt die Anzeige.

### Wenn die NAS fehlt

Das Skript sicherte früher gar nicht, wenn das Ziel nicht eingebunden war —
richtig gedacht (eine Sicherung im nicht eingebundenen Pfad wird beim
Einbinden verdeckt und ist unsichtbar weg), im Ergebnis aber **null
Sicherungen statt schlechter**. Heute weicht es auf `/var/backups/s0counter`
aus und warnt dreifach, zuletzt in der Schlusszeile.

Das schützt gegen eine kaputte Datenbank, einen Fehlgriff bei `set-reading`
und ein misslungenes Update — **nicht** gegen den Ausfall der Speicherkarte,
denn beides liegt darauf. Der Doctor meldet den Zustand als offenen Punkt,
bis die NAS eingebunden ist.

Anpassen über Umgebungsvariablen:

```bash
S0_BACKUP_TARGET=/mnt/nas/strom S0_BACKUP_KEEP_DAYS=180 /opt/s0counter/backup.sh
```

---

## 14. Plausibilitätsprüfung

1. Alle Zähler- und Softwarestände notieren
2. eine bekannte Last laufen lassen
3. nach mindestens 1 kWh erneut vergleichen
4. Softwaredifferenz und Zählerstandsdifferenz müssen im Rahmen der Ableseauflösung
   übereinstimmen
5. monatlich die Zählerstände fotografieren und gegenprüfen

---

## 15. Grenzen

- Gezählt wird nur, solange Pi, Modul und Dienst laufen.
- Ein Impuls entspricht bei 1.000 Imp/kWh genau 1 Wh.
- Die Leistung kommt aus dem Abstand der letzten beiden Impulse. Bei geringer
  Last aktualisiert sie sich entsprechend träge und fällt nach
  `power_timeout_seconds` auf 0. Bei 1.000 Imp/kWh entsprechen die
  voreingestellten 180 s einer Dauerlast von 20 W.
- Impulse, deren Abstand einer Leistung über 60 kW entspräche, werden verworfen
  und im Status unter `implausible` gezählt. Taucht dort etwas auf, stimmt an
  der Verkabelung oder Abschirmung etwas nicht.
- Gemessen wird die Energie hinter jedem Wohnungszähler, nicht ihre Herkunft.

---

### HTTP-Schnittstelle: nur lesend, ohne Anmeldung

Dashboard, `/status`, `/history` und `/audit` antworten **ohne Anmeldung**
und hören standardmäßig auf allen Schnittstellen (`http_host = "0.0.0.0"`).
Jeder im selben Netz kann damit den Verbrauch der Wohnungen ablesen;
schreiben kann über HTTP niemand, es gibt nur `GET`. Betreib den Dienst
deshalb nur im eigenen Heimnetz und leite Port 8080 **nie** ins Internet
weiter. Für Zugriff von unterwegs ein VPN nehmen. Wer das Dashboard nur auf
dem Pi selbst braucht, setzt `http_host = "127.0.0.1"`.

## Anzeigepanel (optional)

`s0counter/panel.py` kann Zählerstände und Hausbilanz zusätzlich auf einem
seriellen EA-eDIP320-Display zeigen, mit einem Taster zum Blättern. Das
Panel ist **standardmäßig aus** (`[panel] enabled = false` in `config.toml`)
und für den Betrieb nicht nötig; Dashboard und `/status` zeigen dasselbe.

Zum Einschalten braucht es das Python-Paket `edip320` für das
Display-Protokoll. Es ist **nicht Teil dieses Repos** und derzeit nicht
veröffentlicht. Fehlt es, meldet der Dienst das im Log und läuft ohne Panel
weiter. Verdrahtung und Spannungsversorgung des Displays stehen bewusst nicht
im Stromlaufplan (siehe `docs/anlage/STROMLAUFPLAN.md`, Abschnitt 5).

## Aufbau

```
s0counter/config.py          Konfiguration lesen und prüfen
s0counter/db.py              SQLite, Zählerstände, Offsets, Audit
s0counter/counter.py         Impulsannahme, Entprellung, Plausibilität
s0counter/homeassistant.py   REST-Anbindung
s0counter/mqtt.py            MQTT mit Discovery, optional
s0counter/main.py            Dienst, HTTP-Schnittstelle
s0counter/nas_setup.py       SMB-Freigabe einbinden (für s0ctl nas-setup)
s0counter/dashboard*.js      Anzeige; -verteilung.js ist die reine
                             Rechenlogik ohne Browser-Abhängigkeiten
s0ctl.py                     Verwaltung
test_gpio.py                 Signaltest der Eingänge
tests/                       Python- und JavaScript-Tests, ohne Hardware
tools/                       Prüfwerkzeuge (Browser, JS-Runner)
```

Tests:

```bash
python3 -m pytest tests/ -q          # Dienst und Werkzeuge
python3 tools/js_tests.py            # Rechenlogik der Anzeige
```

`js_tests.py` braucht `node` und hat drei Ausgänge: 0 bestanden, 1
durchgefallen, **2 nicht gelaufen** (keine Testdatei, kein node, null Tests
oder Zeitlimit gerissen). Ein Runner, der nichts findet, darf keinen Erfolg
melden.

### Was der Tests-Badge oben NICHT abdeckt

Die CI läuft auf einem nackten Ubuntu ohne das Display-Paket `edip320`
(siehe „Anzeigepanel (optional)“). `tests/test_panel.py` (28 Tests für das Anzeigepanel
am eDIP320) überspringt sich dort über `pytest.importorskip` — lautlos und
korrekt, aber eben ungemessen:

| | Umfang |
|---|---|
| CI, grüner Badge | 415 Tests |
| lokal, mit dem Paket `edip320` | 442 Tests |

**Das Anzeigepanel ist also nur lokal geprüft.** Wer daran etwas ändert,
braucht das Paket `edip320` im Pfad; ein grüner Badge sagt darüber nichts. Der CI-Lauf gibt diese Einschränkung selbst als Notiz
aus, damit sie nicht nur hier steht.

Konfiguration prüfen, ohne den Dienst zu starten:

```bash
python3 -m s0counter.main -c /etc/s0counter/config.toml --check-config
```

---

## Fehlersuche

**Keine Impulse.** `test_gpio.py` laufen lassen. Zeigt es auch nichts, liegt es
an der Verdrahtung oder daran, dass gerade kein Verbrauch anliegt — bei 20 W
Grundlast kommt nur alle drei Minuten ein Impuls.

**Zählt doppelt.** `bounce_ms` erhöhen, in Schritten auf 20 oder 50.

**Zählt bei Ruhe statt bei Last.** `active_low` umstellen.

**Home Assistant zeigt nichts.** `sudo s0ctl ha-test` gibt die genaue Ursache
aus. Bei `Token abgelehnt` ein neues anlegen.

**Dienst startet nicht.** `sudo journalctl -u s0counter -n 50`. Der Dienst
prüft die Konfiguration vor dem Start und verweigert ihn bei einem Fehler,
statt mit falschen GPIO-Nummern zu laufen.

**`OSError: [Errno 22] Invalid argument` aus `gpiozero/pins/native.py`.** Die
Meldung führt in die Irre: sie steht für „gpiozero hat die falsche
Pin-Factory genommen". `native` arbeitet über `/sys/class/gpio`, das es ab
Raspberry Pi OS Trixie nicht mehr gibt. Dorthin fällt gpiozero zurück, wenn
`lgpio` sich nicht aufbauen lässt — und den Grund dafür verschluckt es.

Meistens ist der Grund ein Arbeitsverzeichnis ohne Schreibrecht: `lgpio` legt
seine Notify-Datei `.lgd-nfy*` im aktuellen Verzeichnis an. Prüfen:

```bash
# Welche Factory kommt zum Zug? Der Verzeichniswechsel gehört dazu — der
# Test soll dieselbe Lage vorfinden wie der Dienst.
cd /var/lib/s0counter
sudo -u s0counter env PYTHONPATH=/opt/s0counter \
    /opt/s0counter/venv/bin/python -c \
    'from gpiozero import Device, DigitalInputDevice
DigitalInputDevice(17).close()
print(type(Device.pin_factory).__name__)'
```

Dafür muss der Dienst kurz stehen (`sudo systemctl stop s0counter`), sonst ist
GPIO17 belegt.

Erwartet wird `LGPIOFactory`. Kommt `NativeFactory`, ist der Dienst aus einem
Verzeichnis gestartet worden, das `s0counter` nicht gehört —
`WorkingDirectory` in der Unit prüfen, es muss `/var/lib/s0counter` sein.
Ausführlich in `docs/BEFUNDE.md`, Befund F7.
