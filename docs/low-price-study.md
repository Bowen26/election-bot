# Low-price contract exit study

`python3 -m election_bot low-price-study --hours 24` replays recorded SIG, Kalshi and Polymarket scan quotes. Add `--json` for settings, every entry, sampled bid extremes and partial exit paths; `--paper` reads the paper journal instead. The command requires no key or network and opens the journal read-only. It does not submit orders, enable contracts, change limits or reserve coins. Existing running bots already record these snapshots, so a restart is not required for this command. Rerun it to incorporate newly recorded scans. It is an on-demand retrospective study, not a separate continuously running paper broker.

## Question and comparison

Can a low-priced contract be bought and resold at a net profit before a stop, weakening reference signal or time limit? Compare two policies on identical entries:

* `full_target`: sell all at the first observed qualifying bid, subject to displayed depth.
* `half_then_trail`: sell half at that target, then retain the rest while the bid rises. Sell the runner after a 1-cent drawdown from its observed peak. This is a bid-based trailing rule, not a fitted trend forecast. Stops, time limits and reference deterioration can exit sooner.

Policies are counterfactual alternatives; never add their profits together. New entries wait until both paths for that exchange terminate, and never reuse an exit snapshot for entry. There is no simulated common wallet or shared capital/exposure limit across different exchanges. Closed-path P&L is a descriptive sum, not an achievable portfolio return.

## Fixed initial research assumptions

| Control | Default |
| --- | --- |
| Entry ask | 1–10 cents, either YES or NO |
| SIG spread | At most 2 cents |
| Reference quality | Both books fresh and aligned; each spread at most 4 cents; midpoint disagreement at most 3 cents; at least 20 shares at each aligned bid |
| Entry evidence | Lower of the two external bids exceeds SIG ask by at least 1 cent **after both 1-cent cost buffers** |
| Entry size | At most 100 shares and 10 coins including entry buffer; at most 25% of both SIG bid and ask depth; bounded by reference bid depth; minimum 2 shares |
| Profit target | Net 2 cents/share or 50% of entry cost including buffer, whichever is larger |
| Stop | Observed bid at or below 50% of entry ask; actual observed bid used, not the stop price |
| Maximum holding time | 6 hours; request exit at first subsequent valid quote |
| Signal exit | Lower aligned external bid at or below original entry ask |
| Exit size | At most 25% of observed SIG bid depth per new quote; unfilled remainder stays unresolved |
| Observation gap | More than 15 minutes between usable quotes censors the remaining path |

These defaults are a versioned hypothesis (originally `low_price_paths_v1`; the expanded report is `low_price_paths_v2`), not calibrated settings or changes to live strategy. Entry evidence is still the external-price gap; the uncalibrated polling model is not used to manufacture a low-probability fair value. Further work can compare other predeclared entry signals after validation.

For a 3-cent entry and two 1-cent buffers, the default target bid is 7 cents: `0.03 + 0.02 + max(0.02, 0.50 × 0.04)`. Selling at 5 cents is a 67% gross price gain but zero net gain under this conservative buffer assumption. Buffers are modeling allowances, not a claim about SIG's actual fee schedule; at very low prices, they can dominate the economics. The study does not quietly remove costs to make the strategy appear profitable.

## Fill and missing-data limitations

Entries assume the specified fraction of displayed ask depth can be bought at the recorded ask. Exits assume the specified fraction of a later displayed bid is available. These are hypothetical immediate fills, not confirmed trades: top-of-book snapshots do not establish queue priority, latency, persistent liquidity or deeper-book prices. No resting limit order is simulated. The engine checks no unseen price path between scans. A target or stop could have been hit between observations; only observed events count, and their real-world first-passage order remains unknown.

A partial profit target remains price-conditional until half is filled. A full liquidation triggered by a target, stop, time limit, reference deterioration or trailing exit remains latched; later partial fills can receive a worse price. Neither stops nor profit exits guarantee their trigger price. Book source and observation clocks must advance; repeatedly recorded old liquidity cannot create more fills. A missing reference quote does not fabricate deterioration or block a valid SIG target/stop exit.

Invalid, stale, conflicted and preflight snapshots are excluded using the shared research validator. No quote after a gap over 15 minutes is treated as an executable continuation. Open/censored shares retain their entry cost in the report, with partial realized P&L reported separately. No zero return, invented terminal sale or settlement result is assigned. Zero bids/one-price boundary books are excluded by the existing validator, so terminal-outcome coverage is limited too. Summaries show exit-fill counts, first filled exit reasons, target-fill episode counts and holding times for fully closed paths. Closed-only averages/sums have completion bias and must be read alongside unresolved exposure.

The replay starts flat at the requested lookback boundary and uses only retained journal evidence. Contract identity is the recorded exchange and aligned YES orientation; historical mapping changes are not independently fingerprint-verified by these older snapshots. Normal scans can be missing during news pauses or external outages. The study has no independent wallet, competition scoring simulation or calibrated profitability claim. A useful next evaluation is a fixed forward observation period followed by a separate holdout; changing thresholds repeatedly against the same history would overfit it.


## SIG momentum comparison (October 13 increment)

Run both entry hypotheses against the same recorded window:

```bash
python3 -m election_bot low-price-study --hours 24 --entry-mode compare
```

Use `--entry-mode sig_momentum` to inspect just the new study, or `--entry-mode external_gap` for the original rule (still the default). `--json` includes causal entry evidence: snapshot IDs, times, aligned bid/ask sequences, measured rise and breakout level. The command is read-only, needs no restart, and does not install a live momentum strategy. Running it again includes newly recorded quotes.

The momentum hypothesis uses SIG alone for selection. It does not treat an external price discount, the independent polling estimate, or historical realized returns as an entry requirement. This tests whether an observed price move continues far enough to cover costs. It is not a claim that rising prices establish fair value.

At each candidate snapshot, select the two most recent earlier usable observations at least 60 seconds apart, plus the current quote. All three must span at least 120 seconds within a 30-minute window. Source and observation timestamps must advance. The replay resets history after a gap over 15 minutes, and history stays separate by exchange. The selected bid and ask sequences must be nondecreasing; both must rise at least 1 cent from first to last. The bid must also rise at least 25%, and the current bid must exceed the earlier high by at least half a cent. Each selected SIG spread must be at most 2 cents. These thresholds were specified before examining the new study's outcomes; they are provisional and must be evaluated on later holdout data.

The current ask must remain in the original 1–10-cent band. The same coin/share caps, 25% SIG bid/ask participation limit, two cost buffers and target formula apply. Momentum sizing uses SIG liquidity alone, since no external hedge is being executed. Missing/unaligned reference prices do not veto a valid SIG momentum entry, although the existing scanner still requires successful enough feed collection to record snapshots at all.

Both exit alternatives retain their full-target versus half-target/trailing behavior, stop and time limit. The **signal invalidation rule** differs by entry mode: external-gap entries keep the original reference-deterioration exit, while momentum entries request liquidation when the SIG bid falls back to or below the pre-entry bid high (`failed_breakout`). This avoids immediately closing a momentum entry merely because external bids were lower at entry. It is an explicit strategy difference; comparing results cannot isolate the causal effect of changing entry alone. Actual modeled fills remain bounded by later observed bid depth and may be below the trigger price.

Comparisons keep each mode's positions and results separate. The two exit alternatives share entries within each mode, but external-gap and momentum modes can enter different races at different times. Never combine their P&L or interpret them as paired trades. Repeated observations are correlated, and sparse/censored results cannot establish profitability. Existing historical snapshots and live trading settings are not modified.
