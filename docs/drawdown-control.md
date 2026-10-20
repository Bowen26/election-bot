# Portfolio drawdown control

This is a buy-only pause. It includes unrealized losses in addition to confirmed realized profit/loss, without placing liquidation orders. The installed local configuration enables it; the example defaults to disabled.

```json
"drawdown_control": {
  "enabled": true,
  "pause_coins": "2500",
  "resume_coins": "1250",
  "max_mark_age_seconds": 300,
  "refresh_books_per_cycle": 8
}
```

New buys pause at drawdown **greater than or equal to 2,500 coins**, and resume only after a complete valuation shows drawdown **strictly below 1,250 coins**. Both ordinary external-price entries and independent-model entries pass this gate. Sales remain eligible under their existing conditions. Other guards, including STOP, disputes, reconciliation, account mismatch and the existing realized-loss halt, can still prevent trading or exits.

## What is measured

Portfolio P&L is cumulative confirmed realized P&L after recorded cost buffers, plus the estimated proceeds from selling all bot-owned shares through the recorded SIG bid depth, less their remaining FIFO cost (including entry buffers), less the configured exit buffer. NO positions use the complemented SIG book. Realized and unrealized losses are not counted twice.

The persisted peak is the greater of zero and the highest complete portfolio P&L measured by this control. Drawdown is peak minus current portfolio P&L. This is a peak of buffered trading P&L, equivalent to a hypothetical equity baseline plus that P&L; it is **not** the SIG website's account mark, available cash, or a peak reconstructed from before installation. Existing cumulative realized P&L and existing holdings are included from the first complete observation. External manual holdings or deposits are not added to this calculation; existing inventory reconciliation still applies.

`drawdown_state` retains the peak and pause across restarts. Disabling/re-enabling the feature or running `resume` does not erase them. A changed exit-buffer assumption is rejected rather than silently mixing valuation definitions. Changing the configured pause/resume thresholds preserves the peak. If all positions have been sold and realized P&L remains below the recovery threshold, the pause cannot recover through further trading; that situation needs explicit risk-policy review, not an automatic state reset.

## Freshness and incomplete evidence

All bot-owned holdings must have complete bid depth and a valuation no older than five minutes. Marks record the side, quantity, FIFO cost and latest execution sequence. Any fill invalidates older marks for that contract, even if a later round trip restores the same quantity and cost.

Missing, stale, future-dated, partial-depth or changed-inventory marks block new buys. Missing prices are never zeroed and cannot raise the peak or clear a persistent drawdown pause. A missing-valuation block can clear when complete marks arrive; it is distinct from the hysteresis used after a measured drawdown breach.

Normal scans and performance reads feed the valuations. Up to eight additional SIG-only book reads per cycle refresh holdings, including holdings not enabled for normal entry scans. Refresh begins when marks reach half their allowed age. Attempt timestamps rotate work so repeated failures on one contract cannot permanently starve the others. A failed refresh can retain a previous valid mark only until its age limit; insufficient current depth replaces the prior mark and blocks buying. STOP interrupts refresh work. Kalshi/Polymarket outages do not prevent this measurement, although existing sell rules can still require their quotes.

This is an asynchronous valuation assembled from books observed at different times, with at most five minutes of accepted age. It is not a guaranteed loss ceiling or simultaneous executable liquidation quote. Price moves, removed liquidity, API failures and an order already in flight can take drawdown beyond the threshold. The gate rechecks current inventory, saved mark validity and drawdown inside the order-reservation transaction before any new buy. A pause first detected there persists after the rejected reservation rolls back.

## Operation

Stop the existing supervisor with Control-C and wait for it to exit, then start one worker from the bot folder:

```sh
caffeinate -i python3 -m election_bot watch --live
```

Initial valuation may take several batches. During warmup, new buys remain blocked while existing exit checks continue. Restarting the supervisor does not reset risk history.

In a separate terminal, inspect without orders or database writes:

```sh
python3 -m election_bot drawdown-status
python3 -m election_bot drawdown-status --paper
```

The command uses the current configuration file and retained marks. It does not update the persisted peak, change pause state, clear STOP or prove the worker is alive. `not_initialized` means the upgraded worker has not created the tables yet. Worker events named `drawdown_control` expose measured P&L, peak, drawdown, missing marks and pause state. `drawdown_mark_skip` records failed refreshes. Decision diagnostics and reservation rejections explain blocked buys.

This increment does not introduce a stop-loss sale, a declining-position entry rule, capital rotation, or any change to the profit-taking strategy. Those are separate follow-up implementations.


## Recovery visibility

The peak is a persistent high-water mark, not a rolling or decaying peak. There is no timeout. A paused portfolio can remain paused indefinitely, especially if it has crystallized losses and no open positions remain to recover. Exits stay eligible under their existing controls; selling alone does not reset the peak or erase realized losses.

Status now explicitly reports `peak_policy`, `automatic_timeout`, `resume_requires_portfolio_pnl_above` and `recovery_gap_coins`. The target is a strict inequality: with a 3,000 peak and a 1,250 resume threshold, portfolio P&L must rise **above 1,750**. At exactly that target the remaining gap is zero but buying remains paused. Incomplete marks leave the recovery gap unknown. These fields do not modify the pause/resume policy or reset existing history.

Changing to a decaying peak or timed resumption would permit new buys without recovery and requires a separate policy decision. A cap proportional to deployed capital has different behavior as positions are sold or capital is deployed; it is not an automatic substitute for this high-water rule. The existing approved 2,500/1,250 coin thresholds remain in place.
