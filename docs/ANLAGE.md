# Bestandsaufnahme der Anlage

Erhoben am 06.08.2026 durch direkte Abfrage. Alles hier ist gemessen, nicht
angenommen.

Die Pläne zur Anlage liegen in `docs/anlage/`: `stromlaufplan.pdf` mit elf
Blättern (Übersicht, zwei Strompfadpläne, Klemmenpläne, zwei Adernlisten,
Betriebsmittel, Versorgung, Einstellungen, Inbetriebnahme, Grenzen), erzeugt
aus `stromlaufplan.html` über `build-pdf.sh`. Geändert wird das HTML, nie das
PDF — dieses wird neu gebaut.

Zwei Werte im Plan sind bewusst offen und **am Gerät zu prüfen**, weil sie
sich aus keinem Datenblatt belegen ließen: die Klemmenbezeichnung des
S0-Ausgangs am DSZ15D (dort als üblicher Anschluss 20/21 geführt) und der
bestückte Vorwiderstand auf der PC817-Baugruppe. Viele dieser Baugruppen sind
für 24 V ausgelegt und tragen rund 2,2 kΩ; bei 5 V fließen dann etwa 1,7 mA,
und der Optokoppler schaltet unter Umständen nicht sicher durch.

## Home Assistant

| | |
|---|---|
| Adresse | `http://192.168.0.30:8123` |
| Version | 2025.10.3 |
| Betrieb | VM auf der Synology DiskStation, über den Virtual Machine Manager |
| Entitäten | 399 |
| Integrationen | 46 |

Erreichbar nur aus dem Heimnetz. Von außerhalb geht es über die NAS
(SSH via Tailscale).

Die Brücke `hassio` auf der NAS selbst (`172.30.32.1/23`) ist **nicht** die
laufende Instanz — sie steht auf `DOWN` und hat keine Mitglieder. Rest eines
früheren Supervised-Versuchs. Home Assistant läuft in der VM.

## Photovoltaik und Speicher

**Es gibt keine SolarEdge-Integration.** Weder in den Entitäten noch unter den
Integrationen findet sich SolarEdge. Sämtliche PV- und Speicherdaten kommen
über die Sonnenbatterie.

Integration `sonnenbatterie`, Gerät `192.168.0.50`, Seriennummer hier als `<seriennummer>`.

### Live-Werte in Watt

```text
sensor.sonnenbatterie_<seriennummer>_state_production            PV-Erzeugung
sensor.sonnenbatterie_<seriennummer>_state_consumption_current   Hausverbrauch
sensor.sonnenbatterie_<seriennummer>_state_consumption_avg       Verbrauch im Mittel
sensor.sonnenbatterie_<seriennummer>_state_grid_in               Netzbezug
sensor.sonnenbatterie_<seriennummer>_state_grid_out              Netzeinspeisung
sensor.sonnenbatterie_<seriennummer>_state_grid_inout            Netz, Vorzeichen zeigt Richtung
sensor.sonnenbatterie_<seriennummer>_state_battery_in            Akku laden
sensor.sonnenbatterie_<seriennummer>_state_battery_out           Akku entladen
sensor.sonnenbatterie_<seriennummer>_state_battery_inout         Akku, Vorzeichen zeigt Richtung
```

### Zählerstände in kWh

```text
sensor.sonnebatterie_produktion        Erzeugung gesamt
sensor.sonnebatterie_hausverbrauch     Hausverbrauch gesamt
sensor.sonnenbatterie_netzexport       eingespeist gesamt
sensor.sonnenbatterie_netzimport       bezogen gesamt
sensor.sonnenbatterie_ladestrom        in den Akku gesamt
sensor.sonnenbatterie_entladestrom     aus dem Akku gesamt
```

Diese sechs sind über die Integration `integration` gebildet, also durch
Aufsummieren der Leistungswerte. Sie sind keine geeichten Zählerstände.

### Zustand

```text
sensor.sonnenbatterie_<seriennummer>_state_battery_percentage_user   Ladestand in Prozent
sensor.sonnenbatterie_<seriennummer>_battery_system_health           Akkuzustand in Prozent
sensor.sonnenbatterie_<seriennummer>_battery_system_cycles           Ladezyklen
sensor.sonnenbatterie_<seriennummer>_state_system_status             ongrid oder offgrid
sensor.sonnenbatterie_<seriennummer>_state_sonnenbatterie            standby und weitere
sensor.sonnenbatterie_<seriennummer>_state_net_frequency             Netzfrequenz in Hz
sensor.sonnenbatterie_<seriennummer>_battery_grid_tmax               Temperatur in °C
```

### Direkte API der Sonnenbatterie

**Das ist die Bilanzquelle des Dienstes** — nicht der Umweg über Home
Assistant. Antwortet ohne Anmeldung im Heimnetz:

```bash
curl http://192.168.0.50/api/v2/status
```

Liefert unter anderem `Production_W`, `Consumption_W`, `GridFeedIn_W`, `USOC`,
`RSOC`, `RemainingCapacity_Wh`, `BatteryCharging`, `BatteryDischarging`.
Genommen wird `USOC`, nicht `RSOC` — Begründung in `BEFUNDE.md`, F21.

Der Pfad `/api/v2/latestdata` antwortet mit 404 — bei diesem Firmwarestand
nicht vorhanden. `sensor.sonnenbatterie_<seriennummer>_write_api` steht auf `False`,
Schreibzugriff ist also nicht freigeschaltet.

> **Ungeprüft:** Hier stand bis zum 22.09.2026 `/api/v1/`, während
> `sources.py` seit jeher `/api/v2/` aufruft. Welche Version die Firmware
> wirklich bedient, ist von außerhalb des Heimnetzes nicht messbar. Die Doku
> folgt jetzt dem Code, weil der produktiv läuft — das ist ein Schluss, keine
> Messung. Am Pi mit `curl` gegen beide Pfade nachfahren und hier
> festschreiben.

## Prognose

```text
sensor.energy_production_today       geschätzte Erzeugung heute in kWh
sensor.energy_production_tomorrow    geschätzte Erzeugung morgen in kWh
sensor.power_production_now          geschätzte Leistung jetzt in W
sensor.power_highest_peak_time_today Zeitpunkt der Tagesspitze
```

Aus der Integration `forecast_solar`. Das sind Schätzwerte, keine Messwerte.

## Kein MQTT-Broker

Port 1883 geprüft und geschlossen auf:

- `192.168.0.30` Home Assistant
- `192.168.0.40` Synology
- `192.168.0.50` Sonnenbatterie

Deshalb bindet der S0-Zähler über die REST-API an. Kommt später ein Broker
dazu — etwa das Mosquitto-Add-on —, ist `[mqtt]` in der Konfiguration der
bessere Weg.

## Was der S0-Zähler ergänzt

Die Sonnenbatterie kennt den Hausverbrauch als eine Zahl. Wie er sich auf **EG,
OG und DG** verteilt, weiß sie nicht. Genau diese Aufteilung liefern die drei
Eltako-Zähler.

Sinnvoller Gegencheck nach der Inbetriebnahme: Die Summe der drei
Wohnungsleistungen mit `sensor.sonnenbatterie_<seriennummer>_state_consumption_current`
vergleichen. Eine bleibende Differenz sind Verbraucher hinter keinem der drei
Zähler — Wärmepumpe, Allgemeinstrom, Ladepunkt.

Woher der Strom stammt, sagen die S0-Impulse nicht. Für eine Aussage wie
"das DG hat heute 4 kWh PV-Strom verbraucht" braucht es zusätzlich die
Netz-, Erzeugungs- und Akkuwerte der Sonnenbatterie.

## Zugang von außen

Über SSH auf die NAS via Tailscale. Von dort aus ist Home Assistant unter
`192.168.0.30:8123` erreichbar.

Fallen, die Zeit kosten:

- `scp` scheitert, die Synology hat kein SFTP-Subsystem. Dateien per
  `ssh host "cat > /pfad" < datei` übertragen.
- Python auf der NAS ist 3.8 und hat kein `tomllib`. Der Code dieses Projekts
  braucht 3.11 oder neuer und läuft auf dem Pi, nicht auf der NAS.
- SSH auf der DiskStation ist normalerweise abgeschaltet und muss in DSM unter
  Systemsteuerung, Terminal und SNMP aktiviert werden.
