import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync, readdirSync} from 'node:fs';
import {parseLog, buildScenario, stateAt, searchCalls, ticks} from '../../ui/simulate.js';
import {findingsFrom, coverageBySetup, measured} from '../../ui/results.js';
import {flowLabels} from '../../ui/flow.js';

const dir = new URL('../../ui/fixtures/', import.meta.url);
const read = name => readFileSync(new URL(name, dir), 'utf8');
const manifest = JSON.parse(read('scenarios.json'));

for (const item of manifest.scenarios) {
  test(`${item.id}: costs and engine calls equal the log's search_done totals`, () => {
    const events = parseLog(read(item.log));
    const scenario = buildScenario(events, {call: item.call, ghost: parseLog(read(item.ghost)), baseline: parseLog(read(item.baseline))});
    const done = searchCalls(events)[item.call - 1].done;
    const end = stateAt(scenario, scenario.duration);
    assert.ok(Math.abs(end.counters.cost - done.data.cost_usd) < 1e-12);
    assert.equal(end.counters.fetches, done.data.fetches);
    assert.equal(scenario.final.status, done.data.status);
    assert.ok(['live', 'sample'].includes(item.source));
  });

  test(`${item.id}: scrubbing is pure, backward equals forward`, () => {
    const scenario = buildScenario(parseLog(read(item.log)), {call: item.call});
    const marks = ticks(scenario).map(t => t.at);
    const forward = marks.map(at => JSON.stringify({...stateAt(scenario, at), litEngines: [...stateAt(scenario, at).litEngines]}));
    const backward = [...marks].reverse().map(at => JSON.stringify({...stateAt(scenario, at), litEngines: [...stateAt(scenario, at).litEngines]})).reverse();
    assert.deepEqual(forward, backward);
    for (let i = 1; i < marks.length; i += 1) assert.ok(marks[i] > marks[i - 1], 'ticks strictly increase');
    assert.equal(stateAt(scenario, 0).steps.length, 1);
    assert.equal(stateAt(scenario, 0).counters.cost, 0);
  });
}

test('every scenario log in the manifest exists and every fixture log is listed', () => {
  const listed = new Set(manifest.scenarios.flatMap(s => [s.log, s.ghost, s.baseline]));
  const files = readdirSync(dir).filter(name => name.endsWith('.jsonl'));
  assert.deepEqual([...listed].sort(), files.sort());
});

test('the engine accepts any 6.11 log, including one with no rescue', () => {
  const ts = i => new Date(Date.UTC(2026, 10, 14, 19, 0, i)).toISOString();
  const lines = [
    {type: 'run_start', data: {question: 'Q?'}},
    {type: 'search', data: {query: 'q', goal: 'g', engines: ['tavily'], n_results: 5, cost_usd: 0.005}},
    {type: 'check', data: {verdict: 'HIT', why: 'Result 1 states it.', best_url: 'https://example.com/a'}},
    {type: 'search_done', data: {status: 'HIT', cost_usd: 0.005, fetches: 0, note: 'stop'}},
  ].map((e, i) => JSON.stringify({ts: ts(i), run_id: 'second_opinion-x-0123456789ab', question_id: 'x', setup: 'second_opinion', seq: i + 1, ...e}));
  const scenario = buildScenario(parseLog(lines.join('\n')));
  assert.equal(scenario.final.status, 'HIT');
  assert.equal(scenario.final.evidence.url, 'https://example.com/a');
  assert.equal(scenario.annotations.length, 0);
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
  assert.equal(points.single.engines[0], 'tavily');
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

test('no em dashes in the site files', () => {
  for (const name of ['index.html', 'race.html', 'app.js', 'simulate.js', 'sim-view.js', 'results.js', 'flow.js', 'dots.js', 'style.css']) {
    assert.ok(!readFileSync(new URL(`../../ui/${name}`, import.meta.url), 'utf8').includes('\u2014'), name);
  }
});
