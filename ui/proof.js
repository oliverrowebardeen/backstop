/** Derive claims only from complete, comparable held-out setup totals. */
export function proofMetrics(results) {
  if (!results || !Number.isInteger(results.n_test) || results.n_test <= 0) return null;
  const n = results.n_test;
  const {single, telem_default: baseline, second_opinion: ours} = results.setups || {};
  if (![single, baseline, ours].every(s => s && s.correct + s.wrong + s.declined === n && (s.n ?? n) === n)) return null;
  const wrong = single.wrong ? 100 * (single.wrong - ours.wrong) / single.wrong : null;
  const accuracy = 100 * (ours.accuracy - baseline.accuracy);
  const cost = baseline.cost_per_q_total > 0 ? 100 * (baseline.cost_per_q_total - ours.cost_per_q_total) / baseline.cost_per_q_total : null;
  return {n, wrong, accuracy, cost, single, baseline, ours};
}

/** Nondominated observed points. Never force Backstop onto the efficient frontier. */
export function efficientFrontier(points) {
  const sorted = [...points].sort((a, b) => a.cost - b.cost || b.accuracy - a.accuracy);
  let best = -1;
  return sorted.filter(point => {
    if (point.accuracy <= best) return false;
    best = point.accuracy;
    return true;
  });
}
