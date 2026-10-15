# Stromlaufplan und Betriebsmittelliste

Anlage: Erfassung dreier Wohnungszähler
Erstellt: 06.08.2026 · Geändert: 21.09.2026

Betriebsmittelkennzeichen nach DIN EN 81346-2. Die Pläne liegen als
Diagramm in `plaene/`, diese Datei führt die Daten dazu.

---

## 0. Sicherheitshinweise

### Wer nach diesem Plan arbeiten darf

**Dieser Plan ist die Arbeitsvorgabe für eine Elektrofachkraft, kein
Selbstbau-Leitfaden.** Die dokumentierte Anlage wurde von einer
Elektrofachkraft geplant, ausgeführt und in Betrieb genommen.

| Bereich | Wer |
|---|---|
| Hausanschlusskasten, Vorzählerbereich | **nur der Netzbetreiber** oder ein von ihm beauftragtes Unternehmen — ungezählte Energie, fremde Plomben, kein vorgelagerter Schutz |
| Zählerplatz, Verteilung, Inbetriebsetzung | eingetragenes Installationsunternehmen nach **§ 13, § 14 NAV** |
| Arbeiten an den Zählern, S0-Verdrahtung bis `-X1` | Elektrofachkraft nach **DIN VDE 1000-10** |
| ab `-X2` (Optokoppler-Ausgang zum Pi) | SELV, unkritisch |

DIN VDE 1000-10 Anhang A: Es gibt **keine** für alle Gebiete der
Elektrotechnik ausgebildete Elektrofachkraft — außerhalb des eigenen Gebiets
gilt auch eine Fachkraft als Laie. Eine elektrotechnisch unterwiesene Person
(EuP) darf nur unter Leitung und Aufsicht einer Fachkraft tätig werden.

Maßgeblich sind zusätzlich die Technischen Anschlussbedingungen des
zuständigen Netzbetreibers.

### Technische Hinweise

Arbeiten im Zählerschrank an den Eltako-Zählern betreffen die
Niederspannungs-Hauptverteilung. Der S0-Ausgang selbst ist ein potentialfreier
Kontakt und liegt im SELV-Bereich — der Weg dorthin führt aber durch den
Zählerschrank.

- Vor Arbeiten am Zähler: Freischalten, Sichern, Spannungsfreiheit feststellen,
  Erden und Kurzschließen, benachbarte Teile abdecken.
- Die S0-Leitungen sind **getrennt von netzspannungsführenden Leitungen** zu
  verlegen. Bei gemeinsamer Verlegung ist die Isolation der S0-Leitung für die
  höchste vorkommende Spannung auszulegen.
- Die beiden Spannungskreise dieser Anlage sind galvanisch getrennt. Diese
  Trennung darf nicht durch eine gemeinsame GND-Verbindung aufgehoben werden —
  sie ist der Grund, warum ein Fehler im Zählerschrank nicht auf den Raspberry
  Pi durchschlägt. **Beide Netzteile liefern 5 V**, was zum Zusammenlegen der
  Minusklemmen verleitet. Getrennte, beschriftete Klemmen verwenden.

---

## 1. Spannungsversorgungen

Zwei getrennte Kreise, beide 5 V DC. Die Trennung ist Absicht, nicht Zufall.

| Kennz. | Kreis | Spannung | Speist | Trennung |
|---|---|---|---|---|
| **G1** | Pi | 5 V DC, 5 A | A1 (Raspberry Pi) | SELV, getrennt von G2 |
| **G2** | S0-Schleife | 5 V DC, 5 A | Stromschleife durch B1..B3 und die Optokoppler-Eingänge | SELV, **muss** von G1 getrennt bleiben |

Zwei Netzteile genügen der Anlage: eines für G1, eines für G2.

### G1 — Raspberry Pi

| | |
|---|---|
| Typ | Netzteil 5 V DC, vorhanden: **5 A** |
| Strom | mindestens 3 A |
| Begründung | Der Pi 3B zieht laut Herstellerangabe bis 2,5 A. |
| Anschluss A1 | **Micro-USB.** Ein konfektioniertes Micro-USB-Kabel wird am Netzteil aufgelegt (siehe unten) und in den Pi gesteckt. Nur über diese Buchse wirkt die interne Polyfuse des Pi; über die GPIO-Leiste eingespeist ist sie umgangen und es gibt keinen Verpolungsschutz. |

> **Das Micro-USB-Kabel ist die schwächste Stelle von G1.** Es trägt den
> gesamten Strom des Pi über eine Steckverbindung, die dafür nie ausgelegt
> war. Zwei Punkte:
>
> 1. **Ein Ladekabel genügt nicht.** Dünne Adern (oft 0,08..0,13 mm²) fallen
>    bei 2,5 A um mehrere Zehntel Volt ab, und der Pi 3B meldet das als
>    Unterspannungs-Blitz. Kabel mit mindestens **0,35 mm²** (AWG 22) in den
>    Versorgungsadern verwenden und **kurz halten**.
> 2. **Das offene Ende wird auf die Netzteilklemmen aufgelegt**, nicht in eine
>    USB-Buchse gesteckt. Ist das gekaufte Kabel beidseitig konfektioniert,
>    wird das USB-A-Ende abgeschnitten. Dann gilt: **rot ist +5 V, schwarz
>    ist GND**, die Datenadern (grün, weiß) bleiben unbeschaltet und werden
>    einzeln isoliert. Die Farbzuordnung ist **vor dem Auflegen
>    durchzuklingeln** — Billigkabel halten sich nicht immer daran, und eine
>    Verpolung zerstört den Pi.

### G2 — S0-Stromschleife

| | |
|---|---|
| Typ | Hutschienennetzteil 5 V DC, SELV, vorhanden: **5 A** |
| Strom | mindestens 100 mA, ausgeführt mit 5 A |
| Begründung | Drei S0-Kanäle, je nach Optokoppler-Vorwiderstand rund 5..15 mA im aktiven Zustand. Der S0-Ausgang des DSZ15D lässt laut Eltako maximal 20 mA zu. Bei drei gleichzeitig aktiven Kanälen also höchstens 60 mA. |
| Wichtig | **Nicht** aus G1 speisen. Eine gemeinsame Versorgung hebt die galvanische Trennung zwischen Zählerschrank und Pi auf und macht den Optokoppler wirkungslos. |
| Sicherung | **F1 ist Pflicht, nicht optional.** Bei 5 A Netzteilleistung gegenüber 60 mA Last begrenzt das Netzteil im Fehlerfall praktisch nicht; die S0-Verdrahtung wird allein durch F1 geschützt. |

---

## 2. Betriebsmittelliste

| Kennz. | Bezeichnung | Typ | Anmerkung |
|---|---|---|---|
| **A1** | Rechnereinheit | Raspberry Pi 3B | Raspberry Pi OS Lite 64-bit (Trixie) |
| **A2** | Optokopplerbaugruppe | JZK PC817, 4 Kanal | Eingang 3,6..24 V, Kanal 4 unbenutzt |
| **B1** | Wirkverbrauchszähler EG | Eltako DSZ15D-3x80A | 1000 Imp/kWh, geeicht |
| **B2** | Wirkverbrauchszähler OG | Eltako DSZ15D-3x80A | 1000 Imp/kWh, geeicht |
| **B3** | Wirkverbrauchszähler DG | Eltako DSZ15D-3x80A | 1000 Imp/kWh, geeicht |
| **G1** | Netzteil Rechner | 5 V DC, 5 A | speist A1 über W7 |
| **G2** | Netzteil S0-Kreis | 5 V DC, 5 A, SELV | nicht aus G1 speisen, 0 V getrennt halten |
| **F1** | Sicherung G2 | 100 mA träge | **Pflicht** — G2 begrenzt bei 5 A nicht |
| **W7** | Versorgungskabel A1 | Micro-USB, ≥ 0,35 mm² | offenes Ende auf Netzteilklemmen, Datenadern isoliert |

### Leitungen zwischen den Orten

| Kennz. | Von | Nach | Typ | Adern |
|---|---|---|---|---|
| **W1** | +ZS Zähler B1..B3 | +HWR X1 | **Cat-Verlegekabel, starr** | 3 Paare belegt, 1 Paar Reserve |

### Ergänzende Geräte im Netzwerk (nicht Teil dieser Anlage)

| Kennz. | Gerät | Adresse | Rolle |
|---|---|---|---|
| **K1** | Sonnenbatterie | 192.168.0.50 | liefert PV, Netz und Akku über HTTP |
| **K2** | Home Assistant | 192.168.0.30:8123 | Zweitquelle, Visualisierung |

---

## 3. Klemmenplan

### -X1 — S0-Eingangsseite (Zählerschrank)

Kreis G2, 5 V DC SELV. Der S0-Ausgang des Zählers wirkt als Schalter in
der Stromschleife.

| Klemme | Ader | Von | Nach | Paar in W1 |
|---|---|---|---|---|
| X1:1 | +5 V | G2 (+) | B1 S0+ | Paar 1 · orange/weiß |
| X1:2 | Rückführung | B1 S0− | A2 IN1+ | Paar 1 · orange |
| X1:3 | +5 V | G2 (+) | B2 S0+ | Paar 2 · grün/weiß |
| X1:4 | Rückführung | B2 S0− | A2 IN2+ | Paar 2 · grün |
| X1:5 | +5 V | G2 (+) | B3 S0+ | Paar 3 · braun/weiß |
| X1:6 | Rückführung | B3 S0− | A2 IN3+ | Paar 3 · braun |
| X1:7 | 0 V | G2 (−) | A2 IN1−, IN2−, IN3− | — (Schrankverdrahtung) |

Hat die Optokopplerbaugruppe je Kanal nur einen Eingang `INx` und einen
gemeinsamen Eingangs-GND, entfällt X1:7 als Sammelklemme und die
Rückführungen gehen direkt auf `IN1..IN3`, der 0-V-Zweig auf den
gemeinsamen Eingangs-GND.

**Leitung W1 — Netzwerk-Verlegekabel.** Ausgeführt als handelsübliches
Cat-Verlegekabel (Cat 6 oder besser, U/UTP oder F/UTP), Volldraht, vom neuen
Schrank (+HWR) zum Zählerschrank (+ZS). Länge unter 20 m. Das ist elektrisch
eine paarig verseilte Steuerleitung und für S0 vollkommen ausreichend: der
S0-Ausgang ist ein potentialfreier Optokoppler-Kontakt, der Strom liegt bei
5..15 mA je Kanal.

| | |
|---|---|
| Querschnitt | AWG 23/24, entspricht 0,25 bzw. 0,20 mm² |
| Belegung | **Ein Paar je Zähler** — S0+ und S0− desselben Zählers gehören ins **selbe** Paar |
| Reserve | Paar 4 (blau/blau-weiß) bleibt frei, an beiden Enden isoliert |
| Schirm | bei F/UTP **einseitig** auf der Seite von G2 auflegen, am Zähler frei und isoliert |
| Anschluss | **auf Klemmen**, keine RJ45-Stecker |
| Verlegung | getrennt von Netzleitungen, nicht parallel im selben Kanal |

> **Die Paarzuordnung ist der Punkt, an dem man dieses Kabel falsch macht.**
> Hin- und Rückleiter desselben Kanals müssen im selben verdrillten Paar
> laufen, sonst spannt jeder Kanal eine Schleifenfläche mit einem fremden
> Leiter auf und die Verdrillung nützt nichts. Wer stattdessen „alle drei
> Plus in ein Paar, alle drei Rückführungen ins nächste" legt, hat ein
> ungeschirmtes Parallelkabel gebaut. **Nicht nach Farbe raten, sondern nach
> Paar:** die vier Paare eines Cat-Kabels sind orange, grün, braun und blau,
> jeweils volle Farbe plus die zugehörige weiß-gestreifte Ader.

> **Keine RJ45-Stecker, kein Patchfeld.** Beide Enden gehen auf Reihen- oder
> Federklemmen. RJ45-Kontakte sind für einen dauerhaft anstehenden
> Gleichstrom im Schrank nicht gedacht, und ein Steckverbinder mitten in der
> Strecke ist eine Fehlerquelle, die kein Klemmenplan abbildet.

> **Volldraht, nicht Litze — auf die Ader kommt es an, nicht auf den
> Kabelnamen.** Entscheidend ist die Aderart, nicht ob „Verlegekabel" oder
> „Patchkabel" auf dem Mantel steht: Patchkabel gibt es in beiden
> Ausführungen. Starre Adern halten in Federklemmen und brauchen keine
> Hülsen. Ist das Kabel **feindrähtig (Litze)**, sind **Aderendhülsen
> zwingend** — eine aufgesplissene Litze in einer Federklemme ist ein
> Wackelkontakt, der sich als sporadisch fehlende Impulse zeigt und
> praktisch nicht zu finden ist.
>
> **Ausgeführt ist W1 als Patchkabel mit starren Adern**, ohne Hülsen
> (Stand 24.08.2026). Das ist zulässig, weil der Grund für die Hülsen die
> Litze ist und nicht die Bauform des Kabels. **Im Zweifel eine Ader
> aufdrehen und ansehen**, bevor geklemmt wird.

> **Das Kabel führt keine Netzwerkdaten.** W1 ist ausschließlich die
> S0-Verdrahtung. Es darf nicht zusätzlich als Ethernet-Leitung genutzt oder
> in ein Patchfeld gesteckt werden, und aus einem bereits als LAN genutzten
> Kabel dürfen keine Adern für S0 abgezweigt werden. **Beide Enden mit W1
> beschriften**, damit es niemand später für ein freies Netzwerkkabel hält.

### -X2 — Optokoppler-Ausgangsseite zum Pi

Kreis G1, 3,3 V Logik. Der Transistorausgang zieht während des Impulses
gegen GND — Auswertung also aktiv LOW.

| Klemme | Von A2 | Nach A1 (Pin) | BCM | Zähler |
|---|---|---|---|---|
| X2:1 | OUT1 | Pin 11 | GPIO17 | B1, EG |
| X2:2 | OUT2 | Pin 13 | GPIO27 | B2, OG |
| X2:3 | OUT3 | Pin 15 | GPIO22 | B3, DG |
| X2:4 | Ausgangs-GND | Pin 6 | GND | — |
| X2:5 | Ausgangs-VCC | Pin 1 | 3,3 V | nur falls die Baugruppe das fordert |

> **Pin 6 ist nur ein Vorschlag.** Der Pi hat acht GND-Pins — 6, 9, 14, 20,
> 25, 30, 34 und 39 — die intern alle auf demselben Potential liegen. Pin 6
> steht hier, weil er neben Pin 11/13/15 liegt und die Verdrahtung kurz
> hält. **Ausgeführt ist X2:4 auf Pin 9** (Stand 24.08.2026), elektrisch
> gleichwertig.

> **🔴 GND zuerst, Signalleitungen danach.** Wird eine Signalader vor X2:4
> aufgelegt, sucht sich der Rückstrom seinen Weg über die Schutzdioden des
> GPIO-Eingangs. Bei den Strömen dieser Anlage überlebt der Pi das in der
> Regel, aber es ist ein vermeidbares Risiko.
>
> **Sicherer und deshalb der empfohlene Weg: den Pi stromlos machen**, X2
> vollständig auflegen, erst dann einschalten. Dann ist die Reihenfolge
> gleichgültig. Das entspricht Schritt 13 der Prüfliste — dort wird der Pi
> ausdrücklich erst *nach* dem Auflegen von X2 gestartet.

> **X2:5 nur anschließen, wenn die Baugruppe einen Ausgangs-VCC benötigt.**
> Bei reinem Open-Collector-Ausgang genügt der interne Pull-up des GPIO,
> der in der Software über `pull_up=True` aktiviert wird.
> **Ausgeführt ist X2:5 unbelegt** (Stand 24.08.2026) — der Pull-up kommt
> damit aus dem Pi, und an OUT kann konstruktiv keine höhere Spannung als
> 3,3 V anstehen.
>
> **Niemals 5 V, 12 V oder 24 V an einen GPIO.** Vor dem Anschluss ist die
> Spannung an OUT1..OUT3 gegen Pi-GND zu messen; sie darf 3,3 V nicht
> überschreiten.
>
> **🔴 Diese Messung ist wertlos, solange X2 offen ist.** Ein
> Open-Collector-Ausgang ohne Pull-up hängt in der Luft und zeigt 0 V —
> unabhängig davon, woran die Baugruppe intern liegt. Wer bei offenen
> Klemmen misst und „keine Spannung" notiert, hat nichts geprüft. Aussage
> hat die Messung nur mit angeschlossenem Pull-up: entweder gegen ein
> extern aufgelegtes X2:5, oder — bei unbelegtem X2:5 — gar nicht, weil
> dann der Pi selbst die einzige Quelle ist und 3,3 V nicht überschritten
> werden können.

---

## 4. Prüfschritte vor der Inbetriebnahme

Reihenfolge einhalten. Jeder Schritt setzt den vorigen voraus.

**4.1 Leitung W1 vor dem Auflegen, beide Enden frei**
1. Paare bestimmen: je Paar die volle Farbe und die zugehörige
   weiß-gestreifte Ader durchklingeln. Erwartung: **Durchgang nur innerhalb
   eines Paares.**
2. Adern gegeneinander und gegen Schirm prüfen.
   Erwartung: **kein Schluss zwischen den Paaren**, kein Schluss gegen Schirm.
3. Beide Enden mit **W1** und der Aderkennzeichnung beschriften.
   Damit wird das Kabel später nicht für ein freies Netzwerkkabel gehalten.

**4.2 Optokopplerbaugruppe, spannungslos**
4. Eingangs- und Ausgangsseite am Platinenaufdruck bestimmen.
5. Widerstand zwischen Eingangs-GND und Ausgangs-GND messen.
   Erwartung: **hochohmig**. Ein niederohmiger Wert bedeutet, dass die
   Baugruppe keine galvanische Trennung bietet — dann nicht verwenden.

**4.3 Optokoppler mit Versorgung, ohne Pi**
6. G2 anschließen, einen Kanal am Eingang aktivieren.
7. Spannung an OUTx gegen Ausgangs-GND messen.
   Erwartung: **maximal 3,3 V**. Höhere Werte zerstören den GPIO.

   > **🔴 Nur aussagekräftig, wenn ein Pull-up anliegt.** Ohne Pull-up ist
   > der Open-Collector-Ausgang offen und zeigt 0 V, gleich woran die
   > Baugruppe intern liegt — die Messung kann dann gar nicht durchfallen.
   > **Bleibt X2:5 unbelegt, entfällt dieser Schritt:** der Pi liefert den
   > Pull-up selbst, mehr als 3,3 V sind konstruktiv ausgeschlossen. **Wird
   > X2:5 aufgelegt, ist zwingend hier zu messen** — mit angeschlossenem
   > VCC, aber noch ohne Pi.
8. Eingangsstrom bei 5 V messen.
   Erwartung: **unter 20 mA** je Kanal (Grenzwert des S0-Ausgangs).

**4.4 Versorgung G1, bevor der Pi angesteckt wird**
9. Micro-USB-Kabel W7 auflegen: **rot und schwarz durchklingeln**, bevor sie
   auf die Netzteilklemmen kommen. Datenadern einzeln isolieren.
   Erwartung: rot auf +5 V, schwarz auf GND. Eine Verpolung zerstört den Pi.
10. G1 einschalten, Spannung am **Micro-USB-Stecker** messen — Pi noch nicht
    angesteckt. Erwartung: **4,75..5,25 V**, richtige Polung.
11. Widerstand zwischen 0 V von G1 und X1:7 (0 V von G2) messen,
    beide Netzteile aus. Erwartung: **hochohmig.** Ein niederohmiger Wert
    heißt, dass die beiden 0-V-Zweige irgendwo gebrückt sind — dann ist die
    Trennung aufgehoben und der Fehler zu suchen, bevor es weitergeht.

**4.5 Zusammenbau**
12. Pi über W7 anstecken, X2 auflegen, Pi starten.

    > **🔴 In dieser Reihenfolge, und der Pi ist dabei stromlos.** Wird X2
    > am laufenden Pi aufgelegt, läuft der Rückstrom jeder Signalader, die
    > vor X2:4 klemmt, über die Schutzdioden des GPIO. Lässt sich der Pi
    > nicht abschalten, dann **X2:4 (GND) zuerst**, die Signaladern danach.
13. `test_gpio.py` laufen lassen, jeden Kanal einzeln aktivieren.
    Erwartung: **genau ein Ereignis je Aktivierung.**
14. Auf Unterspannungsmeldungen prüfen:
    `vcgencmd get_throttled` — Erwartung: `throttled=0x0`. Ein gesetztes
    Bit 0 oder 16 heißt Unterspannung; dann ist W7 zu dünn oder zu lang.
15. `s0ctl status` aufrufen.
    Erwartung: alle drei Kanäle werden geführt, Impulse laufen auf.

**4.6 Abgleich**
16. Zählerstände aller drei Zähler ablesen und fotografieren.
17. `s0ctl set-reading <kanal> <stand> --force` für jeden Zähler.
18. Nach 24 Stunden Zählerstände erneut ablesen und mit
    `s0ctl status` vergleichen. Die Differenzen müssen im Rahmen der
    Ableseauflösung übereinstimmen.

---

## 5. Optionales Anzeigepanel

Die Software unterstützt zusätzlich ein lokales Anzeigepanel an einem
seriellen EA-eDIP-Display (Modul `s0counter/panel.py`, Konfigurationsblock
`[panel]`, standardmäßig deaktiviert). Diese Erweiterung ist **nicht Teil**
des oben beschriebenen Stromlaufplans und wird hier bewusst nicht mit
Betriebsmittelkennzeichen, eigenem Spannungskreis und Klemmenplan geführt —
wer sie aufbaut, plant Spannungsversorgung, Trennung zu G2 und Verkabelung
für die konkrete Displayvariante eigenständig und dokumentiert sie in einer
eigenen Anlagendokumentation.

Siehe `README.md`, Abschnitt „Anzeigepanel (optional)", für die
softwareseitige Einrichtung.

---

## 6. Was diese Anlage nicht leistet

- **Sie ist nicht eichfähig.** Abrechnungsgrundlage bleibt der Zählerstand
  der geeichten Zähler. Die Erfassung dient der unterjährigen Auswertung und
  der Aufteilung.
- **Ausfallzeiten sind nicht rekonstruierbar.** Fällt Pi, Baugruppe oder
  Versorgung aus, fehlen die Impulse dieser Zeit dauerhaft. Die Differenz
  zeigt sich beim nächsten Abgleich mit dem Zählerstand.
- **Die Herkunft des Stroms wird nicht gemessen.** Ein S0-Impuls sagt, dass
  eine Wattstunde verbraucht wurde, nicht ob sie aus Netz, PV oder Speicher
  kam. Die Zuordnung im Jahresbericht ist ein Verteilschlüssel, kein
  physikalischer Nachweis.
