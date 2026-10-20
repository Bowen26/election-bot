# Capital-rotation study

This first stage records prospective comparisons between retaining part of a bot-owned position and selling that part to buy another race. It cannot place orders, cancel orders, reserve funds or change positions. The local configuration enables collection after a worker restart; the example configuration disables it.

## Collection and candidate selection

The observer uses books already fetched during normal scans. It adds no API requests. Both races and all reference books must still satisfy the configured freshness, spread and reference-disagreement checks at comparison time. A fill invalidates the in-memory candidate cache. Preflight may provide later outcome observations but never creates proposals. No old snapshots are backfilled as decisions.

The source must be owned; the destination race must be unowned. Same-race switches and gross-exposure contracts are excluded from this first version. Global buy pauses, unresolved reservations, disabled mappings, news pauses, race cooldowns and post-sale reentry cooldowns block candidate collection. Existing clock, account, contract and live decision checks run before the observer. A paused position-addition rule is not treated as a prohibition on selling the source; no destination additions are simulated.

The reference value is the lower aligned external bid from Kalshi and Polymarket. It is a comparison signal, **not a calibrated fair value, expected return or a guaranteed exit price**. The independent polling model is not used for rotation ranking in this increment.

For each pair, the proposed source sale is limited to 25% of the holding, 25% of displayed top-bid depth, the existing per-order notional/share limits, and sell-side exposure headroom. Whole-share rounding can leave small positions ineligible. The destination size is limited by net sale proceeds, available cash after that sale, daily gross-buy allowance, post-sale total/race capital, post-sale directional exposure, per-order/share limits and 25% of displayed SIG bid/ask depth. Source sale accounting uses a copied FIFO ledger; realized losses continue to consume capital. The daily gross-buy allowance does not recycle. A simulated sale that reaches the realized-loss halt cannot finance a new purchase.

The target must pass the ordinary minimum reference gap **after both entry and exit buffers**. Candidate scoring compares:

- Hold proxy value: source shares × (source reference bid − future exit buffer).
- Rotate proxy value: target shares × (target reference bid − future exit buffer) + unspent sale proceeds.

Net source sale proceeds already subtract its sale buffer; the destination purchase already includes its entry buffer. The improvement must exceed 3% of net sale proceeds, an experimental threshold. The largest qualifying coin improvement involving the newly scanned contract is recorded. Both involved races then have a persistent 30-minute study cooldown, including across restarts. This cooldown affects research only.

The proposal records input snapshots, source/target sizes and prices, buffers, FIFO cost and hypothetical sale P&L, copied limits, proxy improvement, and leftover cash. `currently_fundable_without_sale` identifies cases where existing live available capital could already pay for the target. Such a proposal is not evidence that selling is necessary.

## Forward comparison

At 15 minutes and one hour, the first fresh SIG observation for each leg supplies a hypothetical liquidation value after its exit buffer. The corresponding windows allow delays of three minutes and 15 minutes. Both legs must have enough top-bid depth for their proposed quantities, and their observation times must be within two minutes of one another. A first valid but thin quote stays marked insufficient; a later favorable quote cannot replace it. Invalid/stale quotes do not count as observations.

The reported difference is:

`target liquidation value + leftover cash − liquidation value of retained source shares`

This compares equal starting source inventory. Initial acquisition cost is common to both choices and is not counted twice. Later live fills do not alter this independent hypothetical comparison. Pending, not-yet-due, missing, thin and unaligned observations are reported separately. Missing data is never counted as zero profit.

The report shows mean incremental coins per measured comparison and counts of rotation/holding wins, without summing proposals into a supposed portfolio profit. Proposals overlap in time and may reuse the same live holdings. This is **not a cash-constrained portfolio replay**, evidence of actual fills, or proof of profitability. It assumes the displayed proposed quantities fill at the recorded bid/ask; sequential-leg latency, queue priority, intervening price moves and execution failures are not simulated. A future live rotation must confirm the sale and refresh the purchase before submitting it.

Coverage is conditional on normal scanning. Sparse polling across races, reference outages, persistent buy pauses or having no unowned eligible destination can produce zero proposals. Strict freshness is retained rather than comparing stale quotes to manufacture more opportunities.

## Use

Restart the existing watcher once to begin collecting:

```sh
python3 -m election_bot watch --live
```

Stop the existing process with Ctrl+C and wait for the prompt before restarting. Start only one watcher. Read the report in another terminal from the bot folder:

```sh
python3 -m election_bot rotation-study --hours 24
python3 -m election_bot rotation-study --hours 24 --json
```

Use `--paper` for the paper journal. Reports are read-only, require no credentials/network, and never create a missing journal. Lookback is limited to seven days. The activity counters are cumulative collection diagnostics, not counts restricted to the report window. The JSON includes the ten most recent proposals.

```json
"rotation_study": {
  "enabled": true,
  "partial_fraction": ".25",
  "depth_fraction": ".25",
  "minimum_improvement_fraction": ".03",
  "cooldown_seconds": 1800
}
```

No live rotation switch exists in this implementation. Broader polling inputs, model calibration, separate candidate-portfolio replay and evidence-based live promotion remain later increments.


## Independent-model ranking and sequential rotation trials

A separate `independent_rotation_v1` study now runs alongside the original external-bid study when `rotation_study.enabled` is true. It adds no network requests or live orders and uses the configured primary independent polling forecast. It reads validated polling evidence and collector health through the same read-only forecast loader as the live model. It does not consume or enlarge the live model's purchase allowance. Missing/sparse polling, failed collector health and changed bindings cannot fall back to exchange prices or an invented model value.

### Ranking and confirmation

The latest portfolio ranking includes all currently fresh, model-covered contracts in the scan cache, not just comparisons involving the last scanned race. It lists owned positions by upper-scenario remaining value over an immediate sale; prospective buys by lower-scenario edge after both buffers per purchase coin; and fully sized hold-versus-switch candidates by total conservative improvement in coins. Quote/model age is at most 15 seconds (or a stricter configured SIG quote age), SIG spread at most 4¢, and prices must satisfy SIG ticks. This is a partial fresh view, not simultaneous coverage of all enabled races. Ranking age and coverage are shown explicitly.

The **holding** comparison uses the source side's upper sensitivity value, while the destination uses its lower sensitivity value. Both YES and NO are oriented to their own contract. Those scenarios remain uncalibrated, not statistical confidence bounds or predictions of near-term repricing. Remaining opportunity determines ranking; an accumulated profit threshold does not itself request a sale. A high remaining source value can reject a switch even when a destination is independently attractive.

A candidate must improve on holding by the existing rotation improvement fraction (3% of net proceeds by default), after source sale, target entry and future exit buffers. It must qualify on three observations at least 60 seconds apart, spanning at least 120 seconds, with no observation gap over 180 seconds. Both quote snapshot IDs must change before confirmation advances. Polling-evidence hashes, race-version hashes, forecast policy or execution-epoch changes reset the chain. Known reversals and buy/news/race pauses invalidate confirmation. Confirmation survives restarts; the quote cache does not.

### Capital and sequential execution assumptions

The source is bot-owned and the destination race is unowned. Gross-exposure contracts and same-race switches are excluded. Each simulated source sale is limited to 25% of holdings, 25% of top-bid depth and existing order/share/exposure limits. A copied FIFO sale supplies the hypothetical realized P&L and post-sale capital/exposure checks. Cash already committed to live pending orders blocks new trials. Existing buy pauses, race cooldowns and news gates remain collection constraints.

The simulated purchase is funded **only by that trial's net sale proceeds**, also capped at the released source FIFO basis. It cannot reinvest a simulated sale profit beyond that basis. Other trials' same-day purchases and pending purchase allowances count against the current gross daily allowance, alongside actual journal spending. Source and destination races cannot overlap another trial through its longest outcome window (75 minutes), or a longer configured cooldown. These restrictions prevent concurrent trials reusing the same source holdings or simulated daily allowance. They are conservative research constraints, not changes to live accounting.

After confirmation, the study records a hypothetical source sale at the displayed bid. It then waits up to 60 seconds for a **new destination observation with both quote timestamps after that sale**. It cannot buy from the cached quote that justified the proposal. The first such observation rechecks source and destination forecasts, inventory epoch, bindings, buy/news gates, capital, daily allowance, exposure, depth, price gap and hold-versus-switch improvement. It may resize downward to the new executable quantity. Failure or expiration leaves the simulated proceeds as cash; it cannot select a later more favorable quote. A real intervening execution also prevents the pending hypothetical purchase.

The sale itself is an assumed displayed fill, not a real confirmed execution. Actual live rotation would still need verified sale fills, exchange rejection handling and purchase revalidation. No live rotation execution switch has been introduced.

### Forward outcomes and use

At 15 minutes and one hour from the simulated sale, compare the actual simulated destination quantity plus residual cash against retaining the same source shares. Cash-only trials require only the source holding's comparison quote. The first valid post-horizon quote is final, including insufficient depth; exit sizing uses the same 25% top-depth allowance. Two traded legs must be observed within two minutes. Missing, pending, insufficient-depth and unaligned results stay separate from measured coin differences. No old observations are backfilled.

These are **capital-constrained paired trials, not a continuous portfolio backtest**. Later trials use the bot's then-current owned inventory rather than rolling a synthetic full portfolio forward. Do not sum their overlapping horizon means into total profit. Forecasts concern election settlement, which may differ from competition-period marks and exit timing. Displayed fills omit real queue priority, latency, partial fills, impact and exchange failure. The report flags cash-budget-fundable destinations: a profitable-looking switch does not establish that selling was necessary, and the prospective spare-cash comparison below separately checks whether the same purchase can be funded without that sale.

Stop the existing watcher with Ctrl+C, wait for its prompt, then restart once:

```sh
python3 -m election_bot watch --live
```

Read both studies without modifying state:

```sh
python3 -m election_bot rotation-study --hours 24
python3 -m election_bot rotation-study --hours 24 --json
```

JSON exposes `independent_model.latest_ranking`, confirmation counts, purchase outcomes and the most recent trials. The original external-price results remain separate and unchanged. No new key, fundamentals import, live haircut activation or trading-limit change is required. Research tables use the `model_rotation_` prefix in the existing live/paper journal. Frozen study-policy changes require a separate reviewed experiment rather than resetting accumulated evidence.


### Hold, cash and replacement attribution

The report now compares three choices on the same complete 15-minute/one-hour observations: keep the source shares, sell them and retain net proceeds as cash, or follow the simulated rotation policy. Cash earns no interest. All three start with the same source inventory; the cash baseline includes the source sale buffer, and replacement value includes purchase and liquidation buffers.

`allocation_attribution` separates `mean_exit_vs_hold_coins` from `mean_reinvestment_vs_cash_coins`. Their sum equals `mean_rotation_vs_hold_coins` (apart from decimal mean rounding). A rotation can beat holding while still losing to cash: that supports the exit decision, not the destination selection. Cash-only trials have zero reinvestment contribution and are separated from completed purchases. Exact terminal-value ties are reported explicitly.

Results include all measured trials, purchased trials, cash-only trials, and the original proposal's cash-budget-fundable/not-fundable cohorts. The fundability label is a budget screen, not a fully validated direct-buy alternative. Cohorts overlap; do not add their counts or returns. Every comparison uses identical complete, aligned, sufficiently deep observations; missing outcomes never become zero. Best-choice counts are hindsight diagnostics, not live trading rules or evidence of calibrated expected returns.

This is a read-only report extension over existing prospective trials, with no schema, study-policy, order, limit or collection changes. Existing collected outcomes work immediately without restarting the watcher. It remains a paired trial study rather than a continuous portfolio replay; the paired spare-cash comparison below adds a direct-buy alternative, while a continuous portfolio simulation is still needed before claiming optimal allocation.


### Prospective spare-cash comparison

New independent-model trials also record a `spare_cash_v1` comparison. At proposal time the trial freezes available account cash. When its first fresh destination quote produces a simulated rotation purchase, the comparison checks whether that **same quantity at the same price** could instead be purchased while retaining the source. It uses the smaller of proposal-time and purchase-time cash, unchanged live source inventory, total and race capital, directional exposure, daily gross-buy allowance including other trials, per-order limits and the realized-loss stop. It does not credit the hypothetical sale's proceeds or released exposure. Shared quote, depth, model, news, binding and inventory-epoch checks already passed for the rotation purchase. The comparison does not reserve real funds, make additional API requests or place orders.

Only prospective, eligible purchases enter `direct_buy_attribution` in each horizon of the independent-model report. All four alternatives start with the **same source shares plus the same initial spare cash**:

- Hold source and cash.
- Sell source and keep all cash.
- Sell source, buy the target, and keep residual cash.
- Hold source, buy the same target using spare cash, and keep residual cash.

The direct-buy alternative uses the same target fill and complete, aligned source/target liquidation observations as the rotation. Both purchase and liquidation buffers are included. `mean_direct_buy_vs_hold_and_cash_coins` measures the target investment's contribution; `mean_direct_buy_vs_rotation_coins` measures the effect of retaining the source instead of selling it. A positive second figure alone does not establish that the new purchase made money. Best-choice counts are hindsight comparisons with explicit ties.

`direct_buy_eligibility` reports reasons for exclusions. Legacy trials are not backfilled. Pending trials, failed rotation purchases, insufficient cash/exposure and missing or thin outcome observations do not become zero-return measurements. This is conditional on qualifying rotation purchases, so it does not discover standalone direct-buy opportunities or compare different purchase sizes. The arms are alternatives, not simultaneous allocations; results are not additive portfolio profits. Full prospective portfolio replay and live capital rotation remain separate work.

Restart the watcher once to collect new comparisons. The existing read-only `python3 -m election_bot rotation-study --hours 24 --json` command includes the results. No live policy or limits change.

### Opportunity diagnostics

The independent-model report now includes `opportunity_diagnostics`, with exact rolling-window counts and per-contract/race breakdowns. This explains why prospective comparisons are not being created without relaxing eligibility. Categories distinguish pending orders, buy pauses, disabled/gross mappings, news or race cooldowns, SIG freshness/spread/tick failures, missing race bindings, collector health, insufficient independent pollsters, no recent poll, and other evidence/validation failures. Unknown exception text is not stored in the diagnostic table.

For observations that pass quote and forecast validation, the report counts `model_and_quote_ready`, then records whether the fresh cache lacks an owned source, lacks an unowned destination, has no qualifying pair after the existing limits and edge checks, is awaiting confirmation, or creates a trial. The last category does not imply a real order. Pair-level reasons describe the cache at that observation; they are not a rejection of only the named race. The combined limits/edge category does not yet isolate every individual sizing constraint.

These are observations reaching the model-rotation hook, not all scanner visits, distinct opportunities or rejected trades. An observation appears in `observed` and may also appear in a ready stage and final outcome; do not sum those counts as independent failures. Only the first blocking gate is recorded. Existing cumulative activity counters remain separate from these windowed counts. Old activity is not reconstructed into new categories. First/last observation timestamps show actual coverage, and no rows means no diagnostic evidence, not proof of no opportunities. Seven days are retained, with hourly pruning during collection.

After one watcher restart, use:

```sh
python3 -m election_bot rotation-study --hours 24
python3 -m election_bot rotation-study --hours 24 --json
```

The text report shows aggregate counts; JSON includes the per-race breakdown. Reading a legacy journal does not create tables or start collection. This instrumentation changes no order policy, budget, confirmation requirement or model gate and makes no additional network requests.
