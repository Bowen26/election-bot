# Entry gate visibility

Every active-engine cycle now emits `scan_complete` with `gate_counts`, `gate_counts_by_phase`, `contracts_visited` and `status`. Counts reset each cycle. Early open-order waits, STOP returns and raised errors also produce a summary; halted summaries include the exception type and preserve the original exception/recovery behavior. Successful scans keep their existing portfolio summary fields.

A gate is counted once per contract, gate name and phase within the cycle, regardless of whether both YES and NO were rejected. Global gates count once per phase. Scan, preflight, pre-submit and reservation blocks are separated. Counts overlap: a race can lack a reference-price gap and also be drawdown-paused. A failed reference route can be followed by a successful independent-model entry. `no_eligible_signal` records the final no-signal decision separately. These are observed route blocks, not mutually exclusive reasons or a count of lost profitable trades. Gates not reached after an earlier return are not evaluated just to populate statistics.

Named gates cover race cooldown, risk-exit preemption, news pause, portfolio drawdown, position guard, reentry cooldown, reference price/depth, coin limits, exposure, opposite inventory, model inputs/budget/price-depth-inventory, preflight recheck, pre-submit quote validation, open orders, and reservation rechecks. Exit priority is labeled as preemption, not a safety failure. Reservation exceptions carry explicit gate identifiers; messages are not parsed to infer gate names.

Example: `gate_counts_by_phase.scan = {"portfolio_drawdown": 12, "reference_price_gap": 8, "no_eligible_signal": 12}` means 12 contract scans were drawdown-paused and 8 also lacked a reference entry gap. It does not mean 32 orders were rejected. See the existing per-contract `decision` and skip events for exact details. No new quote requests, guard evaluation, guard ordering or gate bypass is introduced.

Restart the existing watcher once to load the new instrumentation. Historical events are not retroactively assigned counts.
