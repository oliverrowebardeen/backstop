import {test} from 'node:test';
import assert from 'node:assert/strict';
import {watchRun} from '../../ui/run-stream.js';

class FakeSource {
  static CLOSED = 2;
  static latest;
  constructor(url) {
    this.url = url; this.readyState = 1; this.listeners = new Map(); this.closeCount = 0;
    FakeSource.latest = this;
  }
  addEventListener(type, handler) { this.listeners.set(type, handler); }
  close() { this.readyState = FakeSource.CLOSED; this.closeCount++; }
  emit(type, data) { this.listeners.get(type)?.({data}); }
}

function follow(runId = 'recorded-run') {
  const events = [], failures = [];
  let complete = 0, reconnect = 0;
  const close = watchRun(runId, {
    onEvent: event => events.push(event), onComplete: () => complete++,
    onFailure: reason => failures.push(reason), onReconnect: () => reconnect++,
  }, FakeSource);
  return {source: FakeSource.latest, close, events, failures, completed: () => complete, reconnected: () => reconnect};
}

test('live viewing uses the run stream endpoint and forwards unmodified contract events', () => {
  const run = follow('single-question-123');
  assert.equal(run.source.url, '/stream/single-question-123');
  const event = {run_id: 'single-question-123', seq: 3, type: 'check', data: {verdict: 'HIT', evidence_rank: 2, evidence_title: 'Annual report'}};
  run.source.emit('log', JSON.stringify(event));
  assert.deepEqual(run.events, [event]);
  run.source.emit('complete', '{}');
  assert.equal(run.source.closeCount, 1);
  assert.equal(run.completed(), 1);
  assert.deepEqual(run.failures, []);
});

test('stream errors retain the server reason and close the EventSource', () => {
  const run = follow();
  run.source.emit('stream_error', JSON.stringify({error_type: 'TimeoutError', message: 'The run timed out before it could finish.'}));
  assert.deepEqual(run.failures, ['The run timed out before it could finish.']);
  assert.equal(run.source.closeCount, 1);
  assert.equal(run.completed(), 0);
});

test('malformed log and failure events stop with clear messages', () => {
  const run = follow();
  run.source.emit('log', '{invalid');
  assert.deepEqual(run.failures, ['The run contains an invalid event.']);
  assert.equal(run.source.closeCount, 1);
  const failed = follow();
  failed.source.emit('stream_error', '{invalid');
  assert.deepEqual(failed.failures, ['The server returned an invalid failure event.']);
});

test('a contract validation error stops the stream instead of completing the run', () => {
  const failures = [];
  watchRun('run', {onEvent: () => { throw new Error('Missing totals'); }, onFailure: reason => failures.push(reason)}, FakeSource);
  FakeSource.latest.emit('log', '{}');
  assert.deepEqual(failures, ['The run contains an invalid event.']);
  assert.equal(FakeSource.latest.closeCount, 1);
});

test('transient connection errors keep native Last-Event-ID reconnection active', () => {
  const run = follow();
  run.source.readyState = 0;
  run.source.onerror();
  assert.equal(run.reconnected(), 1);
  assert.equal(run.source.closeCount, 0);
  assert.deepEqual(run.failures, []);
  run.source.readyState = 1;
  run.source.emit('log', '{"seq":4}');
  assert.deepEqual(run.events, [{seq: 4}]);
});

test('a permanently rejected stream reports failure without restarting a race', () => {
  const run = follow();
  run.source.readyState = FakeSource.CLOSED;
  run.source.onerror();
  assert.match(run.failures[0], /could not be opened/);
  assert.equal(run.source.closeCount, 1);
});

test('closing a view ignores queued events and does not send a cancellation request', () => {
  const run = follow();
  run.close();
  run.source.emit('log', '{"seq":5}');
  run.source.emit('stream_error', '{"message":"Late failure"}');
  run.source.emit('complete', '{}');
  run.source.onerror();
  assert.deepEqual(run.events, []);
  assert.deepEqual(run.failures, []);
  assert.equal(run.completed(), 0);
  assert.equal(run.source.closeCount, 1);
});

test('run IDs cannot inject extra URL query parameters or path segments', () => {
  assert.equal(follow('run/next?key=value').source.url, '/stream/run%2Fnext%3Fkey%3Dvalue');
});
