# Independent fair-value research, version 1

The `fair-value` commands and `fair_value_shadow` events are research-only: they never submit orders or reserve funds. A separate, explicitly enabled [live model adapter](model-live.md) now reads fresh validated polling evidence and can generate SIG buys within its own allowance and the existing portfolio limits. It does not promote cached shadow events into orders. All model probabilities remain uncalibrated.

## Commands

Run from the bot folder:

```sh
python3 -m election_bot fair-value --demo
python3 -m election_bot fair-value --seed-public
python3 -m election_bot fair-value --seed-fundamentals
python3 -m election_bot fair-value
python3 -m election_bot fair-value --json
python3 -m election_bot fair-value --fetch-public
python3 -m election_bot fair-value --collect-public
python3 -m election_bot fair-value --collector-status
python3 -m election_bot fair-value --template /tmp/polling-draft.json
python3 -m election_bot fair-value --import-json /path/to/reviewed-polling.json
```

No SIG key is needed. The demo is synthetic and uses a temporary database, never real runtime evidence. The seed imports the dated, manually checked New Hampshire Senate pilot. It uses the configured contract identity and fingerprint; it does not enable a contract or assert new live settlement-rule acceptance. Repeated imports with unchanged content do not refresh evidence receipt times. This pilot does **not** automatically fetch later polling releases.

`--fetch-public` saves the public Decision Labs export under `.runtime/poll-staging/`, with required attribution. This is staging only. The feed inspected during implementation returned 426 records, the newest dated June 8, 2026, and included primary races. A fresh export timestamp cannot make those observations current. Its ambiguous date field and missing stage/fieldwork/publication details prevent automatic admission. No paid feed or account was created.

The draft supports up to ten configured Senate contracts and deliberately contains rejected placeholders. Fill verified candidate names, source links, election date, polling figures and dates before importing. A prior is optional. Evidence lives in the separate `.runtime/fair-value.sqlite3`; the trading journal is not used as a polling database. Import revisions are append-only and transactional. A duplicate latest version is ignored; corrections, withdrawals and deliberate reversions create versions with new receipt times. Poll identity cannot change its pollster, survey ID or race.

## What the model means

All margins are Democratic minus Republican vote-share **percentage points**. Poll shares must be raw shares, not renormalized two-party percentages or candidate win probabilities. Input records require a general-election LV/RV sample, candidate pair, fieldwork interval, explicit publication/availability timestamp, source URL, sample size and methodology note. Use a canonical pollster/panel-family name so syndicated surveys are not counted as independent organizations. Known campaign/party polls are excluded in this first version. Unknown sponsorship must be `null` and receives half weight; it is not silently treated as neutral.

We keep the latest eligible release per pollster, favoring LV over RV for the same fieldwork end. This conservatively suppresses tracking-wave/subsample duplication; it does not remove all shared panels or systematic bias. At least two pollsters are required, polls older than 45 days are excluded, and at least one must end within 21 days. Material other-candidate support above 5% blocks that poll. Runoffs, ranked-choice contests and competitive independent candidates require a different model; this version must not be applied to them.

The prior defaults to a neutral 0-point margin with 12-point standard deviation. It is **not** a historical-fundamentals estimate. An optional sourced `prior` supplies `margin_pp`, `sd_pp` (8–30), `published_at`, `source_url` and a `method` explanation. A source-backed historical-lean alternative is now available as a separate paired experiment (below). Incumbency and a current national-environment feed are not built yet. Neither SIG, Kalshi nor Polymarket prices enter the probability calculation.

For each selected poll, the sampling variance of its D-minus-R margin is approximated from its candidate shares and sample size (capped at 2,000), multiplied by an assumed design effect of 1.5. When supplied, the reported candidate margin of error supplies a conservative margin-error variance floor `(2 * MOE / 1.96)^2`. We add a 3-point pollster-error variance, downweight age with a 14-day half-life, apply a 0.75 factor to RV polls, and halve unknown-sponsorship weight. These likelihood precisions update the normal prior. A shared 4-point error floor and time-to-election uncertainty remain in the predictive distribution; adding polls cannot drive uncertainty to zero.

`P(D wins)` is the normal distribution's probability that the final margin is positive. The contract's reviewed YES party controls orientation; NO is the complement of **that same contract**, not a separate Republican contract. `yes_low` and `yes_high` shift the mean by minus/plus two margin points. They are **sensitivity scenarios, not confidence intervals**. All coefficients are provisional and uncalibrated; the output is not a proven fair price or guaranteed settlement value.

## Prospective comparison and time integrity

After restarting the updated inventory-aware bot, normal scan snapshots also read the research database if present. Imports become visible on later scans without another restart. Estimates include model version, policy constants, input hashes and an estimate ID. Missing/stale inputs, changed contract fingerprints, damaged evidence or missing files cannot authorize an order. Expected research-input errors record an unavailable observation and leave the live strategy intact.

Published/available time and **local receipt time** must both precede prediction time. Newly imported historical polls cannot retrospectively manufacture forecasts for earlier trading snapshots. The dated seed uses first observed time where the precise original release time could not be established, with that basis stated in the record. Raw calendar fieldwork dates are conservatively represented through local end of day. Revisions are selected as known at the prediction time; a withdrawn latest version does not revive its earlier value.

The model proposes one-share price ideas using SIG's executable ask, the configured cost buffer and an edge threshold of at least 5 cents against the conservative sensitivity value. Fresh SIG quotes, one-share depth and valid ticks are required. The separate market comparator uses the existing both-reference-bids entry rules. Both are price studies: account funds, actual positions, news pauses, cooldowns, existing exits and correlated portfolio exposure are not simulated. No model-based sell behavior or blending weights are enabled in this stage.

`fair-value --hours 24` reports recorded candidates and 5-minute, 15-minute and 1-hour forward SIG **bid** changes from the original ask, less two cost buffers. Observations must fall within 2-minute, 3-minute and 15-minute windows respectively, with timestamps after the target. Missing/pending outcomes remain separate from zero returns. Repeated scans are correlated. This is not realized profit or a fill backtest, and it does not measure election-outcome probability calibration. Lookback is capped at the seven-day hot-event retention window; old research events join the lossless archive.

Coverage is conditional on normal scans reaching the snapshot stage. The current scanner still fetches references first and skips some news/cooldown states, so an external-feed outage can prevent a research observation. Once observed, reference disagreement cannot alter the independent estimate. Wider verified race coverage, historical out-of-sample calibration, settlement-aware valuation and competition-end scoring remain follow-up work; the bounded live pilot does not establish that these validation steps are complete. The VoteHub collector below now refreshes the initial reviewed bindings.

## Public source pilot

- [YouGov public survey directory](https://yougov.com/en-us/survey-results) links the [New Hampshire general-election PDF](https://d3nkl3psvxxpe9.cloudfront.net/documents/ttw_nh_20260921_lv.pdf): pages 1–2 provide sample, fieldwork and Senate vote shares. The filename is not used as the fieldwork date. Undecided preference allocation is recorded in the methodology note.
- [Rasmussen New Hampshire toplines](https://www.rasmussenreports.com/public_content/politics/public_surveys/toplines_new_hampshire_senate_october_2_4_2026) provide sample, field dates and Senate response shares. Poll mode/sponsorship are not independently established here; they are not fabricated.
- [UNH's September 24 release](https://scholars.unh.edu/survey_center_polls/1005/) confirms the general-election candidate identities; its polling figures are not imported because its PDF was unavailable to this implementation's retrieval tool.
- [Decision Labs API documentation](https://www.decisionlabs.ai/docs/api) describes the optional staging feed. Attribution: Decision Labs (decisionlabs.ai), CC BY 4.0; underlying source terms still apply.

## Automatic public polling collection (October 10 increment)

`fair-value --collect-public` performs one research collection, without a SIG key or order API. `fair-value --collector-status` reads the last result, success age, import counts and audit path. A successful fetch does not imply that a race has enough eligible polls for an estimate. `fair-value` reports that separately.

Set `"research_polling": {"enabled": true}` to collect hourly in the background during continuous `run` (including the child launched by `watch --live`). The example configuration defaults to false; the local configuration is enabled. Restart the bot once to load the worker. Imports from later collections become visible to existing research scans automatically. `run --once` and order recovery do not start polling workers. Network requests and research database writes run on a separate daemon thread, never on the order loop. STOP and shutdown prevent further collections; the worker checks for shutdown before importing. An in-progress socket read may finish after shutdown, but it has no broker access.

The [VoteHub API](https://votehub.com/polls/api/) supplies Senate, governor and House rows from a rolling 45-day fieldwork window. Attribution: **VoteHub, CC BY 4.0**, with normalization and filtering performed by this project. Underlying poll releases retain their source links. Requests follow the documented endpoints without credentials or redirects, use a 10-second socket timeout and bounded bodies/row counts, and check a 30-second elapsed budget between reads. Only the documented polls wrapper or a bare list is accepted; unknown pagination fails the round. A failed office fetch prevents a partial round from being labeled successful. A dedicated process lock and persisted hourly retry time avoid duplicate collectors and restart-driven retry storms. The explicit one-shot command bypasses the hourly cadence, but not the lock or STOP. Health older than two hours is marked stale. Feed errors preserve previous evidence with its original timestamps; the existing fieldwork-age rules still apply. Collector staleness does not block ordinary market-price trades. The optional live model adapter requires healthy collection within two hours and blocks its own entries when that condition fails.

Reviewed candidate bindings now cover **New Hampshire, Iowa, North Carolina, Michigan, Ohio and Texas Senate**. Exact subject, office, candidate aliases, signed configured contract and fingerprint are required. These bindings cover both signed party orientations if already configured; the collector never enables contracts. Governor and House rows, other Senate races, and unknown pollster families remain staged until reviewed. Existing sourced fundamentals priors are preserved when the binding is unchanged.

Raw batches are stored under `.runtime/poll-staging/`; acceptance and quarantine audits plus health are under `.runtime/poll-collector/`. Inputs enter only `.runtime/fair-value.sqlite3`. Individual provider figures are structured third-party data, not independently checked against every original release. The adapter rejects malformed or future dates, unsupported populations, missing candidates, internal/partisan flags and answer totals over 100.5%. It favors LV over RV within the same pollster/race/fieldwork survey; competing same-population variants are quarantined. Identical duplicates collapse. Pollster-family names come from an explicit registry to reduce false independence.

VoteHub's calendar `created_at` is not treated as an exact publication timestamp. The collector records **first local observation of each changed provider version** as availability. Unchanged repeated collection does not refresh availability or evidence receipt time. Corrected versions remain append-only. A previously accepted provider ID that becomes invalid, conflicted or superseded receives a withdrawal, so its old value cannot remain silently active. Missing rows alone do not prove withdrawal because this is a rolling-window feed. Imports from other providers are not automatically cross-provider survey-deduplicated; the estimator's one-poll-per-canonical-pollster rule still applies.

The feed does not consistently distinguish undecided voters from omitted minor candidates. The adapter conservatively stores `100 - D - R` as an **upper bound including undecided**, not a measured minor-party share. The existing >5% other-share filter consequently excludes many of these inputs. This deliberately limits coverage until richer original-source details are reviewed; it does not renormalize two-party shares or manufacture zero third-party support. All coefficients remain provisional. The separately configured live model route applies the same evidence filters.

The first live API check returned 95 Senate, 92 governor and 42 House rows. Thirteen Senate inputs passed normalization across three bound races. Of 82 quarantined Senate rows, 77 were outside the reviewed race scope, one used an unreviewed pollster, one was partisan, two exceeded the percentage-total limit, and one was a superseded RV subsample. Model eligibility is stricter than ingestion: the pre-existing NH seed supplies additional source-checked inputs; NC still needs enough eligible independent polling. Counts will change as the public feed updates.

## Historical-lean comparison (October 11 increment)

`fair-value --seed-fundamentals` imports a historical-lean alternative for the already reviewed NH, IA and NC Senate bindings. No SIG key is needed. It stores `fundamentals_prior` in a new version of each research race, preserving the configured `prior` and every trading setting. The normal configured forecast remains the existing baseline. `fair-value` and `--json` additionally show `neutral_prior` and `historical_lean` estimates on identical polls. No extra estimate is produced for races without enough eligible polls. This command does not expand the reviewed race catalog.

Inputs are raw D/R presidential candidate totals from the [official FEC 2020 results](https://www.fec.gov/resources/cms-content/documents/2020presgeresults.pdf) (Democratic totals on printed page 2, Republican totals on page 8) and [official FEC 2024 results](https://www.fec.gov/resources/cms-content/documents/2024presgeresults.pdf) (pages 3 and 5). The small, reviewed JSON dataset is `data/research/fec-presidential-lean.json`. It records exact counts, source URLs, page numbers and verification date; priors include its content hash and the component calculations. National totals are those printed in these FEC PDF versions, rather than numbers substituted from other publications. Source documents can be revised, so retain the versioned input counts when reproducing a result.

For each election, compute the D-minus-R two-party margin `100 * (D - R) / (D + R)` for the state and nation, then subtract the national margin from the state margin. Average the two relative margins equally. The alternative prior mean is **half that average**, with a **12-percentage-point standard deviation**. Positive means a Democratic lean. This yields prior means of approximately +1.82 points in NH, -6.21 in IA and -1.92 in NC. These are assumptions about a Senate margin in a neutral national environment, not election-win probabilities or observed 2026 margins. The presidential-to-Senate transfer and use of two-party historical differences with raw polling margins are modeling approximations. The coefficient and uncertainty are not fitted or calibrated. Current national environment and incumbency adjustments are explicitly zero placeholders; candidate quality, ticket splitting and current election conditions are not inferred from these historical counts.

This adds independent historical evidence to an alternative polling estimate, not an external-market price. It retains the same pollster minimums, freshness checks, shared uncertainty floor and sensitivity calculation. The default configured forecast is unchanged. A sizable difference between variants is model sensitivity, not proof that the new estimate is better. Historical priors cannot generate a forecast without the required polling evidence.

New inputs receive their real local import time; the old availability of the elections does not authorize reconstructing earlier bot forecasts with data imported today. Repeated identical imports preserve availability. Changed inputs create new research race versions. The importer refuses changed contract/candidate bindings and never replaces a custom primary prior. It shares the collector lock, and subsequent automatic collections preserve the historical field on an unchanged binding. Tampered component counts, policy values or prior values fail validation against the bundled reviewed dataset.

After restarting, ordinary research observations include both variants and their one-share comparison against SIG asks. The existing configured-model and market-comparator observations remain separate. Forward reports now separate `neutral_prior_300`, `historical_lean_300` and the corresponding 15-minute/1-hour results. They use the same quote snapshots, ask-to-later-bid convention and two cost buffers. Missing outcomes stay missing. Repeated scans are correlated, and variants can propose different subsets of observations; separate averages alone are not a paired causal comparison. These remain displayed-price scenarios, not portfolio backtests or election-outcome calibration. Old events without variants are never backfilled.

Next work remains broader verified polling coverage, independently sourced current national environment and incumbency, out-of-sample calibration, and a portfolio replay with realistic fills and capital constraints. Resting orders and capital rotation are separate execution features and are not introduced here.


## Expanded Senate coverage (October 11 increment)

The research collector adds three candidate bindings, bringing the catalog to six Senate races. It uses only already-enabled, fingerprint-bound contracts; both Democratic and Republican YES orientations are supported. This does not enable new trading contracts or change entry/exit rules.

| Race | Democratic / Republican candidates | Candidate review source |
| --- | --- | --- |
| Michigan | Abdul El-Sayed / Mike Rogers | [Emerson's September release](https://emersoncollegepolling.com/michigan-2026-poll-el-sayed-and-rogers-locked-in-close-election/) |
| Ohio | Sherrod Brown / Jon Husted | [co/efficient's October release](https://battlegrounds.coefficient.org/oh/) |
| Texas | James Talarico / Ken Paxton | [UMass Lowell/YouGov October release](https://www.uml.edu/news/press-releases/2026/10092026-texas-poll.aspx) |

Ohio is the November 3 special general election for the unexpired Senate term, as specified in the [Secretary of State directive](https://www.ohiosos.gov/assets/directive-2025-54-writ-of-special-election-for-united-states-senate-election.pdf). The adapter accepts the provider's exact `2026 Ohio` subject and null seat label; it does not broadly accept special-election or primary labels. General plurality treatment is grounded in Michigan Election Law section 168.101 ([chapter text](https://www.legislature.mi.gov/documents/mcl/pdf/mcl-chap168.pdf)), [Ohio section 3505.35](https://codes.ohio.gov/ohio-revised-code/section-3505.35), and [Texas Election Code 2.001](https://tcss.legis.texas.gov/resources/EL/htm/EL.2.htm). These are research bindings, not new live settlement attestations. Ranked-choice/runoff races and unreviewed independent-candidate contests remain outside the catalog.

UMass Lowell/YouGov maps to the existing `yougov` family because the university identifies YouGov as its fieldwork provider. Joint releases therefore cannot manufacture a second independent pollster. Unknown families, partisan/internal polls, conflicting variants and candidate mismatches still quarantine. Poll acceptance is distinct from forecast eligibility: the two-family minimum, age checks and conservative 5% residual limit remain in place. No probability is invented for sparse races.

At the October 11 increment, the historical-lean dataset was limited to NH, IA and NC. The October 16 extension below fills this gap for the other three catalog states. `--seed-fundamentals` now reports `historical_state_not_reviewed` and leaves other race records untouched, rather than failing the whole import as coverage grows. New states retain the existing neutral primary prior unless separately supplied with a validated custom prior. There is no historical-lean comparison for those states yet.

Restart the existing worker to load this catalog, then hourly collection admits eligible new records. For an immediate research-only collection, use `python3 -m election_bot fair-value --collect-public`, followed by `python3 -m election_bot fair-value`. The model remains uncalibrated. These research commands cannot submit orders; live model entries require the separate opt-in adapter.


## Historical fundamentals for all six catalog states (October 16 increment)

The historical-lean comparison now supports **MI, OH and TX** in addition to NH, IA and NC. These are existing reviewed Senate polling bindings; the change adds historical inputs, not new polling races or new live contracts. The 2020 and 2024 presidential vote totals were checked against the [FEC 2020 results](https://www.fec.gov/resources/cms-content/documents/2020presgeresults.pdf) (printed pages 2 and 8) and [FEC 2024 results](https://www.fec.gov/resources/cms-content/documents/2024presgeresults.pdf) (pages 3 and 5).

| State | 2020 Democratic / Republican votes | 2024 Democratic / Republican votes |
| --- | --- | --- |
| Michigan | 2,804,040 / 2,649,852 | 2,736,533 / 2,816,636 |
| Ohio | 2,679,165 / 3,154,834 | 2,533,699 / 3,180,116 |
| Texas | 5,259,126 / 5,890,347 | 4,835,250 / 6,393,597 |

The added counts live in `data/research/fec-presidential-lean-expanded.json`. The original dataset is retained byte-for-byte. NH/IA/NC continue to use its original hash, values and availability; adding states cannot invalidate their stored priors or silently refresh their receipt time. New-state priors use the separate reviewed dataset hash. Imports remain append-only and idempotent, preserve custom primary priors, skip changed bindings, and never backdate new inputs. Future collections preserve these added alternative priors when the binding is unchanged.

The model policy is unchanged: half of the mean state-minus-national two-party margin across those elections, with a 12-point prior standard deviation. This is an uncalibrated presidential-to-Senate research assumption. Incumbency and the current national environment remain neutral placeholders. Insufficient independent polling still produces no forecast; historical data does not override that filter. The existing live model continues to use the configured primary prior, not this alternative.

Stop the existing watcher with Ctrl+C and wait for the prompt, then import and restart from the bot folder:

```sh
python3 -m election_bot fair-value --seed-fundamentals
python3 -m election_bot watch --live
```

This order avoids giving an older running worker new-state priors it cannot yet validate. The import reports the states actually bound to enabled contracts; support for six catalog states does not mean all six are enabled locally. Use `python3 -m election_bot fair-value --json` to inspect the historical/neutral comparisons. No new API credentials are required. National environment, incumbency, election-outcome calibration and separate portfolio replay remain follow-up work.


## Paired prior evaluation (October 17 increment)

`python3 -m election_bot fair-value --hours 24` now includes paired prior decisions and forward comparisons. `--json` exposes the `paired_priors` coverage, per-horizon and per-race details. This is an immediately available, read-only report: no worker restart or new import is needed, and it uses previously recorded variant observations without inventing missing ones. No live parameters or forecasts change.

The pair must contain neutral and historical forecasts for the same snapshot, base race version, prediction time, policy and polling weights/hashes. Both must be estimated. Entry prices and depth must match the original SIG snapshot. Conflicting duplicate events are excluded in full; unavailable variants, unmatched snapshots and invalid pairs are counted explicitly. Existing separate-route averages remain available, but they need not compare the same opportunities.

For each pair, each policy either hypothetically buys one share on its recorded eligible side or holds cash. Both policies start with the same cash, sufficient for either action; unspent cash has zero return. Trade profit is future executable bid minus original ask minus two per-share cost buffers. Cash profit is zero. The paired difference is historical profit minus neutral profit. Both-cash cases are counted separately and excluded from measured trade averages. Same-side pairs necessarily tie; opposite-side and one-policy-only cases reveal differences in selection. These are recorded research eligibility decisions, not the separate live-model adapter's eligibility or an instruction to trade.

At 5 minutes, 15 minutes and one hour, use the first fresh SIG scan in the existing outcome windows (2, 3 and 15 minutes). It must have one-share exit depth for every traded side. A thin first observation stays unmeasured; later favorable quotes cannot replace it. Missing and pending outcomes are separate from zero returns. Entry and future snapshots must pass the existing quote validation. The report prints sample counts and mean paired coin differences, never sums overlapping hypotheses into portfolio profit.

Each horizon selects the first valid pair per race, then waits through that horizon plus its observation window before selecting another. Selection happens before checking the outcome, including when both policies hold cash or the outcome is missing. Complementary contracts in the same race share this spacing. Moving the report start changes the first selected sample; this is a descriptive report, not a fixed randomized trial. The raw decision counts and mean YES-probability shift include all valid pairs; horizon metrics use the spaced subset. YES shifts describe contract orientation and must not be interpreted as a Democratic probability shift across mixed orientations.

The results remain correlated across races and days, depend on scan/retention coverage, and assume displayed fills. They do not model queue priority, execution latency, capital recycling, account limits or portfolio outcomes. Forward trading-price changes are not election-outcome calibration. No automatic promotion or threshold adjustment is attached to this report; broader verified inputs, calibration and cash-constrained portfolio replay remain later work.


## Prospective near-miss portfolios and rejection diagnostics (October 17)

After restarting the worker, ordinary scan observations with a polling research store also feed a separate research database: `.runtime/near-miss-live.sqlite3` (or `near-miss-paper.sqlite3`). No broker calls, live reservations, live balances, or trading settings are changed. This observer cannot place orders. It adds no network requests; it uses the already fetched SIG book and already computed forecasts. A research write/validation failure is logged as `near_miss_skip` without changing the live decision.

Run `python3 -m election_bot fair-value --hours 24` for a summary, or add `--json` for the `near_miss` section. Reports are read-only and never create missing databases. A restart begins new collection; the report itself cannot create simulated entries from old records. There is no evidence backfill or threshold selection using future outcomes.

**Frozen experiment:** neutral and historical prior variants, each at 3¢, 4¢ and 5¢ thresholds, separately at 5-minute, 15-minute and one-hour exit targets. Each of the 18 portfolios starts with **100 simulated coins** shared across all its races. These are experimental balances, not allocations of the competition account. Each entry buys one whole share, charges the recorded entry buffer, and reserves that cost. One position per race per portfolio prevents stacking and complementary-contract overlap. Balances and positions persist across worker restarts and report windows. Closing proceeds, less the recorded exit buffer, may fund later hypothetical entries. Losses reduce the balance; unavailable exits cannot manufacture cash.

The required entry edge is the conservative sensitivity probability minus the SIG ask minus **both** entry and exit buffers. This is stricter than the older research screen, which subtracts one buffer. The threshold names therefore do not directly match the earlier one-buffer candidate counts. The fixed quote checks require source and observation times within 15 seconds, a positive uncrossed book, spread at most 4¢, a 0.5¢ entry tick, and enough top ask **and bid** depth for one share to use at most 25% of the displayed size. Decisions require an estimated forecast bound to the same race and prediction time. No external market prices are used in these model comparisons.

Diagnostics record, per model/race/side/threshold, price-gap versus uncertainty rejection, entry/exit depth, spread, tick, and the conservative edge shortfall. Raw quote ages and stale/future failures are captured at the original scan time; the report does not reinterpret them using its later run time. Unavailable polling and missing historical variants are recorded separately. Portfolio-level cash limits, existing positions and cooldowns appear alongside these signal checks. Counts overlap across sides, thresholds and models and must not be added as independent opportunities. JSON provides minimum and latest edge shortfall in the selected window.

A position seeks an exit at the first fresh executable scan at or after its target time; both quote timestamps must be at or after the target. Exit depth is capped at the same 25% participation and exit ticks must be valid. If the first post-target quote is thin or otherwise nonexecutable, the position stays open and may exit later. A changed contract fingerprint blocks that exit. The outcome windows allow 2, 3 and 15 minutes respectively. Late exits and exits following an initial failed execution check recycle their actual simulated proceeds but are separately excluded from clean timely-horizon averages. Per-race cooldowns run through the horizon plus its window, regardless of an earlier fill. Open positions past the window are reported overdue, never assumed liquidated for zero profit or loss.

The report separates lifetime balances/open entry cost, realized results for exits in the selected window, clean timely exits, and pending/overdue positions. **Realized P&L excludes unrealized changes in remaining positions**, so it is not a portfolio total return or a fair ranking when coverage differs. Position histories and balances are retained; detailed observation diagnostics retain seven days. Changing the frozen policy is rejected rather than silently resetting the existing experiment.

This is a cash-constrained displayed-price simulation, not proof of real fills. It assumes one-share top-of-book execution without queue priority, sequential latency, market impact or real exchange rejection. It does not apply the live account's exposure/news/drawdown limits; observations themselves remain conditional on the normal worker reaching these scans. All variants are correlated. No automatic threshold promotion or live setting change is attached. Broader polling evidence, election-outcome calibration and execution-quality validation remain separate work.

To begin, stop the existing watcher with Ctrl+C and wait for the prompt, then run:

```sh
python3 -m election_bot watch --live
```

Use a second terminal for `python3 -m election_bot fair-value --hours 24`. No re-seeding of fundamentals is required for this update.
