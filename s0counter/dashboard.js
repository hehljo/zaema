/* Live-Anzeige der Zählerstände.
 *
 * Holt ausschließlich `/status` vom eigenen Dienst — keine fremden Server,
 * keine Bibliothek, kein Build. Was sich ändern kann, steht in `config`;
 * jeder sichtbare Text kommt aus `text`.
 */

import { diagramm, kennzahlen } from './dashboard-chart.js';
import { netzanteile } from './dashboard-verteilung.js';

const config = {
  titel: 'Stromzähler',
  quelle: './status',
  verlaufQuelle: './history',
  intervallMs: 5000,
  // Wählbare Zeiträume. Die Beschriftung steht in `text.zeitraeume`.
  zeitraeume: [
    { id: '1h', sekunden: 3600 },
    { id: '12h', sekunden: 12 * 3600 },
    { id: '24h', sekunden: 24 * 3600 },
    { id: '7d', sekunden: 7 * 86400 },
    { id: '30d', sekunden: 30 * 86400 },
  ],
  zeitraumStandard: '12h',
  zeitraumSchluessel: 's0.zeitraum',
  offenSchluessel: 's0.offen',
  // Ein geöffnetes Diagramm wird seltener nachgeladen als die Kacheln: die
  // Kurve ändert sich in fünf Sekunden nicht sichtbar, und jede Anfrage
  // liest auf dem Pi Hunderte Zeilen.
  verlaufIntervallMs: 60000,
  // Nach so langer Zeit ohne Impuls gilt ein Kanal als still. Der Dienst
  // rechnet mit power_timeout_seconds (Vorgabe 180 s) genauso.
  stillNachSekunden: 180,
  // Ab dieser Abweichung zwischen Haus und Wohnungssumme wird gewarnt.
  // Nicht null: ein Rest gehört immer zum Allgemeinstrom.
  restWarnungWatt: 3000,
  // Ein negativer Rest ist im Live-Bild normal und noch kein Befund: die
  // Wohnungsleistung folgt dem Impulsabstand und reagiert sofort, während
  // die Hausbilanz nur alle sample_interval_seconds (60 s) neu misst. Eine
  // kurze Lastspitze erscheint deshalb erst bei den Wohnungen. Gewarnt wird
  // erst, wenn die Abweichung größer ist, als ein Messversatz erklären kann.
  restNegativToleranzWatt: 1500,
  // Darunter wird der Langzeitanteil als noch nicht belastbar gekennzeichnet.
  // Eine Stunde deckt genug Lastwechsel ab, dass einzelne Spitzen nicht mehr
  // durchschlagen.
  anteilVerlaesslichAbSekunden: 3600,
  // Ab diesem Alter beschreibt der letzte Bilanzmesspunkt nicht mehr das
  // Jetzt, und der Netzanteil in der Wohnungskachel entfällt statt eine
  // veraltete Zahl als aktuell auszugeben. Die Bilanz misst alle 60 s
  // (sample_interval_seconds); 300 s decken vier ausgefallene Läufe ab, ohne
  // dass ein wirklich alter Wert durchgeht. Bewusst kürzer als
  // stillNachSekunden nicht — hier zählt der Takt der Bilanz, nicht der der
  // Impulse.
  bilanzHoechstalterSekunden: 300,
  themaSchluessel: 's0.thema',
  // Welche Abschnitte aufgeklappt sind. Fehlt der Eintrag, ist alles zu —
  // das ist der Startzustand, nicht ein Fehlerfall.
  abschnitteSchluessel: 's0.abschnitte',
  // Die Abrechnung rechnet über lange Zeiträume und ändert sich in
  // Sekunden nicht sichtbar. Ein eigener, langsamer Takt — der Dienst hält
  // das Ergebnis ohnehin zwischen, solange kein Impuls dazukommt.
  abrechnungIntervallMs: 120000,
  abrechnungZeitraumSchluessel: 's0.abrechnung.zeitraum',
  abrechnungModellSchluessel: 's0.abrechnung.modell',
  nachkommaKwh: 3,
  // Reihenfolge der Wohnungen von unten nach oben. Alphabetisch wäre
  // dg, eg, og — für Stockwerke sinnlos. Zähler, die hier nicht stehen,
  // hängen alphabetisch hinten an, statt zu verschwinden.
  reihenfolge: ['eg', 'og', 'dg'],
  // Schwellen für die Rechnerwerte. Die Temperaturgrenzen kommen aus dem
  // Dienst (system.py), weil sie zur Hardware gehören und nicht zur Anzeige.
  ramWarnProzent: 90,
  plattenWarnProzent: 85,
};

const text = {
  wohnungen: 'Wohnungen',
  wohnungenQuelle: 'S0-Impulse',
  haus: 'Hausbilanz',
  themaHell: 'Hell',
  themaDunkel: 'Dunkel',
  einheitWatt: 'W',
  einheitKwh: 'kWh',
  stand: 'Stand',
  zaehlerstand: 'Zählerstand',
  letzterImpuls: 'letzter Impuls',
  impulse: 'Impulse',
  nie: 'noch keiner',
  aktiv: 'liefert Impulse',
  still: 'seit über drei Minuten still',
  pv: 'Erzeugung',
  verbrauchHaus: 'Hausverbrauch',
  netzbezug: 'Netzbezug',
  netzeinspeisung: 'Einspeisung',
  netzKurz: 'Netz',
  netzKlammerHilfe: 'Anteil dieser Wohnung am aktuellen Netzbezug des '
                  + 'Hauses, nach dem gewählten Verteilmodell. Der Rest '
                  + 'kommt aus PV und Speicher.',
  netzKlammerNull: 'Gerade nichts aus dem Netz — der Verbrauch dieser '
                 + 'Wohnung ist vollständig durch PV und Speicher gedeckt.',
  akku: 'Akku',
  akkuLaden: 'lädt',
  akkuEntladen: 'entlädt',
  akkuRuhe: 'in Ruhe',
  ladestand: 'Ladestand',
  rest: 'Nicht erfasst',
  restHilfe: 'Hausverbrauch minus Wohnungen — Allgemeinstrom, Heizung, Technik',
  summeWohnungen: 'Summe Wohnungen',
  anteil: 'Anteil über die Laufzeit',
  anteilHilfe: 'Aus Energie gerechnet, nicht aus Momentanleistung — '
             + 'der Wert wird mit jeder Stunde belastbarer.',
  gemessen: 'gemessen',
  hausEnergie: 'Haus',
  wohnungenEnergie: 'Wohnungen',
  anteilJung: 'Noch wenig Daten — erst ab etwa einer Stunde aussagekräftig.',
  system: 'Rechner',
  systemQuelle: 'Raspberry Pi',
  fassungBuild: 'Fingerabdruck',
  fassungStand: 'eingespielt',
  temperatur: 'Temperatur',
  tempWarm: 'wird warm, drosselt bald',
  tempHeiss: 'zu heiß',
  tempKuehl: 'unauffällig',
  cpuLast: 'Auslastung',
  kerne: 'Kerne',
  ramTitel: 'Arbeitsspeicher',
  belegt: 'belegt',
  frei: 'verfügbar',
  swap: 'Auslagerung',
  ablage: 'Speicherkarte',
  betriebsdauer: 'Betriebsdauer',
  seitNeustart: 'seit dem letzten Neustart',
  drosselung: 'Drosselung',
  drosselJetzt: 'Der Rechner drosselt gerade — Stromversorgung prüfen.',
  drosselFrueher: 'Seit dem Start ist Unterspannung oder Drosselung aufgetreten.',
  drosselUnbekannt: 'nicht messbar',
  backup: 'Letzte Sicherung',
  backupLetzte: 'gesichert am',
  backupStaende: 'Stände',
  backupOrt: 'Ablage',
  backupExtern: 'Netzlaufwerk',
  backupLokal: 'auf der Speicherkarte',
  backupVeraltet: 'Seit über zwei Tagen wurde nicht gesichert.',
  backupNurLokal: 'Die Sicherung liegt auf derselben Speicherkarte wie die '
                + 'Daten — sie schützt nicht gegen deren Ausfall.',
  ramVoll: 'Arbeitsspeicher wird knapp.',
  platteVoll: 'Speicherkarte wird voll — die Datenbank wächst weiter.',
  verlaufZeigen: 'Verlauf anzeigen',
  verlaufVerbergen: 'Aktuellen Wert anzeigen',
  verlaufLaedt: 'Verlauf wird geladen …',
  verlaufLeer: 'Für diesen Zeitraum liegen noch keine Daten vor.',
  verlaufFehler: 'Verlauf konnte nicht geladen werden.',
  kleinstwert: 'min',
  groesstwert: 'max',
  mittelwert: 'Ø',
  zeitraeume: {
    '1h': '1 h', '12h': '12 h', '24h': '24 h', '7d': '7 Tage', '30d': '30 Tage',
  },
  // --- Abrechnung ---
  abrechnung: 'Abrechnung',
  abrechnungQuelle: 'S0-Impulse gegen Hausbilanz',
  abrechnungVerbrauch: 'Verbrauch',
  abrechnungSolar: 'aus PV und Speicher',
  abrechnungNetz: 'aus dem Netz',
  abrechnungSolarAnteil: 'PV-Anteil',
  abrechnungLaedt: 'Abrechnung wird gerechnet …',
  abrechnungFehler: 'Abrechnung konnte nicht geladen werden.',
  abrechnungLeer: 'Für diesen Zeitraum liegen noch keine Daten vor.',
  // --- Erklärung der Verteilmodelle ---
  //
  // Welches Modell gilt, ist eine Entscheidung über Fairness, keine über
  // Technik. Im Dashboard stand bisher nur der Name — wer die Wahl treffen
  // soll, konnte nicht wissen, was sie bedeutet.
  modellErklaerung: {
    proportional: {
      bedeutung: 'Jede Wohnung deckt denselben Prozentsatz ihres Verbrauchs '
               + 'aus dem Netz. Wer 40 % des Verbrauchs hatte, trägt 40 % '
               + 'des Netzbezugs.',
      rechnung: 'Netzbezug × (Verbrauch der Wohnung ÷ Verbrauch aller). '
              + 'Je Viertelstunde einzeln, danach summiert — deshalb zählt, '
              + 'wann verbraucht wurde, nicht nur wie viel.',
    },
    waterfall: {
      bedeutung: 'Der Netzbezug wird zu gleichen Teilen aufgeteilt. Wer mehr '
               + 'zugeteilt bekäme, als er verbraucht hat, gibt den Überschuss '
               + 'an die übrigen ab.',
      rechnung: 'Netzbezug ÷ Anzahl Wohnungen, gedeckelt am eigenen '
              + 'Verbrauch; der Rest wird unter den übrigen erneut gleich '
              + 'verteilt, bis nichts mehr übrig ist. Wirkung: kleine '
              + 'Wohnungen tragen anteilig mehr Netzstrom, der Solarstrom '
              + 'verschiebt sich zum Großverbraucher.',
    },
  },
  exportPdf: 'PDF herunterladen',
  exportCsv: 'Tabelle (CSV)',
  modellBedeutung: 'Bedeutung',
  modellRechnung: 'Rechenweg',
  modellEmpfehlungMarke: 'Empfehlung:',
  // Die Empfehlung nennt die gemessene Spannweite statt einer Behauptung —
  // sie wird bei jedem Aufruf aus den angezeigten Daten neu gerechnet.
  modellEmpfohlen: 'Für gleichmäßige Verteilung ist „anteilig nach Verbrauch" '
                 + 'das fairere Modell — alle bekommen denselben PV-Anteil, '
                 + 'unabhängig von der Wohnungsgröße.',
  modellNichtEmpfohlen: 'Für maximale Fairness „anteilig nach Verbrauch" '
                      + 'wählen: dort bekommt jede Wohnung denselben '
                      + 'PV-Anteil. Dieses Modell begünstigt den '
                      + 'Großverbraucher.',
  modellSpannweite: 'Gemessen in diesem Zeitraum: PV-Anteil zwischen '
                  + '{min} und {max} ({spanne} Unterschied).',
  zeitraumRollierend: 'Zeitraum',
  zeitraumJahre: 'Jahr',
  zeitraumMonate: 'Monat',
  urteilBelastbar: 'Belastbar',
  urteilEingeschraenkt: 'Eingeschränkt belastbar',
  urteilUnbrauchbar: 'Noch nicht aussagekräftig',
  // Der nicht zugeordnete Netzbezug. Zwei Ursachen, die sich verschieden
  // erklären — deshalb zwei Texte statt eines, der in einem der beiden
  // Fälle falsch wäre.
  abrechnungRest: 'Nicht zugeordneter Netzbezug',
  // Das Haus bezog mehr, als die Wohnungen zusammen verbraucht haben.
  abrechnungRestDeckel: 'Das Haus hat mehr bezogen, als die Wohnungen '
                      + 'zusammen verbraucht haben — Anlagentechnik, '
                      + 'Speicherverluste und Verbraucher hinter keinem '
                      + 'Zähler.',
  // Kein einziger Impuls in diesen Intervallen.
  abrechnungRestStill: 'Bezogen, während keine Wohnung Impulse lieferte.',
  abrechnungRestSchluss: 'Wird bewusst nicht auf die Wohnungen verteilt: '
                       + 'welche Wohnung ihn verursacht hat, ist nicht '
                       + 'messbar.',
  laufzeit: 'Dienst läuft seit',
  verworfen: 'verworfen',
  unplausibel: 'unplausibel',
  quelle: 'Quelle',
  keineVerbindung: 'Keine Verbindung zum Zählerdienst. Letzter Stand von ',
  bilanzAus: 'Hausbilanz ist abgeschaltet — ohne sie keine Zuordnung des Netzbezugs.',
  restNegativ: 'Die Wohnungen melden dauerhaft deutlich mehr als der Hausverbrauch. '
             + 'Mehr, als der Messversatz zwischen Impuls und Bilanz erklärt — '
             + 'auf falsche Zuordnung oder einen Zähler hinter der Bilanzgrenze prüfen.',
  restHoch: 'Ungewöhnlich hoher nicht erfasster Anteil.',
};

const zahl = {
  watt: new Intl.NumberFormat('de-DE', { maximumFractionDigits: 0 }),
  kwh: new Intl.NumberFormat('de-DE', {
    minimumFractionDigits: config.nachkommaKwh,
    maximumFractionDigits: config.nachkommaKwh,
  }),
  prozent: new Intl.NumberFormat('de-DE', { maximumFractionDigits: 0 }),
  anteil: new Intl.NumberFormat('de-DE', { maximumFractionDigits: 1 }),
  uhr: new Intl.DateTimeFormat('de-DE', {
    hour: '2-digit', minute: '2-digit', second: '2-digit',
  }),
  // Für den Zeitpunkt des Einspielens: der liegt oft Tage zurück, eine reine
  // Uhrzeit wäre dort irreführend.
  datumZeit: new Intl.DateTimeFormat('de-DE', {
    day: '2-digit', month: '2-digit', year: 'numeric',
    hour: '2-digit', minute: '2-digit',
  }),
};

const el = {
  titel: document.querySelector('[data-feld="titel"]'),
  stand: document.querySelector('[data-feld="stand"]'),
  fassung: document.querySelector('[data-feld="fassung"]'),
  hinweis: document.querySelector('[data-feld="hinweis"]'),
  titelWohnungen: document.querySelector('[data-feld="titel-wohnungen"]'),
  titelHaus: document.querySelector('[data-feld="titel-haus"]'),
  summeWohnungen: document.querySelector('[data-feld="summe-wohnungen"]'),
  summeHaus: document.querySelector('[data-feld="summe-haus"]'),
  quelleWohnungen: document.querySelector('[data-feld="quelle-wohnungen"]'),
  quelleHaus: document.querySelector('[data-feld="quelle-haus"]'),
  bereichSystem: document.querySelector('[data-bereich="system"]'),
  system: document.querySelector('[data-liste="system"]'),
  titelSystem: document.querySelector('[data-feld="titel-system"]'),
  quelleSystem: document.querySelector('[data-feld="quelle-system"]'),
  summeSystem: document.querySelector('[data-feld="summe-system"]'),
  zaehler: document.querySelector('[data-liste="zaehler"]'),
  haus: document.querySelector('[data-liste="haus"]'),
  bereichHaus: document.querySelector('[data-bereich="haus"]'),
  fuss: document.querySelector('[data-liste="fuss"]'),
  thema: document.querySelector('[data-schalter="thema"]'),
  themaText: document.querySelector('[data-feld="thema-text"]'),
  zeitwahl: document.querySelector('[data-liste="zeitwahl"]'),
  bereichAbrechnung: document.querySelector('[data-bereich="abrechnung"]'),
  abrechnung: document.querySelector('[data-liste="abrechnung"]'),
  titelAbrechnung: document.querySelector('[data-feld="titel-abrechnung"]'),
  quelleAbrechnung: document.querySelector('[data-feld="quelle-abrechnung"]'),
  summeAbrechnung: document.querySelector('[data-feld="summe-abrechnung"]'),
  abrechnungZeitraum: document.querySelector(
    '[data-liste="abrechnung-zeitraum"]'),
  abrechnungModell: document.querySelector('[data-liste="abrechnung-modell"]'),
  abrechnungModellInfo:
    document.querySelector('[data-feld="abrechnung-modell-info"]'),
  kurzWohnungen: document.querySelector('[data-feld="kurz-wohnungen"]'),
  kurzHaus: document.querySelector('[data-feld="kurz-haus"]'),
  kurzAbrechnung: document.querySelector('[data-feld="kurz-abrechnung"]'),
  kurzSystem: document.querySelector('[data-feld="kurz-system"]'),
  exportPdf: document.querySelector('[data-feld="export-pdf"]'),
  exportCsv: document.querySelector('[data-feld="export-csv"]'),
  exportPdfText: document.querySelector('[data-feld="export-pdf-text"]'),
  exportCsvText: document.querySelector('[data-feld="export-csv-text"]'),
  abrechnungUrteil: document.querySelector('[data-feld="abrechnung-urteil"]'),
  abrechnungRest: document.querySelector('[data-feld="abrechnung-rest"]'),
};

/* --- Thema ---------------------------------------------------------- */

function themaSetzen(name) {
  document.documentElement.dataset.theme = name;
  el.thema.setAttribute('aria-pressed', String(name === 'dunkel'));
  el.themaText.textContent = name === 'dunkel' ? text.themaHell : text.themaDunkel;
  try { localStorage.setItem(config.themaSchluessel, name); } catch { /* privater Modus */ }
}

function themaStarten() {
  let gespeichert = null;
  try { gespeichert = localStorage.getItem(config.themaSchluessel); } catch { /* egal */ }
  const dunkelGewuenscht = matchMedia('(prefers-color-scheme: dark)').matches;
  themaSetzen(gespeichert ?? (dunkelGewuenscht ? 'dunkel' : 'hell'));
}

/* --- Klappzustand der Abschnitte ------------------------------------- */

/* Standardmäßig ist alles zugeklappt: die Kurzfassungen zeigen die Werte
   ohnehin, und wer den Verlauf sehen will, klappt gezielt auf. Was jemand
   aufklappt, bleibt aufgeklappt — aber nur für dieses Gerät, und ein
   gespeicherter Zustand aus einer früheren Fassung darf nicht dazu führen,
   dass ein Abschnitt für immer offen steht, den es nicht mehr gibt. */
function klappenStarten() {
  let offen = [];
  try {
    const roh = localStorage.getItem(config.abschnitteSchluessel);
    if (roh) offen = JSON.parse(roh);
    if (!Array.isArray(offen)) offen = [];
  } catch { offen = []; /* privater Modus oder kaputter Eintrag */ }

  for (const abschnitt of document.querySelectorAll('[data-bereich]')) {
    const name = abschnitt.dataset.bereich;
    if (abschnitt.tagName !== 'DETAILS') continue;
    abschnitt.open = offen.includes(name);
    abschnitt.addEventListener('toggle', () => {
      // Beim Speichern aus dem DOM lesen statt eine eigene Liste zu führen:
      // eine zweite Liste liefe irgendwann auseinander.
      const jetztOffen = [...document.querySelectorAll('details[data-bereich]')]
        .filter((d) => d.open)
        .map((d) => d.dataset.bereich);
      try {
        localStorage.setItem(config.abschnitteSchluessel,
                             JSON.stringify(jetztOffen));
      } catch { /* privater Modus */ }
    });
  }
}

/* --- Verlaufszustand ------------------------------------------------- */

/* Die Kacheln werden bei jedem Abruf neu aufgebaut. Welche Kachel gerade
   ihren Verlauf zeigt, darf deshalb nicht in der Kachel stehen — sonst
   klappt das Diagramm alle fünf Sekunden zu. Der Zustand liegt hier,
   geordnet nach der Verlaufskennung. */
const offeneVerlaeufe = new Map();   // kennung -> {ziel, einheit}
let zeitraumAktiv = config.zeitraumStandard;

try {
  const gemerkt = localStorage.getItem(config.zeitraumSchluessel);
  if (config.zeitraeume.some((z) => z.id === gemerkt)) zeitraumAktiv = gemerkt;
} catch { /* privater Modus */ }

/* Welche Diagramme offen sind, überdauert das Neuladen. Sonst merkt sich die
   Seite den Zeitraum, aber nicht, worauf man ihn angewendet hat — auf einem
   fest montierten Anzeigegerät wäre nach jedem Neuladen alles wieder zu. */
function offeneMerken() {
  try {
    localStorage.setItem(config.offenSchluessel,
                         JSON.stringify([...offeneVerlaeufe.keys()]));
  } catch { /* privater Modus */ }
}

function offeneLaden() {
  try {
    const roh = JSON.parse(localStorage.getItem(config.offenSchluessel) ?? '[]');
    // Nur Kennungen übernehmen, die es noch gibt — eine umbenannte Größe
    // soll keine leere Kachel aufklappen.
    if (Array.isArray(roh)) {
      for (const kennung of roh) {
        if (typeof kennung === 'string') {
          offeneVerlaeufe.set(kennung, { ziel: null, einheit: '' });
        }
      }
    }
  } catch { /* kaputter Eintrag: dann eben zu */ }
}

function zeitraumSekunden() {
  const treffer = config.zeitraeume.find((z) => z.id === zeitraumAktiv);
  return (treffer ?? config.zeitraeume[0]).sekunden;
}

async function verlaufLaden(kennung) {
  const adresse = `${config.verlaufQuelle}`
    + `?key=${encodeURIComponent(kennung)}`
    + `&seconds=${Math.round(zeitraumSekunden())}`;
  const antwort = await fetch(adresse, { cache: 'no-store' });
  if (!antwort.ok) throw new Error(`HTTP ${antwort.status}`);
  return antwort.json();
}

/** Diagramm einer Kachel neu zeichnen — nach Öffnen oder Zeitraumwechsel. */
async function verlaufZeichnen(kennung, ziel, einheit) {
  ziel.replaceChildren();
  const laedt = document.createElement('p');
  laedt.className = 'verlauf__leer';
  laedt.textContent = text.verlaufLaedt;
  ziel.append(laedt);

  let daten;
  try {
    daten = await verlaufLaden(kennung);
  } catch {
    laedt.textContent = text.verlaufFehler;
    return;
  }

  // Zwischen Anfrage und Antwort kann die Kachel neu gebaut worden sein.
  // Dann hängt `ziel` nicht mehr im Dokument und das Zeichnen wäre umsonst.
  if (!ziel.isConnected) return;

  const bild = diagramm(daten.points ?? [], {
    einheit,
    von: daten.from,
    bis: daten.to,
  });
  ziel.replaceChildren();

  if (bild === null) {
    const leer = document.createElement('p');
    leer.className = 'verlauf__leer';
    leer.textContent = text.verlaufLeer;
    ziel.append(leer);
    return;
  }

  ziel.append(bild);

  const werte = kennzahlen(daten.points, einheit);
  if (werte) {
    const fuss = document.createElement('p');
    fuss.className = 'verlauf__fuss';
    for (const [beschriftung, wert] of [
      [text.kleinstwert, werte.min],
      [text.mittelwert, werte.mittel],
      [text.groesstwert, werte.max],
    ]) {
      const span = document.createElement('span');
      span.textContent = `${beschriftung} ${wert}`;
      fuss.append(span);
    }
    ziel.append(fuss);
  }
}

/** Alle offenen Diagramme neu laden — nach Zeitraumwechsel. */
function alleVerlaeufeNeu() {
  for (const [kennung, zustand] of offeneVerlaeufe) {
    if (zustand.ziel && zustand.ziel.isConnected) {
      verlaufZeichnen(kennung, zustand.ziel, zustand.einheit);
    }
  }
}

function zeitwahlBauen() {
  el.zeitwahl.replaceChildren();
  for (const zeitraum of config.zeitraeume) {
    const knopf = document.createElement('button');
    knopf.type = 'button';
    knopf.className = 'zeitwahl__knopf';
    knopf.textContent = text.zeitraeume[zeitraum.id] ?? zeitraum.id;
    knopf.setAttribute('aria-pressed', String(zeitraum.id === zeitraumAktiv));
    knopf.addEventListener('click', () => {
      zeitraumAktiv = zeitraum.id;
      try {
        localStorage.setItem(config.zeitraumSchluessel, zeitraum.id);
      } catch { /* privater Modus */ }
      for (const anderer of el.zeitwahl.children) {
        anderer.setAttribute('aria-pressed',
                             String(anderer === knopf));
      }
      alleVerlaeufeNeu();
    });
    el.zeitwahl.append(knopf);
  }
  // Die Zeitwahl nützt nur, wenn irgendwo ein Diagramm offen ist — sie
  // bleibt aber sichtbar, damit man den Zeitraum vorher wählen kann.
}

/* --- Bausteine ------------------------------------------------------ */

/** Kurvensymbol für den Umschalter. Über DOM-Methoden statt innerHTML. */
function kurvenSymbol() {
  const NS = 'http://www.w3.org/2000/svg';
  const svg = document.createElementNS(NS, 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('fill', 'none');
  svg.setAttribute('stroke', 'currentColor');
  svg.setAttribute('stroke-width', '2');
  svg.setAttribute('stroke-linecap', 'round');
  svg.setAttribute('stroke-linejoin', 'round');
  svg.setAttribute('aria-hidden', 'true');
  const pfad = document.createElementNS(NS, 'path');
  pfad.setAttribute('d', 'M3 17l5-6 4 4 5-8 4 5');
  svg.append(pfad);
  return svg;
}

function karte({ name, wert, einheit, zeilen = [], puls = null, zustand = null,
                 verlauf = null, zusatz = null }) {
  const wurzel = document.createElement('article');
  wurzel.className = 'karte';
  if (zustand) wurzel.dataset.zustand = zustand;

  const kopf = document.createElement('div');
  kopf.className = 'karte__kopf';
  if (puls) {
    const punkt = document.createElement('span');
    punkt.className = 'puls';
    punkt.dataset.aktiv = puls.aktiv ? 'ja' : 'nein';
    // Farbe allein trägt keine Aussage: der Zustand steht als Text daneben.
    const lesbar = document.createElement('span');
    lesbar.className = 'nurlesen';
    lesbar.textContent = puls.aktiv ? text.aktiv : text.still;
    kopf.append(punkt, lesbar);
  }
  const titel = document.createElement('h3');
  titel.className = 'karte__name';
  titel.textContent = name;
  kopf.append(titel);

  const grosseZahl = document.createElement('p');
  grosseZahl.className = 'karte__wert';
  grosseZahl.textContent = wert;
  if (einheit) {
    const e = document.createElement('span');
    e.className = 'karte__einheit';
    e.textContent = einheit;
    grosseZahl.append(e);
  }
  if (zusatz) {
    const z = document.createElement('span');
    z.className = 'karte__zusatz';
    // Führendes Leerzeichen im TEXT, nicht nur als Rand: sonst liest eine
    // Vorlesehilfe "420W(257 W Netz)" als ein Wort. Der optische Abstand
    // kommt zusätzlich aus dem margin.
    z.textContent = ` ${zusatz.text}`;
    // Farbe allein trägt keine Aussage — der Sinn steht als Titel dran und
    // wird von Vorlesewerkzeugen mitgenommen.
    if (zusatz.titel) z.title = zusatz.titel;
    grosseZahl.append(z);
  }

  // Umschalter nur, wo es auch einen Verlauf gibt. Ein Knopf, der nichts
  // tut, ist schlimmer als keiner.
  let schalter = null;
  let bereich = null;
  if (verlauf) {
    schalter = document.createElement('button');
    schalter.type = 'button';
    schalter.className = 'karte__schalter';
    schalter.append(kurvenSymbol());
    kopf.append(schalter);

    bereich = document.createElement('div');
    bereich.className = 'karte__verlauf';
  }

  wurzel.append(kopf, grosseZahl);

  const zeilenHuelle = document.createElement('div');
  for (const [links, rechts] of zeilen) {
    const zeile = document.createElement('p');
    zeile.className = 'karte__zeile';
    const a = document.createElement('span');
    a.textContent = links;
    const b = document.createElement('b');
    b.textContent = rechts;
    zeile.append(a, b);
    zeilenHuelle.append(zeile);
  }
  wurzel.append(zeilenHuelle);

  if (verlauf) {
    wurzel.append(bereich);

    const gemerkt = offeneVerlaeufe.get(verlauf.key);
    const offen = Boolean(gemerkt);

    const anwenden = (zeigen) => {
      schalter.setAttribute('aria-pressed', String(zeigen));
      schalter.setAttribute('aria-label',
                            zeigen ? text.verlaufVerbergen : text.verlaufZeigen);
      schalter.title = zeigen ? text.verlaufVerbergen : text.verlaufZeigen;
      bereich.hidden = !zeigen;
      // Die Detailzeilen weichen dem Diagramm: beides zusammen macht die
      // Kachel doppelt so hoch und das Raster unruhig.
      zeilenHuelle.hidden = zeigen;
    };

    anwenden(offen);
    if (offen) {
      // Nach dem Neuaufbau hängt das alte SVG nicht mehr im Dokument.
      // Vorhandene Punkte sofort zeichnen, dann im eigenen Takt nachladen.
      gemerkt.ziel = bereich;
      verlaufZeichnen(verlauf.key, bereich, verlauf.unit ?? einheit ?? '');
    }

    // Ein Zuhörer, der Zustand liegt in der Map — nicht am Element.
    schalter.addEventListener('click', () => {
      const jetztOffen = !offeneVerlaeufe.has(verlauf.key);
      if (jetztOffen) {
        offeneVerlaeufe.set(verlauf.key, {
          ziel: bereich, einheit: verlauf.unit ?? einheit ?? '',
        });
        offeneMerken();
        anwenden(true);
        verlaufZeichnen(verlauf.key, bereich, verlauf.unit ?? einheit ?? '');
      } else {
        offeneVerlaeufe.delete(verlauf.key);
        offeneMerken();
        anwenden(false);
        bereich.replaceChildren();
      }
    });
  }

  return wurzel;
}

function alter(letzterImpuls, jetzt) {
  if (!letzterImpuls) return null;
  return Math.max(0, jetzt - letzterImpuls);
}

function dauerText(sekunden) {
  if (sekunden === null) return text.nie;
  if (sekunden < 60) return `vor ${Math.round(sekunden)} s`;
  if (sekunden < 3600) return `vor ${Math.round(sekunden / 60)} min`;
  return `vor ${Math.round(sekunden / 3600)} h`;
}

/** Zeitspanne als Dauer, nicht als Alter ("3 h 12 min" statt "vor 3 h"). */
function dauerLang(sekunden) {
  if (sekunden < 60) return `${Math.round(sekunden)} s`;
  if (sekunden < 3600) return `${Math.round(sekunden / 60)} min`;
  const stunden = Math.floor(sekunden / 3600);
  const minuten = Math.round((sekunden % 3600) / 60);
  return minuten > 0 ? `${stunden} h ${minuten} min` : `${stunden} h`;
}

/* --- Zeichnen ------------------------------------------------------- */

/* Reihenfolge der Wohnungen — eine Stelle, zwei Verwender (Kacheln und
 * Kurzfassung). Als Kopie in der Kurzfassung wäre sie die zweite Liste, die
 * irgendwann von der ersten abweicht. */
function zaehlerReihenfolge(meters) {
  return Object.values(meters ?? {}).sort((a, b) => {
    // Unbekannte Zähler kommen ans Ende, dort alphabetisch. Ohne den
    // Ersatzwert stünde ein neuer Zähler stillschweigend ganz vorn.
    const ia = config.reihenfolge.indexOf(a.id);
    const ib = config.reihenfolge.indexOf(b.id);
    const ra = ia === -1 ? config.reihenfolge.length : ia;
    const rb = ib === -1 ? config.reihenfolge.length : ib;
    return ra - rb || a.name.localeCompare(b.name, 'de');
  });
}

function zaehlerZeichnen(daten) {
  const jetzt = daten.time;
  el.zaehler.replaceChildren();

  const eintraege = zaehlerReihenfolge(daten.meters);
  // Die Verteilrechnung steht in einem eigenen Modul ohne Zugriff auf
  // document/localStorage, damit ein Test sie aufrufen kann statt sie
  // nachzubauen.
  const netz = netzanteile(daten, eintraege, abrechnungModell,
                           config.bilanzHoechstalterSekunden);

  for (const m of eintraege) {
    const seit = alter(m.last_pulse, jetzt);
    const statistik = daten.counters?.[m.id] ?? {};
    const zeilen = [
      [text.zaehlerstand, `${zahl.kwh.format(m.energy_kwh)} ${text.einheitKwh}`],
      [text.letzterImpuls, dauerText(seit)],
    ];
    if (statistik.bounced || statistik.implausible) {
      zeilen.push([
        text.verworfen,
        `${statistik.bounced ?? 0} / ${statistik.implausible ?? 0} ${text.unplausibel}`,
      ]);
    }
    // Der Netzanteil steht hinter der Verbrauchszahl, weil er sie
    // relativiert: 420 W sagt wenig, "420 W, davon 0 aus dem Netz" sagt
    // alles. Fehlt die Hausbilanz, entfällt die Klammer ganz — eine 0 wäre
    // dort eine Behauptung ohne Messung.
    const netzW = netz?.get(m.id);
    el.zaehler.append(karte({
      name: m.name,
      wert: zahl.watt.format(m.power_w ?? 0),
      einheit: text.einheitWatt,
      zusatz: netzW === undefined ? null : {
        text: `(${zahl.watt.format(netzW)} ${text.einheitWatt} ${text.netzKurz})`,
        titel: netzW <= 0.5 ? text.netzKlammerNull : text.netzKlammerHilfe,
      },
      zeilen,
      puls: { aktiv: seit !== null && seit <= config.stillNachSekunden },
      verlauf: { key: `meter.${m.id}`, unit: text.einheitWatt },
    }));
  }

  const summe = eintraege.reduce((s, m) => s + (m.power_w ?? 0), 0);
  el.summeWohnungen.textContent = `${zahl.watt.format(summe)} ${text.einheitWatt}`;
  return summe;
}

function hausZeichnen(daten, summeWohnungen) {
  const bilanz = daten.balance ?? {};
  const letzte = bilanz.latest;

  if (!bilanz.enabled || !letzte) {
    el.bereichHaus.hidden = true;
    el.summeHaus.textContent = '';
    return bilanz.enabled ? null : text.bilanzAus;
  }

  el.bereichHaus.hidden = false;
  el.haus.replaceChildren();

  const rest = letzte.consumption_w - summeWohnungen;

  // Der Hausverbrauch ist die Kopfzahl des Abschnitts — dieselbe Zahl noch
  // einmal als Kachel wäre die zweite Stelle für denselben Wert.
  el.summeHaus.textContent =
    `${zahl.watt.format(letzte.consumption_w)} ${text.einheitWatt}`;

  // Die Quelle steht in den Daten und wird nicht fest hingeschrieben:
  // fällt die Sonnenbatterie aus, springt Home Assistant ein und die
  // Überschrift muss dann die tatsächliche Quelle nennen.
  //
  // Angezeigt wird das ALTER des letzten Messpunkts, nicht das Sammel-
  // intervall. Beides zu verwechseln erzeugt eine Anzeige, die zwischen
  // "alle 1 s" und "alle 59 s" springt, obwohl der Takt konstant ist —
  // und die Zahl steht genau dort, wo man den Messversatz zu den Zählern
  // ablesen will.
  const alterBilanz = Math.max(0, daten.time - letzte.ts);
  el.quelleHaus.textContent =
    `(${letzte.source}, ${dauerText(alterBilanz)})`;

  el.haus.append(karte({
    name: text.rest,
    wert: zahl.watt.format(rest),
    einheit: text.einheitWatt,
    zeilen: [
      [text.summeWohnungen, `${zahl.watt.format(summeWohnungen)} ${text.einheitWatt}`],
      [text.restHilfe, ''],
    ],
    verlauf: { key: 'house.consumption', unit: text.einheitWatt },
  }));

  el.haus.append(karte({
    name: text.pv,
    wert: zahl.watt.format(letzte.production_w),
    einheit: text.einheitWatt,
    verlauf: { key: 'house.production', unit: text.einheitWatt },
  }));

  // grid_w ist auf "Bezug positiv" gedreht (siehe sources.py).
  const bezug = letzte.grid_import_w ?? Math.max(0, letzte.grid_w);
  const einspeisung = letzte.grid_export_w ?? Math.max(0, -letzte.grid_w);
  el.haus.append(karte({
    name: bezug > 0 ? text.netzbezug : text.netzeinspeisung,
    wert: zahl.watt.format(bezug > 0 ? bezug : einspeisung),
    einheit: text.einheitWatt,
    verlauf: { key: 'house.grid', unit: text.einheitWatt },
  }));

  // battery_w: positiv = entladen, negativ = laden (Konvention aus Reading).
  const akkuFluss = letzte.battery_w ?? 0;
  let akkuZustand = text.akkuRuhe;
  if (akkuFluss > 0) akkuZustand = text.akkuEntladen;
  else if (akkuFluss < 0) akkuZustand = text.akkuLaden;

  el.haus.append(karte({
    name: text.akku,
    wert: zahl.prozent.format(letzte.battery_percent ?? 0),
    einheit: '%',
    zeilen: [
      [akkuZustand, `${zahl.watt.format(Math.abs(akkuFluss))} ${text.einheitWatt}`],
    ],
    verlauf: { key: 'house.battery', unit: '%' },
  }));

  // Der Langzeitvergleich rechnet über Energie und wird mit der Laufzeit
  // belastbarer — anders als die Momentanwerte darüber, die bei jeder
  // Lastspitze auseinanderlaufen.
  const v = bilanz.comparison;
  if (v && v.rest_percent !== null) {
    const dauer = dauerLang(v.seconds);
    const jung = v.seconds < config.anteilVerlaesslichAbSekunden;
    el.haus.append(karte({
      name: text.anteil,
      wert: zahl.anteil.format(v.rest_percent),
      einheit: '%',
      zeilen: [
        [`${text.gemessen} ${dauer}`, jung ? text.anteilJung : ''],
        [text.hausEnergie, `${zahl.kwh.format(v.house_kwh)} ${text.einheitKwh}`],
        [text.wohnungenEnergie, `${zahl.kwh.format(v.flats_kwh)} ${text.einheitKwh}`],
      ],
    }));
  }

  if (rest < -config.restNegativToleranzWatt) return text.restNegativ;
  if (rest > config.restWarnungWatt) return text.restHoch;
  return null;
}

/** Bytes menschenlesbar. Ab GB eine Nachkommastelle, darunter keine. */
function bytes(anzahl) {
  if (anzahl >= 1024 ** 3) {
    return `${zahl.anteil.format(anzahl / 1024 ** 3)} GB`;
  }
  return `${zahl.watt.format(anzahl / 1024 ** 2)} MB`;
}

function systemZeichnen(daten) {
  const s = daten.system;
  if (!s) {
    el.bereichSystem.hidden = true;
    return null;
  }

  el.bereichSystem.hidden = false;
  el.system.replaceChildren();
  el.quelleSystem.textContent = `(${text.systemQuelle})`;

  let warnung = null;

  // Temperatur — der Wert, der einen Dauerläufer im Schrank zuerst trifft.
  if (s.temperature_c !== null) {
    const heiss = s.temperature_c >= s.temperature_critical_c;
    const warm = s.temperature_c >= s.temperature_warn_c;
    let lage = text.tempKuehl;
    if (heiss) lage = text.tempHeiss;
    else if (warm) lage = text.tempWarm;

    el.system.append(karte({
      name: text.temperatur,
      wert: zahl.anteil.format(s.temperature_c),
      einheit: '°C',
      zustand: warm ? 'warn' : null,
      zeilen: [[lage, '']],
      verlauf: { key: 'system.temperature', unit: '°C' },
    }));
    el.summeSystem.textContent = `${zahl.anteil.format(s.temperature_c)} °C`;
  } else {
    el.summeSystem.textContent = '';
  }

  if (s.load) {
    el.system.append(karte({
      name: text.cpuLast,
      wert: zahl.watt.format(s.load.load_percent),
      einheit: '%',
      verlauf: { key: 'system.load', unit: '%' },
      zeilen: [
        [`${s.load.cores} ${text.kerne}`,
         `${zahl.anteil.format(s.load.load_1)} / ${zahl.anteil.format(s.load.load_5)} / ${zahl.anteil.format(s.load.load_15)}`],
      ],
    }));
  }

  if (s.memory) {
    const knapp = s.memory.used_percent >= config.ramWarnProzent;
    const zeilen = [
      [text.frei, bytes(s.memory.available_bytes)],
      [text.belegt, `${bytes(s.memory.used_bytes)} / ${bytes(s.memory.total_bytes)}`],
    ];
    if (s.memory.swap_total_bytes) {
      zeilen.push([text.swap,
                   `${bytes(s.memory.swap_used_bytes)} / ${bytes(s.memory.swap_total_bytes)}`]);
    }
    el.system.append(karte({
      name: text.ramTitel,
      wert: zahl.watt.format(s.memory.used_percent),
      einheit: '%',
      zustand: knapp ? 'warn' : null,
      verlauf: { key: 'system.memory', unit: '%' },
      zeilen,
    }));
    if (knapp) warnung = text.ramVoll;
  }

  if (s.disk) {
    const voll = s.disk.used_percent >= config.plattenWarnProzent;
    el.system.append(karte({
      name: text.ablage,
      wert: zahl.watt.format(s.disk.used_percent),
      einheit: '%',
      zustand: voll ? 'warn' : null,
      verlauf: { key: 'system.disk', unit: '%' },
      zeilen: [
        [text.frei, bytes(s.disk.available_bytes)],
        [text.belegt, `${bytes(s.disk.used_bytes)} / ${bytes(s.disk.total_bytes)}`],
      ],
    }));
    if (voll) warnung = text.platteVoll;
  }

  if (s.uptime_seconds !== null) {
    const tage = Math.floor(s.uptime_seconds / 86400);
    const stunden = Math.floor((s.uptime_seconds % 86400) / 3600);
    el.system.append(karte({
      name: text.betriebsdauer,
      wert: tage > 0 ? `${tage} d ${stunden} h` : dauerLang(s.uptime_seconds),
      zeilen: [[text.seitNeustart, '']],
    }));
  }

  // Sicherung. Eine fehlende Sicherung macht sich nirgends bemerkbar: der
  // Dienst läuft, die Zahlen stimmen, und erst der Kartenausfall deckt auf,
  // dass seit Wochen nichts weggeschrieben wurde. Deshalb steht sie hier.
  const b = s.backup;
  if (b) {
    const alterH = b.letzte_ts
      ? (daten.time - b.letzte_ts) / 3600
      : null;
    // Der Timer läuft nachts; 48 h decken einen ausgefallenen Lauf ab, ohne
    // jeden Nachmittag Alarm zu schlagen. Dieselbe Schwelle wie im Doctor.
    const zuAlt = alterH === null || alterH > 48;
    const zeilen = [];
    if (b.letzte_ts) {
      zeilen.push([text.backupLetzte,
                   zahl.datumZeit.format(new Date(b.letzte_ts * 1000))]);
    }
    if (b.anzahl !== null) {
      zeilen.push([text.backupStaende,
                   `${b.anzahl}${b.bytes ? ` (${bytes(b.bytes)})` : ''}`]);
    }
    zeilen.push([text.backupOrt,
                 b.extern ? text.backupExtern : text.backupLokal]);

    el.system.append(karte({
      name: text.backup,
      wert: alterH === null ? '—' : dauerText(daten.time - b.letzte_ts),
      // Ein lokaler Ort ist kein Fehler, aber auch nicht in Ordnung: er
      // schützt nicht gegen den Ausfall der Karte. Warnfarbe in beiden
      // Fällen, der Text darunter unterscheidet sie.
      zustand: (zuAlt || !b.extern) ? 'warn' : null,
      zeilen,
    }));
    if (zuAlt && !warnung) warnung = text.backupVeraltet;
    else if (!b.extern && !warnung) warnung = text.backupNurLokal;
  }

  // Drosselung: nur anzeigen, wenn tatsächlich gemessen. Nicht messbar darf
  // nicht wie "alles in Ordnung" aussehen.
  const t = s.throttling;
  if (t) {
    const jetzt = t.undervoltage_now || t.throttled_now;
    const frueher = t.undervoltage_since_boot || t.throttled_since_boot;
    if (jetzt) warnung = text.drosselJetzt;
    else if (frueher && !warnung) warnung = text.drosselFrueher;
  }

  return warnung;
}

function fassungZeichnen(fassung) {
  if (!el.fassung) return;
  if (!fassung?.version) {
    // Ein älterer Dienst kennt das Feld nicht. Dann bleibt die Stelle leer,
    // statt einen Platzhalter anzuzeigen, der nach einem Fehler aussieht.
    el.fassung.textContent = '';
    el.fassung.removeAttribute('title');
    return;
  }

  el.fassung.textContent = `v${fassung.version}`;

  // Der Fingerabdruck und der Zeitpunkt des Einspielens gehören nicht in die
  // Kopfzeile — sie sind für den seltenen Fall da, dass jemand wissen will,
  // ob eine Änderung wirklich auf dem Gerät angekommen ist.
  const teile = [];
  if (fassung.build) teile.push(`${text.fassungBuild}: ${fassung.build}`);
  if (fassung.stand_ts) {
    const stand = new Date(fassung.stand_ts * 1000);
    teile.push(`${text.fassungStand}: ${zahl.datumZeit.format(stand)}`);
  }
  if (teile.length) el.fassung.title = teile.join(' · ');
  else el.fassung.removeAttribute('title');
}

function fussZeichnen(daten) {
  el.fuss.replaceChildren();
  const teile = [];

  const laufzeit = daten.uptime_seconds ?? 0;
  const stunden = Math.floor(laufzeit / 3600);
  const minuten = Math.floor((laufzeit % 3600) / 60);
  teile.push(`${text.laufzeit} ${stunden} h ${minuten} min`);

  const quelle = daten.balance?.latest?.source;
  if (quelle) teile.push(`${text.quelle}: ${quelle}`);

  const gesamt = Object.values(daten.meters ?? {})
    .reduce((s, m) => s + (m.pulses ?? 0), 0);
  teile.push(`${zahl.watt.format(gesamt)} ${text.impulse}`);

  for (const t of teile) {
    const span = document.createElement('span');
    span.className = 'fuss__zahl';
    span.textContent = t;
    el.fuss.append(span);
  }
}

/* --- Abruf ---------------------------------------------------------- */

let letzterErfolg = null;

/* --- Abrechnung ----------------------------------------------------- */

let abrechnungZeitraum = null;   // erst aus der Antwort des Dienstes belegt
let abrechnungModell = null;
let abrechnungLaeuft = false;

function abrechnungWahlLaden() {
  try {
    abrechnungZeitraum = localStorage.getItem(
      config.abrechnungZeitraumSchluessel);
    abrechnungModell = localStorage.getItem(
      config.abrechnungModellSchluessel);
  } catch { /* privater Modus */ }
}

/** Auswahlknöpfe aus der Antwort des Dienstes bauen.
 *
 * Die Listen kommen mit jeder Antwort mit, statt hier gepflegt zu werden:
 * eine zweite Liste liefe früher oder später auseinander, und dann böte die
 * Anzeige einen Zeitraum an, den der Dienst ablehnt.
 */
function abrechnungWahlBauen(ziel, eintraege, aktiv, schluessel, merker,
                             leeren = true) {
  // `leeren` false, wenn im Ziel schon etwas steht, das bleiben soll —
  // die Gruppenbeschriftung wird vor dem Aufruf eingehängt und wäre sonst
  // gleich wieder weg.
  if (leeren) ziel.replaceChildren();
  const knoepfe = [];

  for (const eintrag of eintraege) {
    const knopf = document.createElement('button');
    knopf.type = 'button';
    knopf.className = 'zeitwahl__knopf';
    knopf.textContent = eintrag.label;
    knopf.setAttribute('aria-pressed', String(eintrag.key === aktiv));
    knopf.addEventListener('click', () => {
      merker(eintrag.key);
      try {
        localStorage.setItem(schluessel, eintrag.key);
      } catch { /* privater Modus */ }
      for (const anderer of knoepfe) {
        anderer.setAttribute('aria-pressed', String(anderer === knopf));
      }
      abrechnungAbrufen();
    });
    knoepfe.push(knopf);
    ziel.append(knopf);
  }
  return knoepfe;
}

/** Zeiträume nach Art gruppieren: rollierend, Jahre, Monate.
 *
 * Bei drei Jahren und zwölf Monaten stehen sonst siebzehn gleich aussehende
 * Knöpfe nebeneinander, und "2026" verschwindet zwischen "30 Tage" und
 * "August 2026". Die Gruppen kommen aus dem Feld `kind` der Antwort — die
 * Anzeige entscheidet nicht selbst, was ein Kalenderzeitraum ist.
 */
function abrechnungZeitraeumeBauen(ziel, eintraege, aktiv) {
  ziel.replaceChildren();
  const gruppen = [
    ['rollierend', text.zeitraumRollierend],
    ['jahr', text.zeitraumJahre],
    ['monat', text.zeitraumMonate],
  ];
  const alle = [];

  for (const [art, beschriftung] of gruppen) {
    const davon = eintraege.filter((e) => (e.kind ?? 'rollierend') === art);
    if (!davon.length) continue;

    const reihe = document.createElement('div');
    reihe.className = 'zeitwahl';
    reihe.setAttribute('role', 'group');
    reihe.setAttribute('aria-label', beschriftung);

    const marke = document.createElement('span');
    marke.className = 'zeitwahl__marke';
    marke.textContent = beschriftung;
    reihe.append(marke);

    alle.push(...abrechnungWahlBauen(
      reihe, davon, aktiv, config.abrechnungZeitraumSchluessel,
      (k) => { abrechnungZeitraum = k; }, false));
    ziel.append(reihe);
  }

  // Ein Knopf aus einer Gruppe muss die anderen Gruppen mit abwählen —
  // sonst stünden zwei Zeiträume gleichzeitig als gewählt da.
  for (const knopf of alle) {
    knopf.addEventListener('click', () => {
      for (const anderer of alle) {
        anderer.setAttribute('aria-pressed', String(anderer === knopf));
      }
    });
  }
}

/** Balken für die Aufteilung PV/Netz einer Wohnung. */
function anteilBalken(solarKwh, netzKwh) {
  const gesamt = solarKwh + netzKwh;
  const balken = document.createElement('div');
  balken.className = 'anteil';
  balken.setAttribute('aria-hidden', 'true');   // Zahlen stehen darunter
  if (gesamt <= 0) return balken;

  for (const [art, wert] of [['solar', solarKwh], ['netz', netzKwh]]) {
    const teil = document.createElement('span');
    teil.className = 'anteil__teil';
    teil.dataset.art = art;
    teil.style.inlineSize = `${(wert / gesamt) * 100}%`;
    balken.append(teil);
  }
  return balken;
}

function urteilZeichnen(urteil) {
  const stufen = {
    belastbar: text.urteilBelastbar,
    eingeschraenkt: text.urteilEingeschraenkt,
    unbrauchbar: text.urteilUnbrauchbar,
  };
  el.abrechnungUrteil.replaceChildren();
  el.abrechnungUrteil.dataset.stufe = urteil.level;

  const kopf = document.createElement('span');
  kopf.className = 'urteil__kopf';
  kopf.textContent = stufen[urteil.level] ?? urteil.level;
  el.abrechnungUrteil.append(kopf);

  for (const grund of urteil.reasons) {
    const zeile = document.createElement('span');
    zeile.className = 'urteil__grund';
    zeile.textContent = grund;
    el.abrechnungUrteil.append(zeile);
  }
}

/* --- Kurzfassung der eingeklappten Abschnitte ---------------------------
 *
 * Eingeklappt soll ein Abschnitt trotzdem sagen, was drinsteht — sonst muss
 * man vier Abschnitte aufklappen, um einen Wert zu sehen. Gebaut aus
 * denselben Daten wie die Kacheln, nicht aus dem gerenderten Text: eine
 * Kurzfassung, die aus dem DOM liest, bricht, sobald sich das Layout ändert.
 */
function kurzTeile(ziel, teile) {
  if (!ziel) return;
  ziel.replaceChildren();
  for (const t of teile) {
    const span = document.createElement('span');
    span.className = 'abschnitt__kurz-teil';
    span.textContent = t;
    ziel.append(span);
  }
}

function kurzfassungenZeichnen(daten, summeWohnungen) {
  // Wohnungen: je Zähler die Momentanleistung, in der Reihenfolge der Kacheln.
  // Kurzname statt "Wohnung EG": eingeklappt stehen alle Zähler in einer
  // Zeile, und der wiederholte Zusatz frisst den Platz für die Zahlen.
  // Die Kennung selbst ist genau das Kürzel, das an der Wohnungstür steht.
  const eintraege = zaehlerReihenfolge(daten.meters);
  kurzTeile(el.kurzWohnungen, eintraege.map(
    (m) => `${m.id.toUpperCase()}: `
         + `${zahl.watt.format(m.power_w ?? 0)} ${text.einheitWatt}`));

  // Haus: Erzeugung, Verbrauch, Netz — die drei Zahlen, für die die
  // Hausbilanz da ist. Bezug und Einspeisung tragen verschiedene Namen,
  // weil ein Vorzeichen am Telefon leicht übersehen wird.
  const letzte = daten.balance?.latest;
  if (letzte) {
    const netz = letzte.grid_w ?? 0;
    kurzTeile(el.kurzHaus, [
      `${text.pv}: ${zahl.watt.format(letzte.production_w ?? 0)} ${text.einheitWatt}`,
      `${text.verbrauchHaus}: ${zahl.watt.format(letzte.consumption_w ?? 0)} ${text.einheitWatt}`,
      netz >= 0
        ? `${text.netzbezug}: ${zahl.watt.format(netz)} ${text.einheitWatt}`
        : `${text.netzeinspeisung}: ${zahl.watt.format(-netz)} ${text.einheitWatt}`,
      `${text.ladestand}: ${zahl.anteil.format(letzte.battery_percent ?? 0)} %`,
    ]);
  } else {
    kurzTeile(el.kurzHaus, []);
  }

  // Rechner: die Werte, wegen derer man überhaupt nachsieht.
  const s = daten.system;
  if (s) {
    const teile = [];
    if (s.temperature_c != null) {
      teile.push(`${text.temperatur}: ${zahl.anteil.format(s.temperature_c)} °C`);
    }
    if (s.load?.load_percent != null) {
      teile.push(`${text.cpuLast}: ${zahl.prozent.format(s.load.load_percent)} %`);
    }
    if (s.memory?.used_percent != null) {
      teile.push(`${text.ramTitel}: ${zahl.prozent.format(s.memory.used_percent)} %`);
    }
    if (s.disk?.used_percent != null) {
      teile.push(`${text.ablage}: ${zahl.prozent.format(s.disk.used_percent)} %`);
    }
    kurzTeile(el.kurzSystem, teile);
  } else {
    kurzTeile(el.kurzSystem, []);
  }
}

/* Die Export-Links tragen den Zeitraum und das Modell, die gerade angezeigt
 * werden — aus der ANTWORT, nicht aus der Anfrage. Vor dem ersten Abruf sind
 * beide Variablen null (der Dienst bestimmt die Vorgabe), und die Links
 * standen dann ohne Parameter da: sie hätten immer 24 h/proportional
 * geliefert, egal was am Bildschirm stand. */
function exportLinksSetzen(daten) {
  if (!el.exportPdf || !el.exportCsv) return;
  const felder = new URLSearchParams();
  if (daten.range?.key) felder.set('range', daten.range.key);
  if (daten.model) felder.set('model', daten.model);
  const abfrage = felder.toString();
  el.exportPdf.href = `./allocation.pdf${abfrage ? `?${abfrage}` : ''}`;
  el.exportCsv.href = `./allocation.csv${abfrage ? `?${abfrage}` : ''}`;
  if (el.exportPdfText) el.exportPdfText.textContent = text.exportPdf;
  if (el.exportCsvText) el.exportCsvText.textContent = text.exportCsv;
}

function modellInfoZeichnen(daten) {
  if (!el.abrechnungModellInfo) return;
  el.abrechnungModellInfo.replaceChildren();

  const erklaerung = text.modellErklaerung[daten.model];
  if (!erklaerung) return;   // unbekanntes Modell: lieber nichts als falsch

  const bedeutung = document.createElement('span');
  bedeutung.textContent =
    `${text.modellBedeutung}: ${erklaerung.bedeutung}`;
  el.abrechnungModellInfo.append(bedeutung);

  const rechnung = document.createElement('span');
  rechnung.className = 'modellinfo__rechnung';
  rechnung.textContent = `${text.modellRechnung}: ${erklaerung.rechnung}`;
  el.abrechnungModellInfo.append(rechnung);

  const empfehlung = document.createElement('span');
  empfehlung.className = 'modellinfo__empfehlung';
  const marke = document.createElement('span');
  marke.className = 'modellinfo__marke';
  marke.textContent = `${text.modellEmpfehlungMarke} `;
  empfehlung.append(marke);
  empfehlung.append(daten.model === 'proportional'
    ? text.modellEmpfohlen
    : text.modellNichtEmpfohlen);

  // Die Spannweite der PV-Anteile aus den gerade angezeigten Zahlen — ein
  // gemessener Beleg statt einer Behauptung, und er stimmt auch dann noch,
  // wenn sich Verbrauch oder Zeitraum ändern. Wohnungen ohne Verbrauch
  // bleiben außen vor: 0 kWh haben keinen PV-Anteil, und ein erfundener
  // Nullwert zöge die Spannweite künstlich auseinander.
  const anteile = Object.values(daten.meters ?? {})
    .filter((m) => m.consumption_kwh > 0)
    .map((m) => m.solar_kwh / m.consumption_kwh * 100);
  if (anteile.length > 1) {
    const min = Math.min(...anteile);
    const max = Math.max(...anteile);
    const spann = document.createElement('span');
    spann.className = 'modellinfo__rechnung';
    spann.textContent = text.modellSpannweite
      .replace('{min}', `${zahl.anteil.format(min)} %`)
      .replace('{max}', `${zahl.anteil.format(max)} %`)
      .replace('{spanne}', `${zahl.anteil.format(max - min)} Punkte`);
    empfehlung.append(spann);
  }
  el.abrechnungModellInfo.append(empfehlung);
}

function abrechnungZeichnen(daten) {
  abrechnungZeitraeumeBauen(
    el.abrechnungZeitraum, daten.ranges, daten.range.key);
  abrechnungWahlBauen(
    el.abrechnungModell, daten.models, daten.model,
    config.abrechnungModellSchluessel, (k) => { abrechnungModell = k; });

  modellInfoZeichnen(daten);
  exportLinksSetzen(daten);
  urteilZeichnen(daten.quality.verdict);

  // Reihenfolge wie bei den Wohnungskacheln: nach Stockwerk, nicht
  // alphabetisch. Unbekannte Zähler hängen hinten an, statt zu fehlen.
  const ids = Object.keys(daten.meters).sort((a, b) => {
    const ia = config.reihenfolge.indexOf(a);
    const ib = config.reihenfolge.indexOf(b);
    if (ia === -1 && ib === -1) return a.localeCompare(b, 'de');
    if (ia === -1) return 1;
    if (ib === -1) return -1;
    return ia - ib;
  });

  // Eingeklappt der PV-Anteil je Wohnung: die eine Zahl, für die der
  // Abschnitt da ist. Wohnungen ohne Verbrauch haben keinen Anteil und
  // blieben mit „0 %" irreführend stehen. Über die Schlüssel statt über
  // zaehlerReihenfolge(): die Zuteilung liefert ihre Zähler ohne `id`-Feld.
  kurzTeile(el.kurzAbrechnung, ids
    .filter((id) => daten.meters[id]?.consumption_kwh > 0)
    .map((id) => {
      const m = daten.meters[id];
      // „% PV" statt „% Erzeugung": gemeint ist der Anteil am eigenen
      // Verbrauch, nicht die erzeugte Menge — und eingeklappt ist kein
      // Platz für den ausgeschriebenen Namen.
      return `${id.toUpperCase()}: `
        + `${zahl.anteil.format(m.solar_kwh / m.consumption_kwh * 100)} % PV`;
    }));

  el.abrechnung.replaceChildren();
  let summe = 0;
  for (const id of ids) {
    const werte = daten.meters[id];
    summe += werte.consumption_kwh;
    const solarAnteil = werte.consumption_kwh > 0
      ? (werte.solar_kwh / werte.consumption_kwh) * 100 : 0;

    const kachel = karte({
      name: daten.names?.[id] ?? id.toUpperCase(),
      wert: zahl.kwh.format(werte.consumption_kwh),
      einheit: text.einheitKwh,
      zeilen: [
        [text.abrechnungSolar,
         `${zahl.kwh.format(werte.solar_kwh)} ${text.einheitKwh}`],
        [text.abrechnungNetz,
         `${zahl.kwh.format(werte.grid_kwh)} ${text.einheitKwh}`],
        [text.abrechnungSolarAnteil,
         `${zahl.anteil.format(solarAnteil)} %`],
      ],
    });
    // Der Balken gehört zwischen die große Zahl und die Zeilen — dort
    // liest man ihn als Aufteilung genau dieser Zahl.
    kachel.insertBefore(anteilBalken(werte.solar_kwh, werte.grid_kwh),
                        kachel.querySelector('.karte__wert').nextSibling);
    el.abrechnung.append(kachel);
  }

  el.summeAbrechnung.textContent =
    `${zahl.kwh.format(summe)} ${text.einheitKwh}`;

  // Der Grund wird aus den Zahlen abgeleitet, nicht pauschal genannt:
  // beide Ursachen sehen in der Summe gleich aus, erklären sich aber
  // verschieden. Ein fester Text läge in einem der Fälle daneben.
  const rest = daten.house.unallocated_grid_kwh;
  const still = daten.house.unallocated_idle_kwh ?? 0;
  const deckel = daten.house.unallocated_capped_kwh ?? 0;
  if (rest > 0.001) {
    const gruende = [];
    if (deckel > 0.001) gruende.push(text.abrechnungRestDeckel);
    if (still > 0.001) gruende.push(text.abrechnungRestStill);
    gruende.push(text.abrechnungRestSchluss);
    el.abrechnungRest.textContent =
      `${text.abrechnungRest}: ${zahl.kwh.format(rest)} `
      + `${text.einheitKwh} — ${gruende.join(' ')}`;
  } else {
    el.abrechnungRest.textContent = '';
  }
}

async function abrechnungAbrufen() {
  // Ein zweiter Abruf, während der erste noch rechnet, brächte nichts und
  // belastete den Pi doppelt.
  if (abrechnungLaeuft) return;
  abrechnungLaeuft = true;
  try {
    const felder = new URLSearchParams();
    if (abrechnungZeitraum) felder.set('range', abrechnungZeitraum);
    if (abrechnungModell) felder.set('model', abrechnungModell);
    const antwort = await fetch(`/allocation?${felder}`, { cache: 'no-store' });

    if (antwort.status === 503) {
      // Hausbilanz abgeschaltet — der ganze Abschnitt hat dann keinen Sinn.
      el.bereichAbrechnung.hidden = true;
      return;
    }
    if (!antwort.ok) throw new Error(`HTTP ${antwort.status}`);

    const daten = await antwort.json();
    el.bereichAbrechnung.hidden = false;
    abrechnungZeichnen(daten);
  } catch {
    // Die Abrechnung ist ein Zusatz: ihr Ausfall darf die Live-Anzeige
    // nicht verdecken. Deshalb hier eine eigene Meldung statt el.hinweis.
    if (!el.abrechnung.children.length) {
      el.abrechnungUrteil.replaceChildren();
      el.abrechnungUrteil.dataset.stufe = 'unbrauchbar';
      el.abrechnungUrteil.textContent = text.abrechnungFehler;
    }
  } finally {
    abrechnungLaeuft = false;
  }
}

async function abrufen() {
  try {
    const antwort = await fetch(config.quelle, { cache: 'no-store' });
    // Ohne diese Prüfung wird ein 500er als gültige Antwort behandelt
    // und scheitert erst beim Auslesen — mit einer irreführenden Meldung.
    if (!antwort.ok) throw new Error(`HTTP ${antwort.status}`);
    const daten = await antwort.json();

    const summe = zaehlerZeichnen(daten);
    const warnungHaus = hausZeichnen(daten, summe);
    const warnungSystem = systemZeichnen(daten);
    fussZeichnen(daten);

    kurzfassungenZeichnen(daten, summe);

    letzterErfolg = new Date();
    el.stand.textContent = `${text.stand} ${zahl.uhr.format(letzterErfolg)}`;
    fassungZeichnen(daten.fassung);
    // Ein Rechnerproblem wiegt schwerer als eine Bilanzabweichung: fehlt der
    // Speicherplatz, hört die Erfassung ganz auf.
    el.hinweis.textContent = warnungSystem ?? warnungHaus ?? '';
  } catch {
    el.hinweis.textContent = letzterErfolg
      ? text.keineVerbindung + zahl.uhr.format(letzterErfolg)
      : text.keineVerbindung.replace(' Letzter Stand von ', '');
  }
}

/* --- Start ---------------------------------------------------------- */

document.title = config.titel;
el.titel.textContent = config.titel;
el.titelWohnungen.textContent = text.wohnungen;
el.titelHaus.textContent = text.haus;
el.quelleWohnungen.textContent = `(${text.wohnungenQuelle})`;
el.titelSystem.textContent = text.system;
el.titelAbrechnung.textContent = text.abrechnung;
el.quelleAbrechnung.textContent = `(${text.abrechnungQuelle})`;

// Ein Zuhörer, der Zustand liegt am Dokument — nicht zwei, die sich
// gegenseitig überholen.
el.thema.addEventListener('click', () => {
  themaSetzen(document.documentElement.dataset.theme === 'dunkel' ? 'hell' : 'dunkel');
});

themaStarten();
klappenStarten();
offeneLaden();
zeitwahlBauen();
abrechnungWahlLaden();
abrufen();
abrechnungAbrufen();

// Offene Diagramme in eigenem, langsamerem Takt nachladen.
setInterval(alleVerlaeufeNeu, config.verlaufIntervallMs);
setInterval(abrufen, config.intervallMs);
// Die Abrechnung in noch langsamerem Takt: sie rechnet über lange
// Zeiträume und ändert sich in zwei Minuten kaum sichtbar.
setInterval(abrechnungAbrufen, config.abrechnungIntervallMs);
