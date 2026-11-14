import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {BENCH_COLUMNS, HERO_FALLBACK, benchmarkRows, economy, heroClaimHolds, measured, raceNote, sameAccuracy, validateResults, withEngineNames} from '../../ui/results.js';

const results = JSON.parse(readFileSync(new URL('../../out/results.json', import.meta.url), 'utf8'));

test('the note under the race comes from out/results.json', () => {
  const metrics = measured(results, validateResults(results));
  const {telem_default: base, second_opinion: ours} = results.setups;
  const note = raceNote(metrics);
  const e = economy(metrics);
  assert.ok(note.startsWith('Live single questions vary. On the held-out test questions, Backstop '));
  assert.ok(note.includes(e.same ? `matched Telem default's accuracy (${(100 * base.accuracy).toFixed(1)}%)`
    : `scored ${(100 * ours.accuracy).toFixed(1)}% accuracy`));
  assert.ok(note.includes(`${Math.abs(e.cost)}% ${e.cost >= 0 ? 'lower' : 'higher'} total cost per question ` +
    `($${ours.cost_per_q_total.toFixed(4)} vs $${base.cost_per_q_total.toFixed(4)})`));
  assert.ok(note.includes(`${Math.abs(e.calls)}% ${e.calls >= 0 ? 'fewer' : 'more'} engine calls`));
  // No question count and no correct/scored fractions: the scored totals differ by setup.
  assert.ok(!/\d+\/\d+/.test(note) && !new RegExp(`\\b${results.n_test}\\b`).test(note), note);
  assert.equal(raceNote(null), null);
});

test('Results lead with accuracy, then cost, engine calls and time saved, with no retrieval coverage', () => {
  const metrics = measured(results, validateResults(results));
  const {telem_default: base, second_opinion: ours} = results.setups;
  assert.equal(sameAccuracy(ours, base), Math.abs(ours.accuracy - base.accuracy) < 0.005);
  assert.ok(metrics && Number.isFinite(ours.latency_p50_ms) && Number.isFinite(ours.engine_calls_per_q));
  const e = economy(metrics);
  assert.equal(e.cost, Math.round(100 * (base.cost_per_q_total - ours.cost_per_q_total) / base.cost_per_q_total));
  assert.equal(e.calls, Math.round(100 * (base.engine_calls_per_q - ours.engine_calls_per_q) / base.engine_calls_per_q));
  assert.equal(e.time, Math.round(100 * (base.latency_p50_ms - ours.latency_p50_ms) / base.latency_p50_ms));
  const html = readFileSync(new URL('../../ui/index.html', import.meta.url), 'utf8');
  const section = html.slice(html.indexOf('id="results"'), html.indexOf('id="developers"'));
  assert.ok(section.includes('class="bench-table"') && !section.includes('accuracy-chart') && !section.includes('frontier-chart') && !/coverage/i.test(section));
  const js = readFileSync(new URL('../../ui/results.js', import.meta.url), 'utf8');
  assert.ok(!/coverage/i.test(js), 'no retrieval coverage on the site');
  assert.deepEqual(BENCH_COLUMNS.map(c => c.label), ['Accuracy', 'Cost per question', 'Engine calls per question', 'Median time per question']);
});

test('the benchmark table shows every setup from out/results.json, with zero-based bars and no question counts', () => {
  const metrics = measured(results, validateResults(results));
  const rows = benchmarkRows(metrics);
  assert.deepEqual(rows.map(r => r.name), ['Single engine', 'Telem default', 'Backstop']);
  assert.deepEqual(rows.map(r => r.ours), [false, false, true]);
  const blocks = ['single', 'telem_default', 'second_opinion'].map(key => results.setups[key]);
  const fields = [s => s.accuracy, s => s.cost_per_q_total, s => s.engine_calls_per_q, s => s.latency_p50_ms];
  const text = [v => `${(v * 100).toFixed(1)}%`, v => `$${v.toFixed(4)}`, v => v.toFixed(2), v => `${(v / 1000).toFixed(1)} s`];
  rows.forEach((row, i) => row.cells.forEach((cell, j) => {
    assert.equal(cell.text, text[j](fields[j](blocks[i])), `${row.name} ${cell.label}`);
    // Accuracy runs 0 to 100%; the rest are scaled to the column's largest value.
    const max = j === 0 ? 1 : Math.max(...blocks.map(fields[j]));
    assert.ok(Math.abs(cell.width - fields[j](blocks[i]) / max) < 1e-9, `${row.name} ${cell.label} bar`);
  }));
  // 94.0% and 94.5% must look alike: the accuracy bars differ by under a percent of their length.
  const acc = rows.map(r => r.cells[0].width);
  assert.ok(Math.max(...acc) - Math.min(...acc) < 0.01);
  // Cost uses the total, not search only, and nothing shows a correct-of-scored count such as 188/199.
  const js = readFileSync(new URL('../../ui/results.js', import.meta.url), 'utf8');
  assert.ok(js.includes('s.cost_per_q_total') && !js.includes('cost_per_q_search'));
  assert.ok(!/correct`|\/\$\{scored|of \$\{count\}/.test(js), 'no correct/scored fractions on the site');
  for (const row of rows) for (const cell of row.cells) assert.ok(!/\d+\/\d+/.test(cell.text), cell.text);
  assert.deepEqual(benchmarkRows(null), []);
});

test('the hero headline claim holds for the committed results, and a fallback replaces it if not', () => {
  const metrics = measured(results, validateResults(results));
  const html = readFileSync(new URL('../../ui/index.html', import.meta.url), 'utf8');
  const h1 = html.match(/<h1 id="hero-title">(.*?)<\/h1>/s)[1].replace(/<[^>]+>/g, '').replace(/&nbsp;/g, ' ');
  assert.equal(h1, 'Same accuracy. Half the cost.');
  assert.ok(heroClaimHolds(metrics), 'out/results.json no longer supports "Same accuracy. Half the cost."');
  const worse = structuredClone(results);
  worse.setups.second_opinion.cost_per_q_total = worse.setups.telem_default.cost_per_q_total * 0.9;
  assert.equal(heroClaimHolds(measured(worse, validateResults(worse))), false);
  assert.equal(heroClaimHolds(null), false);
  assert.ok(HERO_FALLBACK && !/\d/.test(HERO_FALLBACK));
});

test('the site drops the scored-runs caveat and the "N held-out questions" phrasing', () => {
  const html = readFileSync(new URL('../../ui/index.html', import.meta.url), 'utf8');
  const js = readFileSync(new URL('../../ui/results.js', import.meta.url), 'utf8');
  const site = js.slice(0, js.indexOf('export function raceNote')) + js.slice(js.indexOf('export async function loadResults'));
  for (const text of ['scored ${', 'one run failed', 'held-out questions per setup', 'questions we never tuned']) {
    assert.ok(!site.includes(text) && !html.includes(text), text);
  }
});

test('findings show engine names and say where they come from', () => {
  assert.equal(withEngineNames('tavily missed 30; exa then parallel'), 'Tavily missed 30; Exa then Parallel');
  const js = readFileSync(new URL('../../ui/results.js', import.meta.url), 'utf8');
  assert.ok(js.includes('Source: analysis/findings.md'));
});
