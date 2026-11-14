import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync, readdirSync} from 'node:fs';
import {findingsFrom, measured} from '../../ui/results.js';
import {flowLabels, rescueChain, FALLBACK_ORDER} from '../../ui/flow.js';

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

test('findings split into two and a fake-data banner hides them', () => {
  assert.deepEqual(findingsFrom('> banner\n\nFirst part. On the test split second part.'), ['First part.', 'On the test split second part.']);
  assert.deepEqual(findingsFrom('> FAKE DATA\n\nx'), []);
  assert.deepEqual(findingsFrom(null), []);
});

test('rescue chain follows order_after of the engine that just missed, then fallback_order', () => {
  const table = JSON.parse(readFileSync(new URL('../../out/rescue_table.json', import.meta.url), 'utf8'));
  assert.deepEqual(rescueChain(table, 'tavily'), ['tavily', 'exa', 'parallel']);
  assert.deepEqual(rescueChain(table, 'perplexity'), ['perplexity', 'exa', 'parallel']);
  assert.deepEqual(rescueChain({order_after: {tavily: ['exa'], exa: ['tavily', 'brave']}, fallback_order: ['ceramic']}, 'tavily'), ['tavily', 'exa', 'brave']);
  assert.deepEqual(rescueChain(null, 'tavily'), ['tavily', ...FALLBACK_ORDER.slice(0, 2)]);
  assert.deepEqual(flowLabels(table).chains.tavily, ['tavily', 'exa', 'parallel']);
});

test('the How it works diagram is a product flow: no connector labels, numbers or answer-key line', () => {
  const html = readFileSync(new URL('../../ui/index.html', import.meta.url), 'utf8');
  const how = html.slice(html.indexOf('id="how"'), html.indexOf('class="strip"'));
  for (const text of ['goal on every call', 'top 5', 'missed', 'answer key', 'max 2', 'engines per search']) assert.ok(!how.toLowerCase().includes(text), text);
  for (const text of ['Agent asks', 'Telem searches', 'Checker', 'Rescue on a miss', 'Answer', 'Fast decision model', 'LLM checker', 'Tavily Extract', 'Not found']) assert.ok(how.includes(text), text);
  assert.ok(!/\d+(\.\d+)?\s*(%|s\b|of\s+\d)/.test(how.replace(/<[^>]+>/g, ' ')), 'no numbers in the diagram text');
  const js = readFileSync(new URL('../../ui/flow.js', import.meta.url), 'utf8');
  assert.ok(!js.includes("'text'"), 'flow.js draws no connector labels');
});

test('no em dashes in the site files', () => {
  for (const name of readdirSync(new URL('../../ui/', import.meta.url)).filter(name => /\.(html|js|css)$/.test(name))) {
    assert.ok(!readFileSync(new URL(`../../ui/${name}`, import.meta.url), 'utf8').includes('\u2014'), name);
  }
});
