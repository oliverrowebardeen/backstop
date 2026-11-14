/** Check a memo: examples, live check through the guarded demo route, recorded fallback, highlights, panel, export. */
import {VERDICT_WORDS, errorMessage, hostOf, money, safeUrl, seconds, segments, summaryParts, verdictKey} from './memo-view.js';

const $ = selector => document.querySelector(selector);
const section = $('#memo');
const store = {
  get(key) { try { return localStorage.getItem(key); } catch { return null; } },
  set(key, value) { try { localStorage.setItem(key, value); } catch { /* storage unavailable */ } },
};
const MAX_CHARS = 4000;
let cfg = {live_available: false, race_access: 'replay', demo_code_required: false};
let memos = [];
let recordings = [];
let report = null;
let selected = null;
let busy = false;

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function setMessage(text) { $('#memo-message').textContent = text || ''; }

function updateControls() {
  const text = $('#memo-input').value;
  const over = text.length > MAX_CHARS;
  const count = $('#memo-count');
  count.textContent = `${text.length.toLocaleString()} / ${MAX_CHARS.toLocaleString()}`;
  count.classList.toggle('is-over', over);
  const live = cfg.live_available === true;
  const needCode = live && cfg.race_access === 'demo' && cfg.demo_code_required;
  const needKey = live && cfg.race_access === 'api_key';
  $('#memo-code-box').hidden = !needCode;
  $('#memo-key-box').hidden = !needKey;
  const missing = (needCode && !$('#memo-code').value.trim()) || (needKey && !$('#memo-key').value.trim());
  $('#memo-run').disabled = busy || !text.trim() || over || (live && missing) || (!live && !recordingFor(text));
  $('#memo-export').disabled = busy || !report;
  for (const chip of document.querySelectorAll('#memo-examples button')) chip.disabled = busy;
  $('#memo-mode').textContent = live ? 'Live' : 'Recorded';
  $('#memo-mode').classList.toggle('is-live', live);
  $('#memo-mode').classList.toggle('is-sample', !live);
  $('#memo-live-off').hidden = live;
}

function recordingFor(text) {
  const memo = memos.find(item => item.text.trim() === text.trim());
  return memo ? recordings.find(item => item.memo_id === memo.id || item.name === memo.id) : null;
}

function renderExamples() {
  const box = $('#memo-examples');
  box.replaceChildren();
  for (const memo of memos) {
    const chip = el('button', 'example-chip', memo.label);
    chip.type = 'button';
    chip.addEventListener('click', () => {
      $('#memo-input').value = memo.text;
      updateControls();
      if (!cfg.live_available) showRecording(memo.id);
      else $('#memo-input').focus();
    });
    box.append(chip);
  }
  box.hidden = !memos.length;
}

function counter(value, label) {
  const div = el('div');
  div.append(el('dd', '', value), el('dt', '', label));
  return div;
}

function renderReport(data, {replay = false} = {}) {
  report = data;
  selected = null;
  $('#memo-result').hidden = false;
  const totals = $('#memo-totals');
  totals.replaceChildren();
  for (const part of summaryParts(data.totals)) {
    const pill = el('span', `memo-total verdict-${part.key}`);
    pill.append(el('b', '', String(part.count)), document.createTextNode(` ${part.word}`));
    totals.append(pill);
  }
  $('#memo-counters').replaceChildren(
    counter(String(data.engine_calls ?? 0), 'Telem engine calls'),
    counter(money(data.cost_usd), 'Total cost, models included'),
    counter(seconds(data.latency_ms), replay ? 'Time (recorded)' : 'Time'),
  );
  const session = $('#memo-session');
  session.replaceChildren();
  if (data.telem_session_id) {
    session.append(el('span', '', 'Telem session'), el('code', '', data.telem_session_id));
    const copy = el('button', 'chip-button', 'Copy');
    copy.type = 'button';
    copy.addEventListener('click', async () => {
      try { await navigator.clipboard.writeText(data.telem_session_id); copy.textContent = 'Copied'; }
      catch { copy.textContent = 'Copy failed'; }
      setTimeout(() => { copy.textContent = 'Copy'; }, 1500);
    });
    session.append(copy);
  }
  const notes = (data.notes || []).join(' ');
  $('#memo-replay-note').textContent = replay
    ? `Replay of a recorded live check (${data.checked_at || 'saved run'}). ${notes}`.trim()
    : notes;
  $('#memo-replay-note').hidden = !replay && !notes;
  renderText(data);
  const first = data.claims.findIndex(claim => claim.verdict === 'CONTRADICTED');
  renderPanel(first >= 0 ? first : null);
  updateControls();
}

function claimButton(claim, index, text) {
  const key = verdictKey(claim.verdict);
  const button = el('button', `claim claim-${key}`);
  button.type = 'button';
  button.dataset.index = String(index);
  button.setAttribute('aria-pressed', 'false');
  button.setAttribute('aria-label', `${VERDICT_WORDS[claim.verdict] || 'Not found'}: ${claim.text}`);
  button.append(document.createTextNode(text), el('span', 'claim-tag', VERDICT_WORDS[claim.verdict] || 'Not found'));
  button.addEventListener('click', () => renderPanel(index));
  return button;
}

function renderText(data) {
  const box = $('#memo-text');
  box.replaceChildren();
  const legend = el('div', 'memo-legend');
  legend.append(el('span', 'verdict-verified', 'Verified: a source states it'),
    el('span', 'verdict-contradicted', 'Contradicted: a source states a different value'),
    el('span', 'verdict-not_found', 'Not found: no source settled it'));
  box.append(legend);
  const {segments: parts, unplaced} = segments(data.text || '', data.claims || []);
  for (const part of parts) {
    box.append(part.claim === undefined ? document.createTextNode(part.text)
      : claimButton(data.claims[part.claim], part.claim, part.text));
  }
  if (unplaced.length) {
    const wrap = el('div', 'memo-unplaced');
    wrap.append(el('p', '', 'Claims that span text already highlighted:'));
    const list = el('ul');
    for (const index of unplaced) {
      const item = el('li');
      item.append(claimButton(data.claims[index], index, data.claims[index].text));
      list.append(item);
    }
    wrap.append(list);
    box.append(wrap);
  }
}

function renderPanel(index) {
  selected = index;
  for (const button of document.querySelectorAll('#memo-text .claim')) {
    button.setAttribute('aria-pressed', String(Number(button.dataset.index) === index));
  }
  const panel = $('#memo-panel');
  panel.className = 'memo-panel';
  panel.replaceChildren();
  if (index === null || !report?.claims[index]) {
    panel.append(el('p', 'memo-panel-empty', 'Select a highlighted claim to see its verdict, the evidence and the source.'));
    return;
  }
  const claim = report.claims[index];
  const key = verdictKey(claim.verdict);
  panel.classList.add(`panel-${key}`);
  const head = el('div', 'memo-panel-head');
  head.append(el('span', `memo-verdict verdict-${key}`, VERDICT_WORDS[claim.verdict] || 'Not found'));
  const close = el('button', 'chip-button', 'Close');
  close.type = 'button';
  close.setAttribute('aria-label', 'Close claim details');
  close.addEventListener('click', () => {
    const button = document.querySelector(`#memo-text .claim[data-index="${index}"]`);
    renderPanel(null);
    button?.focus();
  });
  head.append(close);
  panel.append(head, el('h3', '', claim.text), el('p', 'memo-why', claim.why || ''));
  if (claim.evidence_snippet) panel.append(el('blockquote', '', claim.evidence_snippet));
  const url = safeUrl(claim.source_url);
  if (url) {
    const link = el('a', 'memo-source', `Source: ${hostOf(url)} ↗`);
    link.href = url;
    link.target = '_blank';
    link.rel = 'noopener noreferrer';
    panel.append(link);
  }
  const meta = el('dl', 'memo-meta');
  const row = (label, value) => { meta.append(el('dt', '', label), el('dd', '', value)); };
  row('Confidence', `${Math.round((claim.confidence || 0) * 100)}%`);
  row('Engines', (claim.engines_used || []).join(', ') || 'none');
  row('Rescues', String(claim.rescues_used || 0));
  row('Cost', money(claim.cost_usd));
  if (claim.error) row('Note', claim.error);
  panel.append(meta);
}

async function showRecording(name) {
  try {
    const response = await fetch(`/memo/recordings/${encodeURIComponent(name)}`, {cache: 'no-store'});
    if (!response.ok) throw new Error('missing');
    const data = await response.json();
    renderReport(data, {replay: true});
    setMessage('');
    return true;
  } catch {
    setMessage('No recorded check is available for this memo.');
    return false;
  }
}

function headers() {
  const out = {'Content-Type': 'application/json'};
  if (cfg.race_access === 'demo' && $('#memo-code').value.trim()) out['X-Backstop-Demo-Code'] = $('#memo-code').value.trim();
  if (cfg.race_access === 'api_key') out.Authorization = `Bearer ${$('#memo-key').value.trim()}`;
  return out;
}

async function runCheck(event) {
  event.preventDefault();
  const text = $('#memo-input').value.trim();
  if (!text || text.length > MAX_CHARS) { updateControls(); return; }
  if (!cfg.live_available) {
    const recording = recordingFor(text);
    if (recording) await showRecording(recording.name);
    return;
  }
  busy = true;
  updateControls();
  const started = performance.now();
  const progress = $('#memo-progress');
  progress.hidden = false;
  const tick = () => { progress.textContent = `Extracting claims and verifying each one through Telem: ${seconds(performance.now() - started)}`; };
  tick();
  const timer = setInterval(tick, 250);
  setMessage('');
  try {
    const response = await fetch('/memo-check', {method: 'POST', headers: headers(), body: JSON.stringify({text})});
    const body = await response.json().catch(() => ({}));
    if (!response.ok) {
      const fallback = recordingFor(text);
      if (response.status === 403 && fallback) {
        await showRecording(fallback.name);
        setMessage('Live checks are off on this server. Showing a recorded check instead.');
      } else {
        setMessage(errorMessage(response.status, body.detail));
      }
      return;
    }
    renderReport(body);
    setMessage(`Checked ${body.claims.length} claims.`);
  } catch {
    setMessage('The check could not reach the server. Try again, or pick an example to see a recorded check.');
  } finally {
    clearInterval(timer);
    progress.hidden = true;
    busy = false;
    updateControls();
  }
}

function exportReport() {
  if (!report) return;
  const blob = new Blob([JSON.stringify(report, null, 2)], {type: 'application/json'});
  const url = URL.createObjectURL(blob);
  const anchor = el('a');
  anchor.href = url;
  anchor.download = `backstop-memo-check-${report.run_id || 'report'}.json`;
  document.body.append(anchor);
  anchor.click();
  anchor.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function applyTheme(dark) {
  section.dataset.theme = dark ? 'dark' : 'light';
  $('#memo-theme').textContent = dark ? 'Light mode' : 'Dark mode';
  $('#memo-theme').setAttribute('aria-pressed', String(!dark));
}

async function init() {
  if (!section) return;
  applyTheme(Boolean(window.matchMedia?.('(prefers-color-scheme: dark)').matches));
  $('#memo-theme').addEventListener('click', () => applyTheme(section.dataset.theme !== 'dark'));
  $('#memo-form').addEventListener('submit', runCheck);
  $('#memo-input').addEventListener('input', updateControls);
  $('#memo-code').value = store.get('backstop.demoCode') || '';
  $('#memo-code').addEventListener('input', event => { store.set('backstop.demoCode', event.target.value.trim()); updateControls(); });
  $('#memo-key').addEventListener('input', updateControls);
  $('#memo-export').addEventListener('click', exportReport);
  updateControls();
  const [configResult, memoResult, recordingResult] = await Promise.allSettled([
    fetch('/config', {cache: 'no-store'}).then(r => r.ok ? r.json() : null),
    fetch('/ui/fixtures/memos.json', {cache: 'no-store'}).then(r => r.ok ? r.json() : null),
    fetch('/memo/recordings', {cache: 'no-store'}).then(r => r.ok ? r.json() : []),
  ]);
  if (configResult.status === 'fulfilled' && configResult.value) cfg = {...cfg, ...configResult.value};
  if (memoResult.status === 'fulfilled' && memoResult.value) memos = memoResult.value.memos || [];
  if (recordingResult.status === 'fulfilled') recordings = recordingResult.value || [];
  renderExamples();
  updateControls();
  const requested = new URLSearchParams(location.search).get('memo');
  if (requested) {
    const memo = memos.find(item => item.id === requested);
    if (memo) $('#memo-input').value = memo.text;
    if (await showRecording(requested)) {
      updateControls();
      if (location.hash === '#memo') section.scrollIntoView();
    }
  }
}

init();
