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
