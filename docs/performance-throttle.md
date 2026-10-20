# Prospective route performance throttling

`performance_throttle.enabled` defaults to false. Collection and read-only assessment run in report-only mode; setting it true allows size reductions after the evidence requirements are met. This is not a learned alpha model or proof of profitability.

## Evidence and decision

At reservation, each new buy records a durable route (`reference`, `patient`, or `model`), race, contract fingerprint, side and contemporaneous SIG bid/ask. This happens in the same transaction as the order reservation. Failed reservations leave no tag. Older untagged fills are never retrospectively assigned or used for this control.

The assessment uses the existing one-hour hypothetical liquidation observations, including their 15-minute observation window. It considers the most recent two complete UTC entry days whose entire observation windows have ended. It takes the first filled buy per route/race at least one hour after the previous retained sample; selection does not depend on its outcome. This reduces overlap but does not make election outcomes independent.

Each day must have at least 20 measured observations across at least five races, and at least 70% measured coverage among eligible spaced fills. Missing or invalid marks remain unmeasured. The daily averages weight retained filled orders equally, not by contract quantity. Buffer-adjusted markout is hypothetical exit P&L per share. Bid movement adds the entry price and both buffers back, then subtracts that order's own decision-time SIG bid. That isolates movement from initial spread/buffer drag; the benchmark precedes the fill and includes any waiting interval.

Both days must have mean buffered markout at or below -0.02 coins/share **and** mean bid movement at or below -0.01. Only then is the route's suggested new-buy quantity multiplier reduced to 0.5. One-share orders retain the minimum one share. Sells are never scaled. Existing entry/exposure/cash limits remain in force; the final reservation rechecks the multiplier under its write lock. Ranking uses the reduced quantity.

A reduced multiplier persists through restarts and insufficient/mixed evidence. It returns to 1 only after two ready days each show nonnegative mean buffered markouts and nonnegative bid movement. It never increases an order beyond ordinary sizing. It does not reset the portfolio drawdown high-water mark or any spending allowance. If an enabled control cannot refresh its assessment or its saved assessment is older than ten minutes, new buys are rejected while exits remain available.

The 20-observation/five-race requirements and thresholds are operating rules, not statistical significance tests. Correlated races, fill selection, quote coverage, estimated buffers and changes in signal mix can bias results. The state records its fixed policy version; a policy change requires a reviewed migration rather than silently relabeling existing evidence.

## Operation

```sh
python3 -m election_bot performance-throttle
python3 -m election_bot performance-throttle --json
```

Add `--paper` for the separate paper journal. These reports do not update state or make network requests. `assessment` is a fresh read-only calculation; `stored_state` is what the worker last wrote. The enabled flag describes the file, not proof that a running worker loaded it.

Restart the watcher once to collect prospective tags. In report-only mode no sizing changes occur. A later activation requires `"performance_throttle": {"enabled": true}` and one restart. The initial live assessment cannot use older fills to skip the two complete days of prospective evidence.
