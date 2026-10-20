# Settlement-value drawdown and exits — October 19, 2026

The Predictions Cup ranks participants by SUSQie balance **after every market settles** (Official Rules §3 and §10). A winning share pays 1 and a losing share pays 0, and the platform charges no trading fees. So a held share is worth roughly its probability of winning, not today's SIG bid minus a 1¢ buffer. These changes let the bot use that view. Both are off by default; current behaviour is unchanged until you edit `config.json` and restart.

## Evidence (`python3 -m election_bot settlement-review --hours 72`)

Snapshot of 34 positions on 2026-10-19 (91,981 shares, cost 78,996 with no buffer):

| Valuation | P&L |
|---|---|
| SIG bid, with 1¢ buffers (current drawdown gate) | −1,981 → buys paused |
| SIG bid, no buffer | +233 |
| SIG midpoint | +490 |
| Kalshi/Polymarket midpoint (expected settlement) | **+7,465** |

- **The pause comes from the buffer.** Over 72 hours of hourly replay, the unbuffered SIG-bid drawdown peaked at 1,433. That never reaches the 2,500 pause; the 1¢ entry and exit buffers on about 92k shares (about 1,840) make up the difference.
- **Every sale was below fair value.** All 40 sales in the last 10 days that had reference quotes sold below reference fair value. The settlement gate would have blocked them and kept about +231 of expected settlement value.

## 1. Drawdown at reference fair value

Add to `drawdown_control`:

```json
"valuation": "reference_fair", "max_fair_mark_age_seconds": 1800
```

- **What a mark is.** A position is marked at the average Kalshi/Polymarket midpoint for its side. Both venues must be fresh, within `max_reference_spread`, and agree within `max_reference_disagreement`. Cost and realized P&L use actual fill prices with no buffer.
- **When marks are taken.**
  - Every scan of a held race records a fair mark.
  - Drawdown refresh fetches reference books for stale holdings: up to `refresh_books_per_cycle` per cycle, Kalshi/Polymarket only, plus a cached SIG contract check.
  - A holding without a valid mark makes the valuation incomplete, which keeps buys blocked, the same as today.
- **Separate state.** Peak and pause state live in `drawdown_fair_state` and never reuse the bid-based peak. On the first complete valuation the peak becomes the current fair P&L (about +7,465 today). Buys then pause only if expected settlement value falls 2,500 below its high and resume within 1,250.
- **Unchanged:** `pause_coins`, `resume_coins` and the buy-only nature of the pause. `drawdown-status` shows whichever valuation is configured.

## 2. Settlement-aware exits

`election_bot/settlement_value.py` provides `gate_exit`, configured as:

```json
"settlement_exits": {"enabled": true, "margin": "0.01", "block_risk_exits": true}
```

- **The rule.** A sale from any route (convergence, profit target, trend profit target) is kept only if the SIG bid is at least reference fair value plus `margin`. If references are unavailable, the sale is blocked.
- **Overpriced exits.** These already require the bid to clear the reference ask and still pass.
- **Risk exits.** These sell on SIG-only price drops that say nothing about settlement value. Turn them off with `"risk_exit": {"enabled": false}` (config only, no code).

**Wiring.**
- `ActiveEngine.decide` applies `gate_exit` right after `checked_exit`. Preflight reruns `decide()` with fresh quotes, so the gate applies at both scan and preflight. A blocked sale is logged in the decision's `exit_check` as `below_settlement_value` or `settlement_value_unavailable`, with the fair value and required bid.
- With `block_risk_exits` (the default when enabled), `risk_exit_decision` returns no signal. Set it to false to keep SIG-only risk exits alongside the gate.
- `engine.validate_config` rejects an invalid `settlement_exits` block at startup.

## Limitations

- Kalshi/Polymarket midpoints are an estimate of settlement probability, not a guarantee. A gate that never sells keeps correlated upset risk.
- The replay samples hourly and carries holdings with no recent aligned quote at cost; this was about 2,942 shares, mostly SD Senate.
- Nothing here sizes for the top-3 prize structure; see the leaderboard before deciding how much risk to keep.
