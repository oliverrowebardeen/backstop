/** DOM view and player for "Simulate a miss". All log text goes through textContent. */
import {ENGINES, VERDICT_LABEL, engineName, stateAt, ticks, formatSeconds} from './simulate.js';

const el = (tag, cls, text) => {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined && text !== null) node.textContent = text;
  return node;
};
const svg = (tag, attrs = {}) => {
  const node = document.createElementNS('http://www.w3.org/2000/svg', tag);
  for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
  return node;
};
const usd = value => `$${(value || 0).toFixed(4)}`;
const verdictClass = verdict => `verdict verdict-${String(verdict || '').toLowerCase().replace(/_/g, '-')}`;

export function safeURL(value) {
  try {
    const url = new URL(value);
    return ['https:', 'http:'].includes(url.protocol) ? url : null;
  } catch { return null; }
}

function sourceLink(url) {
  const safe = safeURL(url);
  if (!safe) return null;
  const path = decodeURIComponent(safe.pathname).split('/').filter(Boolean).pop() || '';
  const link = el('a', 'source-link', `${safe.hostname.replace(/^www\./, '')}${path ? ` / ${path.length > 38 ? `${path.slice(0, 36)}...` : path}` : ''}`);
  link.href = safe.href; link.target = '_blank'; link.rel = 'noopener noreferrer';
  return link;
}

export function verdictChip(verdict) {
  return el('span', verdictClass(verdict), VERDICT_LABEL[verdict] || verdict);
}

function stepRow(step, index) {
  const row = el('li', `trace-row trace-${step.kind}`);
  row.dataset.target = `step-${index}`;
  const head = el('div', 'trace-head');
  if (step.kind === 'search') {
    head.append(el('span', 'trace-kind', step.rescue ? 'Rescue search' : 'Search'));
    step.engines.forEach(engine => head.append(el('span', 'engine-tag', engineName(engine))));
    head.append(el('span', 'trace-meta', `via Telem${step.nResults ? `, ${step.nResults} results` : ''}`));
    row.append(head, el('p', 'trace-query mono', step.query));
  } else if (step.kind === 'check') {
    head.append(el('span', 'trace-kind', 'Checker'), verdictChip(step.verdict));
    if (Number.isFinite(step.confidence)) head.append(el('span', 'trace-meta', `confidence ${step.confidence.toFixed(2)}`));
    row.append(head, el('p', 'trace-why', step.why));
  } else if (step.kind === 'read') {
    head.append(el('span', 'trace-kind', 'Page read'),
      el('span', 'read-tag', step.via === 'tavily' ? 'read by Tavily Extract' : step.via === 'telem' ? 'read by Telem fetch' : 'reading page'));
    row.append(head);
    const link = sourceLink(step.url);
    if (link) row.append(link);
  } else if (step.kind === 'rewrite') {
    head.append(el('span', 'trace-kind', 'Rewrite'));
    if (step.engine) head.append(el('span', 'trace-meta', `next engine from the rescue table: ${engineName(step.engine)}`));
    row.append(head);
    const rewrite = el('p', 'rewrite mono');
    if (step.rewritten) {
      rewrite.append(el('del', '', step.from), el('span', 'rewrite-arrow', ' → '), el('ins', '', step.to));
    } else rewrite.append(el('span', '', step.to || ''));
    row.append(rewrite);
  }
  return row;
}

function trailStep(step, index) {
  const item = el('li', `trail-step trail-${step.kind}`);
  item.dataset.target = `step-${index}`;
  if (step.kind === 'search') step.engines.forEach(engine => item.append(el('span', 'engine-tag', engineName(engine))));
  else if (step.kind === 'check') item.append(verdictChip(step.verdict));
  else if (step.kind === 'read') item.append(el('span', 'read-tag', step.via === 'tavily' ? 'Tavily Extract read' : 'Page read'));
  else if (step.kind === 'rewrite') item.append(el('span', 'trail-rewrite', 'Rewrite'));
  return item;
}

function finalPanel(scenario) {
  const {final} = scenario;
  const panel = el('div', `card-final ${final.status === 'HIT' ? 'final-verified' : 'final-declined'}`);
  panel.dataset.target = 'final';
  const badge = el('div', 'final-badge');
  badge.append(el('span', 'final-icon', final.status === 'HIT' ? '✓' : '∅'), el('span', '', final.status === 'HIT' ? 'Verified answer' : 'Not found'));
  panel.append(badge);
  if (final.status === 'HIT' && final.evidence) {
    panel.append(el('p', 'final-label', final.evidence.fromEvidenceObject ? 'Evidence' : 'Evidence, in the checker’s words'));
    panel.append(el('blockquote', 'evidence', final.evidence.text));
    const meta = el('div', 'final-meta');
    const link = sourceLink(final.evidence.url);
    if (link) meta.append(link);
    const agreeing = final.evidence.enginesAgreeing;
    const engines = final.evidence.sourceEngines;
    meta.append(el('span', 'agree', agreeing !== null
      ? `${agreeing} engine${agreeing === 1 ? '' : 's'} agreeing`
      : `${engines.length || 1} engine${engines.length === 1 || !engines.length ? '' : 's'} agreeing on this source${engines.length ? ` (${engines.map(engineName).join(', ')})` : ''}`));
    meta.append(el('code', 'audit-chip', `audit seq ${final.seq} · ${scenario.shortId}`));
    panel.append(meta);
    if (final.answerLead) {
      const answer = el('p', 'answer-lead');
      answer.append(el('span', '', 'Agent’s final answer: '), document.createTextNode(final.answerLead));
      panel.append(answer);
    }
  } else {
    panel.append(el('p', 'declined-lead', 'Backstop declined instead of guessing.'));
    panel.append(el('p', 'declined-note', final.note));
    const meta = el('div', 'final-meta');
    meta.append(el('span', 'agree', `Tried ${final.triedEngines.map(engineName).join(', ')} and ${final.rescues} rescue${final.rescues === 1 ? '' : 's'}`));
    meta.append(el('code', 'audit-chip', `audit seq ${final.seq} · ${scenario.shortId}`));
    panel.append(meta);
  }
  return panel;
}

function ghostCard(scenario) {
  const ghost = scenario.ghost;
  const card = el('aside', 'ghost ghost-left');
  if (!ghost) { card.hidden = true; return card; }
  card.setAttribute('aria-label', 'What a plain single-engine agent did on the same question');
  card.append(el('p', 'ghost-kicker', 'Plain single-engine agent'));
  card.append(el('p', 'ghost-title', `${ghost.firstCall?.engines.map(engineName).join(', ') || 'One engine'} only. Results go to the agent unchecked.`));
  card.append(el('p', 'ghost-line', `Whole run: ${ghost.searches} searches, ${ghost.fetches} page reads, ${ghost.cost !== null ? usd(ghost.cost) : 'cost not recorded'}.`));
  if (ghost.answer) card.append(el('p', 'ghost-answer', `Answered: ${ghost.answer.final.split(/(?<=[.!?])\s/)[0]}`));
  const grade = ghost.grade === 'wrong' ? 'Graded wrong by the judge.' : ghost.grade === 'correct' ? 'Graded correct by the judge.' : 'Nothing checked its searches against the goal.';
  card.append(el('p', `ghost-grade${ghost.grade === 'wrong' ? ' ghost-wrong' : ''}`, grade));
  if (scenario.run?.cost !== null && scenario.run) {
    card.append(el('p', 'ghost-compare', `Backstop’s whole run on this question: ${scenario.run.searches} searches, ${usd(scenario.run.cost)}.`));
  }
  return card;
}

function baselineCard(scenario) {
  const base = scenario.baseline;
  const card = el('aside', 'ghost ghost-right');
  if (!base) { card.hidden = true; return card; }
  card.setAttribute('aria-label', 'What Telem default routing did on the same first search');
  card.append(el('p', 'ghost-kicker', 'Telem default'));
  card.append(el('p', 'ghost-title', `${base.engineCalls} engines, chosen before any results exist.`));
  const chips = el('div', 'ghost-chips');
  base.engines.forEach(engine => chips.append(el('span', 'engine-tag', engineName(engine))));
  card.append(chips);
  card.append(el('p', 'ghost-line', `${usd(base.cost)} for ${base.sameQuery ? 'the same query' : 'its first search'}. Status ${VERDICT_LABEL[base.status] || base.status}.`));
  return card;
}

export class SimPlayer {
  /** @param {HTMLElement} root the #simulate section */
  constructor(root, {reducedMotion = false} = {}) {
    this.root = root;
    this.reduced = reducedMotion;
    this.q = selector => root.querySelector(selector);
    this.stage = this.q('.sim-stage');
    this.card = this.q('.sim-card');
    this.track = this.q('.track');
    this.play = this.q('#sim-play');
    this.scenario = null; this.t = 0; this.playing = false; this.last = 0; this.frame = 0;
    this.renderedKey = '';
    this.play.addEventListener('click', () => this.toggle());
    this.track.addEventListener('keydown', event => this.onKey(event));
    this.track.addEventListener('pointerdown', event => this.onPointer(event));
    new ResizeObserver(() => this.layoutNotes()).observe(this.stage);
  }

  load(entry) {
    this.pause();
    this.entry = entry;
    this.scenario = entry.scenario;
    this.t = 0;
    this.renderedKey = '';
    this.q('.sim-stage .ghost-left')?.replaceWith(ghostCard(this.scenario));
    this.q('.sim-stage .ghost-right')?.replaceWith(baselineCard(this.scenario));
    this.buildCardShell();
    this.buildTicks();
    this.updateButton();
    this.render();
  }

  buildCardShell() {
    const s = this.scenario;
    this.card.replaceChildren();
    const head = el('header', 'card-head');
    head.append(el('span', 'card-kicker', 'Agent asks'), el('code', 'card-run', `${s.questionId} · search ${s.callIndex} of ${s.callCount}`));
    this.card.append(head, el('p', 'card-question', s.question));
    const goal = el('p', 'card-goal');
    goal.append(el('span', 'goal-label', 'Goal'), document.createTextNode(s.goal));
    this.card.append(goal);
    const engines = el('div', 'card-engines');
    engines.setAttribute('aria-label', 'Engines behind Telem');
    const used = new Set(s.steps.filter(x => x.kind === 'search').flatMap(x => x.engines));
    [...ENGINES, ...[...used].filter(e => !ENGINES.includes(e))].forEach(id => {
      const chip = el('span', 'engine-chip', engineName(id));
      chip.dataset.engine = id;
      engines.append(chip);
    });
    this.card.append(engines);
    this.trace = el('ol', 'card-trace');
    this.trace.setAttribute('aria-label', 'Steps so far');
    this.card.append(this.trace);
    this.finalSlot = el('div', 'final-slot');
    this.card.append(this.finalSlot);
  }

  buildTicks() {
    const s = this.scenario;
    const list = this.q('.ticks');
    list.replaceChildren();
    ticks(s).forEach((tick, i) => {
      const button = el('button', `tick tick-${tick.kind}${i % 2 ? ' tick-low' : ''}`);
      button.type = 'button';
      button.tabIndex = -1;
      button.style.left = `${(tick.at / s.duration) * 100}%`;
      button.append(el('span', 'tick-time', formatSeconds(tick.real)), el('span', 'tick-caption', tick.caption));
      button.setAttribute('aria-label', `${tick.caption} at ${formatSeconds(tick.real)}`);
      button.addEventListener('click', () => { this.pause(); this.seek(tick.at); });
      list.append(button);
    });
    this.track.setAttribute('aria-valuemax', String(Math.round(s.duration)));
  }

  toggle() {
    if (this.playing) { this.pause(); return; }
    if (this.t >= this.scenario.duration) this.t = 0;
    // Bring the card and the timeline into view together before playing.
    const top = this.stage.getBoundingClientRect().top;
    const bottom = this.q('.timeline').getBoundingClientRect().bottom;
    const lo = bottom - innerHeight + 12, hi = top - 76;
    const delta = lo <= hi ? (lo > 0 ? lo : hi < 0 ? hi : 0) : hi;
    if (Math.abs(delta) > 4) scrollTo({top: scrollY + delta, behavior: this.reduced ? 'auto' : 'smooth'});
    this.playing = true; this.last = performance.now();
    this.updateButton();
    this.frame = requestAnimationFrame(now => this.tick(now));
  }

  pause() {
    this.playing = false;
    cancelAnimationFrame(this.frame); this.frame = 0;
    if (this.scenario) this.updateButton();
  }

  tick(now) {
    if (!this.playing) return;
    const dt = Math.min(100, now - this.last); this.last = now;
    this.t = Math.min(this.scenario.duration, this.t + dt);
    this.render();
    if (this.t >= this.scenario.duration) { this.pause(); return; }
    this.frame = requestAnimationFrame(next => this.tick(next));
  }

  seek(t) {
    this.t = Math.max(0, Math.min(this.scenario.duration, t));
    this.render();
    this.updateButton();
  }

  onKey(event) {
    const marks = ticks(this.scenario).map(x => x.at);
    if (event.key === ' ' || event.key === 'Enter') { event.preventDefault(); this.toggle(); return; }
    let target = null;
    if (event.key === 'ArrowRight' || event.key === 'ArrowUp') target = marks.find(at => at > this.t + 1) ?? this.scenario.duration;
    if (event.key === 'ArrowLeft' || event.key === 'ArrowDown') target = [...marks].reverse().find(at => at < this.t - 1) ?? 0;
    if (event.key === 'Home') target = 0;
    if (event.key === 'End') target = this.scenario.duration;
    if (target !== null) { event.preventDefault(); this.pause(); this.seek(target); }
  }

  onPointer(event) {
    if (event.target.closest('.tick')) return;
    event.preventDefault();
    this.pause();
    this.track.focus();
    const rail = this.q('.rail').getBoundingClientRect();
    const move = e => this.seek(((e.clientX - rail.left) / rail.width) * this.scenario.duration);
    move(event);
    this.track.setPointerCapture(event.pointerId);
    const up = () => { this.track.removeEventListener('pointermove', move); this.track.removeEventListener('pointerup', up); };
    this.track.addEventListener('pointermove', move);
    this.track.addEventListener('pointerup', up);
  }

  updateButton() {
    const finished = this.t >= this.scenario.duration;
    const label = this.playing ? 'Pause' : finished ? 'Replay' : this.t > 0 ? 'Resume' : 'Simulate a miss';
    this.play.querySelector('.play-label').textContent = label;
    this.play.querySelector('.play-icon').textContent = this.playing ? '❚❚' : finished ? '↻' : '▶';
    this.play.setAttribute('aria-pressed', String(this.playing));
    this.root.classList.toggle('is-playing', this.playing);
  }

  render() {
    const s = this.scenario;
    const state = stateAt(s, this.t);
    // Progress and scrubber.
    this.q('.rail-fill').style.width = `${state.progress * 100}%`;
    this.q('.thumb').style.left = `${state.progress * 100}%`;
    this.track.setAttribute('aria-valuenow', String(Math.round(state.t)));
    this.track.setAttribute('aria-valuetext', `${formatSeconds(state.counters.realMs)} of ${formatSeconds(s.totals.realMs)}, ${state.current?.caption || ''}`);
    this.root.querySelectorAll('.tick').forEach((tick, i) => tick.classList.toggle('is-past', s.steps[i].at <= state.t));
    // Counters update every frame.
    this.q('[data-counter="engines"]').textContent = String(state.counters.engineCalls);
    this.q('[data-counter="reads"]').textContent = String(state.counters.fetches);
    this.q('[data-counter="cost"]').textContent = usd(state.counters.cost);
    this.q('[data-counter="time"]').textContent = formatSeconds(state.counters.realMs);
    // Card body only when the visible step set changes.
    const key = `${state.steps.length}:${state.done}`;
    if (key === this.renderedKey) return;
    this.renderedKey = key;
    this.card.querySelectorAll('.engine-chip').forEach(chip => {
      const id = chip.dataset.engine;
      chip.classList.toggle('is-lit', state.litEngines.has(id));
      chip.classList.toggle('is-active', state.activeEngine === id);
    });
    const shown = state.steps.map((step, index) => [step, index]).filter(([step]) => !['ask', 'done'].includes(step.kind));
    // Once resolved, the path compacts to one trail of chips so the answer and every annotation fit.
    const rows = state.done ? shown.map(([step, index]) => trailStep(step, index)) : shown.map(([step, index]) => stepRow(step, index));
    if (rows.length && !state.done) rows.at(-1).classList.add('is-new');
    this.trace.classList.toggle('is-trail', state.done);
    this.trace.setAttribute('aria-label', state.done ? 'Path this search took' : 'Steps so far');
    this.trace.replaceChildren(...rows);
    this.finalSlot.replaceChildren(...(state.done ? [finalPanel(s)] : []));
    this.trace.scrollTop = this.trace.scrollHeight;
    this.card.classList.toggle('is-done', state.done);
    this.card.classList.toggle('is-declined', state.done && s.final.status !== 'HIT');
    this.trace.scrollTop = this.trace.scrollHeight;
    this.q('.sim-status').textContent = state.done
      ? (s.final.status === 'HIT' ? 'Verified answer returned to the agent.' : 'Not found returned to the agent. It does not guess.')
      : state.current?.kind === 'ask' ? 'The agent calls search(query, goal).' : `${state.current?.caption || ''}`;
    this.renderNotes(state);
  }

  renderNotes(state) {
    const layer = this.q('.notes');
    const list = this.q('.notes-list');
    const shown = state.annotations;
    layer.replaceChildren(...shown.map(note => {
      const box = el('p', `note note-${note.side}${note.kind === 'cost' ? ' note-cost' : ''}`, note.text);
      box.dataset.target = note.target;
      return box;
    }));
    list.replaceChildren(...shown.map(note => el('li', note.kind === 'cost' ? 'note-cost' : '', note.text)));
    this.layoutNotes();
  }

  /** Place annotations beside the card and draw thin leader lines to their targets. */
  layoutNotes() {
    const leaders = this.q('.leaders');
    if (!leaders || !this.scenario) return;
    leaders.replaceChildren();
    const stage = this.stage.getBoundingClientRect();
    if (!stage.width || getComputedStyle(this.q('.notes')).display === 'none') return;
    leaders.setAttribute('viewBox', `0 0 ${stage.width} ${stage.height}`);
    const card = this.card.getBoundingClientRect();
    const used = {left: [], right: []};
    // Newest first: when space runs out, older annotations give way instead of overlapping.
    for (const box of [...this.q('.notes').children].reverse()) {
      const target = box.dataset.target === 'counters' ? this.q('[data-counter="cost"]')?.parentElement : this.root.querySelector(`[data-target="${box.dataset.target}"]`);
      if (!target) { box.hidden = true; continue; }
      const rect = target.getBoundingClientRect();
      // A trace row scrolled out of the card has nothing visible to point at.
      if (this.trace.contains(target)) {
        const view = this.trace.getBoundingClientRect();
        if (rect.bottom < view.top + 8 || rect.top > view.bottom - 8) { box.hidden = true; continue; }
      }
      box.hidden = false;
      const side = box.classList.contains('note-left') ? 'left' : 'right';
      const ghost = this.q(`.sim-stage .ghost-${side}`);
      const ghostRect = ghost && !ghost.hidden ? ghost.getBoundingClientRect() : null;
      const width = Math.min(250, side === 'left' ? card.left - stage.left - 70 : stage.right - card.right - 70);
      if (width < 150) { box.hidden = true; continue; }
      box.style.width = `${width}px`;
      const height = box.offsetHeight;
      const want = Math.min(rect.top + rect.height / 2, stage.bottom - 10) - stage.top - height / 2;
      // Blocked bands: other notes on this side, and the ghost card behind it.
      const blocked = used[side].map(([a, b]) => [a - 14, b + 14]);
      if (ghostRect) blocked.push([ghostRect.top - stage.top - 18, ghostRect.bottom - stage.top + 18]);
      const fits = y => y >= 0 && y + height <= stage.height && blocked.every(([a, b]) => y + height <= a || y >= b);
      const candidates = [want, ...blocked.flatMap(([a, b]) => [a - height, b])].filter(fits);
      if (!candidates.length) { box.hidden = true; continue; }
      const y = candidates.reduce((best, c) => (Math.abs(c - want) < Math.abs(best - want) ? c : best));
      used[side].push([y, y + height]);
      const x = side === 'left' ? card.left - stage.left - 48 - width : card.right - stage.left + 48;
      box.style.left = `${x}px`; box.style.top = `${y}px`;
      const targetY = Math.min(Math.max(rect.top + rect.height / 2, card.top + 12), Math.max(card.bottom, rect.bottom) - 6) - stage.top;
      const anchorY = y + Math.min(18, height / 2);
      const fromX = side === 'left' ? x + width + 8 : x - 8;
      // Leaders stop at the card's edge, level with their target, so they never cross card content.
      const insideCard = this.card.contains(target);
      const toX = side === 'left'
        ? (insideCard ? card.left : Math.max(rect.left, card.left)) - stage.left - 4
        : (insideCard ? card.right : Math.min(rect.right, card.right)) - stage.left + 4;
      const midX = side === 'left' ? fromX + 18 : fromX - 18;
      leaders.append(svg('polyline', {points: `${fromX},${anchorY} ${midX},${anchorY} ${toX},${targetY}`, class: 'leader'}),
        svg('circle', {cx: toX, cy: targetY, r: 3, class: 'leader-dot'}));
    }
  }
}
