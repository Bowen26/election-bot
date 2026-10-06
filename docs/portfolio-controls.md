# Portfolio controls and quote observations

Race coin allowances, cooldowns and atomic reservation checks are described in [shared race controls](shared-race-controls.md).

These controls apply to `ActiveEngine` (`execution.enabled: true`) in both paper and live mode. They supplement the existing per-order, per-race, total risk-capital and daily gross-buy coin limits. They do not change the price-gap thresholds, reference-depth gates or news rules.

## Configuration

The user-approved local settings are:

```json
{
  "net_shares_total": "5000",
  "net_shares_per_office": "2500",
  "net_shares_per_region": "2500",
  "realized_loss_stop_fraction": "0.10"
}
```

Add these fields inside `limits`. With the current `total: "25000"`, the realized-loss threshold is 2,500 coins. The fraction uses configured risk capital, not the account's 100,000-coin starting balance. Changing total risk capital also changes this threshold.

Each enabled mapping requires an explicit `office` (`house`, `senate` or `governor`) and integer `exposure_sign` (`1` when SIG YES follows the common factor, `-1` when it opposes it). The current audited catalog uses Democratic-party YES contracts, so its sign is `1`. A NO outcome covers all non-Democratic outcomes; it is not necessarily a Republican contract. New or changed mappings require review, not inference from a ticker in the trading loop. Keep exposure metadata for disabled races that still have holdings. Missing metadata for held or pending inventory halts trading.

Older configurations without these additional fields retain their existing behavior. Setting either share limit requires both. Configured portfolio controls are rejected in the earlier entry-only engine rather than silently ignored. The starter config retains the earlier mode and smaller coin limits; configure active execution and these fields together.

When `net_shares_per_region` is present, both total/office share limits are required. Each enabled mapping also needs `state_code` and `region` consistent with the [Census region table](https://www2.census.gov/geo/pdfs/maps-data/maps/reference/us_regdiv.pdf); a provided race key must match the state. Unsupported locations and missing or inconsistent assignments are rejected. The reviewed catalog explicitly stores these fields rather than inferring geography from a name during trading. Disabled races with holdings/pending orders still count and need valid assignments. Older configurations without the regional limit retain their prior total/office behavior.

The regions are Northeast, Midwest, South (including DC) and West (including Alaska/Hawaii). Geographic grouping is a coarse concentration limit, not a covariance model or claim of independent regional shocks. Runtime risk events include regional net exposure; `coverage` shows saved race counts by region.

## What the share limits mean

The directional measure sums signed shares across the entire bot portfolio: YES is positive and NO negative, multiplied by the mapping's sign. It is a coarse common-factor exposure measure, not a probability model, loss bound or claim that race sensitivities are identical. Purchase prices determine settlement P&L. A YES in one race and a NO in another can both lose; zero net shares does not imply zero risk. Gross coin limits remain in force. Regional caps use the four U.S. Census regions; richer scenario analysis remains future work.

Order sizing checks the total, the order's office and its region when the regional cap is configured. It includes an interval from zero to full execution for every unresolved order, so a pending offset is never assumed to fill. Unknown submissions continue to block all new orders under the existing reconciliation rules.

Both buys and sales are checked. At the positive cap, another positive purchase has no headroom, while a purchase in the opposite direction may have room. Selling NO increases positive net exposure, so that sale can be reduced or blocked. For an already breached cap, orders may move exposure back toward the permitted range but cannot worsen the breached direction. The bot does not automatically liquidate or rebalance existing positions. An office or regional restriction can block a trade that reduces the total if it worsens exposure in that group.

Sizing is repeated during final preflight; a final check runs before reservation. For profit-taking exits, the FIFO profitability check uses the reduced quantity. The account's entire nonzero position list must agree with the reconstructed bot inventory when directional controls are enabled. Manual, unknown or settled positions require reconciliation/review and halt new portfolio trading; the bot does not automatically adopt or liquidate them. Remote changes between an account read and order submission cannot be made atomic.

`portfolio_risk` events report signed holdings, possible net shares including pending fills, caps and the realized-loss threshold. The read-only `performance` report includes the latest such event and latest `risk_stop` within its 24-hour diagnostic window; these are timestamped observations, not a live account balance.

## Realized-loss stop

The stop tests `max(0, -cumulative_net_realized_PnL_after_buffers)` against the fraction of configured total risk capital. It uses confirmed FIFO executions, including configured entry/exit buffers, which are estimates rather than verified fees. Realized gains offset realized losses. It is neither the sum of every losing trade nor a drawdown from the account's high-water mark.

It is checked after reconciliation, before submission, and after fills. Meeting or exceeding the threshold writes `.runtime/STOP`, records a `risk_stop`, and ends the batch. Recovery of outstanding orders remains permitted. It does not place liquidation orders. A stop request cannot reverse a request already in flight. The STOP file persists across restarts. `resume` clears that file, but the unchanged realized-loss threshold will trigger it again; do not delete or reset the journal to bypass it. Review the losses and configuration before resuming.

**Unrealized losses are not covered.** An unsold position can lose substantially while realized P&L is zero. The valuation table's partial, asynchronous or stale quotes are not suitable for claiming a portfolio drawdown check. A future equity-based stop needs full position coverage, executable depth, exit-cost treatment, stale-data behavior, settlement accounting and a defined high-water mark.

## Startup validation

Enabled mappings require nonempty names, numeric positive SIG IDs, Kalshi/Polymarket identifiers, explicit boolean outcome orientations and a reviewed SHA-256 contract fingerprint. Names and enabled race/exchange identities must be unique; with exposure controls, even disabled duplicate exchange mappings are rejected to avoid ambiguous risk attribution. Missing fields fail before feed requests instead of causing repeated per-race skips.

All API ISO timestamps must include a timezone (`Z` or an explicit offset). Naive timestamps are rejected rather than interpreted in the computer's local timezone. The tournament close cutoff is checked again after quote requests before submission. Base-engine paper recovery now releases an unfilled reservation with zero cost.

## Data for later strategy studies

New `quote_snapshot` journal events record top bids/asks and displayed sizes, midpoints, source times, local observation times, timestamp basis and quote-quality issues for SIG, Kalshi and Polymarket. Reference outcomes are already aligned with SIG YES. Scan and preflight samples have separate phases and unique snapshot IDs; decisions link to those IDs, and submitted signal events link the final snapshot to the order's idempotency key. These events stay in the journal rather than printing every quote to the console.

Old events are not backfilled. Books are obtained asynchronously; the timestamps do not claim simultaneous exchange observations. Invalid/stale quotes retain their original timestamps and quality flags. Snapshots are observations, not authorizations to trade. Lead/lag tests must use valid, sufficiently aligned samples and avoid counting scan/preflight duplicates or treating repeated fills in correlated races as independent trials. Entry-price/gap reports and the descriptive [reference-leadership and shadow-entry report](reference-leadership.md) are implemented; neither establishes a profitable signal. No new API requests or credentials are required for this collection.
