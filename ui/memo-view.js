/** Pure helpers for Check a memo: no DOM access, so node --test can import them. */

export const VERDICT_WORDS = {VERIFIED: 'Verified', CONTRADICTED: 'Contradicted', NOT_FOUND: 'Not found'};

export function verdictKey(verdict) {
  return verdict === 'VERIFIED' ? 'verified' : verdict === 'CONTRADICTED' ? 'contradicted' : 'not_found';
}

/**
 * Split memo text into plain and claim segments using each claim's [start, end) span.
 * Overlapping spans are clipped to the uncovered part; claims with no usable span are
 * returned in `unplaced` so the page can still list them.
 */
export function segments(text, claims) {
  const placed = [];
  const unplaced = [];
  const sorted = [...claims].map((claim, index) => ({claim, index}))
    .sort((a, b) => (a.claim.span?.[0] ?? Infinity) - (b.claim.span?.[0] ?? Infinity));
  let cursor = 0;
  const out = [];
  for (const {claim, index} of sorted) {
    const span = Array.isArray(claim.span) ? claim.span : null;
    if (!span || !(span[1] > span[0]) || span[1] > text.length) { unplaced.push(index); continue; }
    const start = Math.max(span[0], cursor);
    if (start >= span[1]) { unplaced.push(index); continue; }
    if (start > cursor) out.push({text: text.slice(cursor, start)});
    out.push({text: text.slice(start, span[1]), claim: index});
    placed.push(index);
    cursor = span[1];
  }
  if (cursor < text.length) out.push({text: text.slice(cursor)});
  return {segments: out, unplaced: unplaced.sort((a, b) => a - b), placed};
}

export function summaryParts(totals = {}) {
  return [
    {key: 'verified', count: totals.verified || 0, word: 'verified'},
    {key: 'contradicted', count: totals.contradicted || 0, word: 'contradicted'},
    {key: 'not_found', count: totals.not_found || 0, word: 'not found'},
  ];
}

export function summaryText(totals) {
  return summaryParts(totals).map(part => `${part.count} ${part.word}`).join(', ');
}

export function money(value) {
  return `$${Number(value || 0).toFixed(value >= 0.1 ? 2 : 4)}`;
}

export function seconds(ms) {
  return `${(Number(ms || 0) / 1000).toFixed(1)}s`;
}

/** Only http(s) links are rendered as links. */
export function safeUrl(url) {
  try {
    const parsed = new URL(url);
    return parsed.protocol === 'https:' || parsed.protocol === 'http:' ? parsed.href : null;
  } catch { return null; }
}

export function hostOf(url) {
  try { return new URL(url).hostname.replace(/^www\./, ''); } catch { return url || ''; }
}

export function errorMessage(status, detail) {
  if (detail && typeof detail === 'object' && typeof detail.message === 'string') return detail.message;
  if (typeof detail === 'string') return detail;
  if (status === 422) return 'Paste a memo of 1 to 4,000 characters.';
  if (status === 429) return 'Busy or over the hourly limit. Try again in a moment.';
  return `The check failed (HTTP ${status}).`;
}
