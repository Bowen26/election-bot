# Patient entry and candidate ranking

This increment enables a bounded live execution experiment when configured. It does not promote model rotations, widen trading thresholds, raise portfolio caps, or add polling sources.

## Local pilot configuration

```json
"patient_entry": {
  "enabled": true,
  "allocation": "500",
  "pilot_fraction": ".1",
  "control_fraction": ".5",
  "per_order": "25",
  "max_shares": 50,
  "improvement_ticks": 1,
  "min_sig_spread": ".01",
  "wait_seconds": 5
},
"entry_ranking": {"enabled": true}
```

The pilot samples reference-entry opportunities only; independent-model buys and sells are not repriced. Sampling is a deterministic hash of contract and durable order count; assignment within the sample uses attributed attempt count, including non-fills. Counts change only upon reservation, so scan/preflight/reservation produce the same assignment unless another committed order intervenes. It is not a randomized causal trial. The sample percentage is a target, not a guaranteed fraction of actual fills.

Both sampled arms use the same capped quantity, never increasing the ordinary quantity. Unsampled entries retain ordinary sizing. A patient quote improves the current ask by one tick, clamped at the current bid. It must have a two-sided book, adequate spread and the original qualifying reference signal. A fallback before submitting any order can use the normal control price and is reported separately. Once submitted, a patient order is never repriced or followed with an immediate ask-taking replacement. After the patient gross allowance is exhausted the ordinary route resumes; proceeds do not recycle this allowance. Sampled controls spend the existing ordinary budget, not the separate 500-coin improved-price allowance.

## Known-order lifecycle

Only a newly acknowledged, durably tagged, identity-checked patient order can enter the bounded wait. The wait stops on closure, signal invalidation, STOP or its five-second local deadline, and never runs beyond the original expiration minus a two-second local margin between requests. There is no background order manager or overlapping order submission. Market/account requests can outlast the local deadline; SIG's original order expiration still applies.

Signal monitoring retains quote freshness, reference agreement/depth, news, drawdown, position guard and reentry checks. An error or KeyboardInterrupt attempts cancellation, verifies closure and records actual partial fills before propagating. If cancellation, identity or fill verification fails, the reservation remains unresolved and ordinary recovery applies. Recovering/restarting never starts a new patient wait. Unknown POST outcomes are never resubmitted with a new key. The synchronous wait can delay scanning/exits by its wait and network time; the small pilot is intended to measure this tradeoff.

Paper mode records zero patient fills because there is no queue model. Automated tests use fake brokers and do not prove live fill quality or server expiration behavior.

## Ranking

Exits are handled during scanning before deferred buys. At the end of the batch, buy candidates that remain fresh are ordered by quantity × (signal reference value − price − two cost buffers), then surplus per purchase coin. Patient candidates use the ordinary ask for their score to avoid giving the treatment arm an artificial priority boost. Inputs are uncalibrated reference/model signals and need not be comparable expected probabilities. The score is an ordering heuristic, not expected profit.

Each candidate repeats account, metadata, inventory, reference and SIG checks. New direction/entry-route changes reject the queued buy; a newly justified exit may proceed. The final reservation still checks spending, exposure, cooldown and route constraints under its write lock. Ranking does not refresh stale candidates to manufacture eligibility; early quotes in slow batches may expire. It does not find a globally optimal allocation or rerank all other contracts after each fresh quote.

## Report and activation

From the bot folder:

```sh
python3 -m election_bot patient-study --json
```

The report reads existing SQLite data without network or writes. Pending reservations are not classified as failed fills; closed zero-fill reservations include cases where no POST was attempted. Each order's own quoted ask is the benchmark for improvement. Post-fill bid movement is measured relative to its own decision-time bid. Missing old benchmarks stay missing. Cohorts differ in race, timing and fill selection, so differences are descriptive; no count threshold or positive mean automatically promotes the pilot.

After installation, stop the current watcher with Control+C, wait for it to exit/reconcile, then start exactly one watcher:

```sh
python3 -m election_bot watch --live
```

To roll back, set `patient_entry.enabled` and/or `entry_ranking.enabled` to false and restart. Preserve the journal and any pending reservations; recovery must still resolve owned orders. Existing open positions and selling controls are unchanged.
