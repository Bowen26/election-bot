# Automated party-pair NO baskets

SIG lists each race as separate per-party binary contracts. In a two-party race, at most one of the Democratic and Republican contracts resolves YES. Buying NO on both therefore costs `(1−bid_D) + (1−bid_R)` per set and pays at least 1, so the guaranteed edge is `bid_D + bid_R − 1`. The platform charges no fees.

## How it is represented

- **Disabled leg mapping.** The Republican contract is added to `config.json` as a *disabled* mapping with `"basket_leg": true`. Normal entry, exit, research, polling and election-night routes never scan or trade it.
- **Opposite exposure sign.** The leg carries the opposite `exposure_sign` of its Democratic anchor. In the existing signed exposure model, a matched NO-D/NO-R set therefore nets to zero, and an unmatched leg counts as an ordinary directional position. Race budgets, cooldown bindings, account reconciliation and fair-value drawdown marks all treat it like any mapped contract.
- **Unchanged anchor.** The anchor (your existing Democratic mapping) stays signed, so the polling model and every existing route keep working.

## Discovery: `python3 -m election_bot pair-legs [--race KEY] [--limit N] [--write]`

GET-only and paced (`--interval`, default 2.5 s between SIG reads). For each enabled Democratic anchor, it reads the SIG catalog once and finds the open Republican contract and its Kalshi/Polymarket counterparts. It then runs the same strict `verify_candidate` identity check used for configured contracts, and confirms both reference books are live.

Races are skipped when:
- they have an Independent contract (three-party baskets are not supported);
- the Republican contract is missing, or its references are absent or ambiguous;
- they already have a leg.

The leg stores its full contract fingerprint and a SIG-only `basket_sig_fingerprint`. `--write` backs up `config.json` to `.runtime/` and appends the legs, which stay disabled.

## Trading: `"pair_baskets": {"enabled": true, ...}` (off by default)

Each cycle (after the open-orders check, before scanning), up to `pairs_per_cycle` (3) pairs are checked in rotation:

1. **Skip conditions:** a dispute or feed-health news block skips the race; generic uncertainty pauses do not, because the set is riskless. A race where the bot holds YES on either contract is skipped, since a NO buy there would oppose inventory.
2. **Identity check:** every `verify_seconds` (900), the anchor's full pin and both SIG contracts are re-verified (open, Party Winner, expected parties, same settlement date, unchanged leg fingerprint).
3. **Edge:** both SIG books are read. A pair trades when `bid_D + bid_R − 1 ≥ min_edge` (2¢).
4. **Size:** `depth_fraction` (50%) of the thinner bid, `max_sets_per_order`, the remaining `total_coins` budget for baskets (10,000), and the coins both legs can reserve together under the normal limits (per-order, shared race budget, cash, daily).
5. **First leg:** the thinner leg goes first, through the normal reservation/placement/reconciliation path, tagged in `basket_orders`.
6. **Mark:** the filled contract gets a fresh drawdown mark, so the second leg is not paused as "valuation incomplete".
7. **Second leg:** bought at no more than break-even (`1 − first leg price`). If the price has moved past break-even, the unmatched shares are repaired on later cycles at break-even or better.

Basket legs skip the race cooldown (both legs are in one race), the position-entry guard and the reentry cooldown. They keep drawdown, coin limits and exposure checks. Shares bought for baskets are held to settlement: ordinary exits, risk exits and election-night recycling can only sell shares beyond the basket quantity.

Every check is reported as a `pair_basket` event with its edge, sets, fills or skip reason.

## Limits and risks

- **Unmatched leg.** If the second leg never fills at break-even, the first leg stays as an ordinary directional position, fully accounted and reported as unmatched.
- **Displayed prices.** Quotes are read seconds apart. A matched set's edge is locked only once both legs fill.
- **Locked cash.** Baskets lock cash until settlement (about 0.98 per set) and compete with other buys under the same limits.
- **Settlement assumption.** The guarantee assumes SIG settles at most one party contract YES per race, which the structured Party Winner contracts state.
