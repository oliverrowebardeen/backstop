/**
 * Timeline engine for "Simulate a miss".
 * Turns any section 6.11 event log into display steps with real timings, and
 * answers "what does the card show at time t" as a pure function, so playback,
 * scrubbing backward and scrubbing forward all agree. No DOM access here.
 */

export const VERDICT_LABEL = {
  HIT: 'Hit', STALE: 'Stale', WRONG_ENTITY: 'Wrong entity', SHALLOW: 'Shallow',
  DEAD: 'Dead', NOT_FOUND: 'Not found', UNCHECKED: 'Unchecked',
};
export const ENGINES = ['tavily', 'exa', 'brave', 'perplexity', 'parallel', 'ceramic'];
export const ENGINE_LABEL = {
  tavily: 'Tavily', exa: 'Exa', brave: 'Brave', perplexity: 'Perplexity', parallel: 'Parallel',
  ceramic: 'Ceramic', linkup: 'Linkup', you: 'You.com', seltz: 'Seltz', serpapi: 'SerpApi', tinyfish: 'TinyFish',
};
export const engineName = id => ENGINE_LABEL[id] || String(id);

/** Display pacing. Real elapsed times are kept for every label; these only stop
 * events that were logged in the same millisecond from drawing on top of each other. */
export const PACING = {lead: 900, gap: 650, hold: 1600};

const STEP_EVENTS = new Set(['search', 'check', 'rescue']);

/** @param {string} text JSONL */
export function parseLog(text) {
  const events = [];
  for (const line of String(text).split('\n')) {
    if (!line.trim()) continue;
    let event;
    try { event = JSON.parse(line); } catch { continue; }
    if (event && typeof event.type === 'string' && event.data && typeof event.data === 'object'
        && Number.isFinite(Date.parse(event.ts))) events.push(event);
  }
  return events.sort((a, b) => (a.seq ?? 0) - (b.seq ?? 0));
}

/** Group a run into search tool calls: from a `search` event to its `search_done`.
 * Agent page reads and agent model turns outside a call are not part of it. */
export function searchCalls(events) {
  const calls = [];
  let current = null;
  for (const event of events) {
    if (event.type === 'search' && !current) current = {events: [], done: null};
    if (!current) continue;
    if (event.type === 'search_done') { current.done = event; calls.push(current); current = null; continue; }
    if (event.type === 'llm' && event.data.role !== 'checker') continue;
    if (['search', 'check', 'rescue', 'fetch', 'llm'].includes(event.type)) current.events.push(event);
  }
  return calls;
}

export function pickCall(calls, preferred) {
  if (Number.isInteger(preferred) && preferred >= 1 && preferred <= calls.length) return calls[preferred - 1];
  return calls.find(call => call.events.some(event => event.type === 'rescue')) || calls[0] || null;
}

const ms = event => Date.parse(event.ts);
const num = value => (Number.isFinite(value) ? value : 0);
const eventCost = event => (event.type === 'search' || event.type === 'fetch' || (event.type === 'llm' && event.data.role === 'checker'))
  ? num(event.data.cost_usd) : 0;

export function shortRunId(runId) {
  const tail = String(runId || '').split('-').pop() || '';
  return /^[0-9a-f]{8,}$/i.test(tail) ? tail.slice(0, 8) : String(runId || '').slice(0, 8);
}

export function firstSentence(text) {
  const value = String(text || '').trim();
  const match = value.match(/^.*?[.!?](?=\s|$)/s);
  return (match ? match[0] : value).trim();
}

function runSummary(events) {
  if (!events?.length) return null;
  const start = events.find(e => e.type === 'run_start');
  const end = [...events].reverse().find(e => e.type === 'run_end');
  const answer = [...events].reverse().find(e => e.type === 'answer');
  const searches = events.filter(e => e.type === 'search');
  const calls = searchCalls(events);
  return {
    runId: events[0].run_id, setup: events[0].setup, question: start?.data.question || '',
    searches: end?.data.searches ?? calls.length, engineCalls: end?.data.engine_calls ?? searches.reduce((n, e) => n + e.data.engines.length, 0),
    fetches: end?.data.fetches ?? events.filter(e => e.type === 'fetch').length,
    cost: end?.data.cost_usd_total ?? null, latencyMs: end?.data.latency_ms ?? null,
    answer: answer?.data || null, first: calls[0] || null,
  };
}

function callTotals(call) {
  if (!call) return null;
  const searches = call.events.filter(e => e.type === 'search');
  return {
    query: searches[0]?.data.query || '',
    engines: searches.flatMap(e => e.data.engines || []),
    engineCalls: searches.reduce((n, e) => n + (e.data.engines?.length || 0), 0),
    cost: call.done ? num(call.done.data.cost_usd) : call.events.reduce((s, e) => s + eventCost(e), 0),
    status: call.done?.data.status || null,
  };
}

/**
 * Build a playable scenario from a log.
 * @param {object[]} events parsed 6.11 events
 * @param {{call?: number, ghost?: object[]|null, baseline?: object[]|null, ghostGrade?: string|null}} options
 */
export function buildScenario(events, options = {}) {
  const calls = searchCalls(events);
  const call = pickCall(calls, options.call);
  if (!call) throw new Error('The log has no search call to replay.');
  const callIndex = calls.indexOf(call) + 1;
  const runStart = events.find(e => e.type === 'run_start');
  const firstSearch = call.events.find(e => e.type === 'search');
  const t0 = ms(firstSearch);
  const steps = [{kind: 'ask', at: 0, real: 0, caption: 'Question', question: runStart?.data.question || '', goal: firstSearch.data.goal || ''}];
  const timed = [];
  let previous = 0;
  let searchCount = 0;
  const place = (event, makesStep) => {
    const real = Math.max(0, ms(event) - t0);
    const at = makesStep ? Math.max(real + PACING.lead, previous + PACING.gap) : Math.max(real + PACING.lead, previous);
    if (makesStep) previous = at;
    return {at, real};
  };
  call.events.forEach((event, index) => {
    const {at, real} = place(event, STEP_EVENTS.has(event.type));
    timed.push({event, at, real});
    const d = event.data;
    if (event.type === 'search') {
      searchCount += 1;
      const engines = d.engines || [];
      steps.push({kind: 'search', at, real, seq: event.seq, rescue: searchCount > 1, engines, query: d.query || '',
        nResults: d.n_results ?? null, latencyMs: d.latency_ms ?? null, cached: Boolean(d.cached),
        caption: engines.map(engineName).join(' + ') || 'Search'});
    } else if (event.type === 'check') {
      steps.push({kind: 'check', at, real, seq: event.seq, verdict: d.verdict, why: d.why || '', confidence: d.confidence ?? null,
        bestUrl: d.best_url || null, caption: VERDICT_LABEL[d.verdict] || d.verdict});
    } else if (event.type === 'rescue') {
      if (d.action === 'FETCH_PAGE') {
        const fetch = call.events.slice(index + 1).find(e => e.type === 'fetch');
        steps.push({kind: 'read', at, real, seq: event.seq, url: d.url || fetch?.data.url || null,
          via: fetch?.data.via || null, ok: fetch ? fetch.data.ok !== false : null, reason: d.reason || '',
          caption: 'Read page'});
      } else {
        const from = d.original_query || null;
        const to = d.rewritten_query || d.query || null;
        steps.push({kind: 'rewrite', at, real, seq: event.seq, engine: d.engine || null, from, to,
          rewritten: Boolean(from && to && from !== to), reason: d.reason || '',
          caption: d.engine ? `Rescue on ${engineName(d.engine)}` : 'Rescue'});
      }
    }
  });
  const done = call.done;
  const doneReal = done ? Math.max(0, ms(done) - t0) : (timed.at(-1)?.real ?? 0);
  const doneAt = Math.max(doneReal + PACING.lead, previous + PACING.gap);
  const status = done?.data.status || 'UNCHECKED';
  const lastCheck = [...steps].reverse().find(s => s.kind === 'check');
  const ranking = Array.isArray(done?.data.ranking) ? done.data.ranking : [];
  const evidenceObject = done?.data.evidence && typeof done.data.evidence === 'object' ? done.data.evidence : null;
  const bestUrl = evidenceObject?.url || lastCheck?.bestUrl || null;
  const sourceEngines = ranking.find(r => r.url === bestUrl)?.engines || [];
  const answerEvent = events.find(e => e.type === 'answer' && e.seq > (done?.seq ?? 0));
  const final = {
    status, note: done?.data.note || '', seq: done?.seq ?? null,
    evidence: status === 'HIT' ? {
      text: evidenceObject?.snippet || lastCheck?.why || '',
      fromEvidenceObject: Boolean(evidenceObject?.snippet),
      url: bestUrl,
      enginesAgreeing: Number.isInteger(evidenceObject?.engines_agreeing) ? evidenceObject.engines_agreeing : null,
      sourceEngines,
    } : null,
    triedEngines: [...new Set(steps.filter(s => s.kind === 'search').flatMap(s => s.engines))],
    rescues: steps.filter(s => s.kind === 'read' || s.kind === 'rewrite').length,
    answerLead: status === 'HIT' && answerEvent && answerEvent.data.status !== 'NOT_FOUND'
      ? firstSentence(answerEvent.data.final) : null,
    laterSearches: calls.length - callIndex,
  };
  steps.push({kind: 'done', at: doneAt, real: doneReal, seq: final.seq, status, caption: status === 'HIT' ? 'Verified' : VERDICT_LABEL[status] || status});
  const totals = {
    engineCalls: timed.filter(t => t.event.type === 'search').reduce((n, t) => n + (t.event.data.engines?.length || 0), 0),
    fetches: timed.filter(t => t.event.type === 'fetch').length,
    cost: done ? num(done.data.cost_usd) : timed.reduce((s, t) => s + eventCost(t.event), 0),
    realMs: doneReal,
  };
  const baseline = callTotals(pickCall(searchCalls(options.baseline || []), 1));
  if (baseline) baseline.sameQuery = baseline.query === firstSearch.data.query;
  const ghostRun = runSummary(options.ghost);
  const ghost = ghostRun ? {...ghostRun, firstCall: callTotals(ghostRun.first), grade: options.ghostGrade || null,
    sameFirstQuery: ghostRun.first ? callTotals(ghostRun.first).query === firstSearch.data.query : false} : null;
  const scenario = {
    runId: firstSearch.run_id, shortId: shortRunId(firstSearch.run_id), questionId: firstSearch.question_id,
    setup: firstSearch.setup, callIndex, callCount: calls.length, question: steps[0].question, goal: steps[0].goal,
    steps, timed, final, totals, baseline, ghost, run: runSummary(events),
    duration: doneAt + PACING.hold, doneAt,
  };
  scenario.annotations = annotate(scenario);
  return scenario;
}

const CAUGHT = {
  WRONG_ENTITY: 'Wrong entity caught before the agent saw it.',
  STALE: 'Out-of-date results caught before the agent saw it.',
  DEAD: 'Nothing relevant came back. Caught before the agent saw it.',
  SHALLOW: 'Right page, thin snippet. Not passed off as an answer.',
};

const usd = value => `$${value.toFixed(4)}`;
const plural = (n, word) => `${n} ${word}${n === 1 ? '' : 's'}`;

/** Short plain sentences tied to a step, each pointing at one element of the card. */
export function annotate(scenario) {
  const notes = [];
  const missChecks = scenario.steps.filter(s => s.kind === 'check' && s.verdict !== 'HIT');
  const read = scenario.steps.find(s => s.kind === 'read' && s.via === 'tavily');
  const prefer = ['WRONG_ENTITY', 'STALE', 'DEAD', 'SHALLOW'].map(v => missChecks.find(s => s.verdict === v)).find(Boolean);
  if (prefer && CAUGHT[prefer.verdict] && !(prefer.verdict === 'SHALLOW' && read)) {
    notes.push({at: prefer.at, target: `step-${scenario.steps.indexOf(prefer)}`, text: CAUGHT[prefer.verdict]});
  }
  if (read) notes.push({at: read.at, target: `step-${scenario.steps.indexOf(read)}`, text: 'Snippet too thin, so the page is read by Tavily Extract and checked again.'});
  const rewrite = scenario.steps.find(s => s.kind === 'rewrite' && s.rewritten);
  if (rewrite) notes.push({at: rewrite.at, target: `step-${scenario.steps.indexOf(rewrite)}`, text: 'Query rewritten, not retried blindly.'});
  const {totals, baseline, final} = scenario;
  if (final.status === 'NOT_FOUND') {
    notes.push({at: scenario.doneAt, target: 'final', text: `${plural(final.rescues, 'rescue')} failed, so Backstop declined instead of guessing.`});
  }
  if (baseline) {
    const ours = `${plural(totals.engineCalls, 'engine call')}${totals.fetches ? ` and ${plural(totals.fetches, 'page read')}` : ''}, ${usd(totals.cost)} with the checker.`;
    const theirs = `Telem default: ${plural(baseline.engineCalls, 'engine call')}${baseline.sameQuery ? ' on the same query' : ' on its first search'}, ${usd(baseline.cost)}, unchecked.`;
    notes.push({at: scenario.doneAt, target: 'counters', text: `${ours} ${theirs}`, kind: 'cost'});
  }
  return notes.sort((a, b) => a.at - b.at).map((note, index) => ({...note, side: note.target === 'counters' ? 'right' : index % 2 ? 'left' : 'right'}));
}

/** Linear map from display time to the recorded run's elapsed time. */
export function realAt(scenario, t) {
  const knots = [{at: 0, real: 0}, ...scenario.timed.map(({at, real}) => ({at, real})), {at: scenario.doneAt, real: scenario.totals.realMs}]
    .sort((a, b) => a.at - b.at);
  if (t <= 0) return 0;
  for (let i = 1; i < knots.length; i += 1) {
    const a = knots[i - 1], b = knots[i];
    if (t <= b.at) return b.at === a.at ? b.real : a.real + (b.real - a.real) * (t - a.at) / (b.at - a.at);
  }
  return scenario.totals.realMs;
}

/** Everything the card shows at display time t. Pure: safe for scrubbing in either direction. */
export function stateAt(scenario, t) {
  const time = Math.max(0, Math.min(t, scenario.duration));
  const visible = scenario.steps.filter(step => step.at <= time);
  const happened = scenario.timed.filter(entry => entry.at <= time);
  const lit = new Set(happened.filter(e => e.event.type === 'search').flatMap(e => e.event.data.engines || []));
  const lastSearch = [...visible].reverse().find(s => s.kind === 'search');
  const current = visible.at(-1);
  return {
    t: time,
    progress: scenario.duration ? time / scenario.duration : 0,
    steps: visible,
    current,
    activeEngine: current && current.kind === 'search' ? current.engines[0] : null,
    litEngines: lit,
    lastEngine: lastSearch?.engines[0] || null,
    counters: {
      engineCalls: happened.filter(e => e.event.type === 'search').reduce((n, e) => n + (e.event.data.engines?.length || 0), 0),
      fetches: happened.filter(e => e.event.type === 'fetch').length,
      cost: time >= scenario.doneAt ? scenario.totals.cost : happened.reduce((s, e) => s + eventCost(e.event), 0),
      realMs: realAt(scenario, time),
    },
    done: time >= scenario.doneAt,
    finished: time >= scenario.duration,
    annotations: scenario.annotations.filter(note => note.at <= time),
  };
}

/** Tick marks for the scrubber: every display step with its real elapsed time. */
export function ticks(scenario) {
  return scenario.steps.map((step, index) => ({index, at: step.at, real: step.real, caption: step.caption, kind: step.kind,
    verdict: step.verdict || step.status || null}));
}

export function formatSeconds(msValue) {
  return `${(Math.max(0, msValue) / 1000).toFixed(1)}s`;
}
