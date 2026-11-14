import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {applyEvent, failRun, newRun, safeURL, VERDICTS} from '../../ui/race-state.js';

function fixture(setup) {
  return readFileSync(new URL(`./fixtures/${setup}.jsonl`, import.meta.url), 'utf8').trim().split('\n').map(JSON.parse);
}

for (const setup of ['single', 'second_opinion']) {
  test(`${setup} counters equal the log totals, including checker cost`, () => {
    const state = newRun(setup, 'tavily');
    const events = fixture(setup);
    events.slice(0, -1).forEach(event => applyEvent(state, event));
    const totals = events.at(-1).data;
    assert.ok(Math.abs(state.cost - totals.cost_usd_total) < 1e-10);
    applyEvent(state, events.at(-1));
    assert.equal(state.cost, totals.cost_usd_total);
    assert.equal(state.engineCalls, totals.engine_calls);
    assert.equal(state.elapsed, totals.latency_ms);
    assert.equal(state.telemCalls, 2);
    assert.equal(state.events.length, events.length);
    assert.equal(state.finished, true);
    assert.equal(state.steps[0].engines[0], 'tavily');
    assert.ok(state.session.startsWith('fixture-'));
    assert.equal(state.answer.citations.length, 1);
  });
}

test('reconnected events never inflate cost or the audit trail', () => {
  const state = newRun('single', 'tavily');
  const events = fixture('single');
  events.forEach(event => applyEvent(state, event));
  events.forEach(event => assert.equal(applyEvent(state, event), false));
  assert.equal(state.events.length, 8);
  assert.equal(state.cost, .008);
});

test('page reads preserve their provider and the final check', () => {
  const state = newRun('second_opinion', 'tavily');
  fixture('second_opinion').forEach(event => applyEvent(state, event));
  const page = state.steps.find(step => step.kind === 'fetch');
  assert.equal(page.query, 'read by Tavily Extract');
  assert.equal(page.verdict, 'HIT');
  assert.equal(state.steps[0].verdict, 'STALE');
});

test('not found is a completed outcome', () => {
  const state = newRun('single', 'tavily');
  const events = fixture('single');
  events.find(event => event.type === 'answer').data = {final: 'NOT FOUND', status: 'NOT_FOUND', citations: []};
  events.forEach(event => applyEvent(state, event));
  assert.equal(state.status, 'Not found');
  assert.equal(state.finished, true);
});

test('a multi-engine Telem call counts every engine and one API call', () => {
  const state = newRun('telem_default', 'tavily');
  const event = structuredClone(fixture('single')[2]);
  event.setup = 'telem_default';
  event.data.engines = ['exa', 'tavily', 'brave'];
  applyEvent(state, event);
  assert.equal(state.engineCalls, 3);
  assert.equal(state.telemCalls, 1);
  assert.deepEqual(state.steps[0].engines, ['tavily', 'exa', 'brave']);
});

test('source URLs permit only HTTP and HTTPS', () => {
  assert.equal(safeURL('javascript:alert(1)'), null);
  assert.equal(safeURL('data:text/html,test'), null);
  assert.equal(safeURL('/relative'), null);
  assert.equal(safeURL('https://example.org/source').hostname, 'example.org');
});

test('all five checker verdicts have readable labels', () => {
  for (const verdict of ['HIT', 'STALE', 'WRONG_ENTITY', 'SHALLOW', 'DEAD']) assert.ok(VERDICTS[verdict]);
});

test('malformed events fail clearly before corrupting the audit trail', () => {
  const state = newRun('single', 'tavily');
  const event = fixture('single')[2];
  delete event.data.engines;
  assert.throws(() => applyEvent(state, event), /missing its engines/);
  assert.equal(state.events.length, 0);
  event.type = 'run_end';
  assert.throws(() => applyEvent(state, event), /missing valid final totals/);
});

for (const setup of ['single', 'second_opinion']) {
  test(`${setup} failure cleanup is never a completed answer`, () => {
    const state = newRun(setup, 'tavily');
    const events = fixture(setup);
    applyEvent(state, events[0]);
    const end = structuredClone(events.at(-1));
    Object.assign(end.data, {engine_calls: 0, cost_usd_total: 0, error: 'HTTPStatusError'});
    applyEvent(state, end);
    assert.equal(state.status, 'Failed');
    assert.equal(state.finished, true);
    assert.equal(state.answer, null);
    assert.equal(state.cost, 0);
    failRun(state, 'The provider usage limit has been reached. Check account limits or billing.');
    assert.match(state.failure, /usage limit/);
    assert.equal(state.status, 'Failed');
  });
}

test('a terminal error overrides completion and preserves recorded totals', () => {
  const state = newRun('single', 'tavily');
  fixture('single').forEach(event => applyEvent(state, event));
  failRun(state, 'The provider is unavailable. Try again later.');
  assert.equal(state.status, 'Failed');
  assert.equal(state.cost, .008);
  assert.match(state.failure, /unavailable/);
});

test('failed and deliberate not-found columns stay distinct', () => {
  const left = newRun('single', 'tavily');
  const right = newRun('second_opinion', 'tavily');
  failRun(left, 'The run timed out before it could finish.');
  const events = fixture('second_opinion');
  events.find(event => event.type === 'answer').data = {final: 'NOT FOUND', status: 'NOT_FOUND', citations: []};
  events.forEach(event => applyEvent(right, event));
  assert.equal(left.status, 'Failed');
  assert.equal(right.status, 'Not found');
  assert.equal(right.failure, null);
});

test('totals without an answer are a failure even without an error field', () => {
  const state = newRun('single', 'tavily');
  applyEvent(state, fixture('single').at(-1));
  assert.equal(state.status, 'Failed');
  assert.ok(state.failure);
});
