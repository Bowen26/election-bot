# Strategy review, October 10, 2026

Decision: retain the current live reference and exit-depth policies; implement automated polling research as the next evidence-building increment. More proposals alone do not establish more profitable execution. This review does not change limits or activate independent-model orders.

Local 48-hour reports as of Unix time 1791657193.588:

```sh
python3 -m election_bot leadership --json --hours 48
python3 -m election_bot exit-study --json --hours 48
```

## Reference choice

There were 33,551 quote-snapshot events, 20,469 shadow decisions and 10,136 aligned three-venue scans. Another 19,163 observations had invalid or unaligned references; 4,154 had invalid SIG quotes/snapshots. Coverage limitations matter.

| Reference rule | 5-minute proposals / measured | Mean buffered change per share | 15-minute proposals / measured | Mean buffered change per share |
| --- | ---: | ---: | ---: | ---: |
| Both bids | 17 / 9 | -0.02521 | 12 / 9 | -0.02520 |
| Kalshi bid | 78 / 44 | -0.02598 | 50 / 33 | -0.02575 |
| Polymarket bid | 109 / 61 | -0.02538 | 68 / 32 | -0.02544 |

No measured candidate in these samples had a positive short-horizon buffered outcome. Each single-reference variant had only nine measured proposals paired with the both-bids baseline; paired differences were zero. Five-minute gap/future-SIG-change correlations were 0.0216 for Kalshi and 0.0257 for Polymarket. There were no usable recent-move observations under the report's timing requirements. These data do not establish a reference leader.

These are hypothetical displayed ask-to-future-bid outcomes after two buffers, not fills, realized profits, independent portfolios or evidence about holding to election resolution. Proposals, races and days are correlated. Outcomes missing within their measurement windows are excluded, not treated as zero. The short horizon strongly exposes spread and buffer costs. This is insufficient support for loosening entry gates or choosing a single venue live; it does not prove the overall strategy loses money.

## Exit-depth comparison

There were 9,574 exit-shadow events and 8,477 valid comparisons. All three policies produced zero qualifying proposals. The legacy route rejected 5,796 for price not converged and 2,681 for reference ask depth. Route-specific depth and SIG-only depth both rejected all 8,477 for price not converged. Removing the external-depth check alone did not create exits in this sample.

This study explicitly excludes the live profit-target/trend route. It is not an evaluation of the profitability or frequency of those trend exits. Their behavior must be evaluated separately before tuning exit timing.

## Next increments

1. Completed here: hourly VoteHub collection, reviewed initial candidate bindings, versioned inputs, quarantine and collection-health reporting. Independent probability estimates remain research-only.
2. Broaden source-checked coverage and add independently sourced fundamentals (partisan lean, incumbency, national environment); evaluate out-of-sample calibration and uncertainty.
3. Build a replay with separate candidate portfolios, cash/exposure limits, partial fills, costs and conservative liquidity assumptions. Current short-horizon price studies cannot choose a deployable portfolio strategy.
4. Use that replay to evaluate adaptive edge thresholds, SIG-depth entry sizing and reference-selection variants. Promotion requires measured performance and coverage, not a target trade count.
5. Finish settlement reviews before multi-contract activation, and verify competition closing valuation before deadline-aware allocation.

The collector is one implementation increment; these later strategy items are not claimed complete.
