import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {HERO_FALLBACK, economy, heroClaimHolds, measured, raceNote, sameAccuracy, validateResults, withEngineNames} from '../../ui/results.js';

const results = JSON.parse(readFileSync(new URL('../../out/results.json', import.meta.url), 'utf8'));

test('the note under the race comes from out/results.json', () => {
  const metrics = measured(results, validateResults(results));
  const {telem_default: base, second_opinion: ours} = results.setups;
  const note = raceNote(metrics);
  assert.ok(note.startsWith('Live single questions vary.'));
  assert.ok(note.includes(`Measured on ${results.n_test} held-out`));
  assert.ok(note.includes(`Backstop ${ours.correct}/${ours.n} vs Telem default ${base.correct}/${base.n} correct`));
  assert.ok(note.includes(`$${ours.cost_per_q_total.toFixed(4)} vs $${base.cost_per_q_total.toFixed(4)}`));
  assert.ok(note.includes(ours.cost_per_q_total < base.cost_per_q_total / 2 ? 'less than half the cost' : 'cost per question'));
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
  assert.ok(section.includes('accuracy-chart') && !section.includes('frontier-chart') && !/coverage/i.test(section));
  const js = readFileSync(new URL('../../ui/results.js', import.meta.url), 'utf8');
  assert.ok(!/coverage/i.test(js), 'no retrieval coverage on the site');
  for (const text of ['Accuracy against total cost per question', 'total cost per question', "'accuracy'"]) assert.ok(js.includes(text), text);
});

test('the chart plots accuracy against TOTAL cost per question from out/results.json', () => {
  const metrics = measured(results, validateResults(results));
  const js = readFileSync(new URL('../../ui/results.js', import.meta.url), 'utf8');
  const chart = js.slice(js.indexOf('export function drawAccuracyCost'), js.indexOf('export function findingsFrom'));
  assert.ok(chart.includes('cost_per_q_total') && !chart.includes('cost_per_q_search') && !chart.includes('frontier.json'));
  for (const key of ['single', 'baseline', 'ours']) assert.ok(Number.isFinite(metrics[key].cost_per_q_total));
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
