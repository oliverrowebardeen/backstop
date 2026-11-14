import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {measured, raceNote, validateResults} from '../../ui/results.js';

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
