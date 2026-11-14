import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {applyEvent, citationVerified, failRun, newRun, safeURL, VERDICTS} from '../../ui/race-state.js';
import {raceControls, liveHeaders} from '../../ui/race-access.js';

function fixture(setup) {
  return readFileSync(new URL(`./fixtures/${setup}.jsonl`, import.meta.url), 'utf8').trim().split('\n').map(JSON.parse);
}

test('replay-only controls fail closed until live mode is explicitly enabled', () => {
  for (const liveAvailable of [false, undefined, null]) {
    const controls = raceControls({liveAvailable, active: false, hasKey: true, replayAvailable: true});
    assert.equal(controls.showCredentials, false);
    assert.equal(controls.readOnlyQuestion, true);
    assert.equal(controls.disableRun, true);
    assert.equal(controls.disableReplay, false);
  }
});

test('live controls require a key and honor active work', () => {
  const idle = {liveAvailable: true, active: false, replayAvailable: true};
  assert.equal(raceControls({...idle, hasKey: false}).disableRun, true);
  assert.equal(raceControls({...idle, hasKey: true}).disableRun, false);
  assert.equal(raceControls({...idle, hasKey: true, active: true}).disableRun, true);
});

test('live requests send a bearer header and reject empty credentials', () => {
  assert.throws(() => liveHeaders('  '), /API key/);
  assert.deepEqual(liveHeaders(' test-only-key '), {'Content-Type': 'application/json', Authorization: 'Bearer test-only-key'});
});

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

function feed(state, type, data) {
  const base = fixture('second_opinion')[0];
  applyEvent(state, {...base, seq: state.events.length + 1, type, data});
}

function search(state, query = 'Annual revenue', extra = {}) {
  feed(state, 'search', {query, engines: ['tavily'], n_results: 5, ...extra});
}

test('rewrites show both queries and retain the verdict that prompted the rescue', () => {
  const state = newRun('second_opinion', 'tavily');
  fixture('second_opinion').forEach(event => applyEvent(state, event));
  const rescues = state.steps.filter(step => step.kind === 'rescue');
  assert.equal(rescues[0].query, 'Rewrote query');
  assert.deepEqual(rescues[0].rewrite, {from: 'Stripe valuation', to: 'Stripe March 2023 Series I valuation'});
  assert.equal(rescues[0].verdict, 'STALE');
  assert.equal(rescues[0].why, 'Search for the requested round.');
  assert.equal(rescues[0].action, 'Retry with exa');
  assert.equal(rescues[1].rewrite, null);
  assert.equal(rescues[1].query, 'Read the source page');
});

test('chained rescues compare with the search being rescued, not the original query', () => {
  const state = newRun('second_opinion', 'tavily');
  search(state, 'Revenue');
  feed(state, 'rescue', {action: 'RETRY_ENGINE', query: '2023 revenue', engine: 'exa'});
  search(state, '2023 revenue');
  feed(state, 'rescue', {action: 'RETRY_ENGINE', query: '2023 annual report revenue', original_query: 'Revenue', engine: 'brave'});
  assert.deepEqual(state.steps.at(-1).rewrite, {from: '2023 revenue', to: '2023 annual report revenue'});
});

test('unchanged, empty or unpaired rescue queries do not invent rewrites', () => {
  for (const query of ['Annual revenue', ' Annual revenue ', '', null, undefined, 42]) {
    const state = newRun('second_opinion', 'tavily');
    search(state);
    feed(state, 'rescue', {action: 'RETRY_ENGINE', query});
    assert.equal(state.steps.at(-1).rewrite, null);
    assert.equal(state.steps.at(-1).query, 'Retry search');
  }
  const state = newRun('second_opinion', 'tavily');
  search(state);
  feed(state, 'search_done', {status: 'HIT'});
  feed(state, 'rescue', {action: 'RETRY_ENGINE', query: 'A different tool call'});
  assert.equal(state.steps.at(-1).rewrite, null);
});

test('engine routing details use optional event metadata without inferring a question type', () => {
  const state = newRun('second_opinion', 'tavily');
  search(state, 'Revenue', {first_engine: 'exa', question_type: 'finance', engines: ['exa']});
  assert.deepEqual(state.steps.at(-1).choice, {engine: 'exa', questionType: 'finance'});
  search(state);
  assert.equal(state.steps.at(-1).choice, null);
  search(state, 'Revenue', {question_type: 'finance'});
  assert.deepEqual(state.steps.at(-1).choice, {engine: null, questionType: 'finance'});
  search(state, 'Revenue', {first_engine: 'exa'});
  assert.deepEqual(state.steps.at(-1).choice, {engine: 'exa', questionType: null});
  search(state, 'Revenue', {question_type: 'finance', engines: ['exa', 'tavily']});
  assert.deepEqual(state.steps.at(-1).choice, {engine: null, questionType: 'finance'});
  search(state, 'Revenue', {first_engine: {}, question_type: 2});
  assert.equal(state.steps.at(-1).choice, null);
});

test('ranked replay highlights the recorded result and only badges the checked final citation', () => {
  const state = newRun('second_opinion', 'tavily');
  const events = fixture('ranked_hit'), original = structuredClone(events);
  events.slice(0, -1).forEach(event => applyEvent(state, event));
  assert.ok(Math.abs(state.cost - events.at(-1).data.cost_usd_total) < 1e-10);
  applyEvent(state, events.at(-1));
  assert.equal(state.cost, .0124);
  assert.equal(state.telemCalls, 2);
  assert.equal(state.status, 'Complete');
  assert.deepEqual(state.steps.at(-1).evidence, {
    rank: 2, url: 'https://stripe.com/newsroom/news/series-i', label: 'Answer in result 2',
  });
  assert.deepEqual(state.answer.citations.map(url => citationVerified(state, url)), [true, false]);
  events.forEach(event => assert.equal(applyEvent(state, event), false));
  assert.equal(state.steps.length, 3);
  assert.deepEqual(state.events, original);
  assert.deepEqual(events, original);
});

test('page HIT evidence stays on the page read, separate from the SHALLOW search', () => {
  const state = newRun('second_opinion', 'tavily');
  fixture('second_opinion').forEach(event => applyEvent(state, event));
  const [first, second] = state.steps.filter(step => step.kind === 'search');
  assert.equal(first.verdict, 'STALE');
  assert.equal(second.verdict, 'SHALLOW');
  assert.equal(second.evidence, null);
  assert.equal(state.steps.at(-1).evidence.label, 'Answer on this page');
  assert.equal(citationVerified(state, state.answer.citations[0]), true);
});

test('missing source URLs never borrow an unrelated final citation or ranking position', () => {
  const state = newRun('second_opinion', 'tavily');
  search(state);
  feed(state, 'check', {verdict: 'HIT', evidence_rank: 2});
  feed(state, 'search_done', {status: 'HIT', ranking: [{rank: 2, url: 'https://example.org/unverified'}]});
  feed(state, 'answer', {status: 'ANSWERED', citations: ['https://example.org/unverified']});
  assert.equal(state.steps[0].evidence.label, 'Answer in result 2');
  assert.equal(state.steps[0].evidence.url, null);
  assert.equal(citationVerified(state, 'https://example.org/unverified'), false);
});

test('invalid ranks and unsafe sources degrade to unlinked evidence without a citation badge', () => {
  for (const rank of [null, 0, -1, 1.5, '2', 6]) {
    const state = newRun('second_opinion', 'tavily');
    search(state);
    feed(state, 'check', {verdict: 'HIT', evidence_rank: rank, best_url: 'javascript:alert(1)'});
    assert.deepEqual(state.steps[0].evidence, {rank: null, url: null, label: 'Checker found evidence'});
    assert.equal(citationVerified(state, 'javascript:alert(1)'), false);
  }
});

test('verification matches the recorded source, ignoring fragments but preserving paths and query strings', () => {
  const state = newRun('second_opinion', 'tavily');
  search(state);
  feed(state, 'check', {verdict: 'HIT', best_url: 'https://EXAMPLE.org/report?year=2023#revenue'});
  assert.equal(citationVerified(state, 'https://example.org/report?year=2023#table'), true);
  for (const url of ['https://example.org/', 'https://example.org/report?year=2022', 'http://example.org/report?year=2023']) {
    assert.equal(citationVerified(state, url), false);
  }
  assert.equal(citationVerified(newRun('single', 'tavily'), 'https://example.org/report?year=2023'), false);
});

test('a later non-HIT for the same source removes its verification badge', () => {
  const state = newRun('second_opinion', 'tavily'), url = 'https://example.org/report';
  search(state);
  feed(state, 'check', {verdict: 'HIT', best_url: url});
  assert.equal(citationVerified(state, url), true);
  search(state, 'A different year');
  feed(state, 'check', {verdict: 'STALE', best_url: url});
  assert.equal(citationVerified(state, url), false);
  assert.equal(state.steps.at(-1).evidence, null);
});

test('legacy page checks can identify a successful read, but failed reads and orphan checks cannot', () => {
  for (const ok of [true, false]) {
    const state = newRun('second_opinion', 'tavily');
    feed(state, 'fetch', {url: 'https://example.org/report', via: 'telem', ok});
    feed(state, 'check', {verdict: 'HIT'});
    assert.equal(citationVerified(state, 'https://example.org/report'), ok);
  }
  const state = newRun('second_opinion', 'tavily');
  feed(state, 'check', {verdict: 'HIT', best_url: 'https://example.org/report'});
  assert.equal(citationVerified(state, 'https://example.org/report'), false);
});

test('a live run keeps the page clock and counts only spend made in this run', () => {
  const state = newRun('second_opinion', 'tavily', {live: true});
  state.elapsed = 1234;  // the race view sets this from its own clock
  const events = fixture('second_opinion');
  const cached = events.map(event => event.type === 'search' ? {...event, data: {...event.data, cached: true}} : event);
  cached.forEach(event => applyEvent(state, event));
  const totals = events.at(-1).data;
  const cachedSpend = events.filter(event => event.type === 'search').reduce((sum, event) => sum + (event.data.cost_usd || 0), 0);
  assert.equal(state.elapsed, 1234, 'logged latency never replaces the live clock');
  assert.ok(Math.abs(state.cost - (totals.cost_usd_total - cachedSpend)) < 1e-10);
  const replay = newRun('second_opinion', 'tavily');
  cached.forEach(event => applyEvent(replay, event));
  assert.equal(replay.elapsed, totals.latency_ms, 'a replay keeps its recorded timing');
  assert.equal(replay.cost, totals.cost_usd_total);
});
