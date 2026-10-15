/* Verteilung des momentanen Netzbezugs auf die Wohnungen.
 *
 * Eigene Datei ohne jeden Zugriff auf `document`, `localStorage` oder
 * `fetch` — damit ein Test die Funktionen AUFRUFEN kann statt sie
 * nachzubauen. Ein Test, der die Prüflogik selbst enthält, prüft nur seine
 * eigene Kopie; belegt am 26.08.2026 an anderer Stelle mit acht Tests, die
 * eine sabotierte Funktion nicht bemerkten.
 *
 * Inhaltlich sind das dieselben zwei Modelle wie in balance.py, nur in Watt
 * statt in Kilowattstunden. Der Wert ist eine MOMENTAUFNAHME und nicht die
 * Abrechnung: die Abrechnung summiert Intervall für Intervall über den
 * ganzen Zeitraum, hier steht der Stand von jetzt.
 */

/** Anteilig nach Verbrauch — Gegenstück zu share_proportional().
 *
 * @param {Array<[string, number]>} leistungen  je Zähler die Momentanleistung in W
 * @param {number} netzW  aktueller Netzbezug des Hauses in W
 * @returns {Map<string, number>}
 */
export function anteileProportional(leistungen, netzW) {
  const summe = leistungen.reduce((s, [, w]) => s + w, 0);
  if (summe <= 0) return new Map(leistungen.map(([id]) => [id, 0]));
  // Der Deckel wie in balance.py: keiner Wohnung darf mehr Netzbezug
  // zugerechnet werden, als sie überhaupt verbraucht. Läuft ein Verbraucher
  // hinter keinem Zähler mit (Wärmepumpe, Allgemeinstrom), übersteigt der
  // Hausbezug sonst die Wohnungssumme.
  const verteilbar = Math.min(netzW, summe);
  return new Map(leistungen.map(([id, w]) => [id, verteilbar * w / summe]));
}

/** Zu gleichen Teilen mit Weitergabe — Gegenstück zu share_waterfall().
 *
 * Wer mehr zugeteilt bekäme, als er verbraucht, gibt den Überschuss ab; er
 * wird unter den übrigen erneut gleichmäßig verteilt.
 */
export function anteileWasserfall(leistungen, netzW) {
  const ergebnis = new Map(leistungen.map(([id]) => [id, 0]));
  const grenze = new Map(leistungen);
  let offen = new Set(leistungen.filter(([, w]) => w > 0).map(([id]) => id));
  let rest = netzW;

  while (rest > 1e-9 && offen.size > 0) {
    const anteil = rest / offen.size;
    let verteilt = 0;
    const gedeckelt = new Set();

    for (const id of offen) {
      const luft = grenze.get(id) - ergebnis.get(id);
      const nimmt = Math.min(anteil, luft);
      ergebnis.set(id, ergebnis.get(id) + nimmt);
      verteilt += nimmt;
      // Die Toleranz gegen Fließkommaeffekte, wie in balance.py: ohne sie
      // gilt eine Wohnung als gedeckelt, die rechnerisch genau aufgeht.
      if (nimmt < anteil - 1e-9) gedeckelt.add(id);
    }

    rest -= verteilt;
    // Kein zusätzlicher Ausgang bei `gedeckelt.size === 0`: konnten alle den
    // vollen Anteil nehmen, ist `verteilt === rest` und die Schleife endet
    // über die Kopfbedingung. Ein solcher `break` sähe wie eine Absicherung
    // aus, wäre aber unerreichbar — geprüft am 26.08.2026 mit einer
    // Sabotage, die grün blieb. Die Schleife endet in jedem Fall: entweder
    // ist `rest` aufgebraucht, oder mindestens eine Wohnung wird gedeckelt
    // und `offen` schrumpft.
    offen = new Set([...offen].filter((id) => !gedeckelt.has(id)));
  }
  return ergebnis;
}

export const VERTEILMODELLE = {
  proportional: anteileProportional,
  waterfall: anteileWasserfall,
};

/** Netzanteil je Zähler in Watt — oder null, wenn nicht berechenbar.
 *
 * null heißt „nicht gemessen", nicht „null Watt". Der Unterschied ist der
 * ganze Zweck der Anzeige: ohne Hausbilanz weiß niemand, wieviel vom Strom
 * gerade aus dem Netz kommt, und eine 0 an dieser Stelle wäre eine
 * Behauptung, die nicht gedeckt ist.
 *
 * @param {object} daten  die Antwort des Dienstes
 * @param {Array<object>} eintraege  Zähler in Anzeigereihenfolge
 * @param {string|null} modellName  gewähltes Verteilmodell
 * @param {number} hoechstalter  ab welchem Alter der Messpunkt verfällt (s)
 */
export function netzanteile(daten, eintraege, modellName, hoechstalter) {
  const letzte = daten.balance?.latest;
  if (!daten.balance?.enabled || !letzte) return null;

  // Ein alter Messpunkt beschreibt nicht mehr das Jetzt. Die Bilanz misst
  // alle 60 s; ein Wert von vor einer Stunde als „aktuell" auszugeben wäre
  // schlimmer als keine Angabe.
  const alterBilanz = Math.max(0, daten.time - letzte.ts);
  if (!Number.isFinite(alterBilanz) || alterBilanz > hoechstalter) return null;

  // grid_w ist auf „Bezug positiv" gedreht (siehe sources.py). Bei
  // Einspeisung ist er negativ und der Bezug damit 0 — genau der Fall, in
  // dem 0 W in der Klammer stehen soll.
  //
  // Das `Math.max(0, …)` steht bewusst nur AN EINER Stelle. Zweimal
  // abgesichert wäre keine doppelte Sicherheit, sondern eine Zeile, die
  // sich abschalten lässt, ohne dass ein Test rot wird — belegt am
  // 26.08.2026: mit beiden Absicherungen blieb jede Einzel-Sabotage grün.
  const bezug = Math.max(0, letzte.grid_import_w ?? letzte.grid_w ?? 0);
  const leistungen = eintraege.map((m) => [m.id, Math.max(0, m.power_w ?? 0)]);

  const modell = VERTEILMODELLE[modellName] ?? anteileProportional;
  return modell(leistungen, bezug);
}
