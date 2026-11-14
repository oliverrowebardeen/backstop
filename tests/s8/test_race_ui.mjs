// O10: the single-request race reader, the comparison line under the race and the replay label.
import test from 'node:test';
import assert from 'node:assert/strict';
import {sseParser, readEvents} from '../../ui/race-stream.js';
import {raceSummary, replayLabel} from '../../ui/race-summary.js';

test('SSE frames split across chunks are parsed whole, comments skipped', () => {
  const parse = sseParser();
  assert.deepEqual(parse('event: race\ndata: {"left":"telem_def'), []);
  assert.deepEqual(parse('ault"}\n\n: keep-alive\n\nevent: log\r\ndata: {"side":"left"}\r\n'), [
    {event: 'race', data: {left: 'telem_default'}, id: null},
  ]);
  assert.deepEqual(parse('\r\nid: 7\nevent: done\ndata: {}\n\n'), [
    {event: 'log', data: {side: 'left'}, id: null},
    {event: 'done', data: {}, id: '7'},
  ]);
  assert.throws(() => sseParser()('event: log\ndata: not json\n\n'), /invalid event/);
});

test('readEvents reads a fetch body byte by byte, including multi-byte characters', async () => {
  const text = 'event: log\ndata: {"q":"Café → 東京"}\n\nevent: done\ndata: {}\n\n';
  const bytes = new TextEncoder().encode(text);
  const body = new ReadableStream({start(controller) { for (const byte of bytes) controller.enqueue(new Uint8Array([byte])); controller.close(); }});
  const seen = [];
  for await (const message of readEvents(body)) seen.push(message);
  assert.deepEqual(seen.map(m => m.event), ['log', 'done']);
  assert.equal(seen[0].data.q, 'Café → 東京');
});

const telem = {name: 'Telem default', engineCalls: 9, cost: 0.1069, elapsed: 13200};
const backstop = {name: 'Backstop', engineCalls: 1, cost: 0.0161, elapsed: 6100};

test('summary names the lower side for each measure, in one sentence when one side leads all three', () => {
  assert.equal(raceSummary(telem, backstop),
    'Backstop made 8 fewer engine calls (1 vs 9), cost 85% less in total ($0.0161 vs $0.1069) and finished 7.1 s sooner (6.1 s vs 13.2 s).');
});

test('summary splits by subject when the sides trade wins, and reports ties plainly', () => {
  const slow = {...backstop, engineCalls: 3, cost: 0.0954, elapsed: 23000};
  const fast = {...telem, engineCalls: 6, cost: 0.1131, elapsed: 19200};
  assert.equal(raceSummary(fast, slow),
    'Backstop made 3 fewer engine calls (3 vs 6) and cost 16% less in total ($0.0954 vs $0.1131). ' +
    'Telem default finished 3.8 s sooner (19.2 s vs 23.0 s).');
  const same = {name: 'Single engine', engineCalls: 1, cost: 0.0161, elapsed: 6150};
  assert.equal(raceSummary(same, backstop),
    'Both made 1 engine call, cost about $0.0161 in total and took about 6.2 s.');
  assert.equal(raceSummary(backstop, {...telem, engineCalls: 2}).startsWith('Backstop made 1 fewer engine call (1 vs 2)'), true);
  assert.equal(raceSummary(backstop, {name: 'x', engineCalls: NaN, cost: 0, elapsed: 0}), null);
});

test('replay label states date, commit and recording settings from the manifest, never for synthetic fixtures', () => {
  const pair = {recorded_at: '2026-11-14T22:40:05+00:00', commit: '0ef610b456cc', fixture: false,
    checker_backend: 'cascade', first_engine_routing: 'on', cache: 'off'};
  const label = replayLabel(pair);
  assert.equal(label, 'Example race. Recorded live Nov 14, 2026 on commit 0ef610b with the cascade checker, ' +
    'first-engine routing on and the cache off.');
  assert.ok(!/random|seed|whatever/i.test(label));
  assert.equal(replayLabel({...pair, checker_backend: 'llm', first_engine_routing: undefined, cache: undefined}),
    'Example race. Recorded live Nov 14, 2026 on commit 0ef610b with the LLM checker.');
  assert.equal(replayLabel({recorded_at: pair.recorded_at, commit: 'not a sha'}), 'Example race. Recorded live Nov 14, 2026.');
  assert.equal(replayLabel({...pair, recorded_at: undefined}), '');
  assert.equal(replayLabel({...pair, fixture: true}), '');
  assert.equal(replayLabel(null), '');
});
