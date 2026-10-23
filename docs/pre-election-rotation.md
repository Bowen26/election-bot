# Pre-election capital rotation

This is the live trading route. The earlier research-only [capital-rotation study](capital-rotation.md) records comparisons and never trades.

Before election night the bot can be fully invested: ordinary entries with 6–11¢ gaps are blocked only by coins, while some holdings already trade within a cent or two of reference fair value. Rotation sells part of such a holding and lets the blocked entry buy with the proceeds. It is off by default (`"capital_rotation": {"enabled": true}` turns it on) and never runs inside the election-night window, where night recycling does the same job.

## The rule (`election_bot/capital_rotation.py`)

1. **A pending entry:** an ordinary entry that passed every check except coins: price gap, reference depth, exposure caps, the drawdown pause, the position guard, the reentry cooldown and news pauses. Its size is what the per-order, per-race and daily limits and exposure would allow. The coins it needs include any shortfall below the cash floor.
2. **A converged holding:** its SIG bid is at most `max_giveup` (2¢) below fair value, the average of the Kalshi and Polymarket midpoints. Basket-locked shares are never sold, nor shares in the pending entry's own race.
3. **Clearly better:** per share sold, `(bid / target ask) × target edge − give-up ≥ min_gain` (3¢). The target edge is the conservative entry measure: lower reference bid − ask − buffer.
4. **Size:** enough to cover the coins the entry needs, capped by the holding, `depth_fraction` (50%) of the SIG bid, `max_shares` (2,000), shares per order and per-order coins.

Proceeds shrink every pending need, since cash is shared, so two holdings do not both sell for the same entry. The sold race then sits out the usual 30-minute reentry cooldown, which prevents buying it straight back. The entry buys on its next visit through the ordinary route, with every ordinary check.

## Settings

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `false` | Turn rotation on |
| `max_giveup` | `0.02` | Largest discount to fair value accepted on a sale (0–0.05) |
| `min_gain` | `0.03` | Required net gain per share sold (0.005–0.50) |
| `pending_seconds` | `900` | How long a blocked entry stays a funding target without a fresh visit |
| `depth_fraction` | `0.5` | Share of the displayed SIG bid one sale may take |
| `max_shares` | `2000` | Shares per sale |

## Watching it

Journal events: `capital_rotation_pending` when an entry first becomes a funding target, and `capital_rotation_decision` for each holding checked against one. The dashboard shows `Rotation checks (1h)`. Sales appear in `Orders (1h)` with the `capital_rotation` reason in the order log.
