# Profit-target exits and post-sale reentry pause

The October 7 exit study found no additional proposals from either reference-depth alternative: price convergence still blocked them. Increasing depth alone therefore would not have created sells in those observations. This change adds an independent profit-taking route; it is a deliberate strategy change, not a proven increase in returns.

The local configuration enables `execution.profit_target_enabled: true`, `execution.profit_exit_mode: "trend"`, and `execution.reentry_cooldown_seconds: 1800`. The standalone fixed threshold described below is the underlying profit eligibility calculation; live trend mode adds confirmation and partial sizing, as described in [trend-based partial exits](trend-profit-exits.md). The starter config keeps profit-target exits off. Configs with profit targets disabled preserve their old behavior. Enabling `profit_target_enabled` without specifying `profit_exit_mode` now selects `trend`; the standalone fixed threshold requires an explicit `profit_exit_mode: "fixed"`. A restart is required to load changes; installation does not restart the bot or submit orders. Turning the flag off removes the new profit-target route; the separately configured post-sale pause continues to apply.

## Order selection

In fixed mode, existing overpriced and profitable-convergence exits retain priority and their original requirements. If neither produces a valid sale, the fixed route checks the following. In trend mode, overpriced exits remain independent but all profit-taking routes, including convergence, use the added trend gate.

- A fresh SIG bid for the actual held YES or NO outcome.
- The bid minus the configured exit buffer must exceed both the whole position's average purchase cost and the actual FIFO cost of the proposed sale by at least `execution.take_profit_min` (currently 0.02 coins/share). Purchase costs already include their entry buffer. These are estimated costs, not verified venue fees.
- Size is limited by SIG's best bid depth, owned shares, exposure headroom, the maximum shares per order, and the existing per-order coin limit. It does not sweep deeper price levels. Reference depth does not limit this route's size because the order executes only on SIG.

Both external books must still pass the existing freshness, two-sided-book, spread and agreement checks. This route removes the reference-convergence requirement, not data validation. External-feed failures and stale timestamps can still block exits. Contract identity, account inventory, news pauses, unresolved orders, STOP, clock and portfolio controls also remain in force.

For example, shares purchased at 0.60 with a 0.01 entry buffer have a 0.61 basis. A SIG bid of 0.64 clears the 0.01 exit buffer and 0.02 profit threshold even if reference bids remain at 0.73. If the oldest FIFO lots cost more, their cost can still prevent this sale. The engine does not search for a smaller size solely to select favorable lots.

The existing forced preflight recomputes the full decision from new books and positions. Under the reservation transaction, FIFO profitability is checked again for both profit-target and convergence sales. Actual partial fills and realized P&L still come from confirmed SIG execution reports. An overpriced exit can still realize a loss under the original policy; the new profit-target route cannot intentionally do so under its buffered estimate.

## Avoiding immediate repurchases

Any confirmed nonzero sale starts a 30-minute pause on new buys across that race, including disabled or historically bound sibling contracts. It applies to either outcome, survives restarts through confirmed execution timestamps, and is checked again under the atomic reservation guard. A zero-fill order does not start the pause. Further sells remain eligible, subject to the ordinary order cooldown and all existing checks. Each subsequent filled sale restarts the reentry timer.

After the pause, new buys can qualify under the original entry rules. This pause reduces rapid churn but does not guarantee that later reentry will be beneficial. No budgets or share exposure caps are raised.

Confirmed sales release the cost associated with the shares sold from the open-risk budget, subject to realized-loss accounting. They do not reset daily gross-buy spending or expand risk allowances with profits.

## Observability and limits

`decision.exit_check` records `profit_target`, the legacy rejection reason, the average/FIFO target, and the hypothetical per-share profit. `reentry_wait_seconds` and entry check reason `reentry_cooldown` explain post-sale pauses. `signal.reason: profit_target` identifies an intended new-route order; `order_closed` records confirmed fill quantity. A proposal or signal is not a confirmed sale.

Use `python3 -m election_bot performance` and `python3 -m election_bot diagnostics` to review actual sells, realized buffered P&L, and exit blockers. `exit-study` continues to compare only the three original depth policies, recomputed without profit-target exits. It must not be interpreted as a backtest of this new policy. Earlier selling can forgo later gains; more trades alone are not evidence of better performance.
