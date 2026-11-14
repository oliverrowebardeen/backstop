/** Wiring for the product site: hero dots, flow, results, autopsy, copy buttons. */
import {startDots} from './dots.js';
import {VERDICT_LABEL, engineName, verdictChip} from './labels.js';
import {loadResults} from './results.js';
import {drawFlow, flowLabels} from './flow.js';

const $ = selector => document.querySelector(selector);
const el = (tag, cls, text) => {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined && text !== null) node.textContent = text;
  return node;
};
const reduced = matchMedia('(prefers-reduced-motion: reduce)').matches;
const params = new URLSearchParams(location.search);
if (params.has('still')) document.documentElement.classList.add('still');
async function getJSON(path) {
  try { const r = await fetch(path, {cache: 'no-store'}); return r.ok ? await r.json() : null; } catch { return null; }
}

// Hero.
const canvas = $('.hero .dots');
if (canvas) startDots(canvas, {reducedMotion: reduced});

// Miss autopsy: GET /v1/autopsy/{session_id} with the caller's key.
function initAutopsy() {
  const form = $('#autopsy-form');
  if (!form) return;
  const message = $('.autopsy-message');
  const list = $('.autopsy-steps');
  form.addEventListener('submit', async event => {
    event.preventDefault();
    const key = $('#autopsy-key').value.trim();
    const session = $('#autopsy-session').value.trim();
    if (!key || !session) return;
    message.textContent = 'Replaying the session through the checker.';
    list.replaceChildren();
    const button = form.querySelector('button');
    button.disabled = true;
    try {
      const response = await fetch(`/v1/autopsy/${encodeURIComponent(session)}`, {headers: {Authorization: `Bearer ${key}`}, cache: 'no-store'});
      const data = await response.json().catch(() => null);
      if (!response.ok) {
        const detail = data?.detail;
        throw new Error(typeof detail === 'string' ? detail : detail?.message || `The autopsy failed with HTTP ${response.status}.`);
      }
      renderAutopsy(data, message, list);
    } catch (error) {
      message.textContent = error.message || 'The autopsy could not run.';
    } finally { button.disabled = false; }
  });
}

function renderAutopsy(data, message, list) {
  const steps = Array.isArray(data?.steps) ? data.steps : [];
  message.textContent = `${data.n_searches} searches in session ${String(data.session_id).slice(0, 8)}. Goal (${data.goal_source === 'recorded' ? 'recorded' : 'from the first query'}): ${data.goal}`;
  list.replaceChildren(...steps.map(step => {
    const item = el('li', 'autopsy-step');
    const head = el('div', 'autopsy-head');
    head.append(el('span', 'step-index mono', String(step.index + 1).padStart(2, '0')));
    (step.providers || []).forEach(p => head.append(el('span', 'engine-tag', engineName(p))));
    if (step.checker_verdict) head.append(verdictChip(step.checker_verdict));
    else head.append(el('span', 'verdict', step.checker_error ? 'Check failed' : 'Unchecked'));
    item.append(head, el('p', 'autopsy-query mono', step.query));
    if (step.checker_why) item.append(el('p', 'autopsy-why', step.checker_why));
    if (step.index === data.first_failure_index) {
      item.classList.add('is-first-miss');
      const next = data.next_rescue;
      const rescue = next ? ` Next: ${next.action === 'FETCH_PAGE' ? 'read the page' : `retry on ${engineName(next.engine)}`}${next.query ? ` with "${next.query}"` : ''}.` : '';
      item.append(el('p', 'autopsy-marker', `Backstop would have stopped here. It catches this ${VERDICT_LABEL[step.checker_verdict] || 'miss'} before the agent sees it.${rescue}`));
    }
    if (step.index === data.stop_index && data.searches_after_stop > 0) {
      item.append(el('p', 'autopsy-marker autopsy-marker-hit', `First hit. ${data.searches_after_stop} later search${data.searches_after_stop === 1 ? ' was' : 'es were'} not needed.`));
    }
    return item;
  }));
  if (!steps.length) message.textContent = 'This session has no recorded searches.';
}

// Copy buttons copy the code as it should be pasted (removed diff lines left out).
function initCopy() {
  document.querySelectorAll('.copy').forEach(button => {
    button.addEventListener('click', async () => {
      const pre = document.getElementById(button.dataset.copy);
      const clone = pre.cloneNode(true);
      clone.querySelectorAll('.del').forEach(line => { line.nextSibling?.nodeType === 3 && (line.nextSibling.textContent = line.nextSibling.textContent.replace(/^\n/, '')); line.remove(); });
      const text = clone.textContent.replace(/\n{3,}/g, '\n\n').trim() + '\n';
      try {
        await navigator.clipboard.writeText(text);
        button.textContent = 'Copied';
      } catch {
        const range = document.createRange(); range.selectNodeContents(pre);
        const selection = getSelection(); selection.removeAllRanges(); selection.addRange(range);
        button.textContent = 'Selected';
      }
      setTimeout(() => { button.textContent = 'Copy'; }, 1600);
    });
  });
}

async function initData() {
  const config = await getJSON('/config');
  const firstEngine = config?.first_engine || 'tavily';
  let rescueTable = null;
  try { ({rescueTable} = await loadResults($('#results'), {firstEngine})); } catch { /* results show their pending state */ }
  drawFlow($('#how'), flowLabels(rescueTable, 'tavily'));
}

initAutopsy();
initCopy();
initData();
