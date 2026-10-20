# Independent fair-value live pilot

The active engine can buy SIG competition contracts from the configured independent polling forecast when `model_trading.enabled` and `acknowledge_uncalibrated` are both true. The example configuration defaults to disabled. The installed local pilot enables both flags. Active execution, selling and public polling collection must also be enabled.

This uses the existing `poll_margin_v1` estimate, with its validated primary prior (neutral unless a custom primary prior was supplied). The historical-lean comparison remains research-only. Model probabilities and the two-point sensitivity range are uncalibrated; the lower sensitivity value is not a statistical confidence bound and does not establish a profitable trade.

## Allowance and eligibility

| Control | Pilot setting |
| --- | --- |
| Lifetime gross model purchases plus pending reservations | 1,000 coins |
| Gross model purchases per UTC day | 250 coins |
| Lifetime gross model purchases per race | 250 coins |
| Per order, including entry cost buffer | 25 coins |
| Maximum shares per order | 50 |
| Minimum modeled edge after entry and exit buffers | 8 cents |
| Eligible SIG ask | 10–90 cents |
| Maximum SIG spread | 4 cents |
| Maximum fraction of displayed SIG bid and ask depth | 25% |
| Maximum age of successful polling collection | 2 hours |

These are sublimits within existing coin, cash, net-share, office, region and loss limits. Partial or zero fills release the unfilled reservation; confirmed purchase cost remains counted even after a sale. Selling does not replenish this pilot's lifetime allowance. Restarting does not reset it. Daily accounting uses the order reservation's UTC date, consistent with the main journal. Unknown submissions retain their reservation until reconciled.

Only already enabled Senate mappings with matching reviewed polling evidence, race identity, contract fingerprint and signed party orientation qualify. Gross-exposure multi-contract races are excluded. The polling filters still require at least two independent pollster families and sufficiently recent eligible polls. A fresh collector timestamp alone cannot refresh old polling evidence. YES and NO are supported, but an opposite-side buy cannot close or flip existing inventory.

## Decision and execution

Existing exits have first priority, followed by existing external-price entries. If neither produces an order, the model route can consider a buy without requiring a discount to Kalshi or Polymarket. It still requires healthy, fresh reference books within the configured spread and disagreement limits. Model quantity depends on SIG liquidity; external depth does not set model quantity. It avoids entries already meeting the ordinary external-overpricing exit condition.

For YES, the entry value is the forecast's lower sensitivity value. For NO, it is one minus the upper YES sensitivity value. Subtract the SIG ask and both cost buffers; the result must exceed the greater of the model and ordinary entry thresholds. The pilot buys only at the best ask, with the existing limit-order expiration and cancellation behavior.

Forecasts are recomputed during scanning, preflight and the reservation transaction. The final check rereads polling evidence and collector health and rechecks quote age, budgets and exposure. Existing clock, contract, account, news, STOP, cooldown and unresolved-order checks continue to apply. A model input failure blocks model entries; it does not authorize a fallback model trade or disable ordinary market-price trading.

Existing inventory-aware exits, including trend-aware profit taking when configured, manage the resulting holdings. This increment adds no model-driven sell or stop-loss rule. The model can be wrong, especially with sparse polls and systematic polling bias; the edge threshold is an assumption, not measured expected profit.

Each model reservation writes a durable `model_orders` row in the same transaction as the pending order. It includes the model version, forecast, source hashes, fingerprint and allowance settings. `model_decision` journal events explain eligible or blocked scans. A submitted signal uses `reason: model_fair_value`. Cached research events never authorize an order. Fake-broker tests cover fills, partial fills, zero fills, timeouts, budget rollover, input changes and reservation rollback.

## Loading the change

From the bot folder, stop the existing supervisor with Control-C and wait for it to exit, then run:

```sh
caffeinate -i python3 -m election_bot watch --live
```

Start only one supervisor. A running Python worker must restart to load the code and configuration. This installation does not itself submit orders or restart the worker. To disable new model entries, set `model_trading.enabled` to false and restart; attribution and existing positions remain available for normal accounting and exits.

## Measuring this pilot

`python3 -m election_bot strategy-performance` separates confirmed model purchases, FIFO-attributed realized profit, remaining cost, pending reservations and unused pilot allowance from other entries. Use `--json` for per-race figures and full markout coverage. It also explains recent model blocks by scan/preflight phase. The [report methodology](strategy-performance.md) describes mixed-route inventory, cost assumptions and limitations. Running the report requires no worker restart.


## Tail sensitivity and optional execution haircut

The polling model's `yes_low`/`yes_high` are two-margin-point sensitivity scenarios, not calibrated confidence bounds. In the normal distribution's tails, their probability distance from the point estimate becomes tiny and can eventually round to zero. There is no universal +18-point collapse boundary; it depends on predictive uncertainty. More numerically stable tail arithmetic alone would not provide a meaningful probability haircut.

The live adapter now supports `model_trading.minimum_probability_haircut`, an absolute probability deduction between 0 and .25. The backward-compatible default is **0 (disabled)**. With an explicitly selected .05 setting, each purchase-side execution bound is `max(0, min(original_sensitivity_bound, side_point_probability - .05))`. YES and NO are handled symmetrically. Existing stronger sensitivity deductions remain intact, and both entry and exit cost buffers still apply afterward. This is an operational conservative rule, not a fitted confidence level or a revised forecast.

`model_decision` diagnostics and reserved model-order attribution now include point probability, original sensitivity bound/haircut, effective execution bound/haircut and whether the floor binds. The same calculation runs at scan, preflight and under the reservation lock. The primary research forecast and historical comparisons remain unchanged. Existing configurations retain their current behavior until a nonzero haircut is selected; no pilot budget increase accompanies this option.
