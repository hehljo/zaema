/* Verlaufsdiagramm als SVG, ohne Bibliothek.
 *
 * Eine Kurve mit Fläche, dazu ein Band zwischen Minimum und Maximum, wo
 * verdichtete Daten es hergeben. Das Band ist kein Schmuck: ab der
 * Viertelstunden-Stufe ist jeder Punkt ein Mittelwert, und ein Mittel von
 * 52 °C sagt nichts darüber, ob zwischendrin 78 °C anlagen.
 *
 * Gezeichnet wird in einem festen Koordinatenraum (VIEW), den das SVG über
 * `preserveAspectRatio="none"` auf die tatsächliche Breite streckt. Deshalb
 * tragen Linien `vector-effect: non-scaling-stroke` — ohne das würde die
 * Strichstärke mitverzerrt.
 */

const VIEW = { w: 320, h: 140 };
const RAND = { oben: 8, unten: 18, links: 34, rechts: 4 };

const zahlKurz = new Intl.NumberFormat('de-DE', { maximumFractionDigits: 0 });
const zahlFein = new Intl.NumberFormat('de-DE', { maximumFractionDigits: 1 });
const uhr = new Intl.DateTimeFormat('de-DE', {
  hour: '2-digit', minute: '2-digit',
});
const tagUhr = new Intl.DateTimeFormat('de-DE', {
  day: '2-digit', month: '2-digit', hour: '2-digit',
});

function svgEl(name, attribute = {}) {
  const el = document.createElementNS('http://www.w3.org/2000/svg', name);
  for (const [schluessel, wert] of Object.entries(attribute)) {
    el.setAttribute(schluessel, String(wert));
  }
  return el;
}

/** Achsenbeschriftung: runde Werte statt der exakten Extremwerte. */
function schrittweite(spanne) {
  if (spanne <= 0) return 1;
  const roh = spanne / 3;
  const groesse = 10 ** Math.floor(Math.log10(roh));
  const rest = roh / groesse;
  const stufe = rest >= 5 ? 5 : rest >= 2 ? 2 : 1;
  return stufe * groesse;
}

/**
 * Zeichnet den Verlauf und gibt das SVG-Element zurück.
 * `punkte` sind Objekte {t, avg, min, max}; avg darf null sein (Lücke).
 */
export function diagramm(punkte, { einheit = '', von, bis } = {}) {
  const svg = svgEl('svg', {
    class: 'diagramm',
    viewBox: `0 0 ${VIEW.w} ${VIEW.h}`,
    preserveAspectRatio: 'none',
    role: 'img',
  });

  const gueltig = punkte.filter((p) => p.avg !== null && p.avg !== undefined);
  if (gueltig.length < 2) return null;

  const werte = [];
  for (const p of gueltig) {
    werte.push(p.avg);
    if (p.min !== null && p.min !== undefined) werte.push(p.min);
    if (p.max !== null && p.max !== undefined) werte.push(p.max);
  }

  let unten = Math.min(...werte);
  let oben = Math.max(...werte);

  // Eine völlig flache Reihe hätte Spanne 0 und würde durch Null teilen.
  if (oben - unten < 1e-9) {
    const puffer = Math.max(Math.abs(oben) * 0.1, 1);
    unten -= puffer;
    oben += puffer;
  } else {
    const luft = (oben - unten) * 0.08;
    unten -= luft;
    oben += luft;
  }

  // Die Zeitachse spannt den angefragten Zeitraum — aber nur, solange er
  // halbwegs gefüllt ist. Kurz nach dem Start reichen die Daten oft nur
  // über einen Bruchteil: eine Kurve, die auf 11 % der Breite klebt und
  // daneben leere Fläche zeigt, ist schlechter lesbar als eine, die den
  // vorhandenen Bereich nutzt. Die tatsächliche Spanne steht ohnehin an
  // der Achse, es wird also nichts verschleiert.
  const datenVon = gueltig[0].t;
  const datenBis = gueltig[gueltig.length - 1].t;
  const angefragt = Math.max((bis ?? datenBis) - (von ?? datenVon), 1);
  const belegt = Math.max(datenBis - datenVon, 1);

  const wenigDaten = belegt / angefragt < 0.5;
  const t0 = wenigDaten ? datenVon : (von ?? datenVon);
  const t1 = wenigDaten ? datenBis : (bis ?? datenBis);
  const zeitSpanne = Math.max(t1 - t0, 1);

  const breite = VIEW.w - RAND.links - RAND.rechts;
  const hoehe = VIEW.h - RAND.oben - RAND.unten;
  const x = (t) => RAND.links + ((t - t0) / zeitSpanne) * breite;
  const y = (v) => RAND.oben + (1 - (v - unten) / (oben - unten)) * hoehe;

  // --- Achsenlinien und Beschriftung ---
  const schritt = schrittweite(oben - unten);
  const format = schritt < 10 ? zahlFein : zahlKurz;
  const ersteMarke = Math.ceil(unten / schritt) * schritt;
  for (let wert = ersteMarke; wert <= oben; wert += schritt) {
    const yy = y(wert);
    svg.append(svgEl('line', {
      class: 'diagramm__achse',
      x1: RAND.links, x2: VIEW.w - RAND.rechts, y1: yy, y2: yy,
    }));
    const text = svgEl('text', {
      class: 'diagramm__marke',
      x: RAND.links - 4, y: yy + 3, 'text-anchor': 'end',
    });
    text.textContent = format.format(wert);
    svg.append(text);
  }

  // --- Band zwischen Minimum und Maximum ---
  const hatBand = gueltig.some(
    (p) => p.min !== null && p.max !== null && p.max - p.min > 1e-9);
  if (hatBand) {
    const oberkante = gueltig.map((p) => `${x(p.t)},${y(p.max ?? p.avg)}`);
    const unterkante = gueltig
      .slice().reverse()
      .map((p) => `${x(p.t)},${y(p.min ?? p.avg)}`);
    svg.append(svgEl('polygon', {
      class: 'diagramm__band',
      points: [...oberkante, ...unterkante].join(' '),
    }));
  }

  // --- Fläche und Linie ---
  // Lücken trennen die Linie in Abschnitte: eine durchgezogene Linie über
  // eine Messlücke behauptet Werte, die nie gemessen wurden.
  const abschnitte = [];
  let laufend = [];
  for (const p of punkte) {
    if (p.avg === null || p.avg === undefined) {
      if (laufend.length > 1) abschnitte.push(laufend);
      laufend = [];
    } else {
      laufend.push(p);
    }
  }
  if (laufend.length > 1) abschnitte.push(laufend);

  const basis = VIEW.h - RAND.unten;
  for (const abschnitt of abschnitte) {
    const punkteText = abschnitt.map((p) => `${x(p.t)},${y(p.avg)}`).join(' ');
    svg.append(svgEl('polygon', {
      class: 'diagramm__flaeche',
      points: `${x(abschnitt[0].t)},${basis} ${punkteText} `
            + `${x(abschnitt[abschnitt.length - 1].t)},${basis}`,
    }));
    svg.append(svgEl('polyline', {
      class: 'diagramm__linie', points: punkteText,
    }));
  }

  // --- Zeitachse ---
  const langerZeitraum = zeitSpanne > 36 * 3600;
  const zeitFormat = langerZeitraum ? tagUhr : uhr;
  for (const anteil of [0, 0.5, 1]) {
    const t = t0 + zeitSpanne * anteil;
    const text = svgEl('text', {
      class: 'diagramm__marke',
      x: x(t), y: VIEW.h - 5,
      'text-anchor': anteil === 0 ? 'start' : anteil === 1 ? 'end' : 'middle',
    });
    text.textContent = zeitFormat.format(new Date(t * 1000));
    svg.append(text);
  }

  const spanneText = `${format.format(Math.min(...werte))} bis `
                   + `${format.format(Math.max(...werte))} ${einheit}`;
  svg.setAttribute('aria-label', `Verlauf, ${spanneText}`);
  return svg;
}

/** Kennzahlen unter dem Diagramm: Bereich und Mittel. */
export function kennzahlen(punkte, einheit) {
  const werte = punkte
    .map((p) => p.avg)
    .filter((v) => v !== null && v !== undefined);
  if (!werte.length) return null;
  const min = Math.min(...punkte.map((p) => p.min ?? p.avg).filter(
    (v) => v !== null && v !== undefined));
  const max = Math.max(...punkte.map((p) => p.max ?? p.avg).filter(
    (v) => v !== null && v !== undefined));
  const mittel = werte.reduce((s, v) => s + v, 0) / werte.length;
  const f = (max - min) < 10 ? zahlFein : zahlKurz;
  return {
    min: `${f.format(min)} ${einheit}`,
    max: `${f.format(max)} ${einheit}`,
    mittel: `${f.format(mittel)} ${einheit}`,
    punkte: werte.length,
  };
}
