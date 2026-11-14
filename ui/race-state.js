export const SETUPS = {
  single: {name: 'Single engine', description: 'One engine through Telem. No result check.'},
  telem_default: {name: 'Telem default', description: 'Telem accuracy routing across multiple engines.'},
  second_opinion: {name: 'Second Opinion', description: 'Check the evidence. Rescue a miss.'},
};

export const VERDICTS = {
  HIT: 'Hit', STALE: 'Stale', WRONG_ENTITY: 'Wrong entity', SHALLOW: 'Shallow',
  DEAD: 'Dead', NOT_FOUND: 'Not found', UNCHECKED: 'Unchecked',
};

/** @param {keyof typeof SETUPS} setup @param {string} firstEngine */
export function newRun(setup, firstEngine) {
  return {setup, firstEngine, events: [], seen: new Set(), runId: null, session: null, engineCalls: 0,
    telemCalls: 0, cost: 0, elapsed: 0, startedAt: null, finished: false,
    steps: [], answer: null, status: 'Ready'};
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
  state.elapsed = Math.max(state.elapsed, Date.parse(event.ts) - state.startedAt);
  state.status = 'Running';
  if (event.type === 'search') {
    state.engineCalls += data.engines.length;
    state.telemCalls += 1;
    state.cost += data.cost_usd || 0;
    state.steps.push({kind: 'search', query: data.query, goal: data.goal,
      engines: [...data.engines].sort((a, b) => Number(b === state.firstEngine) - Number(a === state.firstEngine)),
      verdict: null, why: '', cached: data.cached});
  } else if (event.type === 'check') {
    const step = [...state.steps].reverse().find(item => ['search', 'fetch'].includes(item.kind));
    if (step) Object.assign(step, {verdict: data.verdict, why: data.why});
  } else if (event.type === 'rescue') {
    state.steps.push({kind: 'rescue', query: data.action === 'FETCH_PAGE' ? 'Read the source page' : `Retry with ${data.engine}`,
      why: data.reason, engines: [], verdict: null});
  } else if (event.type === 'fetch') {
    state.cost += data.cost_usd || 0;
    if (data.via === 'telem') state.telemCalls += 1;
    state.steps.push({kind: 'fetch', query: data.via === 'tavily' ? 'read by Tavily Extract' : 'read by Telem fetch',
      url: data.url, why: data.ok ? 'Page read completed.' : 'Page read did not return usable text.',
      engines: [], verdict: null});
  } else if (event.type === 'llm' && data.role !== 'judge') {
    state.cost += data.cost_usd || 0;
  } else if (event.type === 'search_done') {
    const step = [...state.steps].reverse().find(item => item.kind === 'search');
    if (step && !step.verdict) step.verdict = data.status;
  } else if (event.type === 'answer') {
    state.answer = data;
  } else if (event.type === 'run_end') {
    state.engineCalls = data.engine_calls;
    state.cost = data.cost_usd_total;
    state.elapsed = data.latency_ms;
    state.telemCalls = data.telem_calls ?? state.telemCalls;
    state.finished = true;
    state.status = state.answer?.status === 'NOT_FOUND' ? 'Not found' : 'Complete';
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
