# Performance by entry strategy

Run from the bot folder, while the worker continues trading:

```sh
python3 -m election_bot strategy-performance
python3 -m election_bot strategy-performance --json
python3 -m election_bot strategy-performance --paper
```

This command opens the selected journal read-only in one consistent SQLite snapshot. It needs no API key or network access, creates no journal when none exists, and submits no orders. It works immediately after installation without restarting the trading worker. If a configuration file exists, it reads the current model allowance settings; these do not establish which configuration a running process loaded.

## Confirmed accounting

The report separates purchases with a durable `model_orders` tag from `other_entries`. Older untagged purchases remain in the second group; historical signals or cached research forecasts never retroactively reclassify them. An older journal without the model table reports attribution unavailable and does not create the table.

Both groups show filled buy orders, purchased shares, gross purchase cost including the entry buffer, sold shares, net sale proceeds after the exit buffer, released cost basis, realized buffered PnL, and remaining shares and cost. Full JSON includes these figures by race. Unknown legacy race identities fall back to `exchange:<id>`.

A sale belongs to the entry lots it actually consumes under the main ledger's FIFO convention. If one contract contains purchases from both routes, a partial sale can split across the routes. Equal execution timestamps use SQLite row insertion order, matching the ledger. This avoids assigning an entire sale to the latest signal. The sum of route-level open cost and realized profit must reconcile with the ordinary ledger or the report fails.

Pending reservations and zero-fill orders are separate from filled trades. Closed orders missing a confirmed execution are flagged as incomplete accounting, rather than silently treated as zero fills. Broken model/race associations, orphan model tags or oversells fail the report. The command does not repair accounting.

All reported costs and profits use the configured per-execution cost buffers, which are estimates rather than verified exchange fees. Unsold holdings are reported at cost; no unrealized PnL or current account equity is inferred. FIFO attribution describes the shared live portfolio, not two independent strategy simulations or a causal test of which route is better.

## Measurement and allowance

Existing one-minute, five-minute, fifteen-minute, one-hour and one-day markout observations are grouped by entry route. These are hypothetical full-size liquidation outcomes, separate from realized profit. Missing, pending and not-yet-due outcomes retain their existing meanings. Short horizons cover only the historical period in which that tracking was enabled. Never sum horizons or treat repeated buys in one race as independent evidence.

The model allowance reports gross confirmed model purchase cost plus pending reservations against current total, UTC daily and per-race limits. Daily usage follows the original reservation date. Sales do not recycle the pilot allowance. Per-race next-order ceilings are budget ceilings only: they do not establish evidence coverage, liquidity, available account cash or permission to submit. Portfolio controls still apply separately.

The last 24 hours of retained model-decision events show statuses, blocking reasons and input errors, separately for scan and preflight. These are repeated observations, not unique opportunities or rejected orders. Many scans concern markets outside the reviewed polling catalog; `model_race_unmapped` is expected there. A recent model event does not prove the process is still alive. The report makes no automatic changes to thresholds, position sizes, priors or exits.

## Next strategy work

Use this attribution alongside the existing independent forecast and low-price studies to choose testable changes. Broader reviewed polling coverage, independently sourced national environment and incumbency inputs, election-outcome calibration, and a portfolio replay with cash and exposure constraints remain separate work. This report does not claim to implement those models or validate the uncalibrated pilot's profitability.
