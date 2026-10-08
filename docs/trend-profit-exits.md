# Trend-based partial profit exits

The fixed profit trigger is replaced in the local configuration by `execution.profit_exit_mode: "trend"`. It still requires `profit_target_enabled: true`. The starter configuration keeps the feature off and mode `fixed`; missing mode preserves prior behavior. No order, race, daily, portfolio, directional or loss limits change.

A two-cent profit is eligibility, not an automatic sale. Profit must clear both the position's average basis and the actual FIFO basis of the proposed quantity, after configured entry and exit buffers. Those buffers remain estimates. There is no guarantee that a displayed bid will fill or that this strategy outperforms fixed profit-taking.

## Default observations and triggers

History is collected prospectively from normal, successfully validated scans of a held contract. It uses executable outcome bids, not last trades or midpoints. NO positions use complementary books. There is no backfill from earlier snapshots.

| Setting | Default |
| --- | --- |
| Observation window | Last 30 minutes |
| Minimum spacing | 60 seconds |
| Minimum observations | 3 spanning at least 120 seconds |
| Maximum gap between usable observations | 10 minutes |
| Trigger confirmations | Last 2 stored observations, plus a current quote that still satisfies the trigger |
| Partial sale fraction | 25% of remaining holdings, rounded down |
| Trailing bid retreat | 0.02 coins/share |
| Weakening at each reference venue | 0.02 coins/share |
| Remaining gap to both reference bids | At most 0.02 coins/share |

At least one trigger must hold:

1. **Trailing pullback:** the SIG bid is at least two cents below the earlier running high within the usable observation window, in each of the last two observations and in the current quote. Later prices cannot retroactively make an earlier point qualify.
2. **Reference weakening:** both Kalshi and Polymarket outcome bids are at least two cents below their respective first bids in the usable observation window, with the same confirmation requirement. Weakening at one venue alone is insufficient.
3. **Gap narrowed:** SIG's bid is within two cents of the higher of the two reference bids, or above it, with the same confirmation requirement. This requires closeness to both references rather than just the lower one.

With a rising SIG bid, a substantial reference gap and no confirmed weakening/pullback, the bot holds. Flat prices with a large gap also do not cause a sale merely because the profit target was reached. These are explicit heuristics, not an estimated probability of the next price move. The settings are starting choices, not optimized or calibrated thresholds.

For example, bids 0.63, 0.64 and 0.65 with references still around 0.73/0.74 do not trigger a profit sale. Bids 0.69, 0.67 and 0.665 can confirm a trailing sale if the actual shares to be sold still clear the buffered profit target. A single dip from 0.69 to 0.66 is insufficient. A rebound to 0.685 during final preflight invalidates that two-cent trailing trigger.

## Size and execution protections

A trend profit sale is capped at 25% of remaining native holdings, SIG top-bid depth, configured maximum shares, per-order coins and directional headroom. Rounding permits one share for dust positions of fewer than four shares; closing the last share is allowed. The actual FIFO profit check is performed after partial sizing. A thin earlier peak cannot support a trailing sale larger than the liquidity recorded at that peak.

Both reference bids must have at least the existing minimum reference depth (currently 20 shares) in usable history and in the current quote. Reference depth is a quality gate, not a size cap. An unusably thin observation breaks the evidence chain. Long gaps and expired history require rebuilding it. Preflight quotes can invalidate a trigger but cannot manufacture additional spaced confirmations.

All existing source/local freshness, spread, agreement, contract, news, inventory, clock, STOP, pending-order, account and risk checks remain. A stale reference can still prevent an exit. The final preflight recomputes the decision and quantity. The reservation transaction checks both FIFO profit and that confirmed inventory has not changed since the trend evaluation.

The old **overpriced exit remains an independent risk exit** and may realize a loss. It keeps its original sizing and gates. Profitable convergence exits, however, also require trend confirmation and partial sizing in trend mode, so they cannot bypass this change.

## State and restart behavior

A small indexed `exit_trend_samples` table is added to the existing journal on startup in trend mode. No execution records are rewritten. Samples are bound to the contract fingerprint, outcome side, trend/strategy settings, profit threshold and latest nonzero confirmed execution for that exchange. Restart preserves matching, recent evidence; contract/settings changes and any filled buy or sell require fresh evidence. Zero fills do not reset the position epoch. Expired samples are pruned during new sample collection; history is bounded by the configured window and spacing.

The ordinary order cooldown and the existing 30-minute race-wide post-sale pause on new buys remain in place. Further partial profit sales can occur once new trend history qualifies. This prevents repeated trims driven solely by pre-sale observations, but it does not guarantee fewer trades or improved returns.

## Observability and rollout

The current local configuration selects trend mode; restart the running bot to load it. It initially needs at least three qualifying spaced observations for each holding, which may take longer than two minutes when scans or references are delayed. It does not automatically liquidate on startup.

Use `python3 -m election_bot diagnostics` for reasons such as `trend_history_insufficient`, `trend_depth_insufficient`, `hold_supported_rise`, and `hold_no_confirmed_exit`. `decision.exit_check.trend` includes sample count, span, current bid, peak, remaining reference gap, and confirmed triggers. `trend_profit_target` denotes a proposed profit sale; confirmed quantities appear in `order_closed` and aggregate sells/realized buffered P&L in `performance`.

The existing `exit-study` still compares only the three original depth policies. Its results do not validate this strategy. Review actual sells, remaining positions, subsequent bids, costs and foregone upside before changing thresholds. More orders alone are not success. Reverting `profit_exit_mode` to `fixed` restores the standalone threshold after restart; disabling `profit_target_enabled` also requires reverting mode to `fixed` and retains only the original exit rules.

When `profit_target_enabled` is true and `profit_exit_mode` is omitted, trend mode is selected. Explicit fixed mode remains available for deliberate configuration. The starter config leaves profit targets disabled and omits the mode, so enabling the flag alone cannot silently select fixed exits.
