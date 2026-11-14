/** Results section: measured numbers only, with their question counts. Never invents a value. */
/** Derive claims only from complete, comparable held-out setup totals. */
// A setup may finish slightly fewer runs than the test split (a failed run is not scored).
// Each setup is then reported on its own count, which must cover at least 95% of the split.
const scored = (block, n) => block.n ?? n;
const coverageOk = (m, n) => Number.isInteger(m) && m <= n && m >= Math.ceil(0.95 * n);
function proofMetrics(results) {
  if (!results || !Number.isInteger(results.n_test) || results.n_test <= 0) return null;
  const n = results.n_test;
  const {single, telem_default: baseline, second_opinion: ours} = results.setups || {};
  if (![single, baseline, ours].every(s => s && coverageOk(scored(s, n), n) && s.correct + s.wrong + s.declined === scored(s, n))) return null;
  const sameCount = scored(single, n) === scored(ours, n);
  const wrong = !single.wrong ? null : sameCount ? 100 * (single.wrong - ours.wrong) / single.wrong
    : 100 * (single.wrong_rate - ours.wrong_rate) / single.wrong_rate;
  const accuracy = 100 * (ours.accuracy - baseline.accuracy);
  const cost = baseline.cost_per_q_total > 0 ? 100 * (baseline.cost_per_q_total - ours.cost_per_q_total) / baseline.cost_per_q_total : null;
  return {n, wrong, accuracy, cost, single, baseline, ours};
}

/** Nondominated observed points. Never force Backstop onto the efficient frontier. */
function efficientFrontier(points) {
  const sorted = [...points].sort((a, b) => a.cost - b.cost || b.accuracy - a.accuracy);
  let best = -1;
  return sorted.filter(point => {
    if (point.accuracy <= best) return false;
    best = point.accuracy;
    return true;
  });
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

/** Coverage points by setup from frontier.json (answer found in results, not answer accuracy). */
export function coverageBySetup(frontier, firstEngine = 'tavily') {
  if (!frontier || !Array.isArray(frontier.points)) return {};
  const find = predicate => frontier.points.find(predicate) || null;
  // Prefer measured agent runs (same cohort for all three setups) over the one-search hit matrix.
  const runs = Array.isArray(frontier.measured_runs) ? frontier.measured_runs : [];
  const run = kind => runs.find(p => p.kind === kind) || null;
  return {
    single: run('single') || find(p => p.kind === 'combo' && p.engines?.length === 1 && p.engines[0] === firstEngine),
    telem_default: run('telem_default') || find(p => p.kind === 'telem_default'),
    second_opinion: run('second_opinion') || find(p => p.kind === 'second_opinion'),
  };
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
      coverageOk(scored(block, n), n) && counts.reduce((sum, count) => sum + count, 0) === scored(block, n) &&
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

function renderTable(table, metrics, coverage) {
  const body = table.querySelector('tbody');
  body.replaceChildren();
  const rows = [
    ['Wrong answers', 'Final answers graded wrong', key => {
      if (!metrics) return [PENDING];
      const s = metrics[{single: 'single', telem_default: 'baseline', second_opinion: 'ours'}[key]];
      return [pct(s.wrong / scored(s, metrics.n)), `${s.wrong} of ${scored(s, metrics.n)} questions`];
    }],
    ['Coverage', 'Answer found in results', key => {
      const p = coverage[key];
      if (!p || !Number.isFinite(p.coverage)) return [PENDING];
      const n = p.n_questions;
      const count = Number.isInteger(n) ? `${Math.round(p.coverage * n)} of ${n} questions` : 'count not recorded';
      return [pct(p.coverage), p.simulated ? `${count}, simulated` : count];
    }],
    ['Engine calls per question', 'Every engine query Telem bills', key => {
      if (!metrics) return [PENDING];
      const s = metrics[{single: 'single', telem_default: 'baseline', second_opinion: 'ours'}[key]];
      return Number.isFinite(s.engine_calls_per_q) ? [s.engine_calls_per_q.toFixed(2), `over ${scored(s, metrics.n)} questions`] : [PENDING];
    }],
    ['Cost per question', 'Search, page reads and model calls, checker included', key => {
      if (!metrics) return [PENDING];
      const s = metrics[{single: 'single', telem_default: 'baseline', second_opinion: 'ours'}[key]];
      return Number.isFinite(s.cost_per_q_total) ? [usd(s.cost_per_q_total), `over ${scored(s, metrics.n)} questions`] : [PENDING];
    }],
  ];
  for (const [label, sub, value] of rows) {
    const tr = el('tr');
    const th = el('th');
    th.scope = 'row';
    th.append(el('span', 'cell-main', label), el('span', 'cell-sub', sub));
    tr.append(th);
    for (const [key] of SETUPS) {
      const [main, detail] = value(key);
      const td = cell(main, detail);
      if (main === PENDING) td.classList.add('is-pending');
      if (key === 'second_opinion') td.classList.add('is-ours');
      tr.append(td);
    }
    body.append(tr);
  }
}

function renderHeadline(root, metrics) {
  const tiles = root.querySelector('.headline-numbers');
  tiles.replaceChildren();
  const tile = (value, label, detail, pending) => {
    const box = el('div', `headline-tile${pending ? ' is-pending' : ''}`);
    box.append(el('p', 'tile-value', value), el('p', 'tile-label', label), el('p', 'tile-detail', detail));
    tiles.append(box);
  };
  if (!metrics) {
    tile(PENDING, 'Wrong answers, single engine vs Backstop', 'The held-out evaluation has not finished.', true);
    tile(PENDING, 'Accuracy against Telem default', 'Numbers appear when every setup has run.', true);
    tile(PENDING, 'Cost per question against Telem default', 'Checker cost will be counted in.', true);
    return;
  }
  const {n, single, baseline, ours} = metrics;
  const of = b => `${b.wrong} of ${scored(b, n)}`;
  tile(pct(ours.wrong_rate), 'Wrong answers, Backstop',
    `Backstop ${of(ours)}, single engine ${of(single)}, Telem default ${of(baseline)}`);
  // "No accuracy loss" only when the measured rate is not below Telem default's.
  const noLoss = ours.accuracy >= baseline.accuracy;
  tile(noLoss ? 'No accuracy loss' : `${pct(ours.accuracy)} vs ${pct(baseline.accuracy)}`, 'Accuracy, Backstop vs Telem default',
    `${ours.correct} of ${scored(ours, n)} correct vs ${baseline.correct} of ${scored(baseline, n)}`);
  const cost = metrics.cost;
  tile(cost === null ? PENDING : `${Math.abs(cost).toFixed(1)}% ${cost >= 0 ? 'lower' : 'higher'}`, 'Cost per question against Telem default',
    `${usd(ours.cost_per_q_total)} vs ${usd(baseline.cost_per_q_total)}, checker included`, cost === null);
}

export function drawFrontier(container, frontier) {
  const points = frontier && Array.isArray(frontier.points) ? frontier.points.filter(p => Number.isFinite(p.coverage) && Number.isFinite(p.cost_per_q)) : [];
  const note = container.parentElement.querySelector('.chart-note');
  if (!points.length) {
    container.replaceChildren(el('p', 'pending-box', PENDING));
    if (note) note.textContent = 'The frontier appears when out/frontier.json exists.';
    return;
  }
  const W = 900, H = 470, L = 78, R = 30, T = 24, B = 70;
  const maxCost = Math.max(...points.map(p => p.cost_per_q)) * 1.12;
  const x = v => L + (v / maxCost) * (W - L - R);
  const y = v => H - B - v * (H - T - B);
  const ours = points.find(p => p.kind === 'second_opinion');
  const base = points.find(p => p.kind === 'telem_default');
  const n = base?.n_questions || ours?.n_questions || points[0].n_questions;
  const chart = svg('svg', {viewBox: `0 0 ${W} ${H}`, role: 'img', class: 'frontier-svg',
    'aria-label': `Answer found in results against search cost per question for ${points.length} setups on ${n || 'the'} held-out questions.`});
  for (let i = 0; i <= 4; i += 1) {
    const gy = y(i / 4);
    chart.append(svg('line', {x1: L, x2: W - R, y1: gy, y2: gy, class: 'grid'}), svg('text', {x: L - 12, y: gy + 4, 'text-anchor': 'end', class: 'tick-label'}, `${i * 25}%`));
    const value = (maxCost * i) / 4;
    chart.append(svg('text', {x: x(value), y: H - B + 24, 'text-anchor': 'middle', class: 'tick-label'}, `$${value.toFixed(3)}`));
  }
  chart.append(svg('line', {x1: L, x2: L, y1: T, y2: H - B, class: 'axis'}), svg('line', {x1: L, x2: W - R, y1: H - B, y2: H - B, class: 'axis'}));
  const combos = points.filter(p => p.kind === 'combo');
  const front = efficientFrontier(combos.map(p => ({...p, cost: p.cost_per_q, accuracy: p.coverage})));
  if (front.length > 1) chart.append(svg('polyline', {points: front.map(p => `${x(p.cost)},${y(p.accuracy)}`).join(' '), class: 'front-line'}));
  for (const p of combos) {
    const dot = svg('circle', {cx: x(p.cost_per_q), cy: y(p.coverage), r: 4, class: 'dot-combo'});
    dot.append(svg('title', {}, `${(p.engines || []).join(' + ')}: ${pct(p.coverage)} found, $${p.cost_per_q.toFixed(4)} per question`));
    chart.append(dot);
  }
  const label = (p, text, cls, dx, dy) => {
    const dot = svg('circle', {cx: x(p.cost_per_q), cy: y(p.coverage), r: cls === 'ours' ? 8 : 7, class: `dot-${cls}`});
    dot.append(svg('title', {}, `${text}: ${pct(p.coverage)} found, $${p.cost_per_q.toFixed(4)} per question`));
    chart.append(dot, svg('text', {x: x(p.cost_per_q) + dx, y: y(p.coverage) + dy, class: `point-label label-${cls}`, 'text-anchor': dx < 0 ? 'end' : 'start'}, text));
  };
  if (base) label(base, 'Telem default', 'base', -14, 26);
  if (ours) label(ours, ours.simulated ? 'Backstop (simulated)' : 'Backstop', 'ours', 14, -14);
  chart.append(svg('text', {x: (L + W - R) / 2, y: H - 14, 'text-anchor': 'middle', class: 'axis-label'}, 'search cost per question'),
    svg('text', {transform: `translate(20 ${(T + H - B) / 2}) rotate(-90)`, 'text-anchor': 'middle', class: 'axis-label'}, 'answer found in results'));
  container.replaceChildren(chart);
  if (note) {
    const parts = [`${combos.length} engine combinations`, n ? `${n} held-out questions` : null, frontier.non_trap_only ? 'traps excluded' : null].filter(Boolean);
    note.textContent = `${parts.join(', ')}.${!ours?.simulated && ours?.label === 'measured agent runs' ? ` Small dots: one search per engine combination. Labelled points: measured agent runs on ${ours.n_questions} questions, search and checker cost per question.` : ''}${ours?.simulated ? ` The Backstop point is simulated (${String(ours.label || 'not measured').replace(/^simulated,?\s*/i, '') || 'not measured'}) until the held-out evaluation runs.` : ''}`;
  }
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

function renderFindings(root, markdown) {
  const list = root.querySelector('.findings');
  const items = findingsFrom(markdown);
  list.replaceChildren(...(items.length ? items.map((text, i) => {
    const card = el('article', 'finding');
    card.append(el('p', 'label', `Finding ${i + 1}`), el('p', 'finding-text', text));
    return card;
  }) : [el('p', 'pending-box', PENDING)]));
}

export async function loadResults(root, {firstEngine = 'tavily'} = {}) {
  const [data, site] = await Promise.all([getJSON('/results'), getJSON('/site-data')]);
  const results = data?.results || null;
  const metrics = measured(results, validateResults(results));
  const status = root.querySelector('.results-status');
  const counts = metrics ? [metrics.single, metrics.baseline, metrics.ours].map(b => scored(b, metrics.n)) : [];
  status.textContent = metrics ? (counts.every(c => c === metrics.n) ? `${metrics.n} held-out questions per setup` : `${metrics.n} held-out questions; Backstop scored ${scored(metrics.ours, metrics.n)} (one run failed and is not counted)`) : 'Results pending: the held-out evaluation has not finished';
  status.classList.toggle('is-pending', !metrics);
  renderHeadline(root, metrics);
  // Prefer the raw file (it carries the simulated flag and question counts); /results drops those fields.
  const frontier = site?.frontier || data?.frontier || null;
  renderTable(root.querySelector('.results-table'), metrics, coverageBySetup(frontier, firstEngine));
  drawFrontier(root.querySelector('.frontier-chart'), frontier);
  renderFindings(root, site?.findings ?? null);
  return {metrics, rescueTable: site?.rescue_table || null};
}
