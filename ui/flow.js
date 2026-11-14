/**
 * The "How it works" cascade, drawn the way so/second_opinion.py runs it.
 *
 * Connectors are thin SVG curves measured from the cards. The checker's verdict rows
 * branch into the Hit exit, the Shallow page read, the engine retry and the Not found
 * exit; page reads and retries loop back through the checker, at most 2 per search.
 * A dot travels the Hit path once when the section scrolls into view (not with
 * prefers-reduced-motion or ?still=1); focusing a rescue or exit card plays its path.
 */
import {engineName} from './labels.js';

/** so/config.py FALLBACK_ORDER default, used by so/policy.py when out/rescue_table.json is missing. */
export const FALLBACK_ORDER = ['exa', 'perplexity', 'brave', 'parallel', 'ceramic'];
/** First engines Backstop can start on: routed fact engine (out/first_engine.json) and FIRST_ENGINE (routing off). */
export const FIRST_ENGINES = ['tavily', 'perplexity'];

const svg = (tag, attrs = {}, text) => {
  const node = document.createElementNS('http://www.w3.org/2000/svg', tag);
  for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
  if (text !== undefined) node.textContent = text;
  return node;
};

/**
 * Engines tried in one search starting on `first`, as so/second_opinion.py _next_engine picks them:
 * the next untried engine in order_after[last tried], then fallback_order.
 */
export function rescueChain(table, first, rescues = 2) {
  const tried = [first];
  const fallback = Array.isArray(table?.fallback_order) && table.fallback_order.length ? table.fallback_order : FALLBACK_ORDER;
  for (let i = 0; i < rescues; i++) {
    const previous = tried[tried.length - 1];
    const next = [...(table?.order_after?.[previous] || []), ...fallback].find(engine => !tried.includes(engine));
    if (!next) break;
    tried.push(next);
  }
  return tried;
}

/** Engine calls per search tool call for one setup in out/results.json, or null if not recorded. */
export function enginesPerSearch(results, setup) {
  const block = results?.setups?.[setup];
  const calls = block?.engine_calls, searches = block?.search_calls;
  if (!Number.isFinite(calls) || !Number.isFinite(searches) || searches <= 0) return null;
  return {calls, searches, perSearch: calls / searches};
}

/** Connector labels and rescue order from out/rescue_table.json (learn split). Missing data leaves plain labels. */
export function flowLabels(rescueTable, firstEngine = 'tavily') {
  const labels = {ask: 'goal on\nevery call', results: 'top 5\nresults', hit: 'Hit: stop searching', miss: 'on a\nmiss', rescued: 'rescued', failed: 'still a miss after 2 rescues, or a limit reached'};
  const t = rescueTable;
  const misses = t?.miss_counts?.[firstEngine];
  const n = t?.n_questions;
  if (Number.isInteger(misses) && Number.isInteger(n)) labels.miss = `${engineName(firstEngine)} missed\n${misses} of ${n}`;
  const next = t?.order_after?.[firstEngine]?.[0];
  const row = next ? t?.rescue?.[firstEngine]?.[next] : null;
  if (row && Number.isInteger(row.rescued) && Number.isInteger(row.misses)) labels.rescued = `${engineName(next)} rescued\n${row.rescued} of ${row.misses}`;
  labels.chains = Object.fromEntries(FIRST_ENGINES.map(engine => [engine, rescueChain(t, engine)]));
  labels.orderSource = t?.order_after ? 'out/rescue_table.json' : 'fallback order in so/config.py';
  labels.source = Number.isInteger(n) ? `Rescue order and counts from the ${t.built_on_split || 'learn'} split, ${n} questions.` : '';
  return labels;
}

function renderChain(root, labels) {
  const list = root.querySelector('#retry-chain');
  const note = root.querySelector('#retry-note');
  if (!list || !labels.chains) return;
  const rescues = Object.entries(labels.chains).map(([first, chain]) => [first, chain.slice(1)]);
  const same = rescues.every(([, r]) => r.join() === rescues[0][1].join());
  const groups = same ? [[rescues.map(([first]) => first), rescues[0][1]]] : rescues.map(([first, r]) => [[first], r]);
  const items = [];
  groups.forEach(([firsts, engines], index) => {
    if (!same) items.push(Object.assign(document.createElement('li'), {className: 'chain-after', textContent: `after ${engineName(firsts[0])}:`}));
    engines.forEach((engine, i) => {
      if (i) items.push(Object.assign(document.createElement('li'), {className: 'chain-then', textContent: 'then'}));
      items.push(Object.assign(document.createElement('li'), {className: 'engine-tag', textContent: engineName(engine)}));
    });
    if (!same && index < groups.length - 1) items.push(Object.assign(document.createElement('li'), {className: 'chain-break'}));
  });
  if (items.length) list.replaceChildren(...items);
  const names = rescues.map(([first]) => engineName(first)).join(' or ');
  if (note) note.textContent = same ? `Each rescue follows the engine that just missed; shown after a ${names} first search (${labels.orderSource}).` : `Each rescue follows the engine that just missed (${labels.orderSource}).`;
}

async function fillCaptions(root) {
  let results = null;
  try {
    const response = await fetch('/results', {cache: 'no-store'});
    results = response.ok ? (await response.json())?.results : null;
  } catch { results = null; }
  const fmt = new Intl.NumberFormat('en-US');
  const set = (id, setup, text) => {
    const stats = enginesPerSearch(results, setup);
    const node = root.querySelector(id);
    if (stats && node) node.textContent = text(stats.perSearch.toFixed(2), fmt.format(stats.calls), fmt.format(stats.searches));
  };
  set('#default-caption', 'telem_default', (per, calls, searches) => `${per} engines per search on average: ${calls} engine calls over ${searches} searches in the Telem default agent runs (out/results.json).`);
  set('#ours-caption', 'second_opinion', (per, calls, searches) => `${per} engines per search: ${calls} engine calls over ${searches} searches in the Backstop agent runs of the same eval (out/results.json).`);
}

export function drawFlow(root, labels) {
  const flow = root.querySelector('.flow');
  const layer = root.querySelector('.flow-lines');
  const key = root.querySelector('#answer-key');
  if (!flow || !layer) return;
  renderChain(root, labels);
  fillCaptions(root);
  const still = () => document.documentElement.classList.contains('still') || matchMedia('(prefers-reduced-motion: reduce)').matches;
  let segments = {};

  const draw = () => {
    layer.replaceChildren();
    segments = {};
    if (getComputedStyle(layer).display === 'none') return;
    const box = flow.getBoundingClientRect();
    const r = id => {
      const rect = root.querySelector(`#${id}`).getBoundingClientRect();
      return {l: rect.left - box.left, r: rect.right - box.left, t: rect.top - box.top, b: rect.bottom - box.top,
        cx: rect.left - box.left + rect.width / 2, cy: rect.top - box.top + rect.height / 2};
    };
    const defs = svg('defs');
    const marker = svg('marker', {id: 'flow-arrow', viewBox: '0 0 8 8', refX: '7', refY: '4', markerWidth: '7', markerHeight: '7', orient: 'auto-start-reverse'});
    marker.append(svg('path', {d: 'M0 0.5 L7.5 4 L0 7.5 Z', class: 'conn-arrow'}));
    defs.append(marker);
    layer.append(defs);
    const path = (d, cls = 'conn', name, target, rows) => {
      const node = svg('path', {d, class: cls});
      layer.append(node);
      if (name) segments[name] = {node, target, rows};
      return node;
    };
    const curve = (a, b) => {
      const dx = Math.max(24, (b.x - a.x) * 0.5);
      return `M${a.x} ${a.y} C${a.x + dx} ${a.y}, ${b.x - dx} ${b.y}, ${b.x} ${b.y}`;
    };
    const back = (a, b) => {
      const dx = Math.max(24, (a.x - b.x) * 0.5);
      return `M${a.x} ${a.y} C${a.x - dx} ${a.y}, ${b.x + dx} ${b.y}, ${b.x} ${b.y}`;
    };
    const dot = (x, y, cls = 'conn-dot') => layer.append(svg('circle', {cx: x, cy: y, r: 2.6, class: cls}));
    const tag = (x, y, text, cls = '', below = false) => {
      if (!text) return;
      const lines = text.split('\n');
      const node = svg('text', {x, y: below ? y : y - (lines.length - 1) * 14, 'text-anchor': 'middle', class: `conn-label ${cls}`});
      lines.forEach((line, i) => node.append(svg('tspan', {x, dy: i ? '1.25em' : '0'}, line)));
      layer.append(node);
    };

    const ask = r('f-ask'), first = r('f-first'), check = r('f-check'), read = r('f-read'), retry = r('f-retry');
    const ok = r('f-ok'), nf = r('f-nf'), limits = r('g-limits'), open = r('g-open');
    const row = id => r(id).cy;
    const vHit = row('v-hit'), vShallow = row('v-shallow'), vStale = row('v-stale'), vWrong = row('v-wrong'), vDead = row('v-dead');

    // Ask, then one engine, then the checker.
    path(curve({x: ask.r, y: ask.cy}, {x: first.l, y: first.cy}), 'conn', 'ask', 'f-first');
    dot(first.l, first.cy);
    tag((ask.r + first.l) / 2, Math.min(ask.cy, first.cy) - 12, labels.ask);
    path(curve({x: first.r, y: first.cy}, {x: check.l, y: check.cy}), 'conn', 'first', 'f-check');
    dot(check.l, check.cy);
    tag((first.r + check.l) / 2, Math.min(first.cy, check.cy) - 12, labels.results);

    // Hit leaves from its verdict row, over the rescue column, straight to Verified.
    const labelTop = Math.min(...[...flow.querySelectorAll('.col-label')].map(n => n.getBoundingClientRect().top - box.top));
    const top = labelTop - 16, hx = check.r;
    path(`M${hx} ${vHit} C${hx + 44} ${vHit}, ${hx + 44} ${top}, ${hx + 88} ${top} L${ok.l - 80} ${top} C${ok.l - 36} ${top}, ${ok.l - 36} ${ok.cy}, ${ok.l} ${ok.cy}`,
      'conn conn-hit', 'hit', 'f-ok', ['v-hit']);
    dot(ok.l, ok.cy, 'conn-dot conn-dot-hit');
    tag((hx + ok.l) / 2, top - 9, labels.hit, 'conn-hit-label');

    // Shallow reads the page; the page goes back through the checker.
    path(curve({x: check.r, y: vShallow}, {x: read.l, y: read.cy - 8}), 'conn', 'shallow', 'f-read', ['v-shallow']);
    dot(read.l, read.cy - 8);
    const readBackY = vShallow + (vStale - vShallow) * 0.55;
    path(back({x: read.l, y: read.cy + 14}, {x: check.r + 2, y: readBackY}), 'conn conn-loop', 'readBack', 'f-check').setAttribute('marker-end', 'url(#flow-arrow)');

    // Stale, Wrong entity and Dead merge into one retry on the next engine, then loop back.
    const bx = check.r + 14;
    for (const y of [vStale, vWrong, vDead]) path(`M${check.r} ${y} L${bx} ${y}`, 'conn');
    path(`M${bx} ${vStale} L${bx} ${vDead}`, 'conn');
    path(`M${check.r} ${vWrong} L${bx} ${vWrong} ` + curve({x: bx, y: vWrong}, {x: retry.l, y: retry.cy - 8}).replace(/^M[^C]+/, ''), 'conn', 'retry', 'f-retry', ['v-stale', 'v-wrong', 'v-dead']);
    dot(retry.l, retry.cy - 8);
    const retryBackY = Math.min(check.b - 26, vDead + 26);
    const loop = path(back({x: retry.l, y: retry.cy + 16}, {x: check.r + 2, y: retryBackY}), 'conn conn-loop', 'retryBack', 'f-check');
    loop.setAttribute('marker-end', 'url(#flow-arrow)');

    // The "max 2" counter sits on the retry loop.
    const mid = loop.getPointAtLength(loop.getTotalLength() * 0.5);
    const badge = svg('g', {class: 'loop-badge', transform: `translate(${mid.x} ${mid.y})`});
    badge.append(svg('title', {}, 'Page reads and engine retries share a limit of 2 rescues per search.'));
    badge.append(svg('rect', {x: -25, y: -11, width: 50, height: 22, rx: 11}));
    badge.append(svg('text', {x: 0, y: 4, 'text-anchor': 'middle'}, 'max 2'));
    layer.append(badge);

    // Not found: a miss with both rescues used (or a limit reached) leaves the checker under the rescue column.
    const base = Math.max(limits.b, retry.b, nf.b + 24, check.b) + 26;
    const sy = check.b - 8;
    path(`M${check.r} ${sy} C${check.r + 34} ${sy}, ${check.r + 34} ${base}, ${check.r + 70} ${base} L${nf.cx - 22} ${base} Q${nf.cx} ${base} ${nf.cx} ${base - 22} L${nf.cx} ${nf.b + 4}`,
      'conn conn-dash', 'nf', 'f-nf').setAttribute('marker-end', 'url(#flow-arrow)');
    tag((check.r + 70 + nf.cx - 22) / 2, base + 18, labels.failed, '', true);

    // The checker never sees the answer key: dotted lines rise from the line to the checker column.
    const height = Math.max(box.height, base + 30, key ? key.getBoundingClientRect().top - box.top : 0);
    layer.setAttribute('viewBox', `0 0 ${box.width} ${height}`);
    layer.setAttribute('height', String(height));
    if (key) {
      const keyBox = key.getBoundingClientRect();
      const keyTop = keyBox.top - box.top - 8;
      const kx = keyBox.left - box.left + keyBox.width / 2;
      for (const offset of [-16, 16]) {
        path(`M${kx + offset * 2} ${keyTop} C${open.cx + offset} ${keyTop - 30}, ${open.cx + offset} ${open.b + 40}, ${open.cx + offset} ${open.b + 6}`, 'conn-dotted');
      }
    }
  };

  // A dot travels one route; cards light up as it reaches them.
  const ROUTES = {
    hit: ['ask', 'first', 'hit'],
    shallow: ['ask', 'first', 'shallow', 'readBack', 'hit'],
    retry: ['ask', 'first', 'retry', 'retryBack', 'hit'],
    notfound: ['ask', 'first', 'retry', 'retryBack', 'retry', 'retryBack', 'nf'],
  };
  const PX_PER_S = 520;
  let frame = 0;
  const clearLit = () => root.querySelectorAll('.is-lit').forEach(node => node.classList.remove('is-lit'));
  const stop = () => { cancelAnimationFrame(frame); clearLit(); layer.querySelector('.flow-dot')?.remove(); };
  const play = name => {
    if (still() || getComputedStyle(layer).display === 'none') return;
    stop();
    const route = ROUTES[name];
    const lengths = route.map(id => segments[id]?.node.getTotalLength() || 0);
    const total = lengths.reduce((a, b) => a + b, 0);
    if (!total) return;
    const started = performance.now();
    const duration = Math.max(1600, total / PX_PER_S * 1000);
    let lastIndex = -1;
    const tick = now => {
      const p = Math.min(1, (now - started) / duration);
      const eased = p < 0.5 ? 2 * p * p : 1 - (-2 * p + 2) ** 2 / 2;
      let along = eased * total, index = 0;
      while (index < route.length - 1 && along > lengths[index]) { along -= lengths[index]; index++; }
      const segment = segments[route[index]];
      if (!segment) return stop();
      if (index !== lastIndex) {
        clearLit();
        const from = index ? segments[route[index - 1]]?.target : 'f-ask';
        [from, ...(segment.rows || [])].forEach(id => id && root.querySelector(`#${id}`)?.classList.add('is-lit'));
        lastIndex = index;
      }
      let dotNode = layer.querySelector('.flow-dot');
      if (!dotNode) { dotNode = svg('circle', {r: 5, class: 'flow-dot'}); layer.append(dotNode); }
      const point = segment.node.getPointAtLength(Math.min(along, lengths[index]));
      dotNode.setAttribute('cx', point.x);
      dotNode.setAttribute('cy', point.y);
      if (p < 1) frame = requestAnimationFrame(tick);
      else {
        clearLit();
        root.querySelector(`#${segment.target}`)?.classList.add('is-lit');
        frame = requestAnimationFrame(() => setTimeout(() => { if (lastIndex === index) stop(); }, 1400));
      }
    };
    frame = requestAnimationFrame(tick);
  };

  new ResizeObserver(() => { stop(); draw(); }).observe(flow);
  window.addEventListener('load', draw);
  document.fonts?.ready.then(draw);
  draw();

  for (const [id, route] of [['f-ok', 'hit'], ['f-read', 'shallow'], ['f-retry', 'retry'], ['f-nf', 'notfound']]) {
    const card = root.querySelector(`#${id}`);
    card?.addEventListener('focus', () => play(route));
    card?.addEventListener('mouseenter', () => play(route));
  }
  if ('IntersectionObserver' in window) {
    const seen = new IntersectionObserver(entries => {
      if (entries.some(entry => entry.isIntersecting)) { seen.disconnect(); play('hit'); }
    }, {threshold: 0.45});
    seen.observe(flow);
  }

  const note = root.querySelector('.flow-source');
  if (note) note.textContent = labels.source;
}
