/** Live race section of the product site (/#race). Live: type a question and race two setups. Replay: recordings of live runs. */
import {newRun, applyEvent, failRun, citationVerified, VERDICTS, SETUPS, safeURL} from './race-state.js';
import {raceControls, liveHeaders} from './race-access.js';
import {watchRun} from './run-stream.js';

const $ = selector => document.querySelector(selector);
const elements = {}, states = {};
let firstEngine = 'tavily', active = false, replayPair = null, replaySource = null, controller = null;
let replayMode = false, autoplayDone = false, raceVisible = false, ready = false, liveAvailable = false;
const liveStreams = new Map();
let auditQuestion = '', pairs = [];
let mode = 'replay', raceAccess = 'replay', demoRequired = false, maxQuestion = 300, lastRuns = null, lastSaved = null;
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
  $('#run-button').disabled = controls.disableRun || (live && demoRequired && !$('#demo-code').value.trim());
  $('#credentials').hidden = !(live && needsKey);
  $('#demo-code-box').hidden = !(live && demoRequired);
  $('#replay-only').hidden = liveAvailable;
  $('#question').disabled = value;
  $('#question').readOnly = !live;
  $('#api-key').disabled = value;
  $('#example-chips').hidden = !live;
  $('#example-chips').querySelectorAll('button').forEach(chip => { chip.disabled = value; });
  $('#replay-controls').hidden = live;
  $('#replay-button').disabled = controls.disableReplay;
  $('#stop-button').hidden = !value;
  // Setup pickers apply to live races; a replay always shows the setups it recorded.
  for (const side of ['left', 'right']) $(`#${side}-setup`).disabled = value || !live;
  $('#replay-select').disabled = value || pairs.length < 2;
  $('#mode-live').disabled = value || !liveAvailable;
  $('#mode-replay').disabled = value;
  const finished = states.left?.finished && states.right?.finished && !states.left.failure && !states.right.failure;
  $('#save-recording').hidden = !(live && lastRuns && finished && !value && lastSaved !== lastRuns.left_run_id);
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
for (const side of ['left', 'right']) {
  $(`#${side}-setup`).addEventListener('change', event => {
    setups[side] = event.target.value; liveSetups[side] = event.target.value;
    store.set('backstop.race.pair', JSON.stringify(liveSetups));
    applySetups();
  });
}

function render(side) {
  const state = states[side], column = elements[side], find = selector => column.querySelector(selector);
  find('.telem-count').textContent = String(state.telemCalls);
  find('.cost-count').textContent = `$${state.cost.toFixed(4)}`;
  find('.time-count').replaceChildren(document.createTextNode((state.elapsed / 1000).toFixed(1)), element('span', '', 's'));
  find('.run-status').textContent = state.status;
  find('.session').textContent = state.session ? `Telem session · ${state.session}` : state.finished ? 'No Telem session recorded' : 'Session pending';
  const steps = find('.steps');
  if (state.steps.length) {
    steps.replaceChildren(...state.steps.map((step, i) => {
      const row = element('li'), title = element('div', 'step-query');
      title.append(element('span', 'step-num', String(i + 1).padStart(2, '0')), element('span', '', step.query));
      row.append(title);
      if (step.choice) {
        const choice = [step.choice.engine ? `First engine: ${step.choice.engine}` : null,
          step.choice.questionType ? `Question type: ${step.choice.questionType}` : null].filter(Boolean).join(' · ');
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
  find('.answer-label').textContent = state.failure ? 'Run failed' : declined ? 'Not found · a deliberate outcome' : 'Final answer';
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
  $('#export-audit').disabled = !states.left?.events.length && !states.right?.events.length;
}

function closeConnections() {
  replaySource?.close(); replaySource = null; controller?.abort(); controller = null;
  liveStreams.forEach(close => close()); liveStreams.clear();
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
  badge(kind === 'live' ? 'live' : kind === 'fixture' ? 'fixture' : 'replay');
  setActive(true);
}
function badge(kind) {
  const label = {live: 'Live', replay: 'Replay of a live run', fixture: 'Synthetic fixture · not a live run'}[kind];
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
function finish() { setActive(false); const interrupted = Object.values(states).some(state => Boolean(state.failure)); notify(interrupted ? 'One agent stopped before completion. The received audit trail is available below.' : replayMode ? (liveAvailable ? 'Recorded run complete. Switch to Live to race your own question.' : 'Recorded run complete. Replay it or export the audit trail.') : 'Both agents have finished. Export the audit trail, or save the race as a recording.'); }
function fail(message, side) {
  for (const name of side ? [side] : ['left', 'right']) {
    if (side || !states[name].finished) { failRun(states[name], message); render(name); }
  }
  notify(message);
  if (states.left.finished && states.right.finished) setActive(false);
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
  $('#question').value = replayPair.question;
  auditQuestion = replayPair.question;
  notify(replayPair.fixture ? 'Synthetic test events. These are not product results.' : 'Recorded evidence, played at its original pace.');
  const source = new EventSource(`/replay/${encodeURIComponent(replayPair.name)}`);
  replaySource = source;
  source.addEventListener('log', message => { if (source !== replaySource) return; try { const payload = JSON.parse(message.data); receive(payload.side, payload.event); } catch { source.close(); fail('The recording contains an invalid event.'); } });
  source.addEventListener('complete', () => { source.close(); if (!(states.left.finished && states.right.finished)) fail('The recording ended before final totals.'); });
  source.addEventListener('stream_error', () => { source.close(); fail('The recorded stream was interrupted.'); });
  source.onerror = () => { source.close(); if (active) fail('The recording disconnected. You can replay it or run a question.'); };
}
$('#replay-button').addEventListener('click', playReplay);
$('#race-theme').addEventListener('click', () => {
  const light = $('#race').dataset.theme !== 'light';
  $('#race').dataset.theme = light ? 'light' : 'dark';
  $('#race-theme').textContent = light ? 'Dark mode' : 'Light mode';
  $('#race-theme').setAttribute('aria-pressed', String(light));
});
$('#stop-button').addEventListener('click', () => {
  closeConnections();
  fail(replayMode ? 'Replay stopped. The received events are available to export.' : 'Stopped viewing. Live agents continue until completion or the server timeout.');
});

$('#api-key').addEventListener('input', () => setActive(active));
$('#demo-code').value = store.get('backstop.demoCode') || '';
$('#demo-code').addEventListener('input', event => { store.set('backstop.demoCode', event.target.value.trim()); setActive(active); });
$('#question').addEventListener('input', () => { $('#question-error').hidden = true; });

function validQuestion() {
  const question = $('#question').value.trim();
  const problem = !question ? 'Type a question first.' : question.length > maxQuestion ? `Keep the question under ${maxQuestion} characters.` : '';
  $('#question-error').textContent = problem;
  $('#question-error').hidden = !problem;
  return problem ? null : question;
}
function raceHeaders() {
  if (raceAccess === 'api_key') return liveHeaders($('#api-key').value);
  const headers = {'Content-Type': 'application/json'};
  const code = $('#demo-code').value.trim();
  if (code) headers['X-Backstop-Demo-Code'] = code;
  return headers;
}
async function errorMessage(response, fallback) {
  try { const error = await response.json(); return typeof error.detail === 'string' ? error.detail : error.detail?.message || fallback; }
  catch { return fallback; }
}

$('#race-form').addEventListener('submit', async event => {
  event.preventDefault(); if (active || !liveAvailable || mode !== 'live') return;
  const question = validQuestion(); if (!question) return;
  let headers;
  try { headers = raceHeaders(); } catch (error) { notify(error.message); return; }
  $('#question').value = question;
  auditQuestion = question;
  setups.left = $('#left-setup').value; setups.right = $('#right-setup').value;
  lastRuns = null;
  autoplayDone = true; reset('live'); notify('Both agents are searching live through Telem. Every counter comes from the event log.');
  const abort = new AbortController(); controller = abort;
  try {
    const response = await fetch('/race',{method:'POST',headers,body:JSON.stringify({question,left:setups.left,right:setups.right}),signal:abort.signal});
    if (!response.ok) throw new Error(await errorMessage(response, 'The race could not start.'));
    const runs = await response.json();
    if (typeof runs.left_run_id !== 'string' || typeof runs.right_run_id !== 'string') {
      throw new Error('The server did not return both run IDs.');
    }
    lastRuns = runs;
    for (const side of ['left', 'right']) {
      liveStreams.set(side, watchRun(runs[`${side}_run_id`], {
        onEvent: event => receive(side, event),
        onComplete: () => {
          liveStreams.delete(side);
          if (!states[side].finished) fail('The stream ended before final totals.', side);
        },
        onFailure: message => { liveStreams.delete(side); fail(message, side); },
        onReconnect: () => notify('Reconnecting to the run. The agent continues on the server.'),
      }));
    }
  } catch(error) { if (error.name !== 'AbortError') fail(error.message); }
});
$('#save-recording').addEventListener('click', async () => {
  if (!lastRuns) return;
  $('#save-recording').disabled = true;
  try {
    const response = await fetch('/recordings', {method: 'POST', headers: raceHeaders(), body: JSON.stringify(lastRuns)});
    if (!response.ok) throw new Error(await errorMessage(response, 'The recording could not be saved.'));
    const saved = await response.json();
    lastSaved = lastRuns.left_run_id;
    await loadReplays(saved.name);
    notify('Saved. The race is now in the Replay list.');
  } catch (error) { notify(error.message); }
  finally { $('#save-recording').disabled = false; setActive(active); }
});
$('#export-audit').addEventListener('click', () => {
  const data = {product:'Backstop',mode:$('#race-mode').textContent,question:auditQuestion,setups:{...setups},runs:lastRuns && !replayMode ? lastRuns : undefined,left:states.left.events,right:states.right.events};
  const url = URL.createObjectURL(new Blob([JSON.stringify(data,null,2)+'\n'],{type:'application/json'}));
  const anchor = element('a'); anchor.href=url; anchor.download='backstop-race-audit.json';document.body.append(anchor);anchor.click();anchor.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);
});

async function loadConfiguration() {
  const response = await fetch('/config', {cache: 'no-store'});
  if (!response.ok) throw new Error('The race server configuration could not be loaded.');
  const data = await response.json();
  firstEngine = data.first_engine;
  liveAvailable = data.live_available === true;
  raceAccess = data.race_access || (liveAvailable ? 'api_key' : 'replay');
  demoRequired = data.demo_code_required === true;
  maxQuestion = Math.min(Number(data.max_question_chars) || 300, 300);
  $('#question').maxLength = maxQuestion;
  const examples = (Array.isArray(data.examples) && data.examples.length ? data.examples : EXAMPLES).slice(0, 6);
  $('#example-chips').replaceChildren(...examples.map(text => {
    const chip = element('button', 'example-chip', text);
    chip.type = 'button';
    chip.addEventListener('click', () => { $('#question').value = text; $('#question-error').hidden = true; $('#question').focus(); });
    return chip;
  }));
}
function selectPair(pair) {
  replayPair = pair;
  if (!pair) return;
  $('#replay-select').value = pair.name;
  if (!active && mode === 'replay') { $('#question').value = pair.question; setups.left = pair.left; setups.right = pair.right; applySetups(); badge(pair.fixture ? 'fixture' : 'replay'); }
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
  mode = next;
  $('#mode-live').setAttribute('aria-pressed', String(mode === 'live'));
  $('#mode-replay').setAttribute('aria-pressed', String(mode === 'replay'));
  if (mode === 'live') {
    Object.assign(setups, liveSetups);
    $('#question').value = auditQuestion && !replayMode ? auditQuestion : '';
    badge('live');
    applySetups();
    notify('Type a question or pick an example, choose both setups, then press Run race.');
  } else {
    selectPair(replayPair);
    if (!replayPair) { $('#race-mode').textContent = 'No recordings'; notify('No recording is available on this server yet.'); }
    else notify(liveAvailable ? 'Play a recording of a live race, or switch to Live to run your own.' : 'Live races are off on this server. Play a recording of a live race.');
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
