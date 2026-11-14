export const SETUPS = {
  single: {name: 'Single engine', description: 'One engine through Telem. No result check.'},
  telem_default: {name: 'Telem default', description: 'Telem accuracy routing across multiple engines.'},
  second_opinion: {name: 'Backstop', description: 'Check the evidence. Rescue a miss.'},
};

export const VERDICTS = {
  HIT: 'Hit', STALE: 'Stale', WRONG_ENTITY: 'Wrong entity', SHALLOW: 'Shallow',
  DEAD: 'Dead', NOT_FOUND: 'Not found', UNCHECKED: 'Unchecked',
};

/**
 * Live runs (`live: true`) take their time from the page's own clock, start to final event, and count
 * only spend this run made; a replay keeps the recorded timing and totals.
 * @param {keyof typeof SETUPS} setup @param {string} firstEngine @param {{live?: boolean}} [options]
 */
export function newRun(setup, firstEngine, {live = false} = {}) {
  return {setup, firstEngine, live, events: [], seen: new Set(), runId: null, session: null, engineCalls: 0,
    telemCalls: 0, cost: 0, searchCost: 0, cachedCost: 0, elapsed: 0, startedAt: null, finished: false,
    steps: [], currentSearch: null, currentCheck: null, sourceChecks: new Map(),
    answer: null, failure: null, status: 'Ready'};
}

function textField(value) { return typeof value === 'string' && value.trim() ? value : null; }

function sourceIdentity(value) {
  const url = safeURL(value);
  if (!url) return null;
  url.hash = '';
  return url.href;
}

/**
 * A source badge records a checker HIT, not verification of the entire answer.
 * @param {ReturnType<typeof newRun>} state
 * @param {string} url
 * @returns {boolean}
 */
export function citationVerified(state, url) {
  return state.sourceChecks.get(sourceIdentity(url)) === true;
}

function recordCheck(state, step, data) {
  const rank = Number.isInteger(data.evidence_rank) && data.evidence_rank > 0 &&
    (!Number.isInteger(step.resultCount) || data.evidence_rank <= step.resultCount) ? data.evidence_rank : null;
  const url = safeURL(data.best_url || (step.kind === 'fetch' && step.ok ? step.url : null));
  const identity = sourceIdentity(url?.href);
  if (identity) state.sourceChecks.set(identity, data.verdict === 'HIT');
  step.evidence = data.verdict === 'HIT' ? {
    rank, url: url?.href || null,
    label: step.kind === 'fetch' ? 'Answer on this page' : rank ? `Answer in result ${rank}` : 'Checker found evidence',
  } : null;
  Object.assign(step, {verdict: data.verdict, why: data.why});
}

/** A terminal stream failure can arrive after the agent's cleanup totals. */
export function failRun(state, message) {
  state.failure = message || 'The run failed before an answer was completed.';
  state.status = 'Failed';
  state.finished = true;
}

/** Apply one contract event. Repeated SSE deliveries are ignored. */
export function applyEvent(state, event) {
  if (!event?.data || !Number.isFinite(Date.parse(event.ts))) throw new Error('The run contains an invalid event.');
  if (event.type === 'search' && (!Array.isArray(event.data.engines) || typeof event.data.query !== 'string')) {
    throw new Error('A search event is missing its engines or query.');
  }
  if (event.type === 'run_end' && !['engine_calls', 'cost_usd_total', 'latency_ms'].every(key => Number.isFinite(event.data[key]))) {
    throw new Error('The run is missing valid final totals.');
  }
  const identity = `${event.run_id}:${event.seq}`;
  if (state.seen.has(identity)) return false;
  state.seen.add(identity);
  state.events.push(event);
  state.runId = event.run_id;
  state.setup = event.setup;
  const data = event.data;
  const session = data.session_id || data.telem_session_id || data.session;
  if (typeof session === 'string') state.session = session;
  state.startedAt ??= Date.parse(event.ts);
  if (!state.live) state.elapsed = Math.max(state.elapsed, Date.parse(event.ts) - state.startedAt);
  // A cached call cost nothing in this run, so a live counter leaves it out.
  const spend = state.live && data.cached === true ? 0 : data.cost_usd || 0;
  if (state.live && data.cached === true) state.cachedCost += data.cost_usd || 0;
  if (!state.failure) state.status = 'Running';
  if (event.type === 'search') {
    state.engineCalls += data.engines.length;
    state.telemCalls += 1;
    state.cost += spend;
    state.searchCost += spend;
    const engine = textField(data.first_engine);
    const questionType = textField(data.question_type);
    const step = {kind: 'search', query: data.query, goal: data.goal, resultCount: data.n_results,
      engines: [...data.engines].sort((a, b) => Number(b === state.firstEngine) - Number(a === state.firstEngine)),
      choice: engine || questionType ? {engine, questionType} : null,
      verdict: null, why: '', cached: data.cached};
    state.steps.push(step);
    state.currentSearch = step;
    state.currentCheck = step;
  } else if (event.type === 'check') {
    if (state.currentCheck) recordCheck(state, state.currentCheck, data);
  } else if (event.type === 'rescue') {
    const from = textField(state.currentSearch?.query), to = textField(data.query);
    const rewrite = from && to && from.trim() !== to.trim() ? {from, to} : null;
    const action = data.action === 'FETCH_PAGE' ? 'Read the source page' : textField(data.engine) ? `Retry with ${data.engine}` : 'Retry search';
    state.steps.push({kind: 'rescue', query: rewrite ? 'Rewrote query' : action,
      rewrite, action, why: data.reason, engines: [], verdict: state.currentCheck?.verdict || null});
  } else if (event.type === 'fetch') {
    state.cost += spend;
    state.searchCost += spend;
    if (data.via === 'telem') state.telemCalls += 1;
    const step = {kind: 'fetch', query: data.via === 'tavily' ? 'read by Tavily Extract' : 'read by Telem fetch',
      url: data.url, ok: data.ok, why: data.ok ? 'Page read completed.' : 'Page read did not return usable text.',
      engines: [], verdict: null};
    state.steps.push(step);
    state.currentCheck = step;
  } else if (event.type === 'llm' && data.role !== 'judge') {
    state.cost += spend;
    // Search cost: engines, page reads and the checker; the agent model's own turns are the rest.
    if (data.role !== 'agent') state.searchCost += spend;
  } else if (event.type === 'search_done') {
    const step = state.currentSearch;
    if (step && !step.verdict) step.verdict = data.status;
    state.currentSearch = null;
    state.currentCheck = null;
  } else if (event.type === 'answer') {
    state.answer = data;
  } else if (event.type === 'run_end') {
    state.engineCalls = data.engine_calls;
    state.cost = state.live ? Math.max(0, data.cost_usd_total - state.cachedCost) : data.cost_usd_total;
    if (!state.live) state.elapsed = data.latency_ms;
    state.telemCalls = data.telem_calls ?? state.telemCalls;
    state.finished = true;
    if (state.failure || data.error || !state.answer) {
      failRun(state, state.failure || 'The run failed before an answer was completed. Check the server logs for the reason.');
    } else {
      state.status = state.answer.status === 'NOT_FOUND' ? 'Not found' : 'Complete';
    }
  }
  return true;
}

/** @param {string} value @returns {URL | null} */
export function safeURL(value) {
  try {
    const url = new URL(value);
    return ['https:', 'http:'].includes(url.protocol) ? url : null;
  } catch { return null; }
}
