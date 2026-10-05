# SIG election bot

A terminal/VS Code bot that reads SIG, Kalshi and Polymarket order books and can **place and cancel SIG competition orders automatically, without asking before each trade**. A background news monitor collects election headlines, triggers fresh price checks and temporarily pauses entries on uncertainty flags. It uses the documented SIG API. Kalshi and Polymarket are read-only reference feeds; this program cannot submit orders to them.

Python 3.9+ on macOS/Linux. No third-party Python packages required. All commands below run inside this `election-bot` folder. For VS Code, open this folder as your workspace; the included launch configurations use the Python debugger extension, or simply use the integrated terminal.

If you installed Python from python.org on macOS, run the included **Install Certificates.command** from its folder in Applications to initialize HTTPS certificate trust. For Python 3.14, the command is `/bin/sh '/Applications/Python 3.14/Install Certificates.command'`. This installs Python's certificate bundle; certificate verification remains enabled.

Try the offline demo now:

```sh
python3 -m election_bot demo
python3 -m unittest discover -s tests -v
```

The demo uses a fake broker and synthetic prices. It exercises a partial fill, cancellation of the remainder, and durable spending accounting. It does not measure profitability.

## Connect your competition account once

1. On [SIG SuperMarket](https://sig.thesuper.market/markets), open your profile → API Keys. Create a key with `read` and `trade` scopes for the competition account. Complete any enrollment/terms steps on the website.
2. Run `python3 -m election_bot setup`. Enter the key at the hidden local prompt, **not in a chat message**. It writes `.env` with owner-only permissions and creates `config.json`. Alternatively set the `SIG_API_KEY` environment variable. Existing local credentials are not overwritten.
3. Run `python3 -m election_bot discover --tournaments`. Copy the correct competition slug and UUID to `config.json`.
4. Run `python3 -m election_bot discover --search "New Hampshire"`. Copy the appropriate SIG market ID and its **exchange ID** into the example mapping. Those are different identifiers.
5. Run `python3 -m election_bot inspect --mapping nh-senate-democratic`. This prints the SIG resolution tree, both external contracts' rules, and outcome orientation. Check that all three refer to the intended race/party. The seeded Kalshi/Polymarket NH contracts are real identifiers, but their mapping to a SIG exchange is deliberately unset.
6. Once you have reviewed the mapping, run `python3 -m election_bot pin --mapping nh-senate-democratic`. This saves a fingerprint and enables that mapping. It does not place an order. A subsequent change in contract text or outcome orientation blocks that market until reviewed and pinned again.

`pin` records your review; it cannot prove legal equivalence. For example, a Kalshi contract on a party's representative being **sworn in** differs from a Polymarket contract on **winning an election**. The bot uses those contracts as noisy reference prices, not as a guaranteed arbitrage. Inspect the SIG resolution tree too. Do not simply match market titles. For an inverted external outcome, set the corresponding `*_yes_matches_sig_yes` value to `false` before pinning.

Test the public feeds without credentials:

```sh
python3 -m election_bot feeds
```

## Run automatically

First examine one pass using live market data and simulated fills:

```sh
python3 -m election_bot run --once
```

Then run the continuous bot. The `--live` flag selects automatic **SIG competition** execution; it does not ask for confirmation per trade:

```sh
python3 -m election_bot run --live
```

Without `--live`, `run` stays in paper mode. `--once` works in either mode. Paper and live journals are separate. The process must keep running; closing the terminal or putting the computer to sleep interrupts it. This version does not install a service or restart itself after failures. In VS Code choose “Paper trading” or “LIVE SIG competition trading” from Run and Debug.

From another terminal in this folder:

```sh
python3 -m election_bot status
python3 -m election_bot stop
```

The stop signal prevents the next submission and lets the running bot cancel its known resting order. A request already in flight can still fill. Ctrl-C also attempts reconciliation. Stop does **not** sell positions. After stopping, `python3 -m election_bot resume` clears the stop flag; launch `run` again separately.

## Strategy and configured limits

For each enabled race, the bot normalizes both external books to the SIG YES outcome, checks freshness, spread, displayed depth and agreement, then looks for a SIG ask sufficiently below **both external bids**. It also evaluates NO using the complementary books. It buys only at the best executable SIG price level with a limit order. Midpoints are used only to check venue disagreement, not as execution prices.

| Setting | Current user configuration |
| --- | --- |
| Minimum gap after cost/uncertainty buffer | 5 cents/share |
| Cost/uncertainty buffer | 1 cent/share |
| Per-order spending | 50 SUSQies |
| Per-race risk capital | 250 SUSQies |
| Total risk capital | 25,000 SUSQies |
| Net directional shares, total | 5,000 shares |
| Net directional shares, per office | 2,500 shares |
| Cumulative net realized-loss stop | 2,500 SUSQies (10% of total risk capital) |
| Gross daily buy spending, UTC | 5,000 SUSQies |
| Maximum shares/order | 100 |
| Minimum reference top-bid depth | 20 shares on each venue |
| Maximum reference spread / midpoint disagreement | 8 cents / 8 cents |
| Maximum book age | 15 seconds |
| Pause between batches / per-exchange cooldown | 1 / 30 seconds |
| Limit-order expiration | 15 seconds |
| Races per scan batch / maximum orders per batch | 12 / 4 |

These limits were selected by the user for the 100,000-coin competition account; the starter `config.example.json` retains smaller limits. Edit `config.json` to change limits and restart the process. Prices, thresholds and monetary caps accept decimal strings. The one-cent buffer is a configurable cushion, not a verified fee schedule or statistical confidence interval. A price gap does not establish positive expected value.

The user configuration now enables the inventory-aware execution mode. It buys qualifying price gaps and sells bot-owned YES or NO shares at a limit price when either (a) the SIG bid exceeds both external asks by at least two cents after the exit buffer, or (b) the SIG bid reaches both external bids and the sale clears a two-cent/share profit threshold after entry/exit buffers. Profit-taking also checks the cost of the actual FIFO shares being sold. An overpriced exit may realize a loss. There is no blanket stop-loss, passive market making, external hedging, guaranteed profit or news-derived valuation.

Risk capital is **open position cost (including entry buffers) plus net realized losses plus unresolved reservations**, globally and per race. Confirmed sales release the cost of the shares sold; losses continue consuming capital, while profits do not expand the configured allowance. The daily cap remains gross buy spending and is not replenished by sales. Each order, including a sale, is limited to 50 coins of notional and available depth. Existing bot purchases are imported from SIG's confirmed lifecycle fill totals on restart; the journal is not reset. Unconfirmed fills halt further submissions.

Only bot-owned inventory is eligible for automatic sales. Account holdings must match the bot's reconstructed position; with portfolio controls enabled, mismatches halt new trading across the portfolio. SIG has no reduce-only parameter in the reviewed API: a sell exceeding holdings can become a complement buy. Avoid concurrent manual trading or another bot on these same positions. This bot checks holdings just before submission, serializes its own writes, verifies the canonical response and cancels its own remainder before halting on a mismatch, but cannot make the remote position check and submission atomic. Settled positions require separate settlement accounting review and are not automatically recycled. The one-contract-per-race rule remains; different races can still be correlated.

`execution.enabled: false` selects the earlier cumulative-spending, buy-only engine. Do not switch accounting modes or change cost buffers mid-run. The example configuration keeps this mode disabled until mappings and limits are configured.

The additional portfolio controls size both buys and sales against signed exposure, including pending fills. They retain the existing gross coin limits. The realized-loss stop persists through `.runtime/STOP`; it does **not** measure losses on unsold positions or liquidate holdings. With these controls enabled, account-wide inventory mismatches halt portfolio trading. See [portfolio controls](docs/portfolio-controls.md) for configuration, offsetting positions, recovery, timezone validation and the new three-venue quote snapshots.

Complete fill-history pagination now anchors new measurements to the last fill. Persistent disputed-result flags identify held positions needing review and remain until explicitly cleared. See [accounting and dispute handling](docs/accounting-and-disputes.md) for behavior, diagnostics and review commands.

## Performance and diagnostics

For a focused read-only report on stale feeds and unsold positions, run `python3 -m election_bot diagnostics` (or add `--paper`). No SIG key or network connection is needed to read this report. It uses newly recorded events; older generic skip messages cannot be reliably attributed to a venue.

`quote_diagnostics_last_24h` identifies SIG, Kalshi and Polymarket separately, with local/source ages, stale or future timestamps, missing two-sided books, crossed books and excessive reference spreads. API request failures also carry a venue where known. SIG source age uses its engine capture time; Kalshi uses HTTP Date minus cache Age; Polymarket uses its book timestamp. Local age measures time since the local Book object was constructed, not network transit time. A recently retrieved book with an old source timestamp could be unchanged or stale; this report does not distinguish those cases or relax the freshness limit. Non-finite timestamps are rejected explicitly. Failed checks are grouped by scan, preflight, pre-submit or performance-only observation, rather than mixed into one count.

`exit_diagnostics_last_24h` explains owned-position checks: selling disabled, insufficient reference ask depth, SIG price not yet converged, profit below the configured minimum, FIFO profit below the minimum, or order/depth limits too small for one share. It records SIG's bid, the required price for each exit route and relevant cost/depth values. The overpriced and profitable-convergence routes are alternatives; neither must satisfy the other's price condition. Cooldowns, open orders, news pauses, inventory mismatches and failed quote checks appear as **not evaluated**, because the guard prevented an exit decision. An eligible exit is still only a proposal until the order fills. Scan and preflight counts remain separate to avoid presenting two checks as two trading opportunities. The report preserves the original strategy and risk limits.

Run `python3 -m election_bot performance` in a separate terminal, or after stopping the bot. Add `--paper` for simulated activity. The command reads local state only. It reports filled orders/shares, open cost, risk capital, realized P&L after configured buffers, recent scan timings and skip reasons. Decision checks identify price gaps, reference depth, available budget, opposite inventory and other blockers. Counts are diagnostic observations, not independent trading opportunities; a final preflight may assess an opportunity twice.

`scan_performance_last_24h` separates scheduled visits from successful quote checks, with median, 95th-percentile and maximum revisit times, both by market and by scheduling lane. A cooldown, failed request or news pause counts as a visit, not a successful quote check. Final preflight rechecks and the SIG-only performance observations are excluded from quote-revisit counts. The first check has no interval; subsequent intervals include any downtime between restarts. Reports group `priority_v1` separately from `news_round_robin`; a policy change resets interval baselines while preserving the ordinary scan cursor, so intervals do not straddle two policies. These descriptive metrics measure coverage and responsiveness, not proof of additional profit.

Each scanned held position gets a bid-depth liquidation estimate. If there is insufficient displayed liquidity, full-position P&L is left unavailable; the report shows executable quantity and quote age.

After each buy, measurements at **1, 5, 15 and 60 minutes, plus 24 hours**, estimate selling the original filled quantity into SIG's displayed bids, across available price levels, after both entry and exit buffers. NO buys use the complementary book. Quotes must be fresh and captured after the target time. Thin books remain unmeasured until enough depth is observed within the window; partial exits are never presented as a full-size return.

At the beginning of each active execution cycle, a separate observation pass reads up to four due SIG books, ordered by the earliest deadline. One book can measure multiple buys. This pass does not require Kalshi, Polymarket or news signals, submits no orders, and shares the existing SIG rate limiter. Authentication and rate-limit errors still pause/halt through the normal recovery policy. Regular trading scans also collect observations. This adds at most four book reads per cycle (each may use the existing short retries); it is best-effort polling, not a guarantee of exact-time quotes.

The allowed delay after each target is 60 seconds, 2 minutes, 3 minutes, 15 minutes and 6 hours respectively. `performance` reports coverage, missing and pending observations, positive-result percentage, P&L per share, return on entry cost, and average/maximum sampling delay, both overall and by market/outcome. Expired windows remain missing even if a later quote becomes available. Low coverage can bias measured results toward liquid markets; it is not evidence that the missing buys performed well.

The shorter horizons apply only to buys filled after the updated bot first starts; older 1-hour/24-hour observations remain intact. The migration is additive and does not reset orders, accounting or budgets. Reporting itself stays read-only. These are hypothetical outcomes even if the actual position was sold earlier; do not add them across horizons or treat repeated buys in one race as independent evidence. This is forward monitoring, not a backtest or proof of predictive advantage. Realized P&L still uses FIFO bot costs and configured buffers, not verified exchange fees. Trading thresholds, order sizing and limits are unchanged by this measurement upgrade.

## Full competition coverage

The current audit covers all 237 SIG contracts (117 contests). The saved configuration enables **107 races: 30 Senate, 31 governor, 46 House**. Ten contests remain excluded because the reference identity or settlement rules could not be verified. See [the complete coverage report](COVERAGE.md), including each exclusion. Separate Republican and independent contracts are not enabled alongside the selected Democratic-party contract. Buying its NO is not identical to buying Republican YES.

Run `python3 -m election_bot coverage` for local counts, limits and exclusions. Changes take effect only after restarting the bot. The scanner rotates through bounded batches and persists its position even after an order or restart. News-priority checks alternate with ordinary coverage to prevent starvation; unprocessed news remains queued in memory until its race is scanned. A restart can lose that priority queue, but persisted news pauses and normal race scanning remain. Public Kalshi/Polymarket book requests run concurrently using separate clients. Quotes are never cached. Contract metadata is cached for up to 15 minutes while scanning, then refreshed and rechecked against the pin before every actionable order. News also invalidates the corresponding cached metadata. Before submission, account, orders, holdings and quotes are refreshed. Up to four orders execute sequentially per twelve-race batch, with confirmed accounting between them; the next batch resumes after one second. SIG read/write pacing remains enforced. This is faster polling, not a streaming or low-latency market maker.

### Priority scanning

Active execution defaults to `execution.priority_scanning: true`. Priority slots consider pending news first, then entries whose last valid net price gap was within two cents of the configured entry threshold, then observed reference-midpoint moves of at least two cents, then bot-owned positions that may need an exit. Same-priority races are ordered by oldest visit. Reference moves compare successful observations no more than five minutes apart; the bot cannot detect a move before it polls that market. Price hints last three minutes, are cleared on an invalid scan, and are discarded on restart. They are scheduling hints only: every order still needs fresh books, contract checks, inventory checks and the existing entry/exit rules.

Priority visits wait at least 30 seconds since the last visit and respect each market's order cooldown. A priority selection is always followed by an ordinary cursor selection; when there is no eligible priority request, the slot also goes to ordinary coverage. No market is selected twice within one batch, and early order limits do not reset the ordinary cursor. This guarantees coverage in selections, not a wall-clock deadline during network outages. Ordinary coverage may revisit a race sooner than the priority interval. News remains queued if a visit is skipped for order cooldown.

To compare against ordinary/news-only scheduling, set `execution.priority_scanning` to `false` and restart. This changes scheduling only; budgets, trade thresholds, order counts and API pacing stay the same. Priority scanning makes no extra network requests of its own. Owned-position and near-entry priority can use up to half the selections, so other races may be revisited less often; inspect per-market coverage in the report. Streaming feeds remain a separate future upgrade.

A read-only six-race validation measured 14.49 seconds uncached and 4.84 seconds cached; this is a small sample excluding order execution. A full sweep and news response can take longer during initialization, active trading, retries or network delays. `python3 scripts/validate_active.py` repeats migration on a copy of the live journal and read-only feed checks; its broker explicitly rejects submission and cancellation. No live journal migration occurs until the actual updated bot starts.

`python3 scripts/audit_catalog.py` produces read-only mapping evidence in `.runtime/catalog-review.json`; it never enables contracts or places orders. `--retry-unmatched` reuses prior candidates and cached discovery, so its results are not an entirely fresh audit. `python3 scripts/check_catalog_books.py` performs a read-only quote check for structurally verified candidates. Discovery does not silently enable future listings. Rules still need review before adding/pinning a contract.

Paper fills assume the displayed best level remains executable and charge the full configured buffer. They do not model competition for fills, queue position, future portfolio inventory or news impact. Do not interpret paper outcomes as a live-performance backtest.

## News monitoring

News starts automatically with `run` and `run --live` when `news.enabled` is true. Restart an already-running bot to load this update: press **Ctrl+C**, wait for the prompt, then run `python3 -m election_bot run --live` again. No new API key, paid subscription or extra terminal process is required for the included sources.

The default sources are [NPR Politics](https://feeds.npr.org/1014/rss.xml), [PBS Politics](https://www.pbs.org/newshour/about/pbs-news-rss-feeds), and [NHPR](https://www.nhpr.org/latest-from-nhpr-rss). They are polled every 60 seconds, using conditional HTTP requests where supported. A separate collector thread handles downloads; news requests do not block order submission, cancellation or reconciliation. RSS publication and caching delays still apply: this is not a guaranteed real-time wire service.

The collector saves election-related headlines across races, with source links, state-name tags, publisher timestamps and local detection times. This is national discovery plus New Hampshire coverage, **not exhaustive monitoring of every midterm race**. State tags and event categories are heuristic, not verified facts. News from unmapped races is stored for review and cannot enable trading in those races. The 107 enabled mappings have explicit state/office or district phrase filters; district filters can miss articles that only name a candidate. See [the coverage report](COVERAGE.md).

For mapped races:

- A fresh matching story wakes the price loop and prioritizes that race's SIG/Kalshi/Polymarket book checks. The existing order cooldown and all spending limits still apply.
- `news_review` records a **paper-only assessment** of the existing price-gap signal, the story, and any entry block. This is not an independently trained news valuation model. In `--live` mode a price-gap trade can still execute if all ordinary controls pass; news never sets fair value, changes quantity limits, or bypasses a guard.
- Headlines flagged as candidate withdrawals or ballot/candidate eligibility changes pause the matching race for 15 minutes. Disputed results create a persistent review flag that blocks entries and automatic exits until explicitly cleared. The included publishers are permitted to trigger these precautionary pauses, but their articles are not independently fact-checked by this code. Basic negation/speculation filtering reduces false alarms; it cannot interpret every headline correctly. Timed pauses expire automatically; duplicates do not extend them. A different qualifying story can extend a timed pause. Disputed-result review flags do not expire automatically.
- News older than six hours, missing a publication timezone/time, or more than 60 seconds in the future cannot trigger an alert or pause. Old headlines remain visible with their eligibility status. Identical normalized headlines are deduplicated across publishers and restarts; a retimestamped copy cannot make an old headline new.
- If **all configured feeds** lack a successful check for five minutes, automatic trading pauses until at least one source recovers. Active news uncertainty pauses also block valuation-based exits. Disputed-result flags persist beyond the timer until an explicit, recorded review clears each article/mapping flag. An individual failed feed is reported without stopping the others. This checks source availability, not complete news coverage. Existing holdings remain in place, and in-flight orders can still fill.

Check source health, recent headlines, active pauses and the most recent paper assessments from another terminal in this folder:

```sh
python3 -m election_bot news --status
```

To fetch once while the bot is stopped (no SIG credentials or orders involved):

```sh
python3 -m election_bot news --once
```

Only one news collector runs per checkout. Its records live in `.runtime/news.sqlite3`. Price snapshots in the live/paper trading journals include `news_event_ids`; `news_review` links them to publication and detection times. These records support later analysis of delays and subsequent price moves; no profitability or latency advantage has been established yet. The monitor does not read full linked articles, interpret article text as instructions, execute embedded content, or send your SIG key to publishers.

Each market needs an explicit `news_match` rule. Every phrase group under `all` must match the title or RSS summary; a phrase in `exclude` prevents matching. For example:

```json
"news_match": {
  "all": [["New Hampshire", "N.H."], ["Senate", "Senator"]],
  "exclude": ["state senate"]
}
```

For additional races, configure precise state/office/district phrases and review the trading contract mapping independently. Candidate-only headlines without the required location/office phrases may be missed. Add direct HTTPS RSS/Atom endpoints under `news.sources` with unique names and an explicit allowlist of article hostnames. Set `pause_on_risk` to false for sources that should only generate review alerts. Feed redirects are rejected; use the publisher's final feed URL. AP election-result data would require a separate integration and access key; it is not included here.

## Failure and recovery behavior

- Order payload and idempotency key are committed to SQLite **before** submission. No new order is submitted while an earlier one is unresolved.
- SIG has no documented immediate-or-cancel field. The bot submits a short-expiry limit order, promptly cancels a resting remainder, and checks that it closed. Expiration is a backstop, not proof that cancellation worked.
- A write timeout, unexpected response or uncertain cancellation halts execution with the reservation intact. Known bot orders can be reconciled on restart. Unknown submissions are never retried under a new key.
- After the original expiration plus five seconds, use `python3 -m election_bot recover`. It replays the exact stored request/key if necessary, then checks/cancels the known order. If the exchange still cannot determine the status, the bot stays halted; check the SIG account/API response before doing anything else. This command works even with a stop signal set.
- Missing timestamps, closed markets, changed rules, thin/crossed books and external-venue disagreement prevent trades.
- SIG reads returning HTTP 502, 503 or 504 are tried up to three times with short delays. During continuous running, transient GET failures that escape a scan (network errors or HTTP 408/429/500/502/503/504) log `connection_pause` and retry with delays of 5, 10, 20, 40, then 60 seconds, honoring any longer server-requested delay. No new orders are submitted during the pause; each resumed cycle reconciles pending orders before scanning. Recovery logs `connection_restored`. Market-specific metadata/book failures can skip that race. Authentication, inconsistent fills, unknown submissions and failed writes still halt with reservations intact. This recovery never automatically replays order submissions. Continuous startup also retries transient tournament connection failures, logging `startup_connection_pause` until connected or stopped. Certificate verification errors remain explicit failures; TLS verification is never disabled. `--once` and utility commands still exit on unrecovered read failures. Ctrl-C and the stop signal interrupt recovery waits.
- Any existing open tournament order blocks new bot entries. Only orders recorded as this bot's own are cancelled. Avoid trading the same account from another process while this one runs: the API does not provide an atomic account lock.
- API requests have eight-second timeouts. SIG calls are paced below documented read/write limits for one process. The local process lock covers this checkout; another copy of the program or another computer has a separate lock.

`.runtime/live.sqlite3` contains reservations, fills/cost bounds, book snapshots and decisions; `.runtime/paper.sqlite3` contains simulations. Never delete the live journal to get around a halt or budget. Keep it with the bot across restarts. Journals bind to the API key and tournament; rotating the key requires deliberate state migration, not deletion. `.env`, `config.json` and `.runtime/` are gitignored.

Kalshi's REST order book has no exchange-event timestamp in the response used here; its freshness check uses HTTP Date/cache Age plus local receipt time. Polymarket supplies a book timestamp, and SIG supplies an engine capture time. A quiet but old Polymarket snapshot can conservatively block trading. REST polling is not a low-latency feed.

## Next useful extensions

Use the recorded news events, price reviews and later snapshots to test whether external prices or news actually lead SIG. Further extensions include structured polling data, additional official election-result sources, websocket price feeds, correlated exposure limits and exit logic. This starter intentionally does not import the older Kalshi market maker or the much larger NautilusTrader framework.

API references: [SIG API](https://sig.thesuper.market/api/v1/docs), [Kalshi public market data](https://docs.kalshi.com/getting_started/quick_start_market_data), [Kalshi order book](https://docs.kalshi.com/api-reference/market/get-market-orderbook), [Polymarket prices and order books](https://docs.polymarket.com/market-data/prices-order-books).
