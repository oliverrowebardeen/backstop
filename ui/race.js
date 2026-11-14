/** Live race section of the product site (/#race). Live: type a question and race two setups. Replay: recordings of live runs. */
import {newRun, applyEvent, failRun, citationVerified, VERDICTS, SETUPS, safeURL} from './race-state.js';
import {raceControls, liveHeaders} from './race-access.js';
import {readEvents} from './race-stream.js';
import {raceSummary, replayLabel} from './race-summary.js';

const $ = selector => document.querySelector(selector);
const elements = {}, states = {};
let firstEngine = null, active = false, replayPair = null, replaySource = null, controller = null;
let replayMode = false, autoplayDone = false, raceVisible = false, ready = false, liveAvailable = false;
let auditQuestion = '', pairs = [], streamPath = '/race/stream';
let mode = 'replay', raceAccess = 'replay', maxQuestion = 300;
const setups = {left: 'telem_default', right: 'second_opinion'};
const origins = {};
const names = {single: 'Single engine', second_opinion: 'Backstop', telem_default: 'Telem default'};
const CAPTIONS = {
  telem_default: "Telem's own routing. No result check.",
  single: 'One engine through Telem. No result check.',
  second_opinion: 'Check the evidence. Rescue a miss.',
};
const EXAMPLES = [
  'Who is the CEO of Tavily?',
  "What was Stripe's valuation in its 2023 round?",
  "What was Walmart's total revenue in fiscal 2020?",
  'Who founded Perplexity AI, and in what year?',
  'When did Adobe abandon its planned acquisition of Figma?',
  'What was the largest liability on American Express\'s 2022 balance sheet?',
];
const store = {
  get(key) { try { return localStorage.getItem(key); } catch { return null; } },
  set(key, value) { try { localStorage.setItem(key, value); } catch { /* storage unavailable */ } },
};
try {
  const saved = JSON.parse(store.get('backstop.race.pair') || 'null');
  if (saved && CAPTIONS[saved.left] && CAPTIONS[saved.right] && saved.left !== saved.right) Object.assign(setups, saved);
} catch { /* ignore a malformed saved pair */ }
const liveSetups = {...setups};
const element = (tag, cls, text) => { const node = document.createElement(tag); if(cls) node.className = cls; if(text !== undefined) node.textContent = text; return node; };
const notify = text => { $('#race-message').textContent = text; };

function setActive(value) {
  active = value;
  const live = mode === 'live';
  const needsKey = raceAccess === 'api_key';
  const controls = raceControls({liveAvailable: live && liveAvailable, active: value,
    hasKey: needsKey ? Boolean($('#api-key').value.trim()) : true, replayAvailable: Boolean(replayPair)});
  $('#run-button').hidden = !live;
  $('#run-button').disabled = controls.disableRun;
  $('#credentials').hidden = !(live && needsKey);
  $('#question').disabled = value;
  $('#question').readOnly = !live;
  $('#api-key').disabled = value;
  $('#example-chips').hidden = !live;
  $('#example-chips').querySelectorAll('button').forEach(chip => { chip.disabled = value; });
  $('#replay-controls').hidden = live;
  $('#replay-meta').hidden = live || !$('#replay-meta').textContent;
  $('#replay-button').disabled = controls.disableReplay;
  $('#stop-button').hidden = !value;
  // Live: pick both setups. A replay always shows the setups it recorded, so the pickers are hidden.
  $('#setup-pickers').hidden = !live;
  for (const side of ['left', 'right']) $(`#${side}-setup`).disabled = value;
  $('#replay-select').disabled = value || pairs.length < 2;
  $('#mode-toggle').hidden = !liveAvailable;
  $('#mode-live').disabled = value;
  $('#mode-replay').disabled = value;
}

function applySetups() {
  for (const side of ['left', 'right']) {
    const setup = setups[side], column = elements[side];
    $(`#${side}-setup`).value = setup;
    if (!column) continue;
    column.classList.toggle('ours', setup === 'second_opinion');
    column.querySelector('.column-tag').textContent = CAPTIONS[setup] || SETUPS[setup]?.description || setup;
    column.querySelector('.column-name').textContent = names[setup] || setup;
  }
  syncPickers();
}

/** The same setup cannot run on both sides. */
function syncPickers() {
  for (const [side, other] of [['left', 'right'], ['right', 'left']]) {
    const taken = $(`#${other}-setup`).value;
    for (const option of $(`#${side}-setup`).options) option.disabled = option.value === taken;
  }
}
/** Empty both columns, so a new pair of setups never shows another run's numbers. */
function clearColumns() {
  for (const name of ['left', 'right']) { states[name] = newRun(setups[name], firstEngine); if (elements[name]) render(name); }
  showSummary();
}
for (const side of ['left', 'right']) {
  $(`#${side}-setup`).addEventListener('change', event => {
    setups[side] = event.target.value; liveSetups[side] = event.target.value;
    store.set('backstop.race.pair', JSON.stringify(liveSetups));
    if (!active) clearColumns();
    applySetups();
  });
}

function render(side) {
  const state = states[side], column = elements[side], find = selector => column.querySelector(selector);
  find('.engine-count').textContent = String(state.engineCalls);
  find('.search-cost').textContent = `$${state.searchCost.toFixed(4)}`;
  find('.cost-count').textContent = `$${state.cost.toFixed(4)}`;
  find('.time-count').replaceChildren(document.createTextNode((state.elapsed / 1000).toFixed(1)), element('span', '', 's'));
  find('.run-status').textContent = state.status;
  find('.session').textContent = state.session ? `Telem session ${state.session}` : state.finished ? 'No Telem session recorded' : 'Session pending';
  const steps = find('.steps');
  if (state.steps.length) {
    steps.replaceChildren(...state.steps.map((step, i) => {
      const row = element('li'), title = element('div', 'step-query');
      title.append(element('span', 'step-num', String(i + 1).padStart(2, '0')), element('span', '', step.query));
      row.append(title);
      if (step.choice) {
        const choice = [step.choice.engine ? `First engine: ${step.choice.engine}` : null,
          step.choice.questionType ? `Question type: ${step.choice.questionType}` : null].filter(Boolean).join(', ');
        row.append(element('p', 'engine-choice', choice));
      }
      if (step.rewrite) {
        const rewrite = element('dl', 'query-rewrite');
        for (const [label, query] of [['From', step.rewrite.from], ['To', step.rewrite.to]]) {
          rewrite.append(element('dt', '', label), element('dd', '', query));
        }
        row.append(rewrite, element('p', 'rescue-action', step.action));
      }
      const chips = element('div', 'step-chips');
      step.engines.forEach(engine => chips.append(element('span', 'chip', engine)));
      if (step.verdict) chips.append(element('span', `verdict verdict-${String(step.verdict).toLowerCase().replace(/_/g, '-')}`, VERDICTS[step.verdict] || step.verdict));
      if (step.cached) chips.append(element('span', 'chip', 'Cached'));
      if (chips.childNodes.length) row.append(chips);
      if (step.why) row.append(element('p', 'step-note', step.why));
      if (step.evidence) {
        const evidence = element('div', 'checker-evidence');
        evidence.append(element('strong', 'evidence-label', step.evidence.label));
        const url = safeURL(step.evidence.url);
        if (url) {
          const source = element('a', 'evidence-link', `${url.hostname.replace(/^www\./, '')}${url.pathname === '/' ? '' : url.pathname} ↗`);
          source.href = url.href; source.target = '_blank'; source.rel = 'noopener noreferrer';
          evidence.append(source);
        } else evidence.append(element('span', 'evidence-unavailable', 'Source URL not recorded.'));
        row.append(evidence);
      }
      return row;
    }));
    steps.scrollTop = steps.scrollHeight;
  } else steps.replaceChildren(element('li', 'empty', 'Waiting for the question.'));
  const declined = state.answer?.status === 'NOT_FOUND';
  find('.answer-label').textContent = state.failure ? 'Run failed' : declined ? 'Not found, a deliberate outcome' : 'Final answer';
  const fullAnswer = state.failure || state.answer?.final || (state.finished ? 'The run ended without a final answer.' : 'Following the evidence…');
  const lead = typeof Intl.Segmenter === 'function' ? [...new Intl.Segmenter('en', {granularity:'sentence'}).segment(fullAnswer)][0]?.segment.trim() : fullAnswer;
  find('.answer-text').textContent = lead || fullAnswer;
  find('.answer-full').textContent = fullAnswer;
  find('.answer-details').hidden = !state.answer;
  find('.sources').replaceChildren(...(state.failure ? [] : state.answer?.citations || []).map((url, i) => {
    const safe = safeURL(url); if (!safe) return null;
    const citation = element('div', 'citation');
    const link = element('a', '', `[${i + 1}] ${safe.hostname.replace(/^www\./, '')} ↗`);
    link.href = safe.href; link.target = '_blank'; link.rel = 'noopener noreferrer'; citation.append(link);
    if (citationVerified(state, url)) {
      const badge = element('span', 'verified-citation', 'Checker-verified');
      badge.title = 'This source received a HIT verdict during this run.';
      citation.append(badge);
    }
    return citation;
  }).filter(Boolean));
  find('.grade-note').hidden = replayMode || !state.events.length;
  find('.watch-recorded').hidden = !(state.failure && !replayMode && pairs.length);
}

/** Once both runs finish cleanly: which side used fewer engine calls, cost less and finished sooner. */
function showSummary() {
  const done = states.left?.finished && states.right?.finished && !states.left.failure && !states.right.failure;
  const side = name => ({name: names[states[name].setup] || states[name].setup, engineCalls: states[name].engineCalls,
    cost: states[name].cost, elapsed: states[name].elapsed});
  const text = done ? raceSummary(side('left'), side('right')) : null;
  $('#race-summary').textContent = text || '';
  $('#race-summary').hidden = !text;
}

function closeConnections() {
  replaySource?.close(); replaySource = null; controller?.abort(); controller = null;
}
function reset(kind) {
  closeConnections(); replayMode = kind !== 'live';
  applySetups();
  const live = kind === 'live', startedAt = performance.now();
  for (const [side, setup] of Object.entries(setups)) {
    // Live: the clock starts when the race starts and stops at the final event.
    states[side] = newRun(setup, firstEngine, {live}); states[side].status = 'Starting';
    origins[side] = live ? startedAt : null; render(side);
  }
  showSummary();
  badge(kind === 'live' ? 'live' : kind === 'fixture' ? 'fixture' : 'replay');
  setActive(true);
}
function badge(kind) {
  const label = {live: 'Live', replay: 'Replay of a live run', fixture: 'Synthetic fixture, not a live run'}[kind];
  $('#race-mode').textContent = label;
  $('#race-mode').classList.toggle('is-live', kind === 'live');
  $('#race-mode').classList.toggle('is-sample', kind === 'fixture');
}
function receive(side, event) {
  if (!states[side]) throw new Error('Unrecognized race side.');
  if (applyEvent(states[side], event)) {
    if (states[side].live) states[side].elapsed = performance.now() - origins[side];
    else origins[side] = performance.now() - states[side].elapsed;
    render(side);
  }
  if (states.left.finished && states.right.finished) finish();
}
function finish() {
  setActive(false);
  showSummary();
  const interrupted = Object.values(states).some(state => Boolean(state.failure));
  notify(interrupted ? 'One run stopped before it finished. Its reason is shown in its column.'
    : replayMode ? (liveAvailable ? 'Recorded race complete. Switch to Live to race your own question.' : 'Recorded race complete. Pick another recording or play this one again.')
    : 'Both runs have finished.');
}
function fail(message, side) {
  for (const name of side ? [side] : ['left', 'right']) {
    if (side || !states[name].finished) { failRun(states[name], message); render(name); }
  }
  notify(message);
  if (states.left.finished && states.right.finished) { setActive(false); showSummary(); }
}

for (const [side, setup] of Object.entries(setups)) {
  const column = $('#column-template').content.firstElementChild.cloneNode(true);
  column.id = `column-${side}`;
  elements[side] = column; states[side] = newRun(setup, firstEngine); $('#columns').append(column);
  column.querySelector('.watch-recorded').addEventListener('click', () => {
    if (active) { closeConnections(); fail('Switched to a recorded run.'); }
    setMode('replay'); playReplay();
  });
}
applySetups();
render('left'); render('right');

setInterval(() => {
  if (!active || document.hidden) return;
  for (const side of ['left', 'right']) {
    if (!states[side].finished && origins[side] !== null) {
      states[side].elapsed = Math.max(states[side].elapsed, performance.now() - origins[side]);
      elements[side].querySelector('.time-count').replaceChildren(document.createTextNode((states[side].elapsed / 1000).toFixed(1)),element('span','','s'));
    }
  }
}, 100);

function playReplay() {
  if (!replayPair || active) return;
  autoplayDone = true;
  setups.left = replayPair.left; setups.right = replayPair.right;
  reset(replayPair.fixture ? 'fixture' : 'replay');
  setQuestion(replayPair.question);
  auditQuestion = replayPair.question;
  notify(replayPair.fixture ? 'Synthetic test events. These are not product results.' : 'Recorded live run, played at its original pace.');
  const source = new EventSource(`/replay/${encodeURIComponent(replayPair.name)}`);
  replaySource = source;
  source.addEventListener('log', message => { if (source !== replaySource) return; try { const payload = JSON.parse(message.data); receive(payload.side, payload.event); } catch { source.close(); fail('The recording contains an invalid event.'); } });
  source.addEventListener('complete', () => { source.close(); if (!(states.left.finished && states.right.finished)) fail('The recording ended before final totals.'); });
  source.addEventListener('stream_error', () => { source.close(); fail('The recorded stream was interrupted.'); });
  source.onerror = () => { source.close(); if (active) fail('The recording disconnected. Play it again or pick another.'); };
}
$('#replay-button').addEventListener('click', playReplay);
$('#stop-button').addEventListener('click', () => {
  const live = !replayMode;
  closeConnections();
  fail(live ? 'Stopped. Both live runs were cancelled on the server.' : 'Replay stopped.');
});

$('#api-key').addEventListener('input', () => setActive(active));
/** The question box is a textarea that grows to fit; a question is one line, so Enter runs the race. */
function fitQuestion() {
  const box = $('#question');
  box.style.height = 'auto';
  box.style.height = `${box.scrollHeight}px`;
}
function setQuestion(text) { $('#question').value = text; fitQuestion(); }
$('#question').addEventListener('input', () => {
  const box = $('#question');
  if (/[\r\n]/.test(box.value)) box.value = box.value.replace(/\s*[\r\n]+\s*/g, ' ');
  $('#question-error').hidden = true;
  fitQuestion();
});
$('#question').addEventListener('keydown', event => {
  if (event.key !== 'Enter' || event.isComposing) return;
  event.preventDefault();
  if (!$('#run-button').disabled && !$('#run-button').hidden) $('#race-form').requestSubmit($('#run-button'));
});
// Refit when the box's width changes: window resizes and the Run race button showing or hiding.
let questionWidth = 0;
new ResizeObserver(([entry]) => {
  if (entry.contentRect.width === questionWidth) return;
  questionWidth = entry.contentRect.width;
  requestAnimationFrame(fitQuestion);
}).observe($('#question'));
document.fonts?.ready.then(fitQuestion);

function validQuestion() {
  const question = $('#question').value.trim();
  const problem = !question ? 'Type a question first.' : question.length > maxQuestion ? `Keep the question under ${maxQuestion} characters.` : '';
  $('#question-error').textContent = problem;
  $('#question-error').hidden = !problem;
  return problem ? null : question;
}
function raceHeaders() {
  if (raceAccess === 'api_key') return liveHeaders($('#api-key').value);
  return {'Content-Type': 'application/json'};
}
async function errorMessage(response, fallback) {
  try { const error = await response.json(); return typeof error.detail === 'string' ? error.detail : error.detail?.message || fallback; }
  catch { return fallback; }
}

/** One request starts both sides and streams both logs back, so it works on serverless hosts too. */
async function runLive(question, headers, signal) {
  const response = await fetch(streamPath, {method: 'POST', headers, signal,
    body: JSON.stringify({question, left: setups.left, right: setups.right})});
  if (!response.ok) throw new Error(await errorMessage(response, 'The race could not start.'));
  if (!response.body) throw new Error('This browser cannot read a streamed response.');
  for await (const message of readEvents(response.body)) {
    const {event, data} = message;
    if (event === 'log') receive(data.side, data.event);
    else if (event === 'stream_error') fail(data.message || 'The run stopped before it finished.', data.side);
    else if (event === 'complete' && !states[data.side]?.finished) fail('The stream ended before final totals.', data.side);
    else if (event === 'done') break;
  }
  if (!(states.left.finished && states.right.finished)) fail('The connection closed before both runs finished.');
}

$('#race-form').addEventListener('submit', async event => {
  event.preventDefault(); if (active || !liveAvailable || mode !== 'live') return;
  const question = validQuestion(); if (!question) return;
  let headers;
  try { headers = raceHeaders(); } catch (error) { notify(error.message); return; }
  setQuestion(question);
  auditQuestion = question;
  setups.left = $('#left-setup').value; setups.right = $('#right-setup').value;
  autoplayDone = true; reset('live');
  notify('Both runs are searching live through Telem. Counts and costs come from their event logs; the clock is real time.');
  const abort = new AbortController(); controller = abort;
  try { await runLive(question, headers, abort.signal); }
  catch (error) { if (error.name !== 'AbortError') fail(error.message); }
  finally { if (controller === abort) controller = null; }
});

async function loadConfiguration() {
  const response = await fetch('/config', {cache: 'no-store'});
  if (!response.ok) throw new Error('The race server configuration could not be loaded.');
  const data = await response.json();
  // With routing on, the first engine depends on the question, so chips keep their recorded order.
  firstEngine = data.first_engine_routing ? null : data.first_engine;
  liveAvailable = data.live_available === true;
  streamPath = typeof data.race_stream === 'string' ? data.race_stream : '/race/stream';
  raceAccess = data.race_access || (liveAvailable ? 'api_key' : 'replay');
  maxQuestion = Math.min(Number(data.max_question_chars) || 300, 300);
  $('#question').maxLength = maxQuestion;
  const reason = liveAvailable ? '' : (typeof data.live_reason === 'string' && data.live_reason) || 'Live races are off on this server, so this section plays recorded races.';
  $('#live-off').textContent = reason;
  $('#live-off').hidden = !reason;
  const examples = (Array.isArray(data.examples) && data.examples.length ? data.examples : EXAMPLES).slice(0, 6);
  $('#example-chips').replaceChildren(...examples.map(text => {
    const chip = element('button', 'example-chip', text);
    chip.type = 'button';
    chip.addEventListener('click', () => { setQuestion(text); $('#question-error').hidden = true; $('#question').focus(); });
    return chip;
  }));
}

function selectPair(pair) {
  replayPair = pair;
  $('#replay-meta').textContent = replayLabel(pair);
  if (!pair) return;
  $('#replay-select').value = pair.name;
  if (!active && mode === 'replay') {
    setQuestion(pair.question); setups.left = pair.left; setups.right = pair.right; applySetups(); badge(pair.fixture ? 'fixture' : 'replay');
  }
}
$('#replay-select').addEventListener('change', event => {
  const pair = pairs.find(p => p.name === event.target.value);
  if (!pair || active) return;
  selectPair(pair); playReplay();
});
async function loadReplays(prefer) {
  const response = await fetch('/replays', {cache: 'no-store'});
  if (!response.ok) throw new Error('The recordings could not be loaded.');
  pairs = await response.json();
  $('#replay-select').replaceChildren(...pairs.map(p=>{const option=element('option','',`${p.title}${p.fixture?' (synthetic fixture)':''}`);option.value=p.name;return option;}));
  replayPair = pairs.find(p => p.name === prefer) || pairs.find(p=>!p.fixture) || pairs[0] || null;
  if (replayPair) $('#replay-select').value = replayPair.name;
}

/** Live: type a question and pick both setups. Replay: play a recording of a live run. */
function setMode(next) {
  if (active) return;
  if (next === 'live' && !liveAvailable) next = 'replay';
  if (next !== mode) clearColumns();
  mode = next;
  $('#mode-live').setAttribute('aria-pressed', String(mode === 'live'));
  $('#mode-replay').setAttribute('aria-pressed', String(mode === 'replay'));
  if (mode === 'live') {
    Object.assign(setups, liveSetups);
    setQuestion(auditQuestion && !replayMode ? auditQuestion : '');
    badge('live');
    applySetups();
    notify('Type a question or pick an example, choose both setups, then press Run race.');
  } else {
    selectPair(replayPair);
    if (!replayPair) { $('#race-mode').textContent = 'No recordings'; notify('No recording is available on this server yet.'); }
    else notify(liveAvailable ? 'Play a recorded race, or switch to Live to run your own question.' : '');
  }
  setActive(false);
}
$('#mode-live').addEventListener('click', () => setMode('live'));
$('#mode-replay').addEventListener('click', () => setMode('replay'));

function maybeAutoplay(){if(ready&&raceVisible&&!autoplayDone&&mode==='replay'&&replayPair&&!replayPair.fixture)playReplay();}
new IntersectionObserver(entries=>{raceVisible=entries[0].isIntersecting;maybeAutoplay();},{threshold:.2}).observe($('#race'));
async function initialize(){
  try {
    await loadConfiguration();
    await loadReplays();
  } catch (error) {
    $('#race-mode').textContent = 'Server unavailable';
    notify(error.message);
  }
  ready = true;
  setMode(liveAvailable ? 'live' : 'replay');
  maybeAutoplay();
}
initialize();
