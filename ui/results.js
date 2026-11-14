/** Results section: measured numbers only, with their question counts. Never invents a value. */
/** Derive claims only from complete, comparable held-out setup totals. */
// A setup may finish slightly fewer runs than the test split (a failed run is not scored).
// Each setup is then reported on its own count, which must cover at least 95% of the split.
const scored = (block, n) => block.n ?? n;
const scoredEnough = (m, n) => Number.isInteger(m) && m <= n && m >= Math.ceil(0.95 * n);
function proofMetrics(results) {
  if (!results || !Number.isInteger(results.n_test) || results.n_test <= 0) return null;
  const n = results.n_test;
  const {single, telem_default: baseline, second_opinion: ours} = results.setups || {};
  if (![single, baseline, ours].every(s => s && scoredEnough(scored(s, n), n) && s.correct + s.wrong + s.declined === scored(s, n))) return null;
  const sameCount = scored(single, n) === scored(ours, n);
  const wrong = !single.wrong ? null : sameCount ? 100 * (single.wrong - ours.wrong) / single.wrong
    : 100 * (single.wrong_rate - ours.wrong_rate) / single.wrong_rate;
  const accuracy = 100 * (ours.accuracy - baseline.accuracy);
  const cost = baseline.cost_per_q_total > 0 ? 100 * (baseline.cost_per_q_total - ours.cost_per_q_total) / baseline.cost_per_q_total : null;
  return {n, wrong, accuracy, cost, single, baseline, ours};
}

const SETUPS = [['single', 'Single engine'], ['telem_default', 'Telem default'], ['second_opinion', 'Backstop']];
const PENDING = 'Results pending';

const el = (tag, cls, text) => {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined && text !== null) node.textContent = text;
  return node;
};
const svg = (tag, attrs = {}, text) => {
  const node = document.createElementNS('http://www.w3.org/2000/svg', tag);
  for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
  if (text !== undefined) node.textContent = text;
  return node;
};
const pct = value => `${(value * 100).toFixed(1)}%`;
const usd = value => `$${value.toFixed(4)}`;

async function getJSON(url) {
  try {
    const response = await fetch(url, {cache: 'no-store'});
    return response.ok ? await response.json() : null;
  } catch { return null; }
}

/** Preserve the removed presentation endpoint's checks on the /results payload. */
export function validateResults(results) {
  if (!results || ['fake', 'fixture', 'simulated'].some(flag => results[flag]) ||
      String(results.generated_from || '').toLowerCase().includes('fake')) return false;
  const n = results.n_test;
  if (!Number.isInteger(n) || n <= 0) return false;
  return SETUPS.every(([key]) => {
    const block = results.setups?.[key];
    if (!block) return false;
    const counts = [block.correct, block.wrong, block.declined];
    return counts.every(count => Number.isInteger(count) && count >= 0) &&
      scoredEnough(scored(block, n), n) && counts.reduce((sum, count) => sum + count, 0) === scored(block, n) &&
      ['accuracy', 'wrong_rate', 'cost_per_q_total'].every(field => Number.isFinite(block[field]) && block[field] >= 0) &&
      Math.abs(block.accuracy - block.correct / scored(block, n)) <= .00011 &&
      Math.abs(block.wrong_rate - block.wrong / scored(block, n)) <= .00011;
  });
}

/** Results are shown only when every setup is complete and validation agrees. */
export function measured(results, vetted) {
  const metrics = proofMetrics(results);
  if (!metrics) return null;
  if (vetted === false) return null;
  return metrics;
}

function cell(text, sub) {
  const td = el('td');
  td.append(el('span', 'cell-main', text));
  if (sub) td.append(el('span', 'cell-sub', sub));
  return td;
}

const seconds = ms => `${(ms / 1000).toFixed(1)} s`;
const pick = (metrics, key) => metrics[{single: 'single', telem_default: 'baseline', second_opinion: 'ours'}[key]];
const latencyOk = block => Number.isFinite(block.latency_p50_ms) && block.latency_p50_ms > 0;
/** Equal within half a point: the two setups answered the same share of questions. */
export const sameAccuracy = (a, b) => Math.abs(a.accuracy - b.accuracy) < 0.005;

function renderTable(table, metrics) {
  const body = table.querySelector('tbody');
  body.replaceChildren();
  const rows = [
    ['Accuracy', 'Final answers graded against the answer key', s =>
      [pct(s.accuracy), `${s.correct}/${scored(s, metrics.n)} correct`]],
    ['Total cost per question', 'Search, page reads, checker and agent model', s =>
      Number.isFinite(s.cost_per_q_total) ? [usd(s.cost_per_q_total)] : [PENDING]],
    ['Engine calls per question', 'Every engine query Telem bills', s =>
      Number.isFinite(s.engine_calls_per_q) ? [s.engine_calls_per_q.toFixed(2)] : [PENDING]],
    ['Median time per question', 'p50 wall time of the agent run', s =>
      latencyOk(s) ? [seconds(s.latency_p50_ms)] : [PENDING]],
  ];
  for (const [label, sub, value] of rows) {
    const tr = el('tr');
    const th = el('th');
    th.scope = 'row';
    th.append(el('span', 'cell-main', label), el('span', 'cell-sub', sub));
    tr.append(th);
    for (const [key] of SETUPS) {
      const [main, detail] = metrics ? value(pick(metrics, key)) : [PENDING];
      const td = cell(main, detail);
      if (main === PENDING) td.classList.add('is-pending');
      if (key === 'second_opinion') td.classList.add('is-ours');
      tr.append(td);
    }
    body.append(tr);
  }
}

/**
 * Backstop against Telem default, as whole percents: cost and engine calls saved, time saved.
 * Positive means Backstop is lower or faster. Null for a value the results do not carry.
 */
export function economy(metrics) {
  if (!metrics) return null;
  const {baseline: base, ours} = metrics;
  const drop = (a, b) => Number.isFinite(a) && Number.isFinite(b) && b > 0 ? Math.round(100 * (b - a) / b) : null;
  return {
    same: sameAccuracy(ours, base),
    cost: drop(ours.cost_per_q_total, base.cost_per_q_total),
    calls: drop(ours.engine_calls_per_q, base.engine_calls_per_q),
    time: latencyOk(ours) && latencyOk(base) ? drop(ours.latency_p50_ms, base.latency_p50_ms) : null,
    ratio: base.cost_per_q_total > 0 ? ours.cost_per_q_total / base.cost_per_q_total : null,
  };
}

/** The hero headline's claim, "Same accuracy. Half the cost.", holds only for these results. */
export const HERO_FALLBACK = 'Pay for another engine only when the first one misses.';
export function heroClaimHolds(metrics) {
  const e = economy(metrics);
  return Boolean(e && e.same && e.ratio !== null && e.ratio >= 0.45 && e.ratio <= 0.55);
}

const change = (value, down, up) => `${Math.abs(value)}% ${value >= 0 ? down : up}`;

/** Accuracy first (the same), then what Backstop saves. */
function renderHeadline(root, metrics) {
  const tiles = root.querySelector('.headline-numbers');
  tiles.replaceChildren();
  const tile = (value, label, detail, pending, cls = '') => {
    const box = el('div', `headline-tile${pending ? ' is-pending' : ''}${cls ? ` ${cls}` : ''}`);
    box.append(el('p', 'tile-value', value), el('p', 'tile-label', label), el('p', 'tile-detail', detail));
    tiles.append(box);
  };
  if (!metrics) {
    tile(PENDING, 'Accuracy against Telem default', 'The held-out evaluation has not finished.', true);
    tile(PENDING, 'Total cost per question', 'Checker and agent cost will be counted in.', true);
    tile(PENDING, 'Engine calls per question', 'Numbers appear when every setup has run.', true);
    tile(PENDING, 'Median time per question', 'Numbers appear when every setup has run.', true);
    return;
  }
  const {single, baseline: base, ours} = metrics;
  const e = economy(metrics);
  if (e.same) tile(pct(ours.accuracy), 'Accuracy, the same as Telem default', `Telem default ${pct(base.accuracy)}, single engine ${pct(single.accuracy)}`);
  else tile(`${pct(ours.accuracy)} vs ${pct(base.accuracy)}`, 'Accuracy, Backstop vs Telem default', `Single engine ${pct(single.accuracy)}`);
  if (e.cost === null) tile(PENDING, 'Total cost per question', 'Cost was not recorded.', true);
  else tile(change(e.cost, 'lower', 'higher'), 'Total cost per question', `${usd(ours.cost_per_q_total)} vs ${usd(base.cost_per_q_total)} for Telem default`);
  if (e.calls === null) tile(PENDING, 'Engine calls per question', 'Engine calls were not recorded.', true);
  else tile(change(e.calls, 'fewer', 'more'), 'Engine calls per question', `${ours.engine_calls_per_q.toFixed(2)} vs ${base.engine_calls_per_q.toFixed(2)} for Telem default`);
  if (e.time === null) tile(PENDING, 'Median time per question', 'Latency was not recorded.', true);
  else tile(change(e.time, 'faster', 'slower'), 'Median time per question', `${seconds(ours.latency_p50_ms)} vs ${seconds(base.latency_p50_ms)} for Telem default`);
}

/** The hero's measured line, from the same numbers as the Results tiles. Hidden without results. */
function renderHero(metrics) {
  const proof = document.querySelector('.hero-proof');
  const title = document.getElementById('hero-title');
  if (title && metrics && !heroClaimHolds(metrics)) title.textContent = HERO_FALLBACK;
  if (!proof) return;
  const e = economy(metrics);
  const items = !e ? [] : [
    e.cost !== null && [change(e.cost, 'lower', 'higher'), 'cost per question'],
    e.calls !== null && [change(e.calls, 'fewer', 'more'), 'engine calls'],
    e.same ? [pct(metrics.ours.accuracy), 'accuracy, the same as Telem default']
      : [`${pct(metrics.ours.accuracy)} vs ${pct(metrics.baseline.accuracy)}`, 'accuracy against Telem default'],
  ].filter(Boolean);
  const list = proof.querySelector('.proof-list');
  list.replaceChildren(...items.map(([value, label]) => {
    const row = el('div');
    row.append(el('dt', null, label), el('dd', null, value));
    return row;
  }));
  proof.hidden = items.length === 0;
}

const narrowChart = container => container.clientWidth > 0 && container.clientWidth < 480;

/** Accuracy against total cost per question, one labelled point per setup. */
export function drawAccuracyCost(container, metrics) {
  const note = container.parentElement.querySelector('.chart-note');
  const points = metrics ? SETUPS.map(([key, name]) => ({key, name, s: pick(metrics, key)}))
    .filter(p => Number.isFinite(p.s.cost_per_q_total) && Number.isFinite(p.s.accuracy)) : [];
  if (points.length < SETUPS.length) {
    container.replaceChildren(el('p', 'pending-box', PENDING));
    if (note) note.textContent = 'The chart appears when out/results.json has every setup.';
    return;
  }
  // A narrow card gets a narrower drawing, so the labels stay readable on a phone.
  const W = narrowChart(container) ? 380 : 620, H = 400, L = 64, R = 34, T = 24, B = 62;
  const maxCost = Math.max(...points.map(p => p.s.cost_per_q_total)) * 1.18;
  // A scatter needs no zero baseline; the floor is a round 5 points, at least 8 below the lowest setup.
  const floor = Math.max(0, Math.floor((Math.min(...points.map(p => p.s.accuracy)) - 0.08) * 20) / 20);
  const x = v => L + (v / maxCost) * (W - L - R);
  const y = v => H - B - ((v - floor) / (1 - floor)) * (H - T - B);
  const chart = svg('svg', {viewBox: `0 0 ${W} ${H}`, role: 'img', class: 'frontier-svg',
    'aria-label': `Accuracy against total cost per question: ${points.map(p => `${p.name} ${pct(p.s.accuracy)} at ${usd(p.s.cost_per_q_total)}`).join(', ')}.`});
  const steps = Math.round((1 - floor) / 0.05);
  for (let i = 0; i <= steps; i += 1) {
    const value = floor + i * 0.05, gy = y(value);
    chart.append(svg('line', {x1: L, x2: W - R, y1: gy, y2: gy, class: 'grid'}), svg('text', {x: L - 12, y: gy + 4, 'text-anchor': 'end', class: 'tick-label'}, `${Math.round(value * 100)}%`));
  }
  for (let i = 0; i <= 4; i += 1) {
    const value = (maxCost * i) / 4;
    chart.append(svg('text', {x: x(value), y: H - B + 24, 'text-anchor': 'middle', class: 'tick-label'}, `$${value.toFixed(3)}`));
  }
  chart.append(svg('line', {x1: L, x2: L, y1: T, y2: H - B, class: 'axis'}), svg('line', {x1: L, x2: W - R, y1: H - B, y2: H - B, class: 'axis'}));
  const ordered = [...points].sort((a, b) => a.s.cost_per_q_total - b.s.cost_per_q_total);
  ordered.forEach((p, i) => {
    const cx = x(p.s.cost_per_q_total), cy = y(p.s.accuracy);
    const count = scored(p.s, metrics.n);
    const dot = svg('circle', {cx, cy, r: p.key === 'second_opinion' ? 9 : 8, class: `dot-${p.key}`});
    dot.append(svg('title', {}, `${p.name}: ${p.s.correct} of ${count} correct (${pct(p.s.accuracy)}), ${usd(p.s.cost_per_q_total)} total per question`));
    // Labels alternate above and below so nearby points never collide.
    const above = i % 2 === 0;
    chart.append(dot,
      svg('text', {x: cx, y: cy + (above ? -32 : 34), 'text-anchor': 'middle', class: `point-label${p.key === 'second_opinion' ? ' label-ours' : ''}`}, p.name),
      svg('text', {x: cx, y: cy + (above ? -15 : 51), 'text-anchor': 'middle', class: 'point-sub'}, `${pct(p.s.accuracy)} at ${usd(p.s.cost_per_q_total)}`));
  });
  chart.append(svg('text', {x: (L + W - R) / 2, y: H - 14, 'text-anchor': 'middle', class: 'axis-label'}, 'total cost per question'),
    svg('text', {transform: `translate(20 ${(T + H - B) / 2}) rotate(-90)`, 'text-anchor': 'middle', class: 'axis-label'}, 'accuracy'));
  container.replaceChildren(chart);
  if (note) note.textContent = `Held-out test questions. Total cost includes search, page reads, the checker and the agent model. The vertical axis starts at ${Math.round(floor * 100)}%.`;
}

/** Two plain findings from analysis/findings.md. A FAKE DATA banner keeps them hidden. */
export function findingsFrom(markdown) {
  if (typeof markdown !== 'string' || /fake data/i.test(markdown)) return [];
  const paragraphs = markdown.split(/\n\s*\n/).map(p => p.trim()).filter(p => p && !p.startsWith('>') && !p.startsWith('#'))
    .map(p => p.replace(/\s+/g, ' ').replace(/[*_`]/g, ''));
  if (paragraphs.length === 1) {
    const split = paragraphs[0].search(/ On the test split/);
    if (split > 0) return [paragraphs[0].slice(0, split).trim(), paragraphs[0].slice(split).trim()];
  }
  return paragraphs.slice(0, 2);
}

const ENGINE_NAMES = {tavily: 'Tavily', exa: 'Exa', brave: 'Brave', perplexity: 'Perplexity', parallel: 'Parallel', ceramic: 'Ceramic'};
/** analysis/findings.md writes engine ids; the site writes engine names. */
export const withEngineNames = text => text.replace(/\b(tavily|exa|brave|perplexity|parallel|ceramic)\b/g, id => ENGINE_NAMES[id]);

function renderFindings(root, markdown) {
  const list = root.querySelector('.findings');
  const items = findingsFrom(markdown);
  list.replaceChildren(...(items.length ? items.map((text, i) => {
    const card = el('article', 'finding');
    card.append(el('p', 'label', `Finding ${i + 1}`), el('p', 'finding-text', withEngineNames(text)));
    return card;
  }) : [el('p', 'pending-box', PENDING)]));
  if (items.length) list.append(el('p', 'findings-note', 'Both findings come from the engine hit matrix, where each question was asked of each engine once, with Tavily as the first engine. They are not from the agent runs above. Source: analysis/findings.md.'));
}

/**
 * One honest line under the live race, built from out/results.json: a single live question can go
 * either way, so the claim is the held-out measurement. Null when results are not complete.
 */
export function raceNote(metrics) {
  if (!metrics) return null;
  const {n, baseline, ours} = metrics;
  const same = Math.abs(ours.accuracy - baseline.accuracy) < 0.005;
  const accuracy = same ? 'same accuracy as Telem default' : `${pct(ours.accuracy)} accuracy vs ${pct(baseline.accuracy)} for Telem default`;
  const ratio = baseline.cost_per_q_total > 0 ? ours.cost_per_q_total / baseline.cost_per_q_total : null;
  const cost = ratio === null ? 'cost per question' : ratio < 0.5 ? 'less than half the cost per question'
    : ratio < 1 ? `${Math.round(100 * (1 - ratio))}% lower cost per question` : `${Math.round(100 * (ratio - 1))}% higher cost per question`;
  return `Live single questions vary. Measured on ${n} held-out factual questions: ${accuracy} at ${cost} ` +
    `(Backstop ${ours.correct}/${scored(ours, n)} vs Telem default ${baseline.correct}/${scored(baseline, n)} correct; ` +
    `${usd(ours.cost_per_q_total)} vs ${usd(baseline.cost_per_q_total)} total per question).`;
}

export async function loadResults(root) {
  const [data, site] = await Promise.all([getJSON('/results'), getJSON('/site-data')]);
  const results = data?.results || null;
  const metrics = measured(results, validateResults(results));
  const status = root.querySelector('.results-status');
  status.textContent = metrics ? 'Same test questions for all three setups' : 'Results pending: the held-out evaluation has not finished';
  status.classList.toggle('is-pending', !metrics);
  renderHero(metrics);
  renderHeadline(root, metrics);
  const note = document.getElementById('race-honest');
  if (note) { const text = raceNote(metrics); note.textContent = text || ''; note.hidden = !text; }
  renderTable(root.querySelector('.results-table'), metrics);
  const chart = root.querySelector('.accuracy-chart');
  drawAccuracyCost(chart, metrics);
  let narrow = narrowChart(chart);
  addEventListener('resize', () => {
    if (narrowChart(chart) === narrow) return;
    narrow = narrowChart(chart);
    drawAccuracyCost(chart, metrics);
  });
  renderFindings(root, site?.findings ?? null);
  return {metrics, rescueTable: site?.rescue_table || null};
}
