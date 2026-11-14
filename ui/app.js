/** Wiring for the product site: page dots, flow, results, the API example's base URL, copy buttons. */
import {startDots} from './dots.js';
import {loadResults} from './results.js';
import {drawFlow, flowLabels} from './flow.js';

const $ = selector => document.querySelector(selector);
const reduced = matchMedia('(prefers-reduced-motion: reduce)').matches;
const params = new URLSearchParams(location.search);
if (params.has('still')) document.documentElement.classList.add('still');

// One dot field behind the whole page; its accent dots belong to the hero.
const canvas = $('.page-dots');
if (canvas) startDots(canvas, {hero: $('.hero'), reducedMotion: reduced || params.has('still')});

// The HTTP example calls this deployment, wherever the page is served from.
function initApiBase() {
  if (!/^https?:$/.test(location.protocol)) return;
  document.querySelectorAll('.api-base').forEach(node => { node.textContent = location.origin; });
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
  let rescueTable = null;
  try { ({rescueTable} = await loadResults($('#results'))); } catch { /* results show their pending state */ }
  drawFlow($('#how'), flowLabels(rescueTable));
}

initApiBase();
initCopy();
initData();
