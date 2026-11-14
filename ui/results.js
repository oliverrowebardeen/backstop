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

const seconds = ms => `${(ms / 1000).toFixed(1)} s`;
const pick = (metrics, key) => metrics[{single: 'single', telem_default: 'baseline', second_opinion: 'ours'}[key]];
const latencyOk = block => Number.isFinite(block.latency_p50_ms) && block.latency_p50_ms > 0;
/** Equal within half a point: the two setups answered the same share of questions. */
export const sameAccuracy = (a, b) => Math.abs(a.accuracy - b.accuracy) < 0.005;

/**
 * Benchmark rows: one per setup, one bar per metric. Accuracy bars run from 0 to 100%, so 94.0% and
 * 94.5% look the same, as they are; the other bars are scaled to the largest value in their column.
 */
export const BENCH_COLUMNS = [
  {label: 'Accuracy', value: s => s.accuracy, text: pct, scale: () => 1},
  {label: 'Cost per question', value: s => s.cost_per_q_total, text: usd},
  {label: 'Engine calls per question', short: 'Engine calls', value: s => s.engine_calls_per_q, text: v => v.toFixed(2)},
  {label: 'Median time per question', short: 'Median time', value: s => latencyOk(s) ? s.latency_p50_ms : NaN, text: seconds},
];

/** Rows of {key, name, ours, cells: [{label, text, width}]}: width 0 to 1, text null when not recorded, label short for phones. */
export function benchmarkRows(metrics) {
  if (!metrics) return [];
  const blocks = SETUPS.map(([key, name]) => ({key, name, s: pick(metrics, key)}));
  return blocks.map(({key, name, s}) => ({
    key, name, ours: key === 'second_opinion',
    cells: BENCH_COLUMNS.map(column => {
      const value = column.value(s);
      const label = column.short ?? column.label;
      if (!Number.isFinite(value)) return {label, text: null, width: 0};
      const max = column.scale ? column.scale() : Math.max(...blocks.map(b => column.value(b.s)).filter(Number.isFinite));
      return {label, text: column.text(value), width: max > 0 ? Math.min(1, value / max) : 0};
    }),
  }));
}

function renderBenchmark(root, metrics) {
  const body = root.querySelector('.bench-table tbody');
  const foot = root.querySelector('.bench-foot');
  const rows = benchmarkRows(metrics);
  if (!rows.length) {
    const tr = el('tr');
    const td = el('td', 'pending-box', PENDING);
    td.colSpan = BENCH_COLUMNS.length + 1;
    tr.append(td);
    body.replaceChildren(tr);
    if (foot) foot.textContent = 'The table fills in when out/results.json has every setup.';
    return;
  }
  body.replaceChildren(...rows.map(row => {
    const tr = el('tr', row.ours ? 'is-ours' : '');
    const th = el('th', null, row.name);
    th.scope = 'row';
    tr.append(th);
    for (const c of row.cells) {
      const td = el('td');
      td.dataset.label = c.label;
      const metric = el('div', 'metric');
      const bar = el('span', 'metric-bar');
      bar.setAttribute('aria-hidden', 'true');
      const fill = el('span');
      fill.style.width = `${(c.width * 100).toFixed(1)}%`;
      bar.append(fill);
      metric.append(el('span', 'metric-value', c.text ?? 'Not recorded'), bar);
      td.append(metric);
      tr.append(td);
    }
    return tr;
  }));
  if (foot) foot.textContent = 'Held-out test questions. Total cost includes search, page reads, the checker and the agent model. Median time is the p50 wall time of the agent run.';
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
  const box = root.querySelector('.findings');
  const items = findingsFrom(markdown);
  if (!items.length) { box.replaceChildren(el('p', 'pending-box', PENDING)); return; }
  const list = el('ol', 'finding-list');
  list.append(...items.map((text, i) => {
    const item = el('li', 'finding');
    item.append(el('p', 'finding-label', `Finding ${i + 1}`), el('p', 'finding-text', withEngineNames(text)));
    return item;
  }));
  box.replaceChildren(list, el('p', 'findings-note', 'Both findings come from the engine hit matrix, where each question was asked of each engine once, with Tavily as the first engine. They are not from the agent runs above. Source: analysis/findings.md.'));
}

/**
 * One honest line under the live race, built from out/results.json: a single live question can go
 * either way, so the claim is the held-out measurement. Null when results are not complete.
 */
export function raceNote(metrics) {
  if (!metrics) return null;
  const {baseline, ours} = metrics;
  const e = economy(metrics);
  const accuracy = e.same ? `matched Telem default's accuracy (${pct(baseline.accuracy)})`
    : `scored ${pct(ours.accuracy)} accuracy vs ${pct(baseline.accuracy)} for Telem default`;
  const cost = e.cost === null ? '' : ` at ${change(e.cost, 'lower', 'higher')} total cost per question ` +
    `(${usd(ours.cost_per_q_total)} vs ${usd(baseline.cost_per_q_total)})`;
  const calls = e.calls === null ? '' : `, with ${change(e.calls, 'fewer', 'more')} engine calls`;
  return `Live single questions vary. On the held-out test questions, Backstop ${accuracy}${cost}${calls}.`;
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
  renderBenchmark(root, metrics);
  renderFindings(root, site?.findings ?? null);
  return {metrics, rescueTable: site?.rescue_table || null};
}
