# Partial risk-reduction exits

This optional exit reduces a losing holding before profitable exit conditions appear. The local configuration enables the user-selected five-cent loss threshold and 25% maximum sale. The distributed example keeps it disabled. It applies to enabled, mapped bot-owned YES and NO holdings, including model-origin holdings.

## Trigger and size

The trigger is the remaining position's average acquisition cost, including entry buffers, plus the configured exit buffer, minus the current executable SIG bid. It must be at least `0.05` coins per share. A five-cent loss is an absolute price difference, not a five-percent return.

Three qualifying normal scan observations must be at least 60 seconds apart and span at least 120 seconds. History is retained for 30 minutes; a gap longer than 10 minutes breaks the run. A valid observed price rebound below the loss threshold breaks the run, including a rebound during preflight. Preflight and reservation checks cannot manufacture confirmations. A fill or policy/pin change requires new history. Sampling follows the existing scheduler, so confirmation can take longer than two minutes.

Each order is capped at the smallest of:

- 25% of the current holding, rounded down to whole shares;
- current top-bid depth, supported by the qualifying observations;
- the existing per-order notional and share caps;
- existing portfolio exposure headroom for selling.

A holding below four shares produces no risk-exit order. A sale does not round up to full liquidation. The order uses the current SIG bid as its limit; execution is not guaranteed. A confirmed positive risk-exit fill starts a 30-minute cooldown for that contract. Partial fills count; zero fills do not. The next risk sale needs new confirmations after the cooldown. Ordinary exit rules can still act during this risk-exit-specific cooldown.

Example: 100 shares bought at 0.60 with a 0.01 entry buffer have average buffered cost 0.61. With a 0.01 exit buffer, a bid of 0.57 represents a five-cent loss. After confirmation, at most 25 shares can be sold, subject to the other caps. A later recovery cancels the pending decision at preflight.

## Independent SIG exit path

The bot first establishes a persistent SIG contract binding from a successful existing full contract-fingerprint check. This initial verification needs the mapped external metadata. Once bound, a qualifying risk exit uses fresh SIG market metadata and a fresh SIG book without requiring Kalshi/Polymarket metadata or prices. A closed market, changed SIG contract text, or changed mapping pin blocks the independent exit until verified again.

This is a separate loss-reduction rule. It does not infer a fair value from headlines, predict whether a recovery will occur, or alter the existing trend-based profit-taking behavior. It can realize a loss before a later rebound. The trigger uses average remaining cost; accounting and the logged estimated realized P&L use FIFO lots, so a cheap older lot may realize a gain even when the whole position is below average cost.

## Existing controls and recovery

STOP, hard clock checks, account and tournament checks, news/dispute pauses, inventory reconciliation, unresolved or open orders, race cooldowns, and the cumulative realized-loss halt still apply. The portfolio drawdown pause blocks buys but does not itself block this exit. Actual sells can contribute to the realized-loss halt.

Before reservation, the engine checks the fresh contract-check timestamp, quote freshness, current inventory, loss confirmations and sale size again under the reservation transaction. The `risk_exit_orders` audit tag is committed atomically with that reservation. A write timeout retains the original pending reservation and tag; existing recovery must resolve it before trading resumes. A restart does not erase confirmation history or confirmed-fill cooldowns.

The local SQLite journal adds `risk_exit_contracts`, `risk_exit_samples`, and `risk_exit_orders` when enabled. No journal reset or migration command is needed. `risk_exit_check` events explain confirmation, cooldown and size decisions. Actual signals carry `reason: risk_reduction_exit`, and ordinary `order_closed` events report confirmed fills. These are not guaranteed execution or performance forecasts.

## Configuration and activation

```json
"risk_exit": {
  "enabled": true,
  "loss_per_share": ".05",
  "partial_fraction": ".25",
  "cooldown_seconds": 1800,
  "window_seconds": 1800,
  "sample_spacing_seconds": 60,
  "max_sample_gap_seconds": 600,
  "minimum_span_seconds": 120
}
```

Active execution and selling must both be enabled. The maximum allowed fraction is 25%; this implementation cannot be configured to liquidate an entire position in one risk sale.

Stop the existing worker/watcher with Ctrl+C and wait for the shell prompt. From the bot folder, start one supervised live instance:

```sh
python3 -m election_bot watch --live
```

Do not start a second bot process. An existing STOP or unresolved order still requires its normal review/recovery; this change does not clear it.
