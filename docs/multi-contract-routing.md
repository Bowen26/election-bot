# Optional multi-contract routing

The implementation supports separate Democratic and Republican SIG contracts in one race. It is **off by default**, and the installed 107-contract configuration is unchanged. No settlement acceptance records were created for live contracts. Existing mappings continue to use their existing price and risk rules.

`execution.multi_contract_races: true` permits multiple enabled contracts for one `race_key` only with the controls below. This is independent, serial routing: the scanner visits each contract, prices it against its own Kalshi/Polymarket references, and sends an eligible order to that native SIG exchange. It is not a cheapest-equivalent-claim optimizer, a hedge, cross-venue execution or a guarantee that the best sibling opportunity is visited first. The existing priority/ordinary coverage lanes remain in use.

## Settlement acceptance is explicit

Every enabled sibling requires a `settlement_review` tied to its exact `contract_fingerprint`, party and race. A review-required or blocked decision cannot trade. The accepted decision is `accept_basis_risk`, which acknowledges unresolved settlement differences; it does not claim equal payouts. Required fields are:

| Field | Requirement |
| --- | --- |
| `version` | Integer `1` |
| `decision` | `accept_basis_risk` only after completing the review |
| `contract_fingerprint` | Exact current pin |
| `race_key` | Exact configured race |
| `party` | Democratic Party or Republican Party |
| `reviewed_at` | Nonfuture ISO timestamp with timezone |
| `full_rules_reviewed` | Explicit `true` after full rules have been examined |
| `rule_sources` | HTTPS source URL for each of SIG, Kalshi and Polymarket; no embedded credentials |
| `rationale` | Explanation of the accepted basis differences and tradeoff |
| `acknowledged_flags` | Exact distinct codes returned by the basis assessment |
| `additional_entry_edge` | Explicit nonnegative increment to the existing entry threshold |
| `entry_edge_rationale` | Reason for that increment, including zero if deliberately chosen |

The software checks the fields, fingerprint, strict race/party identity and current flag set. It cannot prove that a human read the sources or that an increment is calibrated. Rule URLs are retained as citations, not automatically downloaded or treated as instructions. No score-to-price conversion or default surcharge is inferred. Missing evidence must not be filled with fabricated review attestations.

Before every actionable order, the normal fresh metadata preflight rechecks the contract pin, strict identity and assessment flags. Changed rules invalidate the review. Neither an acceptance record nor a larger edge can override failed identity, stale prices, insufficient depth, account mismatches or other existing controls. Candidate/Independent contracts remain unsupported until their attribution has a dedicated verifier.

An additional entry edge increases `strategy.minimum_edge` for that contract in both preflight and scan decisions, including the entry shadow comparison. Exit logic retains its existing depth, FIFO profitability and price rules; it still requires the reviewed contract metadata. Acceptance allows reference pricing despite acknowledged basis risk, not mechanical arbitrage.

## Exposure without assumed offsets

All configured siblings, including disabled ones, must use `exposure_mode: gross` and omit `exposure_sign`. They need matching office, state and region, and identical news matching for enabled siblings. The total, office, region and realized-loss controls are required. Each gross share consumes one unit of both sides of an exposure interval, regardless of whether it is Democratic YES, Democratic NO, Republican YES or Republican NO.

If signed positions contribute `N` and gross holdings contribute `G`, the current interval is `[N-G, N+G]`. Pending gross buys widen both bounds by their full quantity. Pending sales receive no advance release. A confirmed native sale reduces gross exposure; sale headroom cannot exceed the bot's holding of that specific exchange and side. Buy sizing must satisfy both bounds under the existing caps. This is a deliberately conservative concentration convention, not a loss estimate or a correlation model. It can restrict new entries sooner than signed exposure.

The `net_shares` fields show the signed component only. Reports also expose `unnetted_gross_shares`, an exclusion indicator and the full intervals. Coin accounting remains additive across sibling contracts, with shared race limits, losses, reservations and cooldowns. No sibling asset is treated as inventory available to sell on another exchange.

An additive `exposure_modes` table retains gross assignments. Gross contracts cannot silently revert to signed mode or disappear from configuration; keep historical rows disabled to retain accounting and news attribution. Turning off the multi-contract flag does not remove a remaining gross contract's review or extra entry edge. Resolving a mistaken historical assignment requires explicit accounting review, not database deletion.

## News and execution sequencing

A pause or unresolved dispute on any configured sibling blocks the others, including a disabled sibling's stored dispute. The engine preserves native order IDs, idempotency, reconciliation, one-unresolved-order blocking and atomic local budget reservation. Paper mode exercises the same routing and reviews but never calls broker order placement. All competition/live orders still go only to SIG.

The first eligible contract visited can use shared budget and start the race cooldown. Subsequent sibling orders require another eligible visit after that cooldown, refreshed rules/books/account data and remaining headroom. This release does not compare simultaneous sibling books or promise an optimal ordering of exits and entries.

## Current rollout status

The saved Nebraska review still has unresolved taking-office versus election-winner, party-attribution and runoff differences. SIG's Independent Party title also conflicts with its structured Nonpartisan winner label. The live configuration therefore remains on one pinned contract per race; no additional party mappings or acceptance records were enabled by this release.

The remaining activation work is to obtain/review complete settlement evidence for a specific race, record a defensible basis-risk acceptance and edge choice (or exclude it), and run that reviewed configuration in paper mode before a limited live rollout. Code installation or a GitHub push alone does not activate multi-contract trading.
