// Memo Check pure view helpers. Run: node --test tests/s10/test_memo_ui.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import {errorMessage, money, safeUrl, segments, summaryText, verdictKey} from '../../ui/memo-view.js';

test('segments place claims in order and keep the text intact', () => {
  const text = 'Acme was founded in 1990. It has 50 staff.';
  const claims = [{span: [27, 42], verdict: 'NOT_FOUND'}, {span: [5, 24], verdict: 'VERIFIED'}];
  const out = segments(text, claims);
  assert.equal(out.segments.map(s => s.text).join(''), text);
  assert.deepEqual(out.segments.filter(s => s.claim !== undefined).map(s => s.claim), [1, 0]);
  assert.deepEqual(out.unplaced, []);
});

test('segments clip overlaps and list unusable spans', () => {
  const text = 'abcdefghij';
  const out = segments(text, [{span: [0, 5]}, {span: [3, 8]}, {span: [2, 4]}, {span: null}, {span: [8, 99]}]);
  assert.equal(out.segments.map(s => s.text).join(''), text);
  assert.deepEqual(out.segments.find(s => s.claim === 1).text, 'fgh');
  assert.deepEqual(out.unplaced, [2, 3, 4]);
});

test('summary, verdict keys, money, urls and errors', () => {
  assert.equal(summaryText({verified: 6, contradicted: 1, not_found: 2}), '6 verified, 1 contradicted, 2 not found');
  assert.equal(verdictKey('CONTRADICTED'), 'contradicted');
  assert.equal(verdictKey('whatever'), 'not_found');
  assert.equal(money(0.0631), '$0.0631');
  assert.equal(safeUrl('javascript:alert(1)'), null);
  assert.equal(safeUrl('https://sec.gov/x'), 'https://sec.gov/x');
  assert.equal(errorMessage(401, {code: 'demo_code_required', message: 'Enter the demo code to run a live race.'}),
    'Enter the demo code to run a live race.');
  assert.match(errorMessage(422, null), /4,000/);
});
