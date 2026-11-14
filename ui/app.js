import {SETUPS, VERDICTS, newRun, applyEvent, safeURL} from './race-state.js';

const $ = selector => document.querySelector(selector);
const elements = {};
const states = {};
let connections = [];
let active = false;
let generation = 0;
let replayPairs = [];
let firstEngine;
const clockOrigins = {};

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function link(url, label) {
  const safe = safeURL(url);
  if (!safe) return null;
  const node = element('a', '', label || safe.hostname.replace(/^www\./, ''));
  node.href = safe.href;
  node.target = '_blank';
  node.rel = 'noopener noreferrer';
  node.title = safe.href;
  return node;
}

function notify(message) {
  $('#notice').hidden = !message;
  $('#notice').textContent = message;
}

function setActive(value) {
  const startClock = value && !active;
  active = value;
  $('#run').disabled = value;
  $('#run').textContent = value ? 'Race in progress' : 'Run race ↗';
  $('#play').disabled = value || !$('#replay').value;
  $('#question').disabled = value;
  $('#replay').disabled = value;
  for (const column of Object.values(elements)) column.querySelector('.setup').disabled = value;
  if (startClock) requestAnimationFrame(tick);
}

function tick(now) {
  for (const side of ['left', 'right']) {
    const state = states[side];
    if (!state.finished && clockOrigins[side] !== undefined) {
      const seconds = Math.max(state.elapsed, now - clockOrigins[side]) / 1000;
      elements[side].querySelector('.time').replaceChildren(document.createTextNode(seconds.toFixed(1)), element('span', '', 's'));
    }
  }
  if (active) requestAnimationFrame(tick);
}

function reset(left, right) {
  connections.forEach(connection => connection.close());
  connections = [];
  generation += 1;
  for (const [side, setup] of Object.entries({left, right})) {
    delete clockOrigins[side];
    states[side] = newRun(setup, firstEngine);
    elements[side].querySelector('.setup').value = setup;
    render(side);
  }
  notify('');
  setActive(true);
}

function render(side) {
  const state = states[side];
  const column = elements[side];
  const find = selector => column.querySelector(selector);
  column.dataset.setup = state.setup;
  find('.setup-description').textContent = SETUPS[state.setup].description;
  find('.engine-count').textContent = state.engineCalls;
  find('.cost').textContent = `$${state.cost.toFixed(4)}`;
  find('.time').replaceChildren(document.createTextNode((state.elapsed / 1000).toFixed(1)), element('span', '', 's'));
  find('.telem-count').textContent = state.telemCalls;
  find('.session').textContent = state.session ? `Session ${state.session}` : (state.finished ? 'No session recorded' : 'Session pending');
  find('.session').title = state.session ? `Telem session: ${state.session}` : 'No Telem session recorded yet';
  find('.run-status').textContent = state.status;
  find('.run-status').classList.toggle('running', state.status === 'Running');
  find('.event-count').textContent = `${state.events.length} events recorded`;
  find('.export').disabled = !state.events.length;
  const steps = find('.steps');
  if (state.steps.length) {
    const nodes = state.steps.map((step, index) => {
      const row = element('li', 'step');
      const query = element('div', 'step-query');
      query.append(element('span', 'step-number', String(index + 1).padStart(2, '0')), element('span', '', step.query));
      row.append(query);
      const meta = element('div', 'step-meta');
      step.engines.forEach(engine => meta.append(element('span', `chip ${engine === firstEngine ? 'first-engine' : ''}`, engine)));
      if (step.verdict) meta.append(element('span', `verdict ${step.verdict.toLowerCase()}`, VERDICTS[step.verdict] || step.verdict));
      if (step.cached) meta.append(element('span', 'chip', 'Cached'));
      if (meta.childNodes.length) row.append(meta);
      if (step.why) row.append(element('p', 'step-note', step.why));
      if (step.url) {
        const source = link(step.url);
        if (source) { const note = element('p', 'step-note'); note.append(source); row.append(note); }
      }
      if (step.goal) row.title = `Goal: ${step.goal}`;
      return row;
    });
    steps.replaceChildren(...nodes);
    steps.scrollTop = steps.scrollHeight;
  } else {
    const empty = element('li', 'empty-step');
    empty.append(element('span', '', '↗'), element('strong', '', 'The evidence will unfold here.'),
      element('span', '', 'Queries, engines, checks, and page reads.'));
    steps.replaceChildren(empty);
  }
  const declined = state.answer?.status === 'NOT_FOUND';
  find('.answer').classList.toggle('declined', declined);
  find('.answer').classList.toggle('waiting', !state.answer);
  find('.answer-label').textContent = declined ? 'NOT FOUND · A DELIBERATE OUTCOME' : 'FINAL ANSWER';
  find('.answer-text').textContent = state.answer?.final || (state.events.length ? 'Following the evidence…' : 'Waiting for a question.');
  find('.sources').replaceChildren(...(state.answer?.citations || []).map((url, index) => {
    const safe = safeURL(url);
    return safe ? link(url, `[${index + 1}] ${safe.hostname.replace(/^www\./, '')}`) : null;
  }).filter(Boolean));
}

function receive(side, event) {
  if (applyEvent(states[side], event)) {
    clockOrigins[side] = performance.now() - states[side].elapsed;
    render(side);
  }
  if (states.left.finished && states.right.finished) setActive(false);
}

function streamFailure(connection, sides, message) {
  connection.close();
  notify(message);
  for (const side of sides) {
    if (!states[side].finished) {
      states[side].status = 'Interrupted';
      states[side].finished = true;
      render(side);
    }
  }
  if (states.left.finished && states.right.finished) setActive(false);
}

function connect(url, sides, replay = false) {
  const connection = new EventSource(url);
  connections.push(connection);
  connection.addEventListener('log', message => {
    try {
      const payload = JSON.parse(message.data);
      if (replay) receive(payload.side, payload.event);
      else receive(sides[0], payload);
    } catch (error) { streamFailure(connection, sides, error.message); }
  });
  connection.addEventListener('complete', () => connection.close());
  connection.addEventListener('stream_error', message => streamFailure(connection, sides, JSON.parse(message.data).message));
  connection.onerror = () => streamFailure(connection, sides, 'The event stream was interrupted. Recorded events can still be exported.');
}

for (const [side, setup] of Object.entries({left: 'single', right: 'second_opinion'})) {
  const column = $('#column-template').content.firstElementChild.cloneNode(true);
  column.id = `column-${side}`;
  column.querySelector('.column-letter').textContent = side === 'left' ? 'A' : 'B';
  const picker = column.querySelector('.setup');
  picker.value = setup;
  picker.setAttribute('aria-label', `${side} setup`);
  picker.addEventListener('change', () => { states[side] = newRun(picker.value, firstEngine); render(side); });
  column.querySelector('.export').addEventListener('click', () => {
    const state = states[side];
    const blob = new Blob([JSON.stringify(state.events, null, 2)], {type: 'application/json'});
    const url = URL.createObjectURL(blob);
    const anchor = element('a');
    anchor.href = url;
    anchor.download = `${state.runId || side}-audit.json`;
    anchor.hidden = true;
    document.body.append(anchor);
    anchor.click();
    anchor.remove();
    requestAnimationFrame(() => URL.revokeObjectURL(url));
  });
  elements[side] = column;
  states[side] = newRun(setup, firstEngine);
  $('#columns').append(column);
  render(side);
}

$('#race-form').addEventListener('submit', async event => {
  event.preventDefault();
  if (active) return;
  const question = $('#question').value.trim();
  if (!question) { notify('Enter a question first.'); return; }
  const left = elements.left.querySelector('.setup').value;
  const right = elements.right.querySelector('.setup').value;
  reset(left, right);
  const current = generation;
  $('#mode').textContent = 'Live run';
  $('#mode').classList.remove('recording');
  try {
    const response = await fetch('/race', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({question, left, right})});
    const data = await response.json();
    if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'The question could not be submitted.');
    if (current !== generation) return;
    connect(`/stream/${encodeURIComponent(data.left_run_id)}`, ['left']);
    connect(`/stream/${encodeURIComponent(data.right_run_id)}`, ['right']);
  } catch (error) { notify(error.message); setActive(false); }
});

$('#replay').addEventListener('change', () => { $('#play').disabled = active || !$('#replay').value; });
$('#play').addEventListener('click', () => {
  const pair = replayPairs.find(item => item.name === $('#replay').value);
  if (!pair || active) return;
  reset(pair.left, pair.right);
  $('#question').value = pair.question;
  $('#mode').textContent = pair.fixture ? 'Fixture replay · synthetic demonstration' : 'Replay of a live run';
  $('#mode').classList.add('recording');
  connect(`/replay/${encodeURIComponent(pair.name)}`, ['left', 'right'], true);
});

async function loadReplays() {
  try {
    const response = await fetch('/replays');
    if (!response.ok) throw new Error('Replays could not be loaded.');
    replayPairs = await response.json();
    for (const pair of replayPairs) {
      const option = element('option', '', `${pair.fixture ? 'Fixture: ' : ''}${pair.title}`);
      option.value = pair.name;
      $('#replay').append(option);
    }
  } catch (error) { notify(error.message); }
}

function svgNode(tag, attributes = {}, text) {
  const node = document.createElementNS('http://www.w3.org/2000/svg', tag);
  Object.entries(attributes).forEach(([key, value]) => node.setAttribute(key, value));
  if (text !== undefined) node.textContent = text;
  return node;
}

function drawFrontier(frontier) {
  const container = $('#frontier');
  container.replaceChildren();
  if (!frontier?.points.length) { container.append(element('p', 'chart-empty', 'The held-out frontier has not been published yet.')); return; }
  const chart = svgNode('svg', {viewBox: '0 0 760 285', role: 'img', 'aria-label': 'Held-out search coverage versus cost per question'});
  const points = frontier.points;
  const maxCost = Math.max(...points.map(point => point.cost_per_q), 0.001) * 1.12;
  const colors = {combo: 'var(--muted)', telem_default: 'var(--amber)', second_opinion: 'var(--accent)'};
  for (let i = 0; i <= 4; i++) {
    const y = 225 - i * 49;
    chart.append(svgNode('line', {x1: 75, x2: 725, y1: y, y2: y, stroke: 'var(--line)'}));
    chart.append(svgNode('text', {x: 65, y: y + 4, 'text-anchor': 'end'}, `${i * 25}%`));
    const x = 75 + i * 162.5;
    chart.append(svgNode('text', {x, y: 246, 'text-anchor': 'middle'}, `$${(maxCost * i / 4).toFixed(3)}`));
  }
  chart.append(svgNode('text', {x: 400, y: 275, 'text-anchor': 'middle'}, 'Cost per question (USD)'));
  chart.append(svgNode('text', {transform: 'translate(17 127) rotate(-90)', 'text-anchor': 'middle'}, 'Answer found in results'));
  for (const point of points) {
    const name = point.kind === 'combo' ? point.engines.join(' + ') : SETUPS[point.kind].name;
    const dot = svgNode('circle', {cx: 75 + point.cost_per_q / maxCost * 650, cy: 225 - point.coverage * 196,
      r: point.kind === 'combo' ? 5 : 8, fill: colors[point.kind], opacity: point.kind === 'combo' ? .55 : 1, tabindex: 0});
    const label = `${name}: ${(point.coverage * 100).toFixed(1)}% coverage, $${point.cost_per_q.toFixed(4)} per question`;
    dot.setAttribute('aria-label', label);
    dot.append(svgNode('title', {}, label));
    chart.append(dot);
  }
  container.append(chart);
  const legend = element('div', 'chart-legend');
  for (const [kind, label] of Object.entries({combo: 'Engine combinations', telem_default: 'Telem default', second_opinion: 'Second Opinion'})) {
    const item = element('span');
    const dot = element('span', 'legend-dot');
    dot.style.background = colors[kind];
    item.append(dot, document.createTextNode(label));
    legend.append(item);
  }
  container.append(legend);
}

async function loadResults() {
  const notice = $('#results-notice');
  notice.hidden = false;
  notice.textContent = 'Loading held-out results…';
  try {
    const response = await fetch('/results');
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.detail || 'Results could not be loaded.');
    const results = payload.results;
    $('#results-content').replaceChildren();
    $('#result-counts').textContent = results ? `${results.n_test} held-out questions · ${results.n_traps} traps reported separately · Updated ${results.generated_at}` : 'No evaluation counts available yet.';
    notice.hidden = Boolean(results);
    notice.textContent = 'Evaluation results have not been published yet. Replay fixtures are demonstrations, not measured results.';
    if (results) {
      const metrics = [
        ['Correct', row => row.correct], ['Wrong', row => row.wrong], ['Declined', row => row.declined],
        ['Accuracy', row => `${(row.accuracy * 100).toFixed(1)}%`], ['Wrong rate', row => `${(row.wrong_rate * 100).toFixed(1)}%`],
        ['Engines / Q', row => row.engine_calls_per_q.toFixed(2)], ['Search $ / Q', row => `$${row.cost_per_q_search.toFixed(4)}`],
        ['Total $ / Q', row => `$${row.cost_per_q_total.toFixed(4)}`], ['Cost / correct', row => row.cost_per_correct == null ? 'n/a' : `$${row.cost_per_correct.toFixed(4)}`],
        ['Cost / search', row => row.cost_per_search_call == null ? 'n/a' : `$${row.cost_per_search_call.toFixed(4)}`],
        ['P50 time', row => `${(row.latency_p50_ms / 1000).toFixed(1)}s`], ['P95 time', row => `${(row.latency_p95_ms / 1000).toFixed(1)}s`],
        ['Trap confident wrong', row => `${row.trap_confident_wrong} / ${row.trap_n}`],
      ];
      const table = element('table');
      const head = element('thead');
      const headings = element('tr');
      ['Setup', ...metrics.map(([label]) => label)].forEach(label => headings.append(element('th', '', label)));
      head.append(headings);
      const body = element('tbody');
      for (const [setup, row] of Object.entries(results.setups)) {
        const line = element('tr', setup === 'second_opinion' ? 'highlight' : '');
        line.append(element('td', '', SETUPS[setup].name));
        metrics.forEach(([, format]) => line.append(element('td', '', format(row))));
        body.append(line);
      }
      table.append(head, body);
      const wrap = element('div', 'table-wrap');
      wrap.append(table);
      $('#results-content').append(wrap);
    }
    drawFrontier(payload.frontier);
  } catch (error) { notice.hidden = false; notice.textContent = error.message; }
}

for (const view of ['race', 'results']) {
  $(`#${view}-tab`).addEventListener('click', () => {
    for (const candidate of ['race', 'results']) {
      $(`#${candidate}-view`).hidden = candidate !== view;
      $(`#${candidate}-tab`).classList.toggle('active', candidate === view);
      if (candidate === view) $(`#${candidate}-tab`).setAttribute('aria-current', 'page');
      else $(`#${candidate}-tab`).removeAttribute('aria-current');
    }
    if (view === 'results') loadResults();
  });
}
$('#refresh-results').addEventListener('click', loadResults);

function setTheme(theme) {
  document.documentElement.dataset.theme = theme;
  $('#theme').textContent = theme === 'dark' ? 'Light mode' : 'Dark mode';
}
let savedTheme;
try { savedTheme = localStorage.getItem('second-opinion-theme'); } catch { /* Storage is optional. */ }
setTheme(savedTheme || (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'));
$('#theme').addEventListener('click', () => {
  const theme = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
  setTheme(theme);
  try { localStorage.setItem('second-opinion-theme', theme); } catch { /* Storage is optional. */ }
});
async function initialize() {
  $('#run').disabled = true;
  try {
    const response = await fetch('/config');
    if (!response.ok) throw new Error('Server settings could not be loaded. Reload after checking the server.');
    const settings = await response.json();
    firstEngine = settings.first_engine;
    $('#question').maxLength = settings.max_question_chars;
    const engineName = firstEngine.charAt(0).toUpperCase() + firstEngine.slice(1);
    $('#engine-note').textContent = `${engineName} first. Evidence at every step.`;
    SETUPS.single.description = `${engineName} through Telem. No result check.`;
    SETUPS.second_opinion.description = `${engineName} first. Check the evidence. Rescue a miss.`;
    for (const side of ['left', 'right']) { states[side].firstEngine = firstEngine; render(side); }
    $('#run').disabled = false;
    await loadReplays();
  } catch (error) { notify(error.message); }
}
initialize();
