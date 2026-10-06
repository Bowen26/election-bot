# Entry-price and entry-gap analysis

Run this local report from a separate terminal while the bot is running:

```bash
python3 -m election_bot analysis
python3 -m election_bot analysis --json
python3 -m election_bot analysis --paper
```

It reads one consistent snapshot of the selected trading journal. It does not read API credentials, contact an exchange, modify the journal, or change trading settings. The default output contains compact tables; `--json` includes coverage, observation delays, race counts and exploratory uncertainty intervals.

## What is grouped

Entry-price buckets use actual average fill prices for the purchased outcome: 0–0.15, 0.15–0.35, 0.35–0.65, 0.65–0.85 and 0.85–1.00. A NO purchase uses its own price; it is not converted back to YES. Lower endpoints are included and upper endpoints excluded. Valid execution prices lie strictly between zero and one.

Entry-gap buckets are below 5 cents, 5–6, 6–8, 8–12, and 12 cents or more. The gap is the pre-submission reference recorded with the order, minus its actual average fill price and configured entry buffer. It is an execution-adjusted entry comparison, not realized profit or a fresh reference quote at each partial fill. Exit buffers are not subtracted from this grouping variable; both entry and exit buffers are included in measured liquidation P&L.

The report requires an exact `order_key` link between the signal, stored order payload and confirmed execution. Exchange, action, side, quantity and pricing must agree. It does not infer links from nearby timestamps. Older unkeyed signals, missing data, conflicting signals and invalid associations stay in an explicit `unknown` bucket. Repeated identical linked signals do not multiply a filled order. The JSON linkage section reports match and exclusion counts.

## What is measured

Every filled buy appears in a distribution, but five- and fifteen-minute statistics include only buys eligible since short-horizon tracking began. Older buys are explicitly excluded from those horizons. Sales and zero fills are not buy observations. Horizon results use the existing measurement policy and windows; this analysis does not synthesize new historical quotes.

Tables show measured/matured counts, hypothetical P&L per share in cents, positive-observation percentage and missed observations. JSON also distinguishes not-yet-due, awaiting-quote, expired/missed and legacy-excluded buys, and reports coverage, average/max delay, distinct observed races and entry-day counts. Missing observations are never counted as zero returns. Low coverage may select the most liquid races, so it can bias comparisons.

P&L per share is weighted by measured shares. Positive percentage counts measured buy orders, not shares. The result is full-size hypothetical liquidation after configured cost buffers, not realized P&L or a fee-verified backtest. Do not add results across horizons.

## Uncertainty and interpretation

The JSON report includes exploratory 95% day-block bootstrap intervals for P&L per share and positive percentage only after at least five distinct observed UTC entry days. It resamples whole days using 1,000 fixed-seed replicates, preserving dependence among trades within each day. With fewer days, intervals remain null with `insufficient_entry_days`; hundreds of trades on one day still count as one day.

Five days is a reporting minimum, not a claim of adequate statistical power. Positions, polling surprises and the national swing can remain correlated across days. These intervals do not capture all such dependence, repeated peeking, selection among buckets, model uncertainty, unavailable liquidity or unobserved slippage. They describe the recorded sample under the resampling assumption and do not establish a trading edge.

Use the distributions to identify hypotheses worth testing. Neither this report nor the regional limit update changes the minimum edge, reference-depth sizing, exit thresholds, budgets or buffers. The descriptive [reference-leadership and shadow-entry report](reference-leadership.md) is now available. Relative thresholds, sizing changes and settlement-basis scoring remain separate experiments.
