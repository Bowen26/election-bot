# Shared race controls and expansion status

ActiveEngine now enforces `limits.per_market` as one coin allowance for every configured contract with the same `race_key`. Buying a second party contract cannot create a second allowance. Enabled and disabled mappings count, along with assignments retained in the journal. The old entry-only engine continues to allow one enabled contract per race and does not support multi-contract execution.

The shared amount is open FIFO cost plus `max(0, -net realized P&L)` across the race, plus pending reservations. Native YES/NO assets keep separate FIFO lots. Opposite party labels do not cancel cost or imply identical payouts. Confirmed sales release only their sold cost; net losses remain committed. Gains can offset race losses but do not increase the configured cap. Total, UTC daily gross-buy, per-order, cash, directional and regional limits still apply. Pending sales do not release expected proceeds or cost before confirmation.

## Durable assignments and cooldown

At the next ActiveEngine startup, an additive SQLite migration records each numeric SIG exchange's race in `race_bindings`. Existing orders and executions are untouched. Previously ungrouped mappings fall back to their SIG market ID. Historical bindings survive disabling or removing a mapping. A later conflicting race assignment halts startup and needs accounting review; changing a label cannot reset its budget. Unknown inventory or pending commitments stop accounting instead of receiving a new allowance. Keep exposure metadata for disabled holdings, since the existing portfolio checks also need it.

All sibling contracts share the existing cooldown. Scanner hints and the final reservation check use the same race-wide last order. Attempted and zero-fill orders retain the existing cooldown behavior. Logs distinguish `open_races` from `open_contracts` and include race commitments. The offline `contract-review` report uses the same allocation calculation and retained bindings. Older general `performance` reports still label their exchange count `open_races`; use the new execution summaries for grouped counts.

## Final reservation check

Journal reservations use a SQLite `BEGIN IMMEDIATE` transaction. Before inserting an ActiveEngine reservation, the bot reconstructs fresh local inventory and rechecks the race allowance, total/daily/per-order/cash constraints for buys, native FIFO inventory for sales, directional headroom, realized-loss stop and race cooldown. An existing unresolved order still blocks submissions. A rejected check rolls back without reserving coins. Reconciliation and the existing runtime process lock remain required.

This is atomic for the local journal, not the remote exchange account. Account cash and positions are the latest preflight response; the bot cannot lock out manual trades or another bot using a different journal. No new account polling or live orders are introduced by this implementation.

## Settlement review: Nebraska Senate

The saved October 6 rule evidence verifies Democratic and Republican race/party identities but does not establish equal settlement conditions across venues:

- SIG's structured record identifies a general-election party winner and a November 4 settlement date. It does not spell out the nomination, party-switch, runoff or certification conventions in the retrieved record.
- Kalshi's primary rule refers to a party representative taking office for the term beginning in 2027. Its secondary text permits accelerated determination, with full rules still relevant.
- Polymarket refers to the election winner, includes runoffs and defines party attribution through nomination. Its independent-candidate language prevents treating Democratic NO as Republican YES.
- The SIG Independent Party title disagrees with the structured winner label, Nonpartisan. That contract fails the strict identity check.

These are findings from saved evidence, not a legal interpretation or claim of current quotes. Democratic and Republican alternatives remain review-required; the Independent contract remains identity-blocked. No numerical edge surcharge is justified by the available evidence, and none has been invented.

## What remains before additional contracts trade

The one-enabled-contract-per-race validation remains in place. This release implements shared accounting and reservations, not multi-contract routing or a settlement approval mechanism. Additional contracts require a documented acceptance/exclusion policy for their full rules, explicit exposure treatment that does not assume Republican YES is exactly Democratic NO, and tested routing across eligible contracts. Existing pinned mappings and coin limits are unchanged. Restart the running bot once to load these controls; no config editing, database deletion or extra process is needed.
