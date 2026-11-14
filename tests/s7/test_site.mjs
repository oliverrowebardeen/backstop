import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync, readdirSync} from 'node:fs';
import {findingsFrom, coverageBySetup, measured} from '../../ui/results.js';
import {flowLabels, rescueChain, enginesPerSearch, FALLBACK_ORDER} from '../../ui/flow.js';

const dir = new URL('../../ui/fixtures/', import.meta.url);
const read = name => readFileSync(new URL(name, dir), 'utf8');

test('every shipped race recording points at logs that exist, and every fixture log is used', () => {
  const manifests = readdirSync(dir).filter(name => /^race-.*\.json$/.test(name)).map(name => JSON.parse(read(name)));
  assert.ok(manifests.length > 0);
  const listed = new Set(manifests.flatMap(m => [m.left, m.right]));
  const files = readdirSync(dir).filter(name => name.endsWith('.jsonl'));
  assert.deepEqual([...listed].sort(), files.sort());
  for (const name of listed) assert.equal(JSON.parse(read(name).split('\n')[0]).type, 'run_start', name);
});

test('a setup that finished slightly fewer runs is reported on its own count', () => {
  const block = (m, c, w) => ({n: m, correct: c, wrong: w, declined: m - c - w, accuracy: c / m, wrong_rate: w / m, cost_per_q_total: 0.01});
  const data = {n_test: 200, setups: {single: block(200, 188, 8), telem_default: block(200, 189, 11), second_opinion: block(199, 188, 10)}};
  assert.ok(measured(data));
  data.setups.second_opinion = block(150, 140, 5);
  assert.equal(measured(data), null, 'too many unscored runs stays pending');
});

test('results stay pending without complete measured setups', () => {
  assert.equal(measured(null), null);
  const block = (c, w) => ({correct: c, wrong: w, declined: 10 - c - w, accuracy: c / 10, cost_per_q_total: 0.01});
  const full = {n_test: 10, setups: {single: block(7, 2), telem_default: block(9, 1), second_opinion: block(9, 0)}};
  assert.ok(measured(full));
  assert.equal(measured(full, false), null, 'server vetting can veto');
});

test('frontier coverage keeps the simulated flag and question counts', () => {
  const frontier = JSON.parse(readFileSync(new URL('../../out/frontier.json', import.meta.url), 'utf8'));
  const points = coverageBySetup(frontier);
  if (Array.isArray(frontier.measured_runs)) {
    assert.equal(points.single.kind, 'single', 'measured agent runs win over the hit matrix');
    assert.ok(Number.isInteger(points.second_opinion.n_questions));
  } else assert.equal(points.single.engines[0], 'tavily');
  if (points.second_opinion?.simulated) assert.equal(points.second_opinion.simulated, true);
});

test('findings split into two and a fake-data banner hides them', () => {
  assert.deepEqual(findingsFrom('> banner\n\nFirst part. On the test split second part.'), ['First part.', 'On the test split second part.']);
  assert.deepEqual(findingsFrom('> FAKE DATA\n\nx'), []);
  assert.deepEqual(findingsFrom(null), []);
});

test('flow labels come from the rescue table, plain labels without it', () => {
  const table = {built_on_split: 'learn', n_questions: 100, miss_counts: {tavily: 30}, order_after: {tavily: ['exa']}, rescue: {tavily: {exa: {rescued: 15, misses: 30}}}};
  assert.equal(flowLabels(table).miss, 'Tavily missed\n30 of 100');
  assert.equal(flowLabels(table).rescued, 'Exa rescued\n15 of 30');
  assert.equal(flowLabels(null).miss, 'on a\nmiss');
});

test('rescue chain follows order_after of the engine that just missed, then fallback_order', () => {
  const table = JSON.parse(readFileSync(new URL('../../out/rescue_table.json', import.meta.url), 'utf8'));
  assert.deepEqual(rescueChain(table, 'tavily'), ['tavily', 'exa', 'parallel']);
  assert.deepEqual(rescueChain(table, 'perplexity'), ['perplexity', 'exa', 'parallel']);
  assert.deepEqual(rescueChain({order_after: {tavily: ['exa'], exa: ['tavily', 'brave']}, fallback_order: ['ceramic']}, 'tavily'), ['tavily', 'exa', 'brave']);
  assert.deepEqual(rescueChain(null, 'tavily'), ['tavily', ...FALLBACK_ORDER.slice(0, 2)]);
  assert.deepEqual(flowLabels(table).chains.tavily, ['tavily', 'exa', 'parallel']);
  assert.equal(flowLabels(null).orderSource, 'fallback order in so/config.py');
});

test('engines per search divide engine calls by search tool calls', () => {
  const results = {setups: {telem_default: {engine_calls: 1600, search_calls: 280}, single: {engine_calls: 3}}};
  assert.equal(enginesPerSearch(results, 'telem_default').perSearch.toFixed(2), '5.71');
  assert.equal(enginesPerSearch(results, 'single'), null);
  assert.equal(enginesPerSearch(null, 'telem_default'), null);
});

test('no em dashes in the site files', () => {
  for (const name of readdirSync(new URL('../../ui/', import.meta.url)).filter(name => /\.(html|js|css)$/.test(name))) {
    assert.ok(!readFileSync(new URL(`../../ui/${name}`, import.meta.url), 'utf8').includes('\u2014'), name);
  }
});
