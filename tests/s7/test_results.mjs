import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {measured, raceNote, sameAccuracy, validateResults} from '../../ui/results.js';

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

test('Results show accuracy as equal, lead with cost and time, and drop retrieval coverage', () => {
  const metrics = measured(results, validateResults(results));
  const {telem_default: base, second_opinion: ours} = results.setups;
  assert.equal(sameAccuracy(ours, base), Math.abs(ours.accuracy - base.accuracy) < 0.005);
  assert.ok(metrics && Number.isFinite(ours.latency_p50_ms) && Number.isFinite(ours.engine_calls_per_q));
  const html = readFileSync(new URL('../../ui/index.html', import.meta.url), 'utf8');
  const section = html.slice(html.indexOf('id="results"'), html.indexOf('id="autopsy"'));
  assert.ok(section.includes('accuracy-chart') && !section.includes('frontier-chart') && !/coverage/i.test(section));
  const js = readFileSync(new URL('../../ui/results.js', import.meta.url), 'utf8');
  assert.ok(!/coverage/i.test(js), 'no retrieval coverage on the site');
  for (const text of ['correct final answers', 'total cost per question', 'Backstop = Telem default']) assert.ok(js.includes(text), text);
});
