/* Prüft die Verteilung des momentanen Netzbezugs auf die Wohnungen.
 *
 * Ruft die echten Funktionen aus dashboard-verteilung.js AUF. Ein Test, der
 * die Rechnung nachbaut, prüft nur seine eigene Kopie — deshalb liegt die
 * Logik in einem Modul ohne Zugriff auf document/localStorage.
 *
 * Lauf:
 *   node --test tests/test_dashboard_verteilung.mjs
 *
 * Auch von tools/js_tests.py aufgerufen, damit „keine Tests gefunden" als
 * Fehler zählt statt als Erfolg.
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';

import {
  anteileProportional,
  anteileWasserfall,
  netzanteile,
} from '../s0counter/dashboard-verteilung.js';

/** Kurzform: Antwort des Dienstes mit Bilanz. */
function daten({ zeit = 1000, ts = 1000, netz = 0, an = true,
                 exportW = null } = {}) {
  return {
    time: zeit,
    balance: {
      enabled: an,
      latest: {
        ts,
        grid_import_w: exportW === null ? netz : undefined,
        grid_w: exportW === null ? netz : -exportW,
      },
    },
  };
}

const DREI = [
  { id: 'eg', power_w: 200 },
  { id: 'og', power_w: 300 },
  { id: 'dg', power_w: 500 },
];

test('anteilig: jede Wohnung deckt denselben Prozentsatz aus dem Netz', () => {
  const a = anteileProportional([['eg', 200], ['og', 300], ['dg', 500]], 500);
  assert.equal(a.get('eg'), 100);
  assert.equal(a.get('og'), 150);
  assert.equal(a.get('dg'), 250);
});

test('anteilig: Deckel am eigenen Verbrauch', () => {
  // Das Haus bezieht mehr, als die Wohnungen zusammen verbrauchen — der
  // Rest geht an Verbraucher hinter keinem Zähler. Ohne den Deckel bekäme
  // eine Wohnung mehr Netzbezug als Verbrauch zugerechnet.
  const a = anteileProportional([['eg', 200], ['og', 300]], 5000);
  assert.equal(a.get('eg'), 200);
  assert.equal(a.get('og'), 300);
});

test('anteilig: alles aus PV heißt überall 0 W', () => {
  const a = anteileProportional([['eg', 200], ['og', 300]], 0);
  assert.equal(a.get('eg'), 0);
  assert.equal(a.get('og'), 0);
});

test('anteilig: keine Last liefert 0 statt Division durch null', () => {
  const a = anteileProportional([['eg', 0], ['og', 0]], 400);
  assert.equal(a.get('eg'), 0);
  assert.equal(a.get('og'), 0);
});

test('wasserfall: gleichmäßig, solange keiner gedeckelt ist', () => {
  const a = anteileWasserfall([['eg', 500], ['og', 500]], 600);
  assert.equal(a.get('eg'), 300);
  assert.equal(a.get('og'), 300);
});

test('wasserfall: Überschuss fließt an die übrigen weiter', () => {
  // 600 W auf drei: je 200. eg kann nur 50 tragen, die übrigen 150 gehen
  // zu gleichen Teilen an og und dg — je 75 obendrauf.
  const a = anteileWasserfall([['eg', 50], ['og', 400], ['dg', 400]], 600);
  assert.equal(a.get('eg'), 50);
  assert.equal(a.get('og'), 275);
  assert.equal(a.get('dg'), 275);
});

test('wasserfall: Netzbezug über der Summe hängt sich nicht auf', () => {
  // Terminiert die Schleife? Bei einem Bezug über der Summe wird in der
  // ersten Runde jede Wohnung gedeckelt, `offen` läuft leer.
  //
  // Eine Endlosschleife lässt sich hier nicht per Test-Timeout abfangen:
  // sie blockiert den Event-Loop, node:test kommt gar nicht mehr dran.
  // Deshalb setzt tools/js_tests.py ein Zeitlimit auf den ganzen Lauf und
  // meldet den Abbruch als „nicht gelaufen" — ein hängendes Gate ist so
  // wertlos wie ein blindes.
  const a = anteileWasserfall([['eg', 100], ['og', 200]], 9999);
  assert.equal(a.get('eg'), 100);
  assert.equal(a.get('og'), 200);
});

test('wasserfall: summiert sich auf den Netzbezug', () => {
  const a = anteileWasserfall([['eg', 120], ['og', 480], ['dg', 90]], 400);
  const summe = [...a.values()].reduce((s, w) => s + w, 0);
  assert.ok(Math.abs(summe - 400) < 1e-6, `Summe ${summe} statt 400`);
});

test('netzanteile: 100 % PV ergibt überall 0 W', () => {
  const a = netzanteile(daten({ netz: 0 }), DREI, 'proportional', 300);
  assert.deepEqual([...a.values()], [0, 0, 0]);
});

test('netzanteile: Einspeisung zählt nicht als Bezug', () => {
  // grid_w negativ = Einspeisung. Ein fehlendes Math.max(0, …) machte
  // daraus einen negativen "Bezug" und damit negative Klammern.
  const roh = daten({ netz: 0 });
  roh.balance.latest.grid_import_w = undefined;
  roh.balance.latest.grid_w = -1800;
  const a = netzanteile(roh, DREI, 'proportional', 300);
  assert.deepEqual([...a.values()], [0, 0, 0]);
});

test('netzanteile: ohne Hausbilanz null statt 0 W', () => {
  // Der Unterschied ist der ganze Zweck: 0 W wäre eine Behauptung, die
  // nicht gemessen ist.
  assert.equal(netzanteile(daten({ an: false }), DREI, 'proportional', 300),
               null);
  assert.equal(netzanteile({ time: 1, balance: { enabled: true } }, DREI,
                           'proportional', 300), null);
});

test('netzanteile: alter Messpunkt gilt als nicht gemessen', () => {
  const alt = daten({ zeit: 5000, ts: 1000, netz: 900 });
  assert.equal(netzanteile(alt, DREI, 'proportional', 300), null);
});

test('netzanteile: frischer Messpunkt wird gerechnet', () => {
  // Gegenprobe nach GRÜN: eine Altersgrenze, die den gesunden Fall
  // verwirft, würde die Anzeige dauerhaft leer lassen.
  const frisch = daten({ zeit: 1060, ts: 1000, netz: 1000 });
  const a = netzanteile(frisch, DREI, 'proportional', 300);
  assert.notEqual(a, null);
  assert.equal(a.get('eg'), 200);
  assert.equal(a.get('og'), 300);
  assert.equal(a.get('dg'), 500);
});

test('netzanteile: das gewählte Modell wird benutzt', () => {
  // Ohne diesen Test könnte das Modell fest verdrahtet sein und die
  // Klammer wiche von der Abrechnung darunter ab.
  const d = daten({ netz: 600 });
  const eintraege = [{ id: 'eg', power_w: 50 },
                     { id: 'og', power_w: 400 },
                     { id: 'dg', power_w: 400 }];
  const prop = netzanteile(d, eintraege, 'proportional', 300);
  const fall = netzanteile(d, eintraege, 'waterfall', 300);
  assert.notEqual(prop.get('eg'), fall.get('eg'));
  assert.equal(fall.get('eg'), 50);
});

test('netzanteile: unbekanntes Modell fällt auf anteilig zurück', () => {
  const a = netzanteile(daten({ netz: 500 }), DREI, 'quatsch', 300);
  assert.equal(a.get('eg'), 100);
});

test('netzanteile: fehlende Leistung zählt als 0, nicht als NaN', () => {
  const a = netzanteile(daten({ netz: 500 }),
                        [{ id: 'eg' }, { id: 'og', power_w: 500 }],
                        'proportional', 300);
  assert.equal(a.get('eg'), 0);
  assert.equal(a.get('og'), 500);
});
