/**
 * The "How it works" flow: agent asks, Telem searches, the checker decides, a miss is rescued,
 * the agent gets a verified answer or Not found. Plain connectors measured from the cards,
 * no labels or numbers on the lines. The rescue order chips come from out/rescue_table.json.
 */
import {engineName} from './labels.js';

/** so/config.py FALLBACK_ORDER default, used by so/policy.py when out/rescue_table.json is missing. */
export const FALLBACK_ORDER = ['exa', 'perplexity', 'brave', 'parallel', 'ceramic'];
/** First engines Backstop can start on: routed fact engine (out/first_engine.json) and FIRST_ENGINE (routing off). */
export const FIRST_ENGINES = ['tavily', 'perplexity'];

const svg = (tag, attrs = {}) => {
  const node = document.createElementNS('http://www.w3.org/2000/svg', tag);
  for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
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

/** Rescue order per first engine, from out/rescue_table.json (learn split) or the configured fallback order. */
export function flowLabels(rescueTable) {
  return {chains: Object.fromEntries(FIRST_ENGINES.map(engine => [engine, rescueChain(rescueTable, engine)]))};
}

function renderChain(root, labels) {
  const list = root.querySelector('#retry-chain');
  if (!list || !labels?.chains) return;
  const rescues = Object.entries(labels.chains).map(([first, chain]) => [first, chain.slice(1)]);
  const same = rescues.every(([, r]) => r.join() === rescues[0][1].join());
  const groups = same ? [[rescues.map(([first]) => first), rescues[0][1]]] : rescues.map(([first, r]) => [[first], r]);
  const item = (className, textContent = '') => Object.assign(document.createElement('li'), {className, textContent});
  const items = [];
  groups.forEach(([firsts, engines], index) => {
    if (!same) items.push(item('chain-after', `after ${engineName(firsts[0])}:`));
    engines.forEach((engine, i) => {
      if (i) items.push(item('chain-then', 'then'));
      items.push(item('engine-tag', engineName(engine)));
    });
    if (!same && index < groups.length - 1) items.push(item('chain-break'));
  });
  if (items.length) list.replaceChildren(...items);
}

export function drawFlow(root, labels) {
  const flow = root.querySelector('.flow');
  const layer = root.querySelector('.flow-lines');
  if (!flow || !layer) return;
  renderChain(root, labels);

  const draw = () => {
    layer.replaceChildren();
    if (getComputedStyle(layer).display === 'none') return;
    const box = flow.getBoundingClientRect();
    const r = id => {
      const rect = root.querySelector(`#${id}`).getBoundingClientRect();
      return {l: rect.left - box.left, r: rect.right - box.left, t: rect.top - box.top, b: rect.bottom - box.top,
        cy: rect.top - box.top + rect.height / 2};
    };
    const defs = svg('defs');
    for (const [id, cls] of [['flow-arrow', 'conn-arrow'], ['flow-arrow-hit', 'conn-arrow conn-arrow-hit']]) {
      const marker = svg('marker', {id, viewBox: '0 0 8 8', refX: '7', refY: '4', markerWidth: '7', markerHeight: '7', orient: 'auto-start-reverse'});
      marker.append(svg('path', {d: 'M0 0.5 L7.5 4 L0 7.5 Z', class: cls}));
      defs.append(marker);
    }
    layer.append(defs);
    const line = (d, cls = 'conn', arrow = 'flow-arrow') => layer.append(svg('path', {d, class: cls, 'marker-end': `url(#${arrow})`}));
    const curve = (a, b) => {
      const dx = Math.max(20, Math.abs(b.x - a.x) * 0.5) * Math.sign(b.x - a.x || 1);
      return `M${a.x} ${a.y} C${a.x + dx} ${a.y}, ${b.x - dx} ${b.y}, ${b.x} ${b.y}`;
    };

    const ask = r('f-ask'), first = r('f-first'), check = r('f-check'), rescue = r('f-rescue'), ok = r('f-ok'), nf = r('f-nf');
    // The main line runs straight through the first three cards.
    const row = Math.max(ask.t, first.t, check.t) + Math.min(56, (ask.b - ask.t) / 2);
    line(`M${ask.r} ${row} L${first.l - 2} ${row}`);
    line(`M${first.r} ${row} L${check.l - 2} ${row}`);
    // Hit: above the column labels, straight to Verified.
    const labels = [...flow.querySelectorAll('.col-label')].map(n => n.getBoundingClientRect().top - box.top);
    const top = Math.min(...labels, rescue.t) - 22, hy = check.t + 28;
    line(`M${check.r} ${hy} C${check.r + 30} ${hy}, ${check.r + 20} ${top}, ${check.r + 60} ${top} L${ok.l - 60} ${top} C${ok.l - 20} ${top}, ${ok.l - 30} ${ok.cy}, ${ok.l - 2} ${ok.cy}`, 'conn conn-hit', 'flow-arrow-hit');
    // A miss goes to rescue, and the rescued search goes back through the checker.
    line(curve({x: check.r, y: check.cy - 10}, {x: rescue.l - 2, y: rescue.cy - 10}));
    line(curve({x: rescue.l, y: rescue.cy + 14}, {x: check.r + 2, y: check.cy + 14}));
    // Still a miss: Not found.
    line(curve({x: rescue.r, y: Math.max(rescue.cy, nf.t + 20)}, {x: nf.l - 2, y: nf.cy}));
    const height = Math.max(box.height, rescue.b, nf.b) + 4;
    layer.setAttribute('viewBox', `0 0 ${box.width} ${height}`);
    layer.setAttribute('height', String(height));
  };

  new ResizeObserver(draw).observe(flow);
  window.addEventListener('load', draw);
  document.fonts?.ready.then(draw);
  draw();
}
