# Election-night mode

Trading closes at **12:00pm ET on November 4, 2026**. Scores are balances after settlement. After polls close on November 3, Kalshi and Polymarket reprice called races within seconds, while SIG books can lag. This route buys those lagging called races. It is off by default and acts only inside its time window.

## The rule (`election_bot/election_night.py`)

Inside `[start_at, end_at)` (defaults: Nov 3 6:00pm ET → Nov 4 12:00pm ET), for each scanned contract:

1. **Called side:** both Kalshi and Polymarket are fresh and bid the same side at or above `min_reference_bid` (0.95).
2. **Lagging SIG:** the SIG ask on that side is at or below `max_price` (0.99) and at least `min_edge` (2¢) below the average reference midpoint.
3. **Size:** the smallest of available coins (your per-order, per-market, total and daily limits and cash), `max_shares` (2,000), `strategy.max_shares_per_order`, `depth_fraction` (50%) of the displayed SIG ask, and exposure headroom.
4. **Never:** buying against held inventory, or overriding the drawdown pause or the position-entry guard.

The route only acts when no other signal was selected. Exits, including settlement-gated exits, keep priority.

## What it overrides, for this route only

- **News-uncertainty pauses.** Results headlines trigger these all night. While a race is paused, only this route may trade it, so no ordinary entries, exits or model buys. Set `"override_news_pauses": false` to keep them.
- **The reentry cooldown after a sale.** Set `"ignore_reentry_cooldown": false` to keep it.
- **Never overridden:** disputed-result reviews, the no-healthy-news-feed block, STOP, the realized-loss stop, clock checks and reservation-time rechecks.

Called races also get scan priority (score 250, above near-entry). Every evaluation in the window is journaled as `election_night_decision`.

## Rehearse it now

```sh
python3 -m election_bot election-night-rehearsal --hours 24
```

This ignores the window and shows what the rule would buy on recorded quotes. On 2026-10-19 it flagged 32 race sides. Most were safe favorites the bot already holds, with edges of 5–14¢. The time window is what stops the mode from acting on these today.

## Before November 3

- **Total limit.** `limits.total` is 90,000 against about 79,000 committed, leaving roughly 11,000 of headroom. Cash is about 21,000. Raise `total`, or plan which holdings to sell, if you want more room on the night.
- **Buy pause.** The bid-based drawdown pause was active on Oct 19. Switch `drawdown_control.valuation` to `reference_fair` (see [settlement value](settlement-value.md)), or this route will be blocked.
- **API limits.** The platform allows 100 reads and 30 writes per minute. `max_orders_per_cycle` (4) and `poll_seconds` (20) keep the bot well under them.
- **Dry run.** Shift the window to the next hour, start the bot in paper mode and confirm `election_night_decision` events appear:

  ```sh
  python3 -m election_bot run --once
  ```

  Then restore the window.

## Enable

Add to `config.json`, then restart the watcher:

```json
"election_night": {"enabled": true}
```

Optional keys, with their defaults:
- `start_at`, `end_at`: ISO times with an offset
- `min_reference_bid`: "0.95"
- `min_edge`: "0.02"
- `max_price`: "0.99"
- `depth_fraction`: "0.5"
- `max_shares`: 2000
- `ignore_reentry_cooldown`: true
- `override_news_pauses`: true

## Limitations

- References can be wrong or halted after a call. A stale or one-sided reference produces no signal, so the route goes quiet rather than guessing.
- A race called and then reversed (a recount or a late count) can lose. The 0.95 threshold and 2¢ edge are judgement calls, not calibrated values.
- Buys are booked to the reference route for throttle evidence.
