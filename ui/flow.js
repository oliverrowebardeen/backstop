/** Thin curved connectors for the "How it works" flow, labelled with measured counts where they exist. */
import {engineName} from './simulate.js';

const svg = (tag, attrs = {}, text) => {
  const node = document.createElementNS('http://www.w3.org/2000/svg', tag);
  for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
  if (text !== undefined) node.textContent = text;
  return node;
};

/** Connector labels from out/rescue_table.json (learn split). Missing data leaves plain labels. */
export function flowLabels(rescueTable, firstEngine = 'tavily') {
  const labels = {ask: 'goal on\nevery call', results: 'top 5\nresults', hit: 'Hit: stop searching', miss: 'on a\nmiss', rescued: 'rescued', failed: 'two rescues\nfail'};
  const t = rescueTable;
  const misses = t?.miss_counts?.[firstEngine];
  const n = t?.n_questions;
  if (Number.isInteger(misses) && Number.isInteger(n)) labels.miss = `${engineName(firstEngine)} missed\n${misses} of ${n}`;
  const next = t?.order_after?.[firstEngine]?.[0];
  const row = next ? t?.rescue?.[firstEngine]?.[next] : null;
  if (row && Number.isInteger(row.rescued) && Number.isInteger(row.misses)) labels.rescued = `${engineName(next)} rescued\n${row.rescued} of ${row.misses}`;
  labels.source = Number.isInteger(n) ? `Counts from the ${t.built_on_split || 'learn'} split, ${n} questions.` : '';
  return labels;
}

export function drawFlow(root, labels) {
  const flow = root.querySelector('.flow');
  const layer = root.querySelector('.flow-lines');
  const key = root.querySelector('#answer-key');
  if (!flow || !layer) return;
  const draw = () => {
    layer.replaceChildren();
    if (getComputedStyle(layer).display === 'none') return;
    const box = flow.getBoundingClientRect();
    const keyBox = key.getBoundingClientRect();
    const height = Math.max(box.height, keyBox.top - box.top);
    layer.setAttribute('viewBox', `0 0 ${box.width} ${height}`);
    layer.setAttribute('height', String(height));
    const r = id => {
      const rect = root.querySelector(`#${id}`).getBoundingClientRect();
      return {l: rect.left - box.left, r: rect.right - box.left, t: rect.top - box.top, b: rect.bottom - box.top,
        cx: rect.left - box.left + rect.width / 2, cy: rect.top - box.top + rect.height / 2};
    };
    const curve = (a, b, cls = 'conn') => {
      const dx = Math.max(30, (b.x - a.x) * 0.5);
      const path = svg('path', {d: `M${a.x} ${a.y} C${a.x + dx} ${a.y}, ${b.x - dx} ${b.y}, ${b.x} ${b.y}`, class: cls});
      layer.append(path);
      layer.append(svg('circle', {cx: b.x, cy: b.y, r: 2.6, class: 'conn-dot'}));
      return path;
    };
    const tag = (x, y, text, cls = '', below = false) => {
      if (!text) return;
      const lines = text.split('\n').length;
      const node = svg('text', {x, y: below ? y : y - (lines - 1) * 14, 'text-anchor': 'middle', class: `conn-label ${cls}`});
      text.split('\n').forEach((line, i) => node.append(svg('tspan', {x, dy: i ? '1.25em' : '0'}, line)));
      layer.append(node);
    };
    const ask = r('f-ask'), telem = r('f-telem'), check = r('f-check');
    const r1 = r('f-r1'), r2 = r('f-r2'), r3 = r('f-r3'), ok = r('f-ok'), nf = r('f-nf');
    curve({x: ask.r, y: ask.cy}, {x: telem.l, y: telem.cy});
    tag((ask.r + telem.l) / 2, ask.t - 4, labels.ask);
    curve({x: telem.r, y: telem.cy}, {x: check.l, y: check.cy});
    tag((telem.r + check.l) / 2, telem.t - 4, labels.results);
    for (const card of [r1, r2, r3]) curve({x: check.r, y: check.cy}, {x: card.l, y: card.cy});
    tag((check.r + r1.l) / 2, r1.t - 4, labels.miss, 'conn-count');
    // Hit skips the rescue column entirely.
    const lift = Math.min(check.t, r1.t, ok.t) - 92;
    const hx = check.r - 28;
    layer.append(svg('path', {d: `M${hx} ${check.t} C${hx} ${lift}, ${ok.cx} ${lift}, ${ok.cx} ${ok.t}`, class: 'conn conn-hit'}));
    layer.append(svg('circle', {cx: ok.cx, cy: ok.t, r: 3, class: 'conn-dot conn-dot-hit'}));
    tag((hx + ok.cx) / 2, lift + 18, labels.hit, 'conn-hit-label');
    for (const card of [r1, r2, r3]) curve({x: card.r, y: card.cy}, {x: ok.l, y: ok.cy}, 'conn conn-soft');
    tag((r2.r + ok.l) / 2, r2.t - 10, labels.rescued, 'conn-count');
    curve({x: r3.r, y: r3.cy}, {x: nf.l, y: nf.cy}, 'conn conn-dash');
    tag((r3.r + nf.l) / 2, Math.max(r3.b, nf.b) + 18, labels.failed, '', true);
    // The checker never sees the answer key: dotted lines rise from the line to the checker card.
    const keyTop = keyBox.top - box.top - 8;
    for (const offset of [-16, 16]) {
      layer.append(svg('path', {d: `M${keyBox.left - box.left + keyBox.width / 2 + offset * 2} ${keyTop} C${check.cx + offset} ${keyTop - 40}, ${check.cx + offset} ${check.b + 50}, ${check.cx + offset} ${check.b + 6}`, class: 'conn-dotted'}));
    }
  };
  new ResizeObserver(draw).observe(flow);
  window.addEventListener('load', draw);
  document.fonts?.ready.then(draw);
  draw();
  const note = root.querySelector('.flow-source');
  if (note) note.textContent = labels.source;
}
