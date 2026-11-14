/**
 * One plain comparison of two finished runs: engine calls, total cost and time, naming whichever
 * side was lower and by how much. It says nothing about which answer is right: live and replayed
 * questions are not graded here.
 */

const plural = (n, word) => `${n} ${word}${n === 1 ? '' : 's'}`;
const usd = value => `$${value.toFixed(4)}`;
const secs = ms => `${(ms / 1000).toFixed(1)}\u00a0s`;

/**
 * @param {{name: string, engineCalls: number, cost: number, elapsed: number}} a
 * @param {{name: string, engineCalls: number, cost: number, elapsed: number}} b
 * @returns {string | null}
 */
export function raceSummary(a, b) {
  if (![a, b].every(side => side && [side.engineCalls, side.cost, side.elapsed].every(Number.isFinite))) return null;
  const clauses = [];
  const [fewer, more] = a.engineCalls <= b.engineCalls ? [a, b] : [b, a];
  clauses.push(fewer.engineCalls === more.engineCalls
    ? {who: 'Both', text: `made ${plural(fewer.engineCalls, 'engine call')}`}
    : {who: fewer.name, text: `made ${plural(more.engineCalls - fewer.engineCalls, 'fewer engine call')} (${fewer.engineCalls} vs ${more.engineCalls})`});
  const [cheaper, dearer] = a.cost <= b.cost ? [a, b] : [b, a];
  const saving = dearer.cost > 0 ? Math.round(100 * (1 - cheaper.cost / dearer.cost)) : 0;
  clauses.push(saving < 1
    ? {who: 'Both', text: `cost about ${usd(dearer.cost)} in total`}
    : {who: cheaper.name, text: `cost ${saving}% less in total (${usd(cheaper.cost)} vs ${usd(dearer.cost)})`});
  const [faster, slower] = a.elapsed <= b.elapsed ? [a, b] : [b, a];
  const gap = slower.elapsed - faster.elapsed;
  clauses.push(gap < 100
    ? {who: 'Both', text: `took about ${secs(slower.elapsed)}`}
    : {who: faster.name, text: `finished ${secs(gap)} sooner (${secs(faster.elapsed)} vs ${secs(slower.elapsed)})`});
  // One sentence per subject, in the order subjects first appear.
  const order = [...new Set(clauses.map(clause => clause.who))];
  return order.map(who => {
    const parts = clauses.filter(clause => clause.who === who).map(clause => clause.text);
    const joined = parts.length > 1 ? `${parts.slice(0, -1).join(', ')} and ${parts.at(-1)}` : parts[0];
    return `${who} ${joined}.`;
  }).join(' ');
}

/** How a recording was made, read from its manifest. */
const SETTINGS = {
  checker_backend: value => `the ${value === 'llm' ? 'LLM' : value} checker`,
  first_engine_routing: value => `first-engine routing ${value}`,
  cache: value => `the cache ${value}`,
};

/** "Example race. Recorded live Nov 14, 2026 on commit abc1234 with ..." from the manifest; empty for fixtures. */
export function replayLabel(pair) {
  if (!pair || pair.fixture) return '';
  const when = Date.parse(pair.recorded_at || '');
  const date = Number.isFinite(when) ? new Date(when).toLocaleDateString('en-US', {year: 'numeric', month: 'short', day: 'numeric', timeZone: 'UTC'}) : null;
  const commit = typeof pair.commit === 'string' && /^[0-9a-f]{7,40}$/.test(pair.commit) ? pair.commit.slice(0, 7) : null;
  if (!date) return '';
  const settings = Object.entries(SETTINGS).filter(([key]) => /^[a-z]+$/.test(String(pair[key] ?? '')))
    .map(([key, say]) => say(pair[key]));
  const how = settings.length > 1 ? `${settings.slice(0, -1).join(', ')} and ${settings.at(-1)}` : settings[0];
  return `Example race. Recorded live ${date}${commit ? ` on commit ${commit}` : ''}${how ? ` with ${how}` : ''}.`;
}

