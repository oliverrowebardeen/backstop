import {test} from 'node:test';
import assert from 'node:assert/strict';
import {measured, validateResults} from '../../ui/results.js';

function complete() {
  const block = {n: 10, correct: 7, wrong: 2, declined: 1, accuracy: .7, wrong_rate: .2, cost_per_q_total: .02};
  return {n_test: 10, setups: Object.fromEntries(['single', 'telem_default', 'second_opinion'].map(key => [key, {...block}]))};
}

test('results retain complete measured metrics without the presentation endpoint', () => {
  const data = complete();
  assert.equal(validateResults(data), true);
  assert.equal(measured(data, validateResults(data)).n, 10);
  assert.equal(measured(data).wrong, 0);
});

test('synthetic or incomplete evaluations remain hidden after presentation removal', () => {
  for (const value of [null, {}, {...complete(), n_test: 0}, {...complete(), n_test: 10.5}]) {
    assert.equal(validateResults(value), false);
  }
  for (const flag of ['fake', 'fixture', 'simulated']) assert.equal(validateResults({...complete(), [flag]: true}), false);
  assert.equal(validateResults({...complete(), generated_from: 'out/FAKE/results.json'}), false);
  const data = complete();
  delete data.setups.telem_default;
  assert.equal(validateResults(data), false);
});

test('invalid counts, mismatched rates and invalid costs cannot become measured claims', () => {
  for (const patch of [{correct: -1}, {wrong: 1.5}, {declined: 0}, {n: 9}, {accuracy: .8}, {wrong_rate: .1},
    {cost_per_q_total: -1}, {cost_per_q_total: Infinity}, {accuracy: NaN}, {wrong_rate: undefined}]) {
    const data = complete();
    Object.assign(data.setups.second_opinion, patch);
    assert.equal(validateResults(data), false, JSON.stringify(patch));
    assert.equal(measured(data, validateResults(data)), null);
  }
});

test('worse outcomes still produce negative comparative metrics', () => {
  const data = complete();
  Object.assign(data.setups.second_opinion, {correct: 6, wrong: 3, accuracy: .6, wrong_rate: .3, cost_per_q_total: .03});
  const metrics = measured(data, validateResults(data));
  assert.equal(metrics.wrong, -50);
  assert.ok(metrics.accuracy < 0 && metrics.cost < 0);
});
