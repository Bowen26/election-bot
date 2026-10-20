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

## Reference radar and read priority

During the window, every `radar_seconds` (20) the bot reads top-of-book for every race from Kalshi (`GET /markets?tickers=…`) and Polymarket (`POST /books`, the one read-only POST the client allows). This takes a few bulk public requests and no SIG reads.

- **Flagging:** a race newly seen as called (both bids ≥ `min_reference_bid`, timestamps within `radar_max_age_seconds` = 120) is put on the priority lane for an immediate SIG check. Polymarket bulk timestamps reflect the last book change, so they can be tens of seconds old; this looser age applies to detection only. Buys still need fresh single-book references (15 s).
- **Staying on the priority lane:** a race keeps priority only while SIG still lags (an eligible buy, or one cut short by cash), so ~60 safe favorites don't monopolize the lane.
- **Read priority:** research follow-up quotes pause (`pause_research_reads`), and drawdown refreshes are trimmed to `drawdown_refresh_per_cycle` (2), so SIG reads go to trading. The client already spaces SIG reads at least 0.8 s apart. Each `scan_complete` now reports `sig_reads_last_minute`.
- **Failures:** if the radar fails, it is reported as `election_radar_skip`; ordinary scanning continues.
- **Fix:** the post-sale reentry cooldown is now also exempted at reservation for election-night buys (when `ignore_reentry_cooldown` is true). Previously the reservation check still blocked them.

## Alerts

`"alerts": {"enabled": true}` sends a macOS notification when the watcher reports `halted`, `restart_wait`, `recovering` or `stopped`.

For phone alerts, add a `push_url`, for example a private ntfy.sh topic you create: `"push_url": "https://ntfy.sh/<your-topic>"`. The phone receives one short line (status, reason, exit code), never balances or positions. Alerts never block the watcher. They take effect when the watcher restarts.

## Capital recycling (off by default)

Cash, not opportunity, may limit election night. With `"recycle_enabled": true` inside `election_night`, the bot can sell part of a held race to fund a better pending buy. It only runs inside the window.

1. **Pending buys.** When an election-night buy is cut short by a real cash shortfall (not by per-order or other deliberate limits), the route remembers that buy's edge, ask and shortfall for `recycle_pending_seconds` (120).
2. **When it sells.** When the bot next visits a different held race, it may sell part of it (reason `election_night_recycle`) only if all of these hold:
   - both fresh references call that race your way (bids ≥ `min_reference_bid`);
   - the SIG bid is within `recycle_max_giveup` (2¢) of reference fair value;
   - per share sold, `(bid ÷ pending ask) × pending edge − (fair − bid) ≥ recycle_margin` (1¢).
3. **Size.** The sale covers only the shortfall, rounded up, capped by held shares, `depth_fraction` of the SIG bid, `max_shares`, `max_shares_per_order` and `limits.per_order`.

The sale goes through the normal reservation, exposure, inventory and news checks. Like election-night buys, it may pass a generic news-uncertainty pause, never a disputed-result review. The settlement exit gate does not apply to these sales, because they deliberately trade a small give-up for a larger edge elsewhere.

**Trade-off:** each sale gives up up to 2¢ per share of expected value on a race that is almost certainly won, in exchange for a larger expected edge on a newly called race. If the newly called race reverses, both legs lose.

Enable by adding `"recycle_enabled": true` to the `election_night` block and restarting the watcher.

## Auto-recovery (off unless enabled)

`"auto_recovery": {"enabled": true, "max_per_hour": 6}` lets the watcher recover from known-safe halts without you. The worker records each halt reason in `.runtime/supervisor/last_halt.json`.

**Recovered automatically:**
- network and API errors (except 401/403)
- cut-off or invalid JSON responses
- clock-sample failures
- `database is locked`
- an account-vs-ledger mismatch, or unimported or unresolved orders

**How it recovers:** the watcher waits (15 s, doubling up to 5 min; at least 60 s after a 429), runs `recover --wait`, then resumes trading. If that reconciliation fails, the bot halts normally; there is no retry loop. More than `max_per_hour` recoveries sets STOP. Each recovery sends an `auto_recovering` alert.

**Still halts and alerts you:** everything else, including accounting inconsistencies, contract changes, the realized-loss stop, auth failures and configuration errors.
