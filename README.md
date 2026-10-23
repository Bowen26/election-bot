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

Without `--live`, `run` stays in paper mode. `--once` works in either mode. Paper and live journals are separate. The process must keep running; closing the terminal or putting the computer to sleep interrupts it. The `watch` command can restart retryable worker failures; it does not install a background service. In VS Code choose “Paper trading” or “LIVE SIG competition trading” from Run and Debug.

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
| Per-order spending | 100 SUSQies |
| Per-race risk capital | 1,000 SUSQies |
| Total risk capital | 75,000 SUSQies |
| Net directional shares, total | 5,000 shares |
| Net directional shares, per office | 2,500 shares |
| Net directional shares, per Census region | 2,500 shares |
| Cumulative net realized-loss stop | 7,500 SUSQies (10% of total risk capital) |
| Gross daily buy spending, UTC | 5,000 SUSQies |
| Maximum shares/order | 200 |
| Minimum reference top-bid depth | 20 shares on each venue |
| Maximum reference spread / midpoint disagreement | 8 cents / 8 cents |
| Maximum book age | 15 seconds |
| Pause between batches / per-exchange cooldown | 1 / 30 seconds |
| Limit-order expiration | 15 seconds |
| Races per scan batch / maximum orders per batch | 12 / 4 |

These limits were selected by the user for the 100,000-coin competition account; the starter `config.example.json` retains smaller limits. Edit `config.json` to change limits and restart the process. Prices, thresholds and monetary caps accept decimal strings. The one-cent buffer is a configurable cushion, not a verified fee schedule or statistical confidence interval. A price gap does not establish positive expected value.

The user configuration now enables the inventory-aware execution mode. It buys qualifying price gaps and sells bot-owned YES or NO shares at a limit price when either (a) the SIG bid exceeds both external asks by at least two cents after the exit buffer, or (b) the SIG bid reaches both external bids and the sale clears a two-cent/share profit threshold after entry/exit buffers. Profit-taking also checks the cost of the actual FIFO shares being sold. An optional third route, `execution.profit_target_enabled`, takes profit at a fresh SIG bid without requiring external price convergence. It must clear the same average-cost and FIFO profit threshold after buffers. The local configuration uses `execution.profit_exit_mode: "trend"`: crossing the two-cent buffered FIFO target alone does not sell. It requires spaced confirmation of gap narrowing, weakening at both references, or a sustained SIG bid pullback, then limits the profit sale to 25% of the held position and the existing order limits. See [trend-based partial exits](docs/trend-profit-exits.md). A 30-minute race-wide pause on new buys follows any confirmed sale; further exits remain eligible after their own checks. See [profit-target exits](docs/profit-target-exits.md). An overpriced exit may realize a loss. There is no blanket stop-loss, passive market making, external hedging, guaranteed profit or news-derived valuation.

Risk capital is **open position cost (including entry buffers) plus net realized losses plus unresolved reservations**, globally and per race. Confirmed sales release the cost of the shares sold; losses continue consuming capital, while profits do not expand the configured allowance. The daily cap remains gross buy spending and is not replenished by sales. Each order, including a sale, is limited to 100 coins of notional and available depth. Existing bot purchases are imported from SIG's confirmed lifecycle fill totals on restart; the journal is not reset. Unconfirmed fills halt further submissions.

Only bot-owned inventory is eligible for automatic sales. Account holdings must match the bot's reconstructed position; with portfolio controls enabled, mismatches halt new trading across the portfolio. SIG has no reduce-only parameter in the reviewed API: a sell exceeding holdings can become a complement buy. Avoid concurrent manual trading or another bot on these same positions. This bot checks holdings just before submission, serializes its own writes, verifies the canonical response and cancels its own remainder before halting on a mismatch, but cannot make the remote position check and submission atomic. Settled positions require separate settlement accounting review and are not automatically recycled. One contract per race remains the default; the optional reviewed multi-contract mode uses gross exposure and shared race controls. Different races can still be correlated.

`execution.enabled: false` selects the earlier cumulative-spending, buy-only engine. Do not switch accounting modes or change cost buffers mid-run. The example configuration keeps this mode disabled until mappings and limits are configured.

The additional portfolio controls size both buys and sales against signed exposure, including pending fills. They retain the existing gross coin limits. The realized-loss stop persists through `.runtime/STOP`; it does **not** measure losses on unsold positions or liquidate holdings. With these controls enabled, account-wide inventory mismatches halt portfolio trading. See [portfolio controls](docs/portfolio-controls.md) for configuration, offsetting positions, recovery, timezone validation and the new three-venue quote snapshots.

Complete fill-history pagination now anchors new measurements to the last fill. Persistent disputed-result flags identify held positions needing review and remain until explicitly cleared. See [accounting and dispute handling](docs/accounting-and-disputes.md) for behavior, diagnostics and review commands.

Regional exposure checks use the four [U.S. Census regions](https://www2.census.gov/geo/pdfs/maps-data/maps/reference/us_regdiv.pdf), with an explicit state and region for every enabled race. Total, office and regional caps apply together to buys, sales and possible pending fills. Geographic grouping does not make election outcomes independent.

## Performance and diagnostics

Run `python3 -m election_bot analysis` for entry-price and entry-gap tables at five and fifteen minutes. Add `--json` for coverage, sampling delays, race/day counts and exploratory uncertainty intervals; use `--paper` for the simulation journal. Old trades without an exact order-linked reference remain in the unknown-gap bucket. See [entry analysis](docs/entry-analysis.md) for definitions and limitations.

### Patient entry pilot and entry ranking

The optional patient route samples approximately 10% of eligible reference-entry attempts deterministically, splitting those between patient and control pricing. Both sampled arms are capped at 25 coins / 50 shares per order. Patient pricing improves the ask by one 0.005 tick when the spread allows it; its lifetime gross allowance is 500 coins. Unsampled entries retain normal sizing. These are configurable pilot limits, not increases to portfolio limits.

An acknowledged patient order may rest for up to five seconds locally, bounded by its original SIG expiration. Monitoring checks signal validity and stops. The bot then cancels any remainder and confirms fills before continuing. Recovery cancels/reconciles immediately; it never renews patience, changes the original key or chases the ask. A slow network call can outlast the local wait; the original exchange expiration remains in force. No live order has been placed to validate the new waiting path during development.

`python3 -m election_bot patient-study` (optionally `--json` or `--paper`) reports closed-attempt fill rates, pending reservations, improvement against each order’s own quoted ask, and bid movement relative to its own decision-time bid. It does not equate differences in absolute prices across markets with improvement or adverse selection. Missing observations and patient fallbacks are separate. These descriptive groups are not matched causal evidence.

Optional `entry_ranking.enabled` defers buys until the current scan batch is complete and ranks still-fresh candidates by quantity times signal value minus ask and both buffers. Patient/control use the same ask for scoring. Exits run first; every selected candidate repeats normal preflight and reservation checks. Stale candidates are dropped. This is a partial batch ranking, not a whole-portfolio optimizer or calibrated expected profit. Slower scans may leave fewer fresh candidates.

Both switches are off in the example configuration. After changing the local live configuration or code, restart the watcher once. See [pilot operation and rollback](docs/patient-entry.md).

For a focused read-only report on stale feeds and unsold positions, run `python3 -m election_bot diagnostics` (or add `--paper`). No SIG key or network connection is needed to read this report. It uses newly recorded events; older generic skip messages cannot be reliably attributed to a venue.

`quote_diagnostics_last_24h` identifies SIG, Kalshi and Polymarket separately, with local/source ages, stale or future timestamps, missing two-sided books, crossed books and excessive reference spreads. API request failures also carry a venue where known. SIG source age uses its engine capture time; Kalshi uses HTTP Date minus cache Age; Polymarket uses its book timestamp. Reference local age starts at each HTTP response receipt; SIG book local age starts at construction. Transport diagnostics separately retain request duration. A recently retrieved book with an old source timestamp could be unchanged or stale; this report does not distinguish those cases or relax the freshness limit. Non-finite timestamps are rejected explicitly. Failed checks are grouped by scan, preflight, pre-submit or performance-only observation, rather than mixed into one count.

`exit_diagnostics_last_24h` explains owned-position checks: selling disabled, insufficient reference ask depth, SIG price not yet converged, profit below the configured minimum, FIFO profit below the minimum, or order/depth limits too small for one share. It records SIG's bid, the required price for each exit route and relevant cost/depth values. The overpriced and profitable-convergence routes are alternatives; neither must satisfy the other's price condition. Cooldowns, open orders, news pauses, inventory mismatches and failed quote checks appear as **not evaluated**, because the guard prevented an exit decision. An eligible exit is still only a proposal until the order fills. Scan and preflight counts remain separate to avoid presenting two checks as two trading opportunities. The report preserves the original strategy and risk limits.

Run `python3 -m election_bot performance` in a separate terminal, or after stopping the bot. Add `--paper` for simulated activity. The command reads local state only. It reports filled orders/shares, open cost, risk capital, realized P&L after configured buffers, recent scan timings and skip reasons. Decision checks identify price gaps, reference depth, available budget, opposite inventory and other blockers. Counts are diagnostic observations, not independent trading opportunities; a final preflight may assess an opportunity twice.

`scan_performance_last_24h` separates scheduled visits from successful quote checks, with median, 95th-percentile and maximum revisit times, both by market and by scheduling lane. A cooldown, failed request or news pause counts as a visit, not a successful quote check. Final preflight rechecks and the SIG-only performance observations are excluded from quote-revisit counts. The first check has no interval; subsequent intervals include any downtime between restarts. Reports group `priority_v1` separately from `news_round_robin`; a policy change resets interval baselines while preserving the ordinary scan cursor, so intervals do not straddle two policies. These descriptive metrics measure coverage and responsiveness, not proof of additional profit.

Each scanned held position gets a bid-depth liquidation estimate. If there is insufficient displayed liquidity, full-position P&L is left unavailable; the report shows executable quantity and quote age.

After each buy, measurements at **1, 5, 15 and 60 minutes, plus 24 hours**, estimate selling the original filled quantity into SIG's displayed bids, across available price levels, after both entry and exit buffers. NO buys use the complementary book. Quotes must be fresh and captured after the target time. Thin books remain unmeasured until enough depth is observed within the window; partial exits are never presented as a full-size return.

At the beginning of each active execution cycle, a separate observation pass reads up to four due SIG books, ordered by the earliest deadline. One book can measure multiple buys. This pass does not require Kalshi, Polymarket or news signals, submits no orders, and shares the existing SIG rate limiter. Authentication and rate-limit errors still pause/halt through the normal recovery policy. Regular trading scans also collect observations. This adds at most four book reads per cycle (each may use the existing short retries); it is best-effort polling, not a guarantee of exact-time quotes.

The allowed delay after each target is 60 seconds, 2 minutes, 3 minutes, 15 minutes and 6 hours respectively. `performance` reports coverage, missing and pending observations, positive-result percentage, P&L per share, return on entry cost, and average/maximum sampling delay, both overall and by market/outcome. Expired windows remain missing even if a later quote becomes available. Low coverage can bias measured results toward liquid markets; it is not evidence that the missing buys performed well.

The shorter horizons apply only to buys filled after the updated bot first starts; older 1-hour/24-hour observations remain intact. The migration is additive and does not reset orders, accounting or budgets. Reporting itself stays read-only. These are hypothetical outcomes even if the actual position was sold earlier; do not add them across horizons or treat repeated buys in one race as independent evidence. This is forward monitoring, not a backtest or proof of predictive advantage. Realized P&L still uses FIFO bot costs and configured buffers, not verified exchange fees. Trading thresholds, order sizing and limits are unchanged by this measurement upgrade.

## Reference leadership and shadow entries

`python3 -m election_bot leadership` reports whether reference gaps and recent moves precede SIG price changes, with explicit missing observations at 5 and 15 minutes. Add `--json` for day/race breakdowns and paired comparisons, `--hours 24` for a shorter lookback, or `--paper` for the paper journal. It reads local data only.

After restarting the active bot, it also records prospective entry proposals using the current conservative reference, Kalshi's bid and Polymarket's bid, while retaining common liquidity/risk checks. These records cannot submit orders or change live decisions. Their hypothetical outcomes assume displayed fills, use both buffers and require sufficient future SIG depth; they are not realized returns or an independent portfolio simulation. See [the methodology and limitations](docs/reference-leadership.md). Existing snapshots support the leadership report; historical shadow decisions are not backfilled.

## Exit-depth comparison and stale-feed diagnostics

After restarting, `python3 -m election_bot exit-study` compares the original convergence/overpricing rules against two simulated depth alternatives: use the reference side relevant to the exit route, then size from SIG liquidity while retaining reference-depth gates. Add `--json --hours 24` for detailed proposals and follow-up outcomes. This report excludes the separately configured profit-target route and does not enable exits or change budgets. See [the exit-depth methodology](docs/exit-depth-study.md).

`diagnostics` now separates HTTP receipt/cache timing from exchange book age. Reference local age starts at each HTTP response receipt; source timestamps and freshness requirements stay intact. The new fields help investigate old Polymarket timestamps without treating recent retrieval as proof of current prices.

## Clock verification and feed workers

Run `python3 -m election_bot clock-check` for a read-only SIG timing check. Execution verifies the local clock before orders and halts when fresh response timing cannot establish agreement within five seconds. Public reference requests now reuse a bounded two-worker pool with explicit shutdown. See [clock and worker behavior](docs/clock-and-reference-workers.md) for uncertainty, recovery and timing details.

## Alternative contracts and settlement review

`python3 -m election_bot contract-review` groups the saved Democratic, Republican and Independent Party contract listings by race, flags settlement-basis differences and shows a combined race budget. Use `--race 2026:senate:NE --json` for details or add `--refresh` to fetch fresh rules and books for that one configured race. Default reporting is offline; refresh uses GET requests only. See [contract-review methodology](docs/contract-review.md).

Structural identity checks do not establish equivalent payouts. Candidate mappings stay disabled, basis surcharges remain uncalibrated, and no new party contracts trade automatically. Active execution now shares race coin budgets and cooldowns across configured sibling contracts and rechecks limits when reserving orders. [Shared race controls](docs/shared-race-controls.md) explains persistent assignments and the settlement findings. An optional [multi-contract mode](docs/multi-contract-routing.md) now supports independent serial routing with fingerprint-bound settlement reviews and gross exposure. It remains off in the installed configuration; no live acceptance records or additional contracts were enabled.

## Full competition coverage

The current audit covers all 237 SIG contracts (117 contests). The saved configuration enables **107 races: 30 Senate, 31 governor, 46 House**. Ten contests remain excluded because the reference identity or settlement rules could not be verified. See [the complete coverage report](COVERAGE.md), including each exclusion. Separate Republican and independent contracts are not enabled alongside the selected Democratic-party contract. Buying its NO is not identical to buying Republican YES.

Run `python3 -m election_bot coverage` for local counts, limits and exclusions. Changes take effect only after restarting the bot. The scanner rotates through bounded batches and persists its position even after an order or restart. News-priority checks alternate with ordinary coverage to prevent starvation; unprocessed news remains queued in memory until its race is scanned. A restart can lose that priority queue, but persisted news pauses and normal race scanning remain. Public Kalshi/Polymarket book requests run concurrently using separate clients and one reusable two-worker pool. Quotes are never cached. Contract metadata is cached for up to 15 minutes while scanning, then refreshed and rechecked against the pin before every actionable order. News also invalidates the corresponding cached metadata. Before submission, account, orders, holdings and quotes are refreshed. Up to four orders execute sequentially per twelve-race batch, with confirmed accounting between them; the next batch resumes after one second. SIG read/write pacing remains enforced. This is faster polling, not a streaming or low-latency market maker.

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
- SIG can reject an expired recovery replay with `expirationDate must be in the future.` Explicit `recover` then has a narrow fallback after expiration plus 90 seconds: it checks complete, checkpoint-consistent exchange order history and matching account inventory twice. Every remote order must already be accounted for locally, and every locally acknowledged order must appear with matching terms and fills. Only then does it atomically record the evidence and release the unused reservation as a zero fill. Unknown orders, missing history, changed fills, inventory differences, other HTTP errors or incomplete projections retain the reservation. It never extends the expiration, invents an order ID or replaces the idempotency key. Normal `run` does not invoke this fallback automatically. The live watcher can invoke it through its separate `recover --wait` child after an uncertain submission.
- Missing timestamps, closed markets, changed rules, thin/crossed books and external-venue disagreement prevent trades.
- SIG reads returning HTTP 502, 503 or 504 are tried up to three times with short delays. During continuous running, transient GET failures that escape a scan (network errors or HTTP 408/429/500/502/503/504) log `connection_pause` and retry with delays of 5, 10, 20, 40, then 60 seconds, honoring any longer server-requested delay. No new orders are submitted during the pause; each resumed cycle reconciles pending orders before scanning. Recovery logs `connection_restored`. Market-specific metadata/book failures can skip that race. Authentication, inconsistent fills, unknown submissions and failed writes still halt with reservations intact. The in-process read retry never replays submissions; `watch --live` can launch a separate bounded recovery stage for uncertain mutations. Continuous startup also retries transient tournament connection failures, logging `startup_connection_pause` until connected or stopped. Certificate verification errors remain explicit failures; TLS verification is never disabled. `--once` and utility commands still exit on unrecovered read failures. Ctrl-C and the stop signal interrupt recovery waits.
- Any existing open tournament order blocks new bot entries. Only orders recorded as this bot's own are cancelled. Avoid trading the same account from another process while this one runs: the API does not provide an atomic account lock.
- API requests have eight-second timeouts. SIG calls are paced below documented read/write limits for one process. The local process lock covers this checkout; another copy of the program or another computer has a separate lock.

`.runtime/live.sqlite3` contains reservations, fills/cost bounds, book snapshots and decisions; `.runtime/paper.sqlite3` contains simulations. Never delete the live journal to get around a halt or budget. Keep it with the bot across restarts. Journals bind to the API key and tournament; rotating the key requires deliberate state migration, not deletion. `.env`, `config.json` and `.runtime/` are gitignored.

Kalshi's REST order book has no exchange-event timestamp in the response used here; its freshness check uses HTTP Date/cache Age plus local receipt time. Polymarket supplies a book timestamp, and SIG supplies an engine capture time. A quiet but old Polymarket snapshot can conservatively block trading. REST polling is not a low-latency feed.

## Next useful extensions

Use the recorded news events, price reviews and later snapshots to test whether external prices or news actually lead SIG. Further extensions include structured polling data, additional official election-result sources, websocket price feeds, correlated exposure limits and exit logic. This starter intentionally does not import the older Kalshi market maker or the much larger NautilusTrader framework.

API references: [SIG API](https://sig.thesuper.market/api/v1/docs), [Kalshi public market data](https://docs.kalshi.com/getting_started/quick_start_market_data), [Kalshi order book](https://docs.kalshi.com/api-reference/market/get-market-orderbook), [Polymarket prices and order books](https://docs.polymarket.com/market-data/prices-order-books).


### Automatic crash recovery

Use `python3 -m election_bot watch --live` instead of `run --live` to supervise the bot in the terminal. Stop the existing bot with Ctrl+C first. Omit `--live` for supervised paper mode. The watcher restarts unexpected worker crashes with 5/10/20-second delays, at most three times in 15 minutes, then sets STOP for review. For a SIG submission/cancellation timeout or transient write failure, or an unresolved submission found on startup, the live watcher runs `recover --wait` before restarting trading. Recovery retains the original request and key, waits past expiration plus 90 seconds for unknown requests, refreshes SIG timing, and must reconcile successfully. Recovery failures consume the same three-attempt budget. The watcher never clears the journal, resets budgets, or runs `resume` automatically.

Ctrl+C, `stop`, normal completion, and known accounting, database, authentication, hard clock or risk halts do not auto-restart. Temporary API read outages already retry within the worker. Keep the terminal session running and the Mac awake; this is not a login/background service. `status` includes the watcher's last recorded state, which is not a liveness check. See [supervision behavior](docs/automatic-recovery.md).

Slow, expired or wall/monotonic-discontinuous SIG clock samples cause `clock_pause` and fresh-cycle retries rather than terminating continuous execution. Clock adjustments and suspend/resume require a new timing sample; the old sample is never accepted or refreshed. Invalid numeric evidence and definite excessive offsets on fresh consistent evidence still halt. See [clock verification and recovery](docs/clock-and-reference-workers.md).


### Review fixes and event retention

Enabling `profit_target_enabled` with no `profit_exit_mode` now selects trend confirmation and partial exits. Fixed mode must be explicitly selected. Existing installed trend settings and trading budgets are unchanged.

The inventory-aware engine checks exposure metadata for existing local holdings and pending orders during startup. It logs `exit_coverage` after reconciliation and whenever configuration-level coverage changes: owned positions, disabled/unmapped contracts, and disabled selling. This is not a guarantee of an executable exit; quotes, news and account checks still apply. `performance` includes the latest coverage event from the past 24 hours.

Disabled news mappings can still protect held contracts: disputed-result ingestion now records their persistent dispute without rolling back the feed. Mapping-name and exchange-based dispute checks remain active.

Old high-volume diagnostic events are preserved in compressed archives before removal from the active SQLite journal. See [event retention](docs/event-retention.md) for the seven-day hot window, reporting limitations and failure guarantees.

### Independent fair-value research

`python3 -m election_bot fair-value --demo` demonstrates an independent polling model without real data or trades. `fair-value --seed-public` imports a dated, source-checked New Hampshire Senate pilot; `fair-value` reports estimates and forward price comparisons. After restart, ordinary scans record research proposals alongside the current market strategy. These research commands never submit orders. A separate opt-in [live model route](docs/model-live.md) can buy from fresh independent forecasts within its own allowance and existing portfolio limits; its provisional probabilities remain uncalibrated. Hourly public polling collection now supports reviewed NH, IA, NC, MI, OH and TX Senate bindings when `research_polling.enabled` is true. Use `fair-value --collect-public` for a one-shot import and `fair-value --collector-status` for health. Other races/offices remain staged. Versioned evidence and quarantine audits are available; see [independent fair-value research](docs/independent-fair-value.md) for commands, source limitations and the remaining work.

The [October 10 strategy review](docs/strategy-review-2026-10-10.md) records why the initial exit-depth and single-reference studies do not yet justify changing live rules.

The [local capacity settings](docs/capacity-settings.md) document the October 11 deployment, turnover and exposure limits, plus the current limits of capital rotation. Live configuration remains local and is not overwritten by Git pulls.

`fair-value --seed-fundamentals` adds a research-only historical-lean comparison using official 2020/2024 FEC results for NH, IA and NC. Reports compare neutral and historical priors; normal scans record separate forward price outcomes after restart. The configured forecast and live trade rules are unchanged. See [fair-value methodology](docs/independent-fair-value.md#historical-lean-comparison-october-11-increment).

### Low-price profit-target research

`python3 -m election_bot low-price-study --hours 24` compares full profit-taking with a half-sale plus trailing runner on recorded 1–10-cent contract entries. It includes both cost buffers, partial depth-limited exits, stops, a six-hour holding limit and explicit unresolved outcomes for quote gaps. Add `--json` for complete paths. This is a read-only retrospective study, not live execution or a portfolio backtest; it needs no bot restart. See [low-price study assumptions](docs/low-price-study.md).

`python3 -m election_bot low-price-study --hours 24 --entry-mode compare` additionally tests SIG momentum entries against the original external-gap rule. Momentum requires three spaced, rising bid/ask observations and a fresh breakout; an observed failed breakout can trigger exit. Both modes retain costs and depth limits, and results stay separate. This remains research-only; see [momentum assumptions](docs/low-price-study.md#sig-momentum-comparison-october-13-increment).

### Strategy performance attribution

Run `python3 -m election_bot strategy-performance` (or add `--json` / `--paper`) for confirmed FIFO profit and remaining cost by model versus other entries, model allowance remaining, and recent blocking reasons. The report runs read-only without stopping the bot. Hypothetical markouts stay separate from realized profit. See [methodology and limits](docs/strategy-performance.md).

### Portfolio drawdown pause

The optional [drawdown control](docs/drawdown-control.md) pauses new buys when buffered portfolio P&L, including unsold positions valued through SIG bid depth, falls 2,500 coins below its recorded peak. The local setting resumes buys below 1,250 coins of drawdown. Incomplete valuations also block buying; existing exit rules remain available subject to their normal safeguards. State survives restarts. Run `python3 -m election_bot drawdown-status` for a read-only check; restart the worker once to load this control.

### Declining-position entry guard

The optional [position entry guard](docs/position-entry-guard.md) blocks additional purchases after a confirmed two-cent decline in both SIG bid and ask. It requires spaced scan history, persists the pause through restarts and partial sales, and requires two later recovery observations before additions resume. Both market-price and model additions pass it; eligible exits retain priority. The local configuration enables it after worker restart, with a short observation warmup for existing holdings.

### Partial risk-reduction exits

The optional [risk-reduction exit](docs/risk-reduction-exit.md) sells up to 25% of a holding after three spaced SIG observations show a loss of at least five cents per share, including entry and exit buffers. It works for YES and NO holdings, uses fresh SIG prices, and does not require an external reference price once the contract binding has been verified. Confirmed risk sales have a 30-minute per-contract cooldown. Existing order caps and safety checks still apply. The local configuration enables it after a worker restart; the example configuration leaves it off.

### Capital-rotation research

The [capital-rotation study](docs/capital-rotation.md) records hypothetical partial sales that could fund a stronger external-price opportunity in another unowned race. It respects simulated post-sale capital and exposure limits and compares rotation with keeping those shares at 15-minute and one-hour horizons. It uses existing scan quotes and does not change live trades. The local configuration enables collection after a worker restart. Run `python3 -m election_bot rotation-study --hours 24` for measured comparisons and missing-data counts, or add `--json` for recent proposal details. External prices are labeled as proxies; no live rotation or calibrated fair-value claim is introduced.

### Expanded historical fundamentals

Historical-lean research now covers all six reviewed Senate catalog states: NH, IA, NC, MI, OH and TX. The additional official FEC counts use a separate versioned dataset so existing priors remain reproducible. Stop the current watcher, run `python3 -m election_bot fair-value --seed-fundamentals`, then restart it to import and observe the expanded comparisons. This does not change the live model's primary prior or trading limits. See the [October 16 methodology update](docs/independent-fair-value.md#historical-fundamentals-for-all-six-catalog-states-october-16-increment).


### Paired evaluation of historical priors

`python3 -m election_bot fair-value --hours 24` now compares neutral and historical model decisions on matching recorded evidence and quotes, including one-share buys versus holding cash. It reports cost-adjusted paired differences at 5 minutes, 15 minutes and one hour, with per-race details in `--json`. Missing outcomes stay unmeasured and observations are spaced by race. This read-only report needs no watcher restart and changes no live settings. See [paired evaluation](docs/independent-fair-value.md#paired-prior-evaluation-october-17-increment) for coverage and limitations.


### Prospective threshold simulations and rejection reasons

After one worker restart, independent polling research also collects **3¢/4¢/5¢ near-miss simulations** with 100 simulated coins per model/threshold/horizon, one-share entries, both cost buffers, depth limits, and persistent cash/positions. It logs polling, freshness, price, uncertainty, depth and cash rejection reasons. Run `python3 -m election_bot fair-value --hours 24` or add `--json` for per-race detail. Old observations are not converted into trades; live thresholds and budgets remain unchanged. See [near-miss assumptions and activation](docs/independent-fair-value.md#prospective-near-miss-portfolios-and-rejection-diagnostics-october-17).


### Entry-gate counts and model-bound diagnostics

After a worker restart, `scan_complete` includes per-gate counts split by scan, preflight and reservation phase, including summaries for early waits and halts. This distinguishes missing edges from drawdown, news, exposure and model-budget blocks; overlapping counts are not independent rejected orders. See [gate count semantics](docs/entry-gate-metrics.md). Drawdown status now shows the strict recovery target without changing its permanent peak. Live-model records also expose the actual probability haircut and support an optional absolute minimum, disabled by default; see [model tail handling](docs/model-live.md#tail-sensitivity-and-optional-execution-haircut).


### Independent-model opportunity ranking and rotations

After a watcher restart, the enabled rotation study also ranks fresh independent-model opportunities and compares keeping an owned position with funding another race. Three spaced confirmations precede a simulated sale; a later destination quote must pass refreshed model/capital/depth checks before a hypothetical purchase. Failed switches remain simulated cash. Run `python3 -m election_bot rotation-study --hours 24 --json` for `independent_model` rankings and paired outcomes. This remains research-only, with no live rotations or threshold/limit changes. See [model-rotation assumptions and activation](docs/capital-rotation.md#independent-model-ranking-and-sequential-rotation-trials).

## Route performance control and expanded polling coverage

`python3 -m election_bot performance-throttle` shows a prospective route sizing assessment (add `--json` or `--paper`). It defaults to report-only. If enabled, two sufficiently covered, underperforming UTC days reduce new-buy quantities on that route to half; exits stay available. It separates bid movement from spread and cost-buffer effects, persists its state, and rechecks sizing during reservation. See [policy and activation](docs/performance-throttle.md).

The polling catalog now includes reviewed Massachusetts and Rhode Island Senate pairings. A healthy Senate ingestion no longer fails solely because an auxiliary Governor/House staging download failed. Bound races still need to pass the existing forecast-quality checks; this does not enlarge the live model pilot. See [coverage review and sources](docs/polling-coverage-2026-10-19.md).

## Research follow-ups, entry timing and polling coverage

These three additions are research-only. They do not change live sizing, entry thresholds, the drawdown buy pause or live model inputs. See [details and activation](docs/research-followups-entry-timing.md).

- `research_followups` (disabled by default) requests a bounded number of extra SIG-only books after research anchors and simulated positions, so 5/15-minute study outcomes stop going missing. `python3 -m election_bot research-followups` shows coverage.
- `python3 -m election_bot entry-timing-study` compares immediate entry with waiting for a fresh, sustained SIG bid improvement, splitting each outcome into price movement, half-spreads and buffers.
- `python3 -m election_bot polling-coverage` explains, for each Senate contract, its binding, accepted polls and why an estimate is or is not produced. New Arkansas, Minnesota, New Mexico and South Carolina bindings plus newly reviewed pollster families are research-tier only. The live model ignores them.


## Settlement-value drawdown and exits

The competition scores balances after settlement, and the platform charges no fees. `python3 -m election_bot settlement-review` replays holdings, past sales and the drawdown pause at SIG bid versus Kalshi/Polymarket fair value. `drawdown_control.valuation: "reference_fair"` makes the buy pause track expected settlement value without the 1¢ buffer. `settlement_exits` blocks sales below reference fair value. Both are off by default; see [settlement value](docs/settlement-value.md) for evidence and activation.

## Election-night mode

`election_night` (off by default) buys races both Kalshi and Polymarket bid at 0.95+ when SIG still lags by at least 2¢. It runs only from Nov 3 6pm ET to the Nov 4 noon close, and for this route only it overrides generic news pauses (never disputed results). Rehearse with `python3 -m election_bot election-night-rehearsal`. See [election-night mode](docs/election-night.md) for the rule, overrides, pre-election checklist and activation.

Inside that window, called-race buys also bypass the drawdown pause, the post-restart position guard and net-share exposure caps; per-race and coin limits still apply. Each bypass can be switched off.

## Pre-election capital rotation

`capital_rotation` (off by default) sells part of a holding whose SIG bid is within 2¢ of reference fair value when that funds an ordinary entry blocked only by coins, with a net gain of at least 3¢ per share sold. It never runs during the election-night window. See [pre-election rotation](docs/pre-election-rotation.md).

## Swing stress test and party-pair scan

`python3 -m election_bot swing-risk` stress-tests holdings against a uniform national swing at reference fair value. `python3 -m election_bot pair-arbitrage` scans SIG's per-party markets for baskets priced away from 1 (paced reads, no orders). See [risk and arbitrage reports](docs/risk-and-arbitrage-reports.md).
