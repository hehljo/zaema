# Befunde zum Zählerprojekt

Zwei Prüfungen, getrennt gehalten:

- **F1–F6** — Prüfung des Uploads `eltako-s0-raspi.zip` (Stand 04.08.2026),
  am Schreibtisch. Der Upload war eine eigene, KI-gestützte Erstfassung des
  Autors; „Upload“ meint hier nur diesen frühen Stand.
- **F7–F10** — Test auf echter Hardware (09.08.2026, Raspberry Pi OS Trixie).
  Diese Fehler sind auf einem Entwicklungsrechner nicht auffindbar.

Die Grundstruktur des Uploads war tragfähig — SQLite als Ablage, Offset statt
Überschreiben, systemd-Härtung, Audit-Log. Die folgenden Punkte hätten im
Dauerbetrieb Probleme gemacht.

---

## F1 — Zählerstand wird bei jeder Abfrage neu gezählt

**Schwere: hoch.** Wird mit der Zeit unbrauchbar langsam.

Im Upload, `s0counter/db.py`, Methode `state()`:

```sql
SELECT m.name, m.pulses_per_kwh, m.offset_kwh, COUNT(p.id), MAX(p.ts)
FROM meters m LEFT JOIN pulses p ON p.meter_id=m.meter_id
WHERE m.meter_id=? GROUP BY m.meter_id
```

Das zählt bei **jeder** Abfrage sämtliche Impulse des Zählers. Aufgerufen wird
`state()` bei jedem einzelnen Impuls (über `pulse()`), bei jeder
MQTT-Veröffentlichung und bei jedem HTTP-Aufruf.

Die Größenordnung: 1.000 Impulse je kWh, drei Wohnungen mit zusammen rund
6.000 kWh im Jahr sind sechs Millionen Zeilen jährlich. Der `COUNT` läuft über
den Index, aber über alle Zeilen — auf einem Pi 3B wird das im zweiten
Betriebsjahr spürbar, und zwar genau in dem Moment, in dem ein Impuls
eintrifft.

**Behoben durch** die Tabelle `counters`: laufende Summe plus die beiden
letzten Zeitstempel, in derselben Transaktion wie der Impuls fortgeschrieben.
`state()` liest danach eine Zeile mit Primärschlüsselzugriff.

Damit das nicht zur zweiten Wahrheit wird, prüft `_repair_counters()` beim
Start die Summe gegen die Rohimpulse und baut sie bei Abweichung neu auf —
mit Eintrag im Audit-Log. Der Test
`test_zaehlerstand_wird_aus_rohdaten_wiederhergestellt` sichert das ab, ein
weiterer Test die Migration einer Datenbank ohne `counters`-Tabelle.

---

## F2 — Doppelte Entprellung, davon eine wirkungslos

**Schwere: mittel.** Erzeugt den Eindruck einer Prüfung, die nicht greift.

Der Upload entprellt zweimal:

```python
# in setup_gpio()
dev = DigitalInputDevice(..., bounce_time=float(c.get('bounce_ms',10))/1000.0)

# in pulse()
minimum = float(c.get('bounce_ms',10))/1000.0
if mono - self.last_accepted.get(mid,0) < minimum: return
```

`gpiozero` filtert bereits mit `bounce_time`. Der Aufruf kommt also gar nicht
erst durch, wenn er zu früh wäre — die zweite Prüfung in `pulse()` löst nie
aus. Sie ist nicht falsch, aber sie prüft nichts.

Was tatsächlich fehlte: eine Prüfung gegen Abstände, die zwar über der
Entprellzeit liegen, aber trotzdem kein echter Verbrauch sein können. Ein
Impulsabstand von 20 ms entspricht bei 1.000 Imp/kWh einer Leistung von
180 kW — an einem 3x80-A-Zähler unmöglich.

**Behoben durch** `PulseCounter` mit zwei getrennten Stufen: Entprellung nach
`bounce_ms`, darüber eine Plausibilitätsgrenze bei 60 kW. Verworfene Impulse
werden nach Ursache getrennt gezählt und stehen im Status unter `bounced` und
`implausible` — Einstreuungen werden dadurch sichtbar, statt still den
Zählerstand zu verfälschen.

Die Prüfung greift nur oberhalb der Entprellung: wer `bounce_ms` bewusst hoch
setzt, soll nicht zusätzlich Warnungen bekommen.

---

## F3 — Keine Prüfung der Konfiguration

**Schwere: mittel.** Ein Tippfehler fällt erst Wochen später auf.

Der Upload liest die TOML-Datei und verwendet die Werte direkt. Zwei
Zähler mit derselben GPIO-Nummer werden klaglos angenommen — beide zählen
dann dieselben Impulse, und die Wohnungsstände sind still falsch. Auch eine
GPIO-Nummer außerhalb des nutzbaren Bereichs oder ein `pulses_per_kwh` von 0
(Division durch null in der Leistungsberechnung) wird nicht bemerkt.

**Behoben durch** `s0counter/config.py`: doppelte GPIO-Belegung, reservierte
Pins (0, 1 für die HAT-Erkennung), I2C-Pins mit festem Pull-up (2, 3),
Nummern außerhalb 4 bis 27, nicht-positive Impulszahlen und negative
Entprellzeiten führen zu einer verständlichen Fehlermeldung.

Der systemd-Dienst ruft die Prüfung über `ExecStartPre` auf und startet bei
einem Fehler gar nicht erst.

---

## F4 — `install.sh` aktualisiert die Konfiguration nie

**Schwere: niedrig, aber unangenehm bei jedem Update.**

Im Upload:

```bash
if [ ! -f /etc/s0counter/config.toml ]; then
    install -m 640 ... "$BASE/config.toml" /etc/s0counter/config.toml
fi
```

Eine bestehende Konfiguration nicht zu überschreiben ist richtig — sie enthält
Offsets und gegebenenfalls Zugangsdaten. Aber neue Optionen einer späteren
Version erreichen den Betrieb dadurch nie, und es gibt keinen Hinweis darauf.

**Behoben durch:** die Vorlage landet bei Unterschieden als `config.toml.neu`
daneben, mit ausgegebenem `diff`-Befehl.

---

## F5 — Leistung bleibt nach dem letzten Impuls stehen

**Schwere: niedrig.** Im Upload bereits teilweise berücksichtigt.

`power_timeout_seconds` wird im Upload in `publish()` und `states()`
angewandt, aber nicht in `db.state()` selbst. Wer `state()` direkt aufruft —
etwa `s0ctl status` — bekommt die Leistung des letzten Impulsabstands, auch
wenn der Stunden zurückliegt.

**Behoben durch:** klare Trennung. `db.state()` liefert den Rohwert,
`Service.states()` wendet den Timeout an. `s0ctl status` zeigt zusätzlich das
Alter des letzten Impulses, damit ein alter Wert erkennbar ist.

---

## F6 — MQTT war eingeplant, es gibt aber keinen Broker

**Schwere: hoch für die Inbetriebnahme.** Die Anbindung wäre nie zustande
gekommen.

Der Upload sieht MQTT als einzigen Weg zu Home Assistant vor. In der Anlage
läuft kein Broker — geprüft am 06.08.2026 auf `192.168.0.30`, `192.168.0.40`
und `192.168.0.50`, Port 1883 überall zu.

Das Home Assistant selbst läuft auf `192.168.0.30:8123`, Version 2025.10.3,
als VM auf der DiskStation.

**Behoben durch** `s0counter/homeassistant.py`: Anbindung über die REST-API,
gegen die laufende Installation getestet. Der MQTT-Weg bleibt vollständig
erhalten und ist der bessere, sobald ein Broker da ist — die Grenzen beider
Wege stehen in der README.

---

# Befunde aus dem Test auf echter Hardware

Erhoben am 09.08.2026 auf dem Entwicklungs-Pi (`DevPi`, Raspberry Pi OS Lite
Trixie, Kernel 6.18, Python 3.13.5) — ohne angeschlossene Zähler, aber mit
echtem GPIO, echtem systemd und echtem `install.sh`.

Diese Befunde stammen **nicht** aus dem Upload. Sie sind auf einem
Entwicklungsrechner strukturell unsichtbar: dort läuft alles als derselbe
Benutzer, aus dem Projektverzeichnis, ohne systemd-Absicherung. Die Testsuite
war vollständig grün, während der Dienst auf dem Pi in der Neustartschleife
hing.

---

## F7 — Der Dienst startet auf Trixie überhaupt nicht

`install.sh` lief durch, meldete Erfolg, und der Dienst war tot. Im Journal
stand nur:

```
OSError: [Errno 22] Invalid argument
  File "gpiozero/pins/native.py", line 251, in export
    with io.open(self.path('export'), 'wb') as f:
```

Die Kette dahinter hat drei Glieder und ist an keiner Stelle sichtbar:

1. `WorkingDirectory=/opt/s0counter` gehört `root:root`. Der Dienst läuft als
   `s0counter` und darf dort nicht schreiben.
2. `lgpio` legt seine Notify-Datei `.lgd-nfy*` über einen **relativen** Pfad
   an — also im Arbeitsverzeichnis. Das scheitert mit `EACCES`.
3. gpiozero probiert die Pin-Factories der Reihe nach durch, **verschluckt**
   den Fehler von `lgpio` und nimmt `native`. Die arbeitet über
   `/sys/class/gpio` — dieses Interface gibt es ab Trixie nicht mehr.

Die gemeldete Ursache liegt damit zwei Glieder neben der echten. Wer nur die
Meldung liest, sucht am sysfs-Interface und findet nichts.

**Korrektur** in drei Teilen, weil jedes Glied für sich eine Falle bleibt:

- `WorkingDirectory=/var/lib/s0counter` — gehört dem Dienstbenutzer und steht
  ohnehin schon in `ReadWritePaths`. Dazu `Environment=PYTHONPATH=/opt/s0counter`,
  denn `python -m s0counter.main` fand das Paket bisher nur über das
  Arbeitsverzeichnis.
- `counter.use_lgpio()` nagelt die Factory fest, **bevor** gpiozero importiert
  wird. Mit `os.environ.setdefault`, nicht per Zuweisung: eine bewusst gesetzte
  `GPIOZERO_PIN_FACTORY` soll zur Fehlersuche Vorrang behalten.
- Dieselbe Zuweisung zusätzlich in der Unit — wer nur die Unit liest, soll es
  dort sehen, ohne den Code zu kennen.

Die Trennung „gpiozero erst in `GpioBinding.setup()` importieren" bleibt davon
unberührt; `use_lgpio()` setzt nur eine Umgebungsvariable und importiert
nichts. Die Tests laufen weiterhin ohne Hardware.

---

## F8 — `install.sh` meldet Erfolg, obwohl der Dienst tot ist

Das Skript endete mit Status 0 und der Zeile „Fertig. Nächste Schritte:",
während der Dienst in der Neustartschleife hing. Zwei Gründe:

- `systemctl enable --now` meldet Erfolg, sobald systemd den Start
  *angenommen* hat — nicht, wenn er geglückt ist.
- `ExecStartPre` mit `--check-config` lief sauber durch und täuschte
  zusätzlich, weil die Konfiguration ja in Ordnung war. Nur `ExecStart` starb.

Bei einer unbeaufsichtigten Installation ist das der schlimmste Fall: es sieht
fertig aus und zählt nichts.

**Korrektur:** nach dem Start bis zu 15 s auf einen stabilen Zustand warten
(`Restart=on-failure` lässt `activating (auto-restart)` zu), dann `is-active`
prüfen. Bei Misserfolg die letzten 30 Journalzeilen ausgeben und mit Status 1
abbrechen — wer installiert, sitzt vor dem Gerät und soll die Ursache sehen,
ohne einen zweiten Befehl zu suchen.

---

## F9 — Ein zweiter Lauf zieht den neuen Stand nicht

`systemctl enable --now` lässt einen bereits laufenden Dienst unangetastet. Ein
`install.sh` über eine bestehende Installation kopierte also neue Dateien nach
`/opt/s0counter`, lud die neue Unit — und ließ den alten Prozess weiterlaufen.
Nachgewiesen über eine geänderte Unit: die PID blieb dieselbe, der alte Stand
lief weiter.

Das fällt erst beim nächsten Neustart des Pi auf, also frühestens Wochen
später, und dann ohne erkennbaren Zusammenhang zum Update.

**Korrektur:** vor `daemon-reload` merken, ob der Dienst lief, und danach
gezielt `restart`. Die Prüfung aus F8 sitzt hinter dem Neustart, damit sie den
neuen Prozess bewertet und nicht den alten.

Der Rest der Idempotenz war in Ordnung und ist so bestätigt: die bestehende
`/etc/s0counter/config.toml` bleibt unangetastet, gesetzte Zählerstände
überleben (`eg` stand vor und nach dem Update auf 1234,567 kWh), die Timer
bleiben deaktiviert.

---

## F10 — Der dokumentierte Signaltest stirbt weiterhin

Nach F7 lief der Dienst — der Signaltest aus README und Installationsabschluss
aber nicht:

```bash
sudo -u s0counter /opt/s0counter/venv/bin/python /opt/s0counter/test_gpio.py
```

Derselbe `OSError` wie in F7. Der Grund ist, dass ein von Hand aufgerufenes
Skript die Absicherung der Unit **nicht erbt**: kein gesetztes
`WorkingDirectory`, kein `GPIOZERO_PIN_FACTORY`. Es läuft in dem Verzeichnis,
in dem der Monteur gerade steht — im README-Beispiel `/opt/s0counter`, also
genau dem Verzeichnis, in dem `lgpio` nicht schreiben darf.

Das ist der erste Schritt der Inbetriebnahme. Er wäre vor Ort fehlgeschlagen,
mit einer Meldung, aus der niemand etwas ableiten kann.

**Korrektur:** `test_gpio.py` sorgt selbst für seine Voraussetzungen, statt
sich auf die Umgebung zu verlassen — `use_lgpio()` und ein Wechsel in ein
`tempfile.mkdtemp()`-Verzeichnis für die Laufzeit, aufgeräumt im `finally`
nach dem Schließen der Geräte (vorher kann `lgpio` seine Notify-Datei nicht
mehr selbst entfernen).

Allgemein: **jeder dokumentierte Befehl gehört einmal wörtlich ausgeführt**,
aus einem beliebigen Verzeichnis. Ein Dienst, der läuft, sagt nichts über die
Handgriffe daneben.

---

## F11 — Die Verteilrechnung gab es nur im Jahresbericht

**Schwere: hoch.** Die Rechnung, für die die Anlage gebaut wurde, war im
Betrieb nicht erreichbar.

`Allocator.allocate()` war vollständig, getestet und richtig — aufgerufen
wurde sie ausschließlich von `report.py`, also einmal im Jahr von Hand. Das
Dashboard zeigte Zählerstände und Hausbilanz, aber nirgends, welcher Anteil
des Wohnungsverbrauchs aus der PV kam.

Das ist derselbe Fall wie eine Szene ohne Weg dorthin: gebaut, geprüft,
dokumentiert — und im Produkt nicht vorhanden. Kein Test fiel darüber, weil
alle prüften, ob die Rechnung *stimmt*, keiner, ob man *hinkommt*.

**Korrektur:** `/allocation?range=…&model=…` mit wählbarem Zeitraum und
Verteilmodell, dazu ein eigener Abschnitt im Dashboard. Die Auswahllisten
kommen aus der Antwort des Dienstes, nicht aus einer zweiten Liste in der
Anzeige — `test_auswahl_kommt_aus_dem_dienst` sichert das ab.

---

## F12 — `allocate()` skalierte nicht auf den Jahreszeitraum

**Schwere: hoch**, sobald der Zeitraum wählbar wird.

`_pulses_per_interval()` holte über `iter_pulses()` **jeden einzelnen Impuls**
nach Python und sortierte ihn per Schleife in sein Intervall. Gemessen mit der
Datendichte der Anlage (aus dem Live-Stand hochgerechnet: 19.004 Impulse/Tag,
6,94 Mio./Jahr):

| Zeitraum | Impulse | vorher | nachher |
|---|---|---|---|
| 1 Tag | 19.004 | 49 ms | 5,7 ms |
| 30 Tage | 570.141 | 1.430 ms | 173 ms |
| 1 Jahr | 6.936.731 | 18.221 ms | 2.333 ms |

Auf einem Pi 3B (grob Faktor 7) wären das im Jahreszeitraum über zwei Minuten
gewesen — und zwar blockierend, mit gehaltenem Datenbank-Lock, während die
Impulserfassung im selben Prozess läuft.

**Die Rechnung war nie das Problem.** Ein Profil zeigte: 302 ms Schleife,
6,4 s SQLite. Der Aufwand steckte im Transport der Zeilen, nicht in der
Verteilung. Zwei Zwischenschritte, die *nicht* trugen und deshalb hier
stehen, damit sie niemand nochmal probiert:

- **`GROUP BY` über `pulses`** brachte nur Faktor 2,5 — SQLite baut für einen
  berechneten Ausdruck einen `TEMP B-TREE` über alle 6,9 Mio. Zeilen.
- **Ein Ausdrucksindex** brachte Faktor 3, kostete aber 104 MB und band die
  Abfrage an ein festes Raster von 900 s. Damit wäre der wählbare Zeitraum
  wieder weg gewesen.

**Korrektur:** die Tabelle `pulse_minutes` — Impulse je Zähler und Minute,
in derselben Transaktion wie der Impuls fortgeschrieben. Sie hat rund ein
Sechzigstel der Zeilen, und weil `minute` im Primärschlüssel führt, liest
SQLite den Bereich sortiert.

Dasselbe Muster wie `counters` (siehe F1), mit denselben Sicherungen:

- `_repair_pulse_minutes()` gleicht beim Start gegen die Rohimpulse ab und
  baut das Aggregat bei Abweichung neu auf, mit Eintrag im Audit-Log.
- `verify_pulse_minutes()` prüft Minute für Minute — findet auch
  Verschiebungen, bei denen die Summe stimmt.
- Die Rohimpulse bleiben die einzige Wahrheit; das Aggregat ist jederzeit
  daraus neu berechenbar.

**Das Aggregat wird nur benutzt, wo es exakt sein kann.** `_aggregat_nutzbar()`
verlangt, dass sich jedes Intervall lückenlos aus ganzen Minuten
zusammensetzt — sonst wird über die Rohimpulse gezählt. Ein leise falsch
einsortierter Impuls wäre schlimmer als eine langsame Abfrage.

Die alte Schleife steht als `_pulses_per_interval_slow()` weiterhin im Code.
Sie ist die geprüfte Vorgängerfassung und dient in `TestZaehlungGleichwertig`
als Referenz: beide Wege müssen dasselbe liefern, geprüft bis auf die
kWh-Beträge, nicht nur bis auf die Impulszahl.

Dazu ein Zwischenspeicher im `Allocator`, dessen Schlüssel den **Datenstand**
enthält (Impulszahl, `pulses_per_kwh`, `offset_kwh`, letzter Bilanzzeitstempel)
statt einer Uhrzeit. Ein Zeit-Cache hätte für seine Dauer alte Zahlen
ausgeliefert — bei einer Abrechnung der falsche Kompromiss. Gemessen: erster
Abruf 2.333 ms, Folgeabrufe 0,05 ms, nach einem neuen Impuls wieder 2.338 ms.

---

## F13 — Ein Posten, zwei Ursachen, ein falscher Text

**Schwere: mittel.** Gefunden erst im Browsertest, nicht durch Unittests.

`unallocated_grid_kwh` sammelte zwei verschiedene Dinge in einer Zahl:

1. Intervalle, in denen **keine Wohnung** einen Impuls lieferte;
2. den **Deckel** — das Haus bezog mehr, als die Wohnungen verbraucht haben.

Die Anzeige kann daraus nur einen Grund nennen und lag im anderen Fall
daneben. Im Prüfstand stand unter der Abrechnung „Bezogen, während keine
Wohnung Impulse lieferte", während `intervals_without_pulses` null war: die
18 kWh kamen vollständig vom Deckel. Ein falscher Erklärtext an einer
Abrechnungszahl.

**Korrektur:** `unallocated_idle_kwh` und `unallocated_capped_kwh` getrennt
ausweisen, Summe bleibt `unallocated_grid_kwh`. Die Anzeige leitet den Grund
aus den Zahlen ab, statt ihn pauschal zu behaupten. Nebenbei fiel auf, dass
der Kommentar am Deckel nur das Wasserfallmodell nannte — er greift bei
beiden.

Übertragbar: **wo ein Restposten zwei Ursachen haben kann, gehören zwei
Zahlen hin.** Sonst kann die Anzeige nur raten, und sie rät irgendwann falsch.

---

## F14 — Kachelraster erzwang drei Spalten auf dem Telefon

**Schwere: mittel.** Bestand schon vor dieser Änderung (seit `c3f307f`) und
wurde durch die Abrechnungskacheln sichtbar.

`minmax(min(100%, 15rem, 33%), 1fr)` ergab bei 390 px Breite eine
Mindestspalte von rund 118 px — drei Spalten, wo eine hingehört. Die
Kachelköpfe wurden auf 85 px gequetscht, und `overflow-wrap: anywhere` brach
die Titel mitten im Wort: „WOHNU/NG EG", „ERZEUGUN/G", „EINSPEISU/NG". Beim
Wert stand „8,291 kW/h" — über zwei Zeilen gelesen eine andere
physikalische Größe.

Der erste Browsertest meldete das **nicht**: er prüfte auf Überlauf und
Trefferflächen, und beides war in Ordnung. Sichtbar wurde es erst am
Screenshot.

**Korrektur:** `max(11rem, 33%)` als Untergrenze, `overflow-wrap: break-word`
statt `anywhere` bei den Titeln, `normal` beim Wert und `white-space: nowrap`
für die Einheit.

Dazu zwei neue Prüfungen im Browsertest, beide gegenprobiert (mit dem alten
CSS melden sie vier Befunde, mit dem neuen keinen):

- Wortbruch: `Range.getClientRects()` über Elemente **ohne Leerzeichen** —
  mehr als ein Rechteck heißt Bruch mitten im Wort.
- Abgetrennte Einheit: sitzt `.karte__einheit` tiefer als die erste Zeile
  ihres Werts?

Übertragbar: **ein Layouttest, der nur Überlauf misst, sieht keine
Unlesbarkeit.** Ein Text kann vollständig innerhalb seiner Kachel stehen und
trotzdem unlesbar sein.

---

## F15 — Ein Ausfall des Rechners hinterließ keine Spur

**Schwere: hoch.** Betrifft die Belastbarkeit jeder Abrechnung.

`note_gap()` schreibt einen Eintrag, wenn die **Quelle** ausfällt und der
Dienst weiterläuft. Der häufigere Fall ist der andere: Stromausfall, Absturz,
ein Update, ein gezogenes Netzteil — dann ist der Rechner selbst weg, und es
gibt niemanden, der etwas vermerken könnte. Die Impulse dieser Zeit fehlen
ebenfalls, denn ohne laufenden Dienst zählt nichts mit.

Nachgestellt mit einem zwölfstündigen Loch in zwei Tagen Daten:

```text
Intervalle:   144 von 192 erwartet
Abdeckung:    75,0 %
gaps-Tabelle: 0 Einträge      <-- niemand konnte etwas schreiben
```

Die Abdeckung schlug an, das Loch selbst blieb unsichtbar: **wann** der
Ausfall war und **wie lange** er dauerte, stand nirgends — obwohl beides in
den Zeitstempeln steckt.

**Korrektur:** `detect_gaps()` rekonstruiert Löcher aus dem Abstand
aufeinanderfolgender Messpunkte. Alles über dem Dreifachen des üblichen
Abstands gilt als Lücke; erkannt wird auch, wenn die Reihe erst nach dem
Zeitraumbeginn anfängt oder vor seinem Ende aufhört. Das Ergebnis steht als
`quality.detected_gaps` in jeder Antwort und wird in der Bewertung mit
Zeitpunkt und Dauer genannt.

Zwei Entscheidungen, die dabei wichtig waren:

- **Der Abstand kommt aus den Daten** (Median), nicht aus der Konfiguration.
  Eine übergebene Zahl wäre eine zweite Quelle neben
  `sample_interval_seconds` und liefe auseinander, sobald jemand den Takt
  ändert, ohne daran zu denken.
- **Faktor 3, nicht 2.** Ein einzelner verschluckter Messpunkt ist kein
  Ausfall. Ein Gate, das bei jedem Aussetzer meldet, wird ignoriert und
  schützt danach gar nichts mehr — deshalb gibt es
  `test_einzelner_aussetzer_gilt_nicht_als_ausfall` und
  `test_durchgehende_reihe_meldet_nichts` als Gegenrichtung.

---

## F16 — Abrechenbar war nur „die letzten n Tage ab jetzt"

**Schwere: mittel.** Für eine Nebenkostenabrechnung unbrauchbar.

Alle Zeiträume waren rollierend: `365d` waren die letzten 365 Tage ab jetzt,
nicht das Kalenderjahr. Eine Abrechnung für 2026 ließ sich damit nicht
erstellen — und ab dem 2. Januar wäre auch `365d` das falsche Fenster.

**Korrektur:** Kennungen wie `2026` und `2026-08` neben den rollierenden.
Gerechnet wird über `mktime` in **Ortszeit** mit `tm_isdst=-1`, damit Jahre
über die Zeitumstellung hinweg korrekt begrenzt sind und 23- oder
25-stündige Tage richtig enthalten. Ein Schaltjahr hat dadurch 366 Tage, ohne
dass das irgendwo als Zahl steht — `test_schaltjahr_hat_366_tage` sichert das
ab, und mit `start + 365 * 86400` statt der Kalenderrechnung fällt der Test.

Das laufende Jahr wird **am aktuellen Zeitpunkt abgeschnitten**. Ohne das
stünde es im August mit 60 % Abdeckung da, obwohl kein einziger Messpunkt
fehlt — eine Warnung über ein Problem, das es nicht gibt.

Angeboten wird nur, was der Datenbestand hergibt: der früheste Zeitstempel
der Bilanzreihe begrenzt die Liste. Beim Monat wird dabei das **Monatsende**
gegen den ersten Messpunkt gehalten, nicht der Monatsanfang — sonst fiele
genau der laufende Monat heraus, wenn die Aufzeichnung am Fünfzehnten begann.
Genau das passierte im ersten Anlauf: der Prüfstand bot „2026" an, aber
keinen einzigen Monat.

---

## F17 — Gruppenbeschriftungen wurden gleich wieder gelöscht

**Schwere: gering**, aber lehrreich.

Mit Kalenderzeiträumen wurden aus fünf Knöpfen bis zu siebzehn, und „2026"
verschwand zwischen „30 Tage" und „August 2026". Die Reihen bekamen deshalb
Beschriftungen — die im Bild fehlten.

Ursache: `abrechnungWahlBauen()` beginnt mit `ziel.replaceChildren()` und
räumte die unmittelbar davor eingehängte Marke wieder ab. **Kein Knopf ging
dabei kaputt**, die Gruppierung funktionierte, alle bestehenden Prüfungen
blieben grün — nur die Beschriftung fehlte.

Der Browsertest fand es nicht, weil er Knöpfe zählte und nicht das, was
danebensteht. Aufgefallen ist es am Screenshot.

**Korrektur:** ein `leeren`-Schalter am Aufruf, plus eine Prüfung im
Browsertest, dass jede Zeitraumreihe eine nichtleere Beschriftung hat.
Gegenprobe bestanden: mit dem alten Aufruf meldet sie drei Befunde.

Übertragbar: **wer ein Element in einen Container hängt, den eine andere
Funktion befüllt, muss wissen, ob die vorher aufräumt.** Und: eine Prüfung,
die nur die Hauptsache zählt, sieht das Verschwinden der Nebensache nicht.

---

## F18 — Der Abrechnungsabschnitt kam nie auf den Pi

**Schwere: hoch** — die fertige Funktion war in der Anlage nicht vorhanden.

Beim Einspielen von F11–F17 auf den laufenden Pi wurde von Hand verglichen,
welche Dateien sich geändert haben. Verglichen wurden `*.py` und `*.js` — die
vierte geänderte Datei, `dashboard.html`, kam in der Aufzählung nicht vor.

Alles, was danach geprüft wurde, war grün und blieb es auch: die Migration des
Minutenaggregats lief an den echten Daten fehlerfrei, `/allocation` lieferte
für beide Verteilmodelle und alle Zeiträume korrekte Zahlen, die Haussumme ging
ohne Rest auf, der Deckel hielt, kein Impuls ging beim Neustart verloren. **Der
Rechenweg war vollständig da. Nur die sechs HTML-Elemente, in die die Anzeige
ihr Ergebnis schreibt, fehlten** — und damit sah niemand je eine Abrechnung.

Gefunden hat es nicht der Mensch, sondern `tools/dashboard_pruefen.py`: sechs
Befunde, einer je Fenstergröße, „keine Abrechnungskachel erschienen". Das ist
der Beleg dafür, dass die Prüfung im Browser das misst, was sie behauptet —
sie lief gegen einen Server, dessen Endpunkt einwandfrei antwortete.

**Korrektur:** `deploy.sh`. Es ermittelt die Unterschiede über Prüfsummen statt
über eine gepflegte Dateiliste, sichert Code und Datenbank vor dem Austausch,
prüft nach der Übertragung jede Prüfsumme einzeln und startet erst dann neu,
und kontrolliert danach, dass keine Zählerstände zurückgefallen sind.

Zwei Fehler im Skript selbst, beide erst im Lauf sichtbar:

- Die Dateiliste stand darin zunächst wieder dreimal, und `*.html` fehlte in
  zweien davon — derselbe Fehler eine Ebene höher. Jetzt eine Konstante.
- `MUSTER=(*.py *.js *.html)` expandiert **schon bei der Zuweisung** gegen das
  Projektverzeichnis. Aus `*.py` wurden dort `s0ctl.py` und `test_gpio.py`,
  also ausgerechnet die beiden Dateien, die *nicht* zum Paket gehören, während
  keine einzige Paketdatei getroffen wurde. Muster gehören in Anführungszeichen
  und werden erst dort aufgelöst, wo sie hinzeigen.

Übertragbar, und die eigentliche Lehre: **ein Abgleich von Hand prüft die
Dateien, an die man denkt.** Wer beim Deployment aufzählt, was sich geändert
hat, hat schon einen Fehler gemacht — die Frage lautet nicht „was habe ich
geändert?", sondern „was unterscheidet sich?", und die beantwortet eine
Prüfsumme, kein Gedächtnis.

Und: **ein grüner Endpunkt ist keine sichtbare Funktion.** Backend und Anzeige
sind zwei Wege; ein Gate auf dem einen sagt nichts über den anderen.

---

## F19 — Ein Index für 95 MB im Jahr, benutzt im Störfall für zwei Zeilen

**Schwere: mittel** — kein Fehlverhalten, aber der größte vermeidbare
Platzverbrauch der Anlage.

Auf `pulses` lagen zwei Indizes. `idx_pulses_meter_ts` (meter_id, ts) trägt
den gesamten Betrieb: Verlauf, Abrechnung, den Abgleich von `pulse_minutes`,
den Export. `idx_pulses_meter_id` (meter_id, id DESC) bediente **eine einzige
Abfrage** — die Suche nach den beiden jüngsten Zeitstempeln in
`_repair_counters()`. Die läuft nur beim Start, und auch dann nur, wenn die
laufende Summe von den Rohimpulsen abweicht, also nach einem Absturz.

Gemessen an einer Datenbank mit 6.310.942 Zeilen, also einem vollen
Betriebsjahr bei den tatsächlich beobachteten 720 Impulsen je Stunde:

| Objekt | Größe nach einem Jahr |
|---|---|
| Tabelle `pulses` | 189 MB |
| `idx_pulses_meter_ts` | 143 MB |
| `idx_pulses_meter_id` | **95 MB** |
| `pulse_minutes` | 2 MB |

Der Ausweg brauchte keinen Kompromiss zwischen Platz und Geschwindigkeit:
`ORDER BY ts DESC` statt `ORDER BY id DESC` liefert dieselben zwei Werte und
bedient dabei `idx_pulses_meter_ts` als COVERING INDEX. Gemessen an denselben
6,3 Mio Zeilen:

- mit eigenem Index, `ORDER BY id`: **0,064 ms**
- ohne Index, `ORDER BY id`: **661 ms** (voller Durchlauf)
- ohne Index, `ORDER BY ts`: **0,035 ms** — schneller als mit

Die Sortierung nach `id` war der Umweg: gesucht sind die beiden jüngsten
*Zeitpunkte*, und die stehen in `ts`. Die Einfügereihenfolge ist dafür nur ein
Näherungswert, der zufällig meist stimmt.

**Korrektur:** Abfrage auf `ts` umgestellt, Index aus dem Schema entfernt, am
Pi mit `DROP INDEX` abgeräumt (Integritätsprüfung danach `ok`, alle 11.285
Impulse unverändert, Dienst lief durch). Drei Tests dazu: die beiden
Zeitstempel werden auf ihren Wert geprüft, der Abfrageplan muss den
vorhandenen Index nutzen, und auf `pulses` darf kein zweiter Index entstehen.

**Die Gegenprobe deckte einen wertlosen Test auf.** Die erste Fassung des
Plan-Tests schrieb die zu prüfende SQL-Zeile im Test nochmal hin. Beim
Zurückbau auf `ORDER BY id` blieb sie **grün** — sie prüfte ihren eigenen
String, nicht den Code. Jetzt schneidet der Test über
`sqlite3.set_trace_callback` mit, welche Anweisung `_repair_counters()`
wirklich absetzt, und erklärt genau die. Danach fällt er bei beiden
Sabotagen: Sortierung zurückgebaut → rot, Index entfernt → rot.

Übertragbar: **ein Index, der nur einen Sonderfall bedient, wird trotzdem
dauerhaft mitgeschrieben.** Vor dem Anlegen fragen, welche Abfrage ihn braucht
und wie oft die läuft — und ob sich die Abfrage so stellen lässt, dass ein
vorhandener Index sie trägt. Und: **eine im Test wiederholte SQL-Zeile prüft
den Test, nicht das Programm.**

---

## F20 — Die Anlage lief einen Tag ohne jede Sicherung

**Schwere: hoch** — Datenverlustrisiko an produktiven Abrechnungsdaten.

`s0counter-backup.timer` war installiert und in der Inbetriebnahme-Doku als
„Timer installiert, nicht aktiviert" vermerkt. Tatsächlich stand er auf
`disabled/inactive`, und **es existierte keine einzige Sicherung** — während
die Anlage seit dem 24.08. Zählerstände erfasst, die Abrechnungsgrundlage
sind. Ein Ausfall der SD-Karte hätte sie vollständig gekostet.

Beim Einschalten und dem erzwungenen Probelauf zeigte sich der eigentliche
Fund: **die Sicherung schlug fehl.** `/mnt/synology/s0counter` ist nicht
eingebunden, `backup.sh` brach ab. Nachts um 03:27 wäre das lautlos passiert —
der Dienst wäre als `failed` stehengeblieben, und niemand hätte hingesehen.

Der Abbruch war ursprünglich richtig gedacht: Wer in ein nicht eingebundenes
Verzeichnis schreibt, legt die Sicherung dort ab, wo sie beim nächsten
Einbinden verdeckt wird — unsichtbar weg. In der Wirkung war er trotzdem
falsch, weil er statt einer schlechteren Sicherung **gar keine** hinterließ.

**Korrektur:** `backup.sh` weicht auf `/var/backups/s0counter` aus, warnt
dreifach im Log und wiederholt die Warnung als letzte Zeile — wer den
Dienststatus ansieht, liest die letzte, nicht die erste. Der Rückgabewert
bleibt 0: Der Lauf ist gelungen, nur an einem schlechteren Ort, und ein
`failed`-Dienst würde die echte Fehlermeldung im Rauschen begraben.

Belegt statt behauptet, weil eine vorhandene Datei noch keine Sicherung ist:
Integritätsprüfung `ok`, die Zählerstände lassen sich aus der Sicherung
rekonstruieren (EG 6522,648 kWh), die Bilanz-Datenbank ebenfalls `ok`.

Gegenprobe in beide Richtungen, mit einem `tmpfs` als Attrappe für ein
eingebundenes Ziel: Ist das Ziel da, schreibt das Skript dorthin — **ohne**
Warnung und ohne das Ausweichziel anzurühren. Fehlt es, sichert es lokal und
warnt. Ohne die Richtung *grün* wäre offen geblieben, ob es künftig immer nur
noch lokal sichert.

Übertragbar, und der Kern: **einen Timer einzuschalten heißt nicht, dass er
etwas tut.** Nach dem Aktivieren einen Lauf erzwingen und nachsehen, was
entstanden ist — das ist dieselbe Regel wie bei F18 (Endpunkt grün, Anzeige
leer) und beim Prozessbeenden (Rückgabewert 0, Speicher weiter belegt).
Und: **eine Schutzmaßnahme, die im Fehlerfall gar nichts hinterlässt, ist
schlechter als eine, die etwas Unvollkommenes hinterlässt** — solange sie
laut genug sagt, dass es unvollkommen ist.

---

## F21 — Zwei Zahlen für denselben Akku: 23 % gegen 16 %

**Schwere: mittel** — kein falscher Messwert, aber ein falscher.

Das Dashboard zeigte 23 % Ladestand, Home Assistant 16 %. Aufgefallen ist es
beim Nebeneinanderhalten, nicht durch einen Test.

Die Sonnenbatterie meldet **beide** Werte in derselben Antwort:

| Feld | Bedeutung | am 25.08.2026 |
|---|---|---|
| `USOC` | *User State of Charge* — nutzbarer Bereich auf 0–100 % gedehnt | 16 |
| `RSOC` | *Relative State of Charge* — technisch, über die Bruttokapazität | 23 |

`RSOC` erreicht nie 0 % und nie 100 %: der Akku behält in beide Richtungen
eine Reserve. `USOC` ist der Wert, den die Sonnen-App und die
Home-Assistant-Integration anzeigen — und damit der, den ein Mensch meint,
wenn er nach dem Ladestand fragt. `sources.py` las `RSOC`.

**Korrektur:** `USOC` bevorzugt, `RSOC` als Rückfall — eine ältere Firmware
ohne `USOC` soll weiterlaufen statt den Dienst anzuhalten; fehlen beide, ist
die Antwort wirklich unbrauchbar und es gibt einen Fehler. Drei Tests dazu,
Sabotage-Gegenprobe bestanden: zurück auf `RSOC` und zwei Tests fallen.

Übertragbar: **wo ein Gerät zwei Felder für dieselbe Größe liefert, ist die
Feldwahl eine inhaltliche Entscheidung, keine technische.** Der Prüfstein ist
nicht „welcher Wert ist richtiger", sondern **neben welchem anderen Wert der
eigene später steht** — hier neben Home Assistant, das dasselbe Gerät liest.
Zwei verschiedene Zahlen für denselben Akku kosten mehr Vertrauen, als die
präzisere Definition wert ist.

---

## F22 — Die Export-Knöpfe hätten immer den falschen Zeitraum geliefert

**Schwere: mittel** — ein Dokument, dem man nicht ansieht, dass es falsch ist.

Die neuen Knöpfe für PDF und CSV bekamen ihre Parameter aus denselben
Variablen wie die Anzeige — richtig gedacht, aber zur falschen Zeit gelesen:
`abrechnungZeitraum` und `abrechnungModell` sind vor dem ersten Abruf `null`,
weil der Dienst die Vorgabe bestimmt. Die Links standen dadurch ohne
Parameter da und lieferten stets 24 Stunden nach dem Modell „proportional" —
auch wenn am Bildschirm „August 2026" und „zu gleichen Teilen" stand.

Das Tückische: **das Dokument sah einwandfrei aus.** Es enthielt richtige
Zahlen für einen Zeitraum, den niemand angefordert hatte, und nannte ihn im
Kopf korrekt. Wer nicht nachrechnet, hätte es weitergereicht.

**Korrektur:** Die Links werden aus der **Antwort** gesetzt (`daten.range.key`,
`daten.model`) statt aus der Anfrage. Damit tragen sie genau den Zeitraum, den
die Anzeige gerade zeigt — auch beim ersten Aufruf, wo die Anfrage noch leer
war. Belegt am laufenden Pi: nach dem Umschalten auf „August 2026" steht
`range=2026-08` im Link, und die heruntergeladene Datei beginnt am 01.08.

Das Gate prüft jetzt beide Links auf `range=` und `model=`.

Übertragbar: **eine Anfrage ohne Parameter ist kein leerer Zustand, sondern
eine stille Vorgabe.** Wo eine Vorgabe auf der Gegenseite liegt, darf die
eigene Seite sie nicht weglassen und annehmen, es passiere nichts — sie
bekommt dann etwas anderes, als sie zeigt. Und: **was ein Anwender aus der
Hand gibt, muss sich selbst erklären** — der Zeitraum steht im Kopf der Datei,
sonst wäre der Fehler nie auffindbar gewesen.

---

## F23 — Die Sicherung sicherte die Konfiguration nicht

**Schwere: hoch** — das am schwersten Wiederbeschaffbare fehlte.

Beim Bau von `s0ctl doctor` wurde nachgezählt, was `backup.sh` tatsächlich
mitnimmt. Es fehlten:

- **`history.sqlite3`** — der Verlauf der Rechnerwerte hinter den Diagrammen
- **`/etc/s0counter/config.toml`** — die Offsets der Zählerstände und das
  Home-Assistant-Token
- die **systemd-Units**

Die Datenbanken sind Zahlen und lassen sich notfalls neu aufbauen. Die
Offsets dagegen sind an einem bestimmten Tag von den Displays abgelesene
Stände — nach einem Kartenausfall müsste man erneut ablesen und hätte die
Zeit dazwischen verloren. Genau der Teil, der eine Sicherung wertvoll macht,
war nicht darin.

**Und der Weg dorthin war selbst fehlerhaft.** Die Pfade wurden per `grep`
aus der TOML-Datei geholt:

```
grep -A6 '^\[history\]' "$CONFIG" | grep '^database' | cut -d'"' -f2
```

Das fand nichts — weil `[history]` in der Konfiguration **gar nicht steht**:
der Pfad hat einen Vorgabewert im Code. Ein `grep` auf einen Vorgabewert
findet ihn nie. Bei `[balance]` und `[report]` stand er nur zufällig in der
Datei; eine gelöschte Zeile hätte sie ebenso lautlos aus der Sicherung
geworfen.

**Korrektur:** Alle drei Pfade kommen jetzt aus dem Konfigurationsleser
(`config.load()`), der Vorgaben kennt. Die Konfigurationskopie trägt Modus
600. Und das Aufräum-Muster steht in **einer** Variablen — `verlauf-` und
`config-` fehlten dort prompt beim Einbau und wären ewig gewachsen.

Belegt am Pi: sechs Bestandteile in der Sicherung, alle drei Datenbanken
`integrity_check` = `ok`, Konfigurationskopie `600 root:root`, fünf Units.

Übertragbar: **eine Konfiguration parst man nicht, man fragt den Leser.**
Wo Vorgabewerte im Spiel sind, beschreibt die Datei nur die Abweichungen —
wer sie liest, sieht die Hälfte. Und: **wer aufzählt, was gesichert wird,
zählt auf, woran er denkt** — dieselbe Falle wie beim Deployment von Hand
(F18). Die Frage lautet nicht „was sichere ich?", sondern „was liegt hier,
das nach einem Totalausfall fehlen würde?".

---

## F24 — `deploy.sh` sah nur ins Paketverzeichnis

**Schwere: mittel** — ein Abgleich, der „alles gleich" meldet, ohne alles
angesehen zu haben.

`deploy.sh` verglich `s0counter/*.py|js|html` gegen `/opt/s0counter/s0counter/`.
Die drei Dateien **eine Ebene darüber** — `backup.sh`, `s0ctl.py` und
`test_gpio.py` — kamen im Abgleich nicht vor. Der Lauf meldete „Keine
Unterschiede — der Pi hat diesen Stand bereits", während `backup.sh` im Repo
und am Pi auseinandergelaufen waren; nur ein zufälliger Prüfsummenvergleich
von Hand deckte es auf.

Das ist derselbe Fehler wie F18, eine Ebene höher: Das Werkzeug, das gegen
„was habe ich vergessen?" gebaut wurde, vergaß selbst etwas — weil es an
einer Stelle **aufzählte**, statt zu suchen.

**Korrektur:** Die Dateien daneben werden mit `../`-Präfix mitgeführt, zwei
Funktionen (`lokal_pfad`, `fern_pfad`) lösen das Präfix an einer Stelle auf.
Sie bekommen Modus 755 statt 644 — es sind ausführbare Skripte, 644 hätte sie
unbrauchbar gemacht. Die Nachkontrolle fragt jetzt jede Datei einzeln ab,
statt in einer Sammelliste zu suchen: die hätte beide Zielverzeichnisse
kennen müssen, und ein `grep` auf den Dateinamen träfe `main.py` auch in
Zeilen von `s0ctl.py`. Der Rollback-Hinweis nennt je Datei ihr eigenes Ziel —
pauschal kopiert hätte er `backup.sh` ins Paketverzeichnis gelegt.

Gegenprobe in beide Richtungen: bei gleichem Stand meldet der Abgleich
„keine Unterschiede"; nach einer angehängten Zeile in der Pi-Fassung von
`backup.sh` meldet er `../backup.sh` und stellt sie wieder her (Prüfsumme
und Modus 755 danach belegt).

Übertragbar: **eine Auswahl per Verzeichnis ist auch eine Aufzählung.** Wer
`s0counter/*` vergleicht, hat entschieden, dass nichts anderes zählt — und
diese Entscheidung altert schlecht, sobald eine Datei danebengelegt wird.

---

## F25 — Die Sicherung auf die Synology schrieb 0 Bytes

Gefunden am 27.08.2026, am Tag nach dem Einrichten der NAS-Freigabe. Der
Doctor meldete drei grüne Haken: Timer aktiv, Ziel eingebunden, Sicherung
aktuell. Auf der Synology lag genau eine Datei —
`impulse-2026-08-27_1125.sqlite3`, **0 Bytes**.

Der Sicherungsdienst stand seit 42 Minuten auf `activating (start)` und hatte
dabei 9 Sekunden Rechenzeit verbraucht. Er wartete also, statt zu arbeiten.

Ursache: `backup_to()` rief `sqlite3.connect(ziel)` mit einem Pfad auf der
SMB-Freigabe auf. SQLite legt die Datei an und braucht für die Backup-API
dann Dateisperren (`fcntl`), die über CIFS nicht zuverlässig funktionieren.
Die Datei war da, geschrieben wurde nie.

Was die Diagnose zunächst in die falsche Richtung schickte: ein Schreibtest
mit `dd` und 20 MB lief über dieselbe Freigabe in Sekunden durch. **Reines
Schreiben geht, nur Sperren nicht** — wer nur prüft, ob Daten durchkommen,
misst am Problem vorbei. Auch die Schreibprobe des NAS-Assistenten war
deshalb grün: sie schreibt ein paar Bytes in eine gewöhnliche Datei.

Reparatur in drei Teilen, weil drei verschiedene Dinge versagt haben:

1. **`_sichern_ueber_zwischenkopie()`** schreibt lokal neben die Quelle und
   kopiert die fertige Datei mit `shutil.copyfile` — ein Datenstrom ohne
   Sperren. Kopiert wird auf einen Zwischennamen und erst danach umbenannt,
   damit unter dem endgültigen Namen nie ein Torso steht. Ein abgeschnittener
   Stand, der vollständig aussieht, ist schlimmer als gar keiner: die
   Rotation zählt ihn als gültige Sicherung mit.
2. **Der Doctor prüft jetzt die Dateigröße** gegen den 100 Byte langen
   SQLite-Kopf. Vorher zählte er Dateien, nicht Inhalte — und ein grüner
   Haken über einer leeren Datei ist schlimmer als ein roter, weil er die
   Suche beendet.
3. **`TimeoutStartSec=1200` in der Unit.** `Type=oneshot` wartet ohne
   Zeitlimit unbegrenzt; deshalb blieb der Hänger 42 Minuten stehen und hätte
   den nächsten nächtlichen Lauf still übersprungen — systemd startet keinen
   Job, solange der alte noch läuft.

Dieselbe fehlerhafte Zeile stand **dreimal**: in `db.py` und zweimal inline
in `backup.sh` für Hausbilanz und Verlauf. Beide rufen jetzt
`db.sqlite_kopieren()` auf.

Nach der Reparatur am Gerät gemessen: der Lauf braucht **5 Sekunden** statt
42 Minuten, alle drei Datenbanken liegen vollständig auf der NAS,
`PRAGMA integrity_check` meldet `ok`, und die Impulszahlen decken sich mit
der laufenden Datenbank.

Übertragbar: **auf ein Netzlaufwerk gehören fertige Dateien, keine offenen
Handles.** Und: ein Prüfwerkzeug, das die Existenz einer Datei mit einer
gültigen Sicherung verwechselt, verlängert die Fehlersuche, statt sie
abzukürzen.

---

## F26 — Der Timeout galt für alle Zähler, obwohl er an der Impulsrate hängt

Gefunden am 27.08.2026 beim Prüfen, ob das Projekt mit fremden Zählern läuft.

`power_timeout_seconds = 180` stand global in `[general]`, hergeleitet für
1.000 Impulse je kWh: bei 1 Wh je Impuls entspricht ein Abstand von 180 s
gerade 20 W. Die Herleitung stand richtig im Kommentar — aber nur als Text
neben einer Zahl, nicht als Rechnung.

Die Impulsrate ist **nicht genormt**. EN 62053-31 legt sie nicht fest, sie ist
Herstellersache; 10, 100, 500, 1.000 und 2.000 Imp/kWh sind alle im Feld. Bei
einem Zähler mit 100 Imp/kWh entsprechen dieselben 180 s nicht mehr 20 W,
sondern **200 W** — eine Wohnung mit Grundlast stünde dauerhaft auf 0 W.

Betroffen war nur die Anzeige. `states()` setzt `power_w` auf 0, die Impulse
werden weiter gespeichert, und die Verteilrechnung liest die Rohimpulse
(`balance.py`, `pulses.get(m, 0) / ppk[m]`). Abrechnung und Jahresbericht
waren nie falsch.

Behoben, indem der Timeout nicht mehr eingetragen, sondern **aus `min_power_w`
je Zähler berechnet** wird:

    power_timeout_seconds = (wh_per_pulse * 3600) / min_power_w

Bei der Vorgabe 1.000 Imp/kWh und 20 W ergibt das exakt die bisherigen 180 s —
die laufende Anlage verhält sich unverändert, geprüft in
`test_vorgaben_entsprechen_der_hausanlage`. Der globale Wert bleibt als
Rückfall für Zähler, die in der Datenbank stehen, aber nicht mehr in der
Konfiguration; ohne ihn stürbe der Statusabruf an einem abgebauten Zähler.

Dasselbe für `max_plausible_power_w`: stand hart auf 60.000 W, hergeleitet aus
dem DSZ15D-3x80A. Bei einem einphasigen 32-A-Zähler ist das zehnmal zu grob.
Jetzt je Zähler einstellbar, Vorgabe unverändert.

Übertragbar, und der eigentliche Befund: **eine Zahl, deren Herleitung im
Kommentar steht, ist eine abgeschriebene Zahl.** Solange nur ein Zählertyp
verbaut war, stimmte sie; sie stimmte aber aus Zufall, nicht aus Konstruktion.
Die Ableitung hinzuschreiben kostet eine Zeile und nimmt die Gelegenheit,
sie falsch zu setzen — dieselbe Bauweise-vor-Regel wie beim Bauteil mit
eigenem Collider.

Und ein zweiter, aus der Gegenprobe: Der erste Anlauf prüfte, ob die
Plausibilitätsgrenze über die 60 ms rutscht, die EN 62053-31 als kürzesten
Impulsabstand zulässt. Die Prüfung war **falsch konstruiert** — sie hätte
einen völlig normalen 100-Imp/kWh-Zähler mit 8 kW abgelehnt und stattdessen
600 kW verlangt. Aufgefallen ist das nur, weil ein Testfall dabei war, der
**grün sein musste**. Die richtige Prüfung ist eine andere: ob `bounce_ms` die
Plausibilitätsgrenze erreicht und die zweite Filterstufe damit verdeckt (siehe
die zwei getrennten Stufen weiter oben). Zu hohe Grenzen sind kein Fehler mehr,
sondern eine Warnung im Log.

Und ein dritter: Die Umstellung in `main.py` ließ sich zurückdrehen, ohne dass
einer von **436 Tests** rot wurde. Geprüft war, dass der Timeout *existiert* —
nicht, dass er *je Zähler* wirkt. `TestTimeoutJeZaehler` in `test_service.py`
schließt das: zwei Zähler mit verschiedenen Raten, derselbe Impulsabstand, und
die Prüfung, dass der grobe nach 300 s noch Leistung meldet, während der feine
längst auf 0 steht. Bei derselben Sabotage fällt der Test jetzt.

---

## Bestätigt korrekt im Upload

- **Offset statt Überschreiben** bei `set-reading`, mit Audit-Log. Genau
  richtig für einen Zähler, dessen geeichter Displaystand maßgeblich bleibt.
- **`journal_mode=WAL` und `synchronous=FULL`** — die passende Kombination,
  wenn jeder einzelne Impuls einen Stromausfall überleben soll.
- **Sicherung über die SQLite-Backup-API** statt Dateikopie.
- **systemd-Härtung** mit `ProtectSystem=strict`, `NoNewPrivileges`,
  `ReadWritePaths`. Sinnvoll und korrekt gesetzt.
- **`time.monotonic()` für Abstände, `time.time()` für die Ablage** — richtig
  getrennt, eine Zeitumstellung verfälscht die Leistungsberechnung nicht.
- **Die Warnung in der README**, dass der Displaystand die
  Abrechnungsgrundlage bleibt und Ausfallzeiten nicht rekonstruierbar sind.
- **Der Hinweis zur galvanischen Trennung** samt Messanweisung vor dem
  Anschluss. Fachlich richtig und an der richtigen Stelle.
