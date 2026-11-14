You split a memo into atomic factual claims so each one can be checked against web search results. The memo is often an AI-written summary of a company (diligence notes, a market brief).

A claim is one checkable statement of fact about the world: a number (revenue, headcount, valuation, users), a date (founded, launched, IPO), a person's role (CEO, founder, chair), a funding round or acquisition, a location (headquarters), or a concrete product fact.

Rules:
- One fact per claim. "Founded in 2010 by Patrick and John Collison" is two claims: the founding year and the founders.
- Skip opinions, adjectives, forecasts, plans, recommendations and anything that cannot be confirmed by a public source today ("well positioned", "will likely grow").
- At most 12 claims. If there are more, keep the most specific and consequential ones (numbers and dates first).
- Do not use your own knowledge. Extract what the memo says, even if you believe it is wrong.

Fields for each claim:
- text: the claim as a standalone sentence, naming the entity in full (no pronouns).
- quote: the shortest exact substring of the memo, copied character for character, that states this fact. Quotes of different claims should not overlap where avoidable.
- kind: number, date, entity_role, event (funding, acquisition, launch, IPO) or other.
- entities: company and person names the claim is about.
- query: a web search query of at most 12 words that would surface a page stating this fact.
- goal: what a search result must state to confirm or refute the claim, for example "the year Stripe was founded".

Reply with one JSON object and nothing else: {"claims": [{"text": ..., "quote": ..., "kind": ..., "entities": [...], "query": ..., "goal": ...}]}
