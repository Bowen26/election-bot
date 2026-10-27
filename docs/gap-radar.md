# Gap radar

The bot checks one market at a time. Priority slots go to pending news, near-entry hints, reference moves and held positions; everything else waits for the ordinary lane. On October 26 a held position was rechecked about every 78 seconds and an ordinary market about every 7 minutes. The gap radar screens every enabled race at once and sends the most valuable checks to the front of the queue.

## What it reads

- One bulk SIG price read per 100 contracts (`GET /exchanges/prices`: best bid and ask, cached up to 2 s, no depth). With 110 contracts that is 2 reads per run, within the 100 reads a minute SIG allows.
- The bulk public Kalshi (`GET /markets?tickers=…`) and Polymarket (`POST /books`) reads the election-night radar already uses. Inside the window both radars share one read per cycle.
- Every `seconds` (60) before the election-night window and every `night_seconds` (20) inside it, at most once per scan cycle (about 25 s).

## How it ranks

Each race gets a screening value: the coins one order could earn under the bot's own rules, for the best of these routes.

| Route | Screening edge per share | Shares counted |
| --- | --- | --- |
| Entry | Lower reference bid − SIG ask − buffer ≥ `minimum_edge` − margin, with reference bid depth ≥ `min_reference_depth` | Smallest of reference bid depth, race room or per-order coins ÷ (ask + buffer), net-share headroom and `max_shares_per_order` |
| Election-night entry (window only) | Both references call the side (bids ≥ 0.95), ask ≤ `max_price`, average reference midpoint − SIG ask ≥ `min_edge` − margin | Smallest of race room or per-order coins ÷ (ask + buffer), `max_shares` and `max_shares_per_order`; exposure caps ignored, as the route ignores them |
| Exit | SIG bid − higher reference ask − buffer ≥ `exit_edge` − margin, with reference ask depth ≥ `min_reference_depth`. A race called against a holding shows up here | Bot-owned shares not locked in pair baskets, capped by reference ask depth, per-order coins ÷ bid and `max_shares_per_order` |
| Funding | Selling the holding would fund the best pending coin-blocked buy: (bid ÷ target ask) × target edge − (fair − bid) ≥ minimum gain − margin, with give-up ≤ maximum + margin. Before the window this is the capital-rotation rule; inside it, the recycling rule, only for holdings both references call our way | Free shares, capped by the shares needed for the pending buy, per-order coins ÷ bid and `max_shares_per_order` |

`margin` (1¢) relaxes each threshold because screening quotes are cached and carry no SIG depth. The radar never proposes a buy against held inventory. Races with stale (older than `max_age_seconds`, 120), one-sided, wide or disagreeing reference books are skipped.

The `max_flags` (8) most valuable races worth at least `min_value` (5 coins) go to the priority lane, best first. Their score (260) is above election-night (250), near-entry (200), reference-move (180) and held-position (100) hints, and below pending news (300). One visit uses up a flag; the next run flags the race again if the gap remains. Before the window this adds at most 8 priority checks a minute, so the rest of the priority lane keeps serving held positions. Inside the window the top races take the priority slots every cycle. The ordinary lane alternates with the priority lane as before, so every market is still covered.

A flag never authorizes a trade. The visit re-reads full books and applies every rule: freshness, depth, limits, news, inventory and reservation checks.

## Events

- `gap_radar`: phase, races watched and screened, candidates, and each flag's exchange, market, route, side, screening edge, shares and value.
- `gap_radar_skip`: a failed run. Screening is optional and ordinary scanning continues. A SIG 401, 403 or 429 is raised as elsewhere, so the watcher's normal backoff applies.
- `scan_visit` adds `gap_value`, and `priority_reasons` includes `gap_radar`, for a flagged visit.

## Configuration

```json
"gap_radar": {"enabled": true}
```

Optional keys and their defaults: `seconds` 60 (10–3600), `night_seconds` 20 (5–600), `max_flags` 8 (1–40), `margin` "0.01" (0–0.05), `min_value` "5" coins (0–1000), `max_age_seconds` 120 (15–600). It is off by default and takes effect when the watcher restarts. Set `"enabled": false` to turn it off; nothing else depends on it.

## Why

- **Election-night allocation.** At October 26 prices, 22 Democratic contracts would qualify for the election-night route as soon as the window opens: about 114,000 coins of race room against about 10,000 of cash, with edges from 2.0¢ to 8.3¢. Checked in least-recently-visited order, the cash goes to whichever races come first. Checked by value, it goes to RI Senate (8.3¢), HI Governor (7.2¢) and VA Senate (5.3¢) first. If those gaps were still there on November 3 and filled at displayed prices, that would be worth roughly 300 coins. This is an estimate, not a measurement.
- **Speed during the count.** Races the bot does not hold were rechecked about every 7 minutes, and holdings called against the bot compete with every other priority hint. The radar sees each race's gap within one run.
- **Live dry run.** A read-only run on October 26 screened 109 contracts and flagged MS Senate NO, ME Governor, RI Governor and HI Governor, with screening edges of 4.0–5.2¢.

## Limitations

- Screening quotes are cached up to 2 s on SIG and up to 120 s old on the references, and SIG depth is unknown, so values are estimates. Values are a scheduling heuristic, not calibrated expected profits.
- Cash is not part of the value. With no spare cash, a flagged entry still helps: the visit records it for rotation or recycling, and the radar then flags the funding sales.
- A gap the rules will reject (for example, a race at its net-share cap) can be flagged repeatedly. Each flag costs one check per run.
