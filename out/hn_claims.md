# Tested the founder's claims

Hit rate = gold answer in an engine's top 5 (the hit matrix). Test split, non-trap, 200 questions; bootstrap 95% intervals (10000 resamples, seed 14).

## Verdicts

- **NOT SUPPORTED**: "Ceramic is extremely cheap and works well for broad, preliminary searches"
  - extremely cheap: SUPPORTED. so.config.PRICE_USD (what Telem bills us): $0.00005 per call vs median other $0.005 (0.010x); Telem latency-study list prices: $0.00005 per call vs median other $0.006 (0.008x).
  - works well (overall hit rate): NOT SUPPORTED. ceramic minus median of the other engines = -64.0 points (95% CI -71.0 to -58.5), n=200.
  - Caveat: Our questions are specific fact and finance lookups, not broad preliminary searches.
- **INCONCLUSIVE**: "Brave has strong general coverage but may not be best for domain-specific queries like financial research"
  - strong general coverage (overall hit rate): INCONCLUSIVE. brave minus median of the other engines = -1.5 points (95% CI -8.0 to +4.5), n=200.
  - not best on finance: SUPPORTED. brave minus best other engine (exa) = -41.9 points (95% CI -58.1 to -25.6), n=43.
- **INCONCLUSIVE**: "Exa is strong for entity and people search but can be bad at latest news"
  - strong for entity search (fact questions as proxy): SUPPORTED. exa minus median of the other engines = +7.6 points (95% CI +3.2 to +15.3), n=157.
  - bad at latest news (questions mentioning 2025 or 2026): INCONCLUSIVE. no questions in this subset.
  - Caveat: We have no entity or people category; SimpleQA fact questions stand in for entity search. No test question mentions 2025 or 2026, so the news part cannot be tested on this matrix.

## Hit rates by subset

| subset | n | tavily | exa | brave | perplexity | parallel | ceramic |
|---|---|---|---|---|---|---|---|
| overall | 200 | 68.5% (62.0% to 75.0%) | 82.0% (76.5% to 87.0%) | 67.0% (60.5% to 73.5%) | 82.0% (76.5% to 87.0%) | 63.5% (56.5% to 70.0%) | 4.5% (2.0% to 7.5%) |
| finance | 43 | 14.0% (4.7% to 25.6%) | 48.8% (34.9% to 65.1%) | 7.0% (0.0% to 16.3%) | 32.6% (18.6% to 46.5%) | 16.3% (7.0% to 27.9%) | 0.0% (0.0% to 0.0%) |
| fact | 157 | 83.4% (77.1% to 89.2%) | 91.1% (86.6% to 95.5%) | 83.4% (77.7% to 89.2%) | 95.5% (91.7% to 98.7%) | 76.4% (69.4% to 82.8%) | 5.7% (2.5% to 9.6%) |
| recent_2025_2026 | 0 | n/a | n/a | n/a | n/a | n/a | n/a |

No test question or gold answer mentions 2025 or 2026 (newest year mentioned: 2023), so recency cannot be tested here.

## Second engine after a Tavily miss, per dollar (learn split, recommendation only)

Prices: so.config.PRICE_USD (what Telem bills us). 30 Tavily misses.

| engine | rescued | rate | price | rescues per $ |
|---|---|---|---|---|
| parallel | 13/30 | 43.3% | $0.001 | 433 |
| exa | 15/30 | 50.0% | $0.005 | 100 |
| perplexity | 13/30 | 43.3% | $0.005 | 87 |
| brave | 12/30 | 40.0% | $0.005 | 80 |
| ceramic | 0/30 | 0.0% | $0.00005 | 0 |

Per-dollar order: parallel, exa, perplexity, brave, ceramic.

Prices: Telem latency-study list prices. 30 Tavily misses.

| engine | rescued | rate | price | rescues per $ |
|---|---|---|---|---|
| parallel | 13/30 | 43.3% | $0.005 | 87 |
| brave | 12/30 | 40.0% | $0.005 | 80 |
| exa | 15/30 | 50.0% | $0.007 | 71 |
| ceramic | 0/30 | 0.0% | $0.00005 | 0 |
| perplexity | 13/30 | 43.3% | unknown | n/a |

Per-dollar order: parallel, brave, exa, ceramic (unpriced: perplexity).

Rescue table order_after["tavily"] (by rescue rate): exa, parallel, perplexity, brave, ceramic. The per-dollar order (primary prices) differs: per dollar it is parallel, exa, perplexity, brave, ceramic; positions change for parallel, exa. The rescue table is unchanged; with at most two rescues per search, a rescue slot is scarce, so rate per slot matters as much as rate per dollar.

## Share of the all-engine ceiling

Oracle (any of the 6 engines hit) on the test split: 91.0% (182/200).
- SIMULATED (out/frontier.json second_opinion point (simulated), simulated, perfect checker): coverage 89.0% = 97.8% of the ceiling; cost per captured question $0.0082.

## Context

Telem's own published numbers, not ours: Telem's Oct 22 2026 post reported that Tavily found known answers on 244 of 363 questions and that Tavily plus Exa top-5 found 279.
