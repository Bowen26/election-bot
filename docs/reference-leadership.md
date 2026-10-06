# Reference leadership and prospective entry comparisons

Run from the bot folder, in a separate terminal while the live process runs:

```sh
python3 -m election_bot leadership
python3 -m election_bot leadership --json
python3 -m election_bot leadership --hours 24
python3 -m election_bot leadership --paper
```

The command reads a consistent local SQLite snapshot in read-only mode. It requires no key or network access, creates no missing journal/runtime directory and changes no accounting. Default lookback is 48 hours; the maximum is 720 hours. JSON includes per-race/day leadership breakdowns, paired shadow comparisons, missing-data counts and measurement delays.

Restart the active bot once to begin recording `shadow_decision` events. Existing three-venue `quote_snapshot` events can support the leadership report immediately. Old shadow decisions are **not** reconstructed or invented. The recorder uses current scan books and makes no extra API requests. It adds one journal event per successful scan decision; preflight does not record another shadow decision. Live entry/exit rules, limits, news handling and order submission remain unchanged.

## Leadership measurements

Prices are aligned to SIG YES. At each selected scan, predictors are each reference midpoint minus the SIG midpoint, plus their average when both gaps point in the same direction. Outcomes are the subsequent SIG midpoint change. A separate movement predictor uses the reference midpoint change from its preceding valid aligned scan, at most five minutes earlier, with an advancing source timestamp and a move of at least one cent. With sparse polling there may be no eligible movement observations.

The report measures 5-minute and 15-minute horizons. It uses the first valid SIG scan at or after the target, no later than 2 or 3 minutes afterward respectively. Both SIG source and local observation times must be at or after the target. Future reference quotes need not be valid to measure SIG's outcome. Later favorable observations do not replace that first quote.

Entry quotes must have no recorded quality issues, positive two-sided uncrossed top levels, and finite source/local timestamps no older than 15 seconds and not in the future. The maximum cross-venue skew is 5 seconds for both source and local timestamps. Capture/event times must agree within 5 seconds. Exact duplicate snapshot IDs count once; conflicting duplicates are excluded. Preflight snapshots are excluded throughout.

For each horizon, each race reserves a non-overlapping interval consisting of the horizon plus its entire observation window. The first eligible scan is the anchor; another cannot start until that interval ends, even if its outcome is missing. The schedule uses no future outcome to pick entries. Different lookback start times can change this sampling grid, so compare runs with the same window. Separate horizon results must not be added together.

Coverage distinguishes measured, missed, not yet due and awaiting quote. A missing outcome is never treated as no price movement. Follow rate is the fraction of nonzero predictors followed by a same-direction SIG move; flat SIG outcomes remain in the denominator as non-following and are counted separately. Pearson correlations require at least three observations and nonzero variation, otherwise they are unavailable. This numerical minimum does not imply an adequate sample.

These are descriptive associations, not causal or sub-second price-discovery findings. HTTP server/fetch timestamps are proxies. Priority scanning, outages, trading cooldowns and news gates select what is observed. Non-overlap reduces repeated measurements but does not remove election-wide correlation across races or days. Inspect coverage, flat outcomes, delays and day/race concentration before interpreting pooled results. There are no IID confidence intervals, significance claims or automatic winner selection.

## Prospective entry comparison: `reference_bid_v1`

Three entry rules are evaluated against the same scan:

| Rule | Valuation input |
| --- | --- |
| `both_bids` | Lower of the two external outcome bids, the current entry rule |
| `kalshi_bid` | Kalshi's outcome bid |
| `polymarket_bid` | Polymarket's outcome bid |

All variants retain both venues' freshness, spread, agreement and side-specific depth requirements. Both reference depths still limit size. They use the current entry threshold, buffer, SIG tick/best-level depth, maximum shares, real available coin budget, real holdings and directional headroom. NO uses complementary books. An opposite-inventory proposal is suppressed, and a selected live exit takes priority. The baseline is a proposed scan entry, not necessarily an order: normal preflight may reject or resize the actual live order.

Records contain exact snapshot identity, experiment version, settings, available budget, holdings, headroom and each proposed candidate or rejection. They never reserve capital, update inventory or submit orders. A quote aging out during the extra calculation records measurement unavailability without changing the already selected live decision.

Evaluation uses the same non-overlapping anchors and endpoints as leadership. It assumes an immediate full fill at the displayed SIG ask and liquidation at the first follow-up SIG top bid (NO uses one minus the YES ask). The future displayed size must cover the entire proposed quantity; otherwise it is marked insufficient exit depth, without searching later for a better quote or assuming a partial fill. Entry and exit each subtract the stored per-share buffer. These buffers are not a verified fee schedule.

The report checks a candidate against its exact original quote, reference bid, size, edge, budget, inventory and exposure fields. Unknown/malformed/conflicting records remain explicitly unmeasured. Old scans remain `no_prospective_record`. Each rule reports proposed entries, measured outcomes, missing statuses, share-weighted hypothetical P&L/share and proposal-weighted positive fraction. JSON adds the number of additional proposals where the baseline proposed nothing and equal-weighted per-proposal P&L/share differences on **paired measured** anchors only. Unpaired averages alone cannot establish a superior rule.

This is an entry-signal experiment, **not an executable portfolio backtest**. Each variant uses the actual portfolio's contemporaneous budget and inventory; it has no independent evolving wallet or exit policy. Displayed fills are assumptions, not queue/latency/market-impact or partial-fill simulations. Repeated proposals cannot be summed into a strategy profit total. The fixed horizons are hypothetical liquidations, not applications of the live exit rule. No variant is automatically promoted to trading.

Adaptive thresholds, settlement-basis scoring, removal of reference-depth sizing, independent portfolio replay and live strategy changes remain separate work. Use fresh forward observations and a preselected evaluation window rather than repeatedly tuning to the same historical sample.
