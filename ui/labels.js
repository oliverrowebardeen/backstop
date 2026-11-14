/** Display names shared by the site: engines and checker verdicts. */

export const VERDICT_LABEL = {
  HIT: 'Hit', STALE: 'Stale', WRONG_ENTITY: 'Wrong entity', SHALLOW: 'Shallow',
  DEAD: 'Dead', NOT_FOUND: 'Not found', UNCHECKED: 'Unchecked',
};
export const ENGINE_LABEL = {
  tavily: 'Tavily', exa: 'Exa', brave: 'Brave', perplexity: 'Perplexity', parallel: 'Parallel',
  ceramic: 'Ceramic', linkup: 'Linkup', you: 'You.com', seltz: 'Seltz', serpapi: 'SerpApi', tinyfish: 'TinyFish',
};
export const engineName = id => ENGINE_LABEL[id] || String(id);

/** A verdict chip, styled by the shared .verdict-* classes. */
export function verdictChip(verdict) {
  const chip = document.createElement('span');
  chip.className = `verdict verdict-${String(verdict || '').toLowerCase().replace(/_/g, '-')}`;
  chip.textContent = VERDICT_LABEL[verdict] || verdict;
  return chip;
}
