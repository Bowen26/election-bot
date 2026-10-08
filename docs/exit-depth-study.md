# Exit-depth study and reference-feed timing

This is the first targeted experiment after the 24-hour review. It records alternative exit proposals against the same real holdings and SIG book independently of the optional live profit-target route. It does not raise order, race, daily, portfolio or directional limits.

After restarting the bot, use a separate terminal in the bot folder:

```sh
python3 -m election_bot exit-study
python3 -m election_bot exit-study --json --hours 24
python3 -m election_bot exit-study --json --hours 48
python3 -m election_bot diagnostics
```

`exit-study` reads a consistent SQLite snapshot without opening an account connection or changing the database. `--paper` selects the paper journal. A missing journal stays missing. Older scans are not backfilled into hypothetical exit decisions. The report needs new `exit_shadow` records and follow-up quotes after restart.

## What is being compared

| Policy | Reference depth gate | Quantity cap from reference depth |
| --- | --- | --- |
| `legacy` (original rule) | Both references' outcome asks must meet the configured minimum for either exit route | Smaller reference ask depth |
| `route_depth` (simulated) | Ask depth for the overpriced route; bid depth for profitable convergence | Depth on the side used by the selected route |
| `sig_depth` (simulated) | Same route-specific gates as `route_depth` | None; SIG depth, holdings, exposure headroom, share maximum and per-order coin limit still apply |

The original convergence rule compares the SIG bid against both reference bids but currently requires reference ask liquidity. The first alternative tests using liquidity on the same side as that comparison. The second tests removing external depth from sale quantity because SIG is the execution venue. It retains reference depth as a minimum quality gate.

Both price routes, the entry/exit buffers, quote freshness, reference spreads and agreement remain required as applicable. NO positions use complementary books. The overpriced route has priority if both routes qualify; it can realize a loss, just as it can today. Convergence must pass both average-cost and actual FIFO profitability checks at the proposed quantity. If FIFO fails, the proposal is rejected rather than searching for a favorable smaller lot. Directional headroom still applies to sales, including sales that remove an offsetting position. Disabled selling suppresses every policy.

The pure alternatives cannot reserve capital, update holdings, submit orders or fetch extra quotes. Only a normal successful scan with bot-owned inventory records an experiment; preflight is excluded. The real live proposal proceeds through the existing contract, holdings, account, news, pending-order and final quote checks. Measurement aging errors are recorded as unavailable and do not replace the live choice. Database write failures retain normal fail-closed behavior.

## Interpreting the report

Proposal counts are repeated scan evaluations, not distinct executable trades. Additional proposals mean the legacy rule proposed no exit at that scan; larger proposals mean the alternative quantity exceeded the legacy quantity. Each stored proposal includes the original snapshot ID, FIFO basis, quantity, price, buffer and applicable limits. Displayed immediate P&L is a hypothetical full fill, not realized profit.

For follow-up comparisons, the report selects non-overlapping per-race anchors separately for 5- and 15-minute horizons, reserving the entire horizon plus a 2- or 3-minute observation window. The first valid post-target SIG scan is used. Its source and receipt times must be at or after the target. Preflight quotes, stale quotes and duplicate/conflicting identities are excluded. Reference validity at the future time is not required to observe SIG. The entry snapshot must have a valid SIG book and exactly match the prospective record; source validation of the experimental decision occurred when it was recorded.

Sell-now advantage is the original SIG bid minus the later SIG bid for the held outcome. Positive means selling earlier would have been better than selling that same quantity later; negative means holding until that observation would have been better. The same exit buffer is charged on both hypothetical sales and cancels from this difference. Immediate hypothetical P&L separately deducts FIFO entry cost (including its buffer) and the exit buffer.

The entire proposed size must fit the first future top bid. Insufficient size remains unmeasured; the report does not cherry-pick a later favorable quote or invent partial fills. Missing, not-yet-due and awaiting observations are distinct. The share-weighted averages are conditional on measured proposals, so different coverage and quantity can affect comparisons. Check counts and race coverage together. There is no independent portfolio, reinvestment, queue, latency or market-impact simulation. Real fills or buys can change the actual holdings used at the next scan. Do not sum repeated proposals or horizons into strategy profit or treat them as independent trials.

Review the first 24–48 hours of new records for (1) how often depth alone changes a proposal, (2) whether proposed prices clear the FIFO profit test, (3) whether waiting tends to improve or worsen executable bids, and (4) whether follow-up coverage is adequate. There is no automatic promotion or scheduled limit increase. The entry-size and budget settings remain unchanged.

## Polymarket stale-source investigation

The journal sample investigated on October 6 contained 991 Polymarket `source_stale` flags across the latest 1,000 quote-diagnostic events, while many local book ages were below a second. This sample is conditional on failures; it is not an overall feed failure rate or proof that the books were still current.

The HTTP client now records request start, response receipt, request duration, HTTP Date, cache Age and Date minus Age, without response headers, credentials or bodies. Each reference book's local observation time starts at its own response receipt, rather than after both concurrent reference requests finish. Waiting for a slow second venue can no longer make the first response appear newly received. Original exchange/source timestamps are retained.

The diagnostics group distinguishes an old source timestamp with a recent HTTP response, an old cached response, slow requests/local delay, and cases without enough transport evidence. Timing fields only explain failures. They do not replace the source timestamp, relax the 15-second freshness rule or prove that an unchanged-looking book is current. Old records have no transport evidence and stay explicitly unverified. Any future freshness-policy change needs separate validation of the feed's timestamp/cache semantics.

## Separate profit-target route

When `execution.profit_target_enabled` is enabled, the live bot can also sell at the existing buffered FIFO profit threshold before reference convergence. This depth study deliberately recomputes all three original policies with that feature disabled, so a live profit-target signal is never mislabeled as a legacy proposal. The study is not evidence of the profitability of the new route. See [profit-target exits](profit-target-exits.md) for behavior and validation.

Trend mode also leaves these three experimental policies unchanged: they are recomputed without the optional profit route. Trend decisions and their evidence appear in `decision.exit_check.trend`; the depth study is not a backtest of trend exits.
