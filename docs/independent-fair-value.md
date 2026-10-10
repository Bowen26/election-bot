# Independent fair-value research, version 1

This is a research route with no broker interface and no live order mode. The existing entry/exit strategy, account limits, live configuration and holdings are unchanged. A `fair_value_shadow` event can identify a SIG price that differs from an independent polling estimate even when Kalshi and Polymarket do not confirm it. The event cannot become an executable `Signal` or reserve funds.

## Commands

Run from the bot folder:

```sh
python3 -m election_bot fair-value --demo
python3 -m election_bot fair-value --seed-public
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

The prior defaults to a neutral 0-point margin with 12-point standard deviation. It is **not** a historical-fundamentals estimate. An optional sourced `prior` supplies `margin_pp`, `sd_pp` (8–30), `published_at`, `source_url` and a `method` explanation. Historical partisan lean, incumbency and a national-environment feed are not automatically built yet. Neither SIG, Kalshi nor Polymarket prices enter the probability calculation.

For each selected poll, the sampling variance of its D-minus-R margin is approximated from its candidate shares and sample size (capped at 2,000), multiplied by an assumed design effect of 1.5. When supplied, the reported candidate margin of error supplies a conservative margin-error variance floor `(2 * MOE / 1.96)^2`. We add a 3-point pollster-error variance, downweight age with a 14-day half-life, apply a 0.75 factor to RV polls, and halve unknown-sponsorship weight. These likelihood precisions update the normal prior. A shared 4-point error floor and time-to-election uncertainty remain in the predictive distribution; adding polls cannot drive uncertainty to zero.

`P(D wins)` is the normal distribution's probability that the final margin is positive. The contract's reviewed YES party controls orientation; NO is the complement of **that same contract**, not a separate Republican contract. `yes_low` and `yes_high` shift the mean by minus/plus two margin points. They are **sensitivity scenarios, not confidence intervals**. All coefficients are provisional and uncalibrated; the output is not a proven fair price or guaranteed settlement value.

## Prospective comparison and time integrity

After restarting the updated inventory-aware bot, normal scan snapshots also read the research database if present. Imports become visible on later scans without another restart. Estimates include model version, policy constants, input hashes and an estimate ID. Missing/stale inputs, changed contract fingerprints, damaged evidence or missing files cannot authorize an order. Expected research-input errors record an unavailable observation and leave the live strategy intact.

Published/available time and **local receipt time** must both precede prediction time. Newly imported historical polls cannot retrospectively manufacture forecasts for earlier trading snapshots. The dated seed uses first observed time where the precise original release time could not be established, with that basis stated in the record. Raw calendar fieldwork dates are conservatively represented through local end of day. Revisions are selected as known at the prediction time; a withdrawn latest version does not revive its earlier value.

The model proposes one-share price ideas using SIG's executable ask, the configured cost buffer and an edge threshold of at least 5 cents against the conservative sensitivity value. Fresh SIG quotes, one-share depth and valid ticks are required. The separate market comparator uses the existing both-reference-bids entry rules. Both are price studies: account funds, actual positions, news pauses, cooldowns, existing exits and correlated portfolio exposure are not simulated. No model-based sell behavior or blending weights are enabled in this stage.

`fair-value --hours 24` reports recorded candidates and 5-minute, 15-minute and 1-hour forward SIG **bid** changes from the original ask, less two cost buffers. Observations must fall within 2-minute, 3-minute and 15-minute windows respectively, with timestamps after the target. Missing/pending outcomes remain separate from zero returns. Repeated scans are correlated. This is not realized profit or a fill backtest, and it does not measure election-outcome probability calibration. Lookback is capped at the seven-day hot-event retention window; old research events join the lossless archive.

Coverage is conditional on normal scans reaching the snapshot stage. The current scanner still fetches references first and skips some news/cooldown states, so an external-feed outage can prevent a research observation. Once observed, reference disagreement cannot alter the independent estimate. Wider verified race coverage, historical out-of-sample calibration, settlement-aware valuation and competition-end scoring remain follow-up work before any live model route. The VoteHub collector below now refreshes the initial reviewed bindings.

## Public source pilot

- [YouGov public survey directory](https://yougov.com/en-us/survey-results) links the [New Hampshire general-election PDF](https://d3nkl3psvxxpe9.cloudfront.net/documents/ttw_nh_20260921_lv.pdf): pages 1–2 provide sample, fieldwork and Senate vote shares. The filename is not used as the fieldwork date. Undecided preference allocation is recorded in the methodology note.
- [Rasmussen New Hampshire toplines](https://www.rasmussenreports.com/public_content/politics/public_surveys/toplines_new_hampshire_senate_october_2_4_2026) provide sample, field dates and Senate response shares. Poll mode/sponsorship are not independently established here; they are not fabricated.
- [UNH's September 24 release](https://scholars.unh.edu/survey_center_polls/1005/) confirms the general-election candidate identities; its polling figures are not imported because its PDF was unavailable to this implementation's retrieval tool.
- [Decision Labs API documentation](https://www.decisionlabs.ai/docs/api) describes the optional staging feed. Attribution: Decision Labs (decisionlabs.ai), CC BY 4.0; underlying source terms still apply.

## Automatic public polling collection (October 10 increment)

`fair-value --collect-public` performs one research collection, without a SIG key or order API. `fair-value --collector-status` reads the last result, success age, import counts and audit path. A successful fetch does not imply that a race has enough eligible polls for an estimate. `fair-value` reports that separately.

Set `"research_polling": {"enabled": true}` to collect hourly in the background during continuous `run` (including the child launched by `watch --live`). The example configuration defaults to false; the local configuration is enabled. Restart the bot once to load the worker. Imports from later collections become visible to existing research scans automatically. `run --once` and order recovery do not start polling workers. Network requests and research database writes run on a separate daemon thread, never on the order loop. STOP and shutdown prevent further collections; the worker checks for shutdown before importing. An in-progress socket read may finish after shutdown, but it has no broker access.

The [VoteHub API](https://votehub.com/polls/api/) supplies Senate, governor and House rows from a rolling 45-day fieldwork window. Attribution: **VoteHub, CC BY 4.0**, with normalization and filtering performed by this project. Underlying poll releases retain their source links. Requests follow the documented endpoints without credentials or redirects, use a 10-second socket timeout and bounded bodies/row counts, and check a 30-second elapsed budget between reads. Only the documented polls wrapper or a bare list is accepted; unknown pagination fails the round. A failed office fetch prevents a partial round from being labeled successful. A dedicated process lock and persisted hourly retry time avoid duplicate collectors and restart-driven retry storms. The explicit one-shot command bypasses the hourly cadence, but not the lock or STOP. Health older than two hours is marked stale. Feed errors preserve previous evidence with its original timestamps; the existing fieldwork-age rules still apply. Collector staleness is visible diagnostics, not a new live trading gate.

Initial reviewed candidate bindings are **New Hampshire, Iowa and North Carolina Senate**. Exact subject, office, candidate aliases, signed configured contract and fingerprint are required. These bindings cover both signed party orientations if already configured; the collector never enables contracts. Governor and House rows, other Senate races, and unknown pollster families remain staged until reviewed. Existing sourced fundamentals priors are preserved when the binding is unchanged.

Raw batches are stored under `.runtime/poll-staging/`; acceptance and quarantine audits plus health are under `.runtime/poll-collector/`. Inputs enter only `.runtime/fair-value.sqlite3`. Individual provider figures are structured third-party data, not independently checked against every original release. The adapter rejects malformed or future dates, unsupported populations, missing candidates, internal/partisan flags and answer totals over 100.5%. It favors LV over RV within the same pollster/race/fieldwork survey; competing same-population variants are quarantined. Identical duplicates collapse. Pollster-family names come from an explicit registry to reduce false independence.

VoteHub's calendar `created_at` is not treated as an exact publication timestamp. The collector records **first local observation of each changed provider version** as availability. Unchanged repeated collection does not refresh availability or evidence receipt time. Corrected versions remain append-only. A previously accepted provider ID that becomes invalid, conflicted or superseded receives a withdrawal, so its old value cannot remain silently active. Missing rows alone do not prove withdrawal because this is a rolling-window feed. Imports from other providers are not automatically cross-provider survey-deduplicated; the estimator's one-poll-per-canonical-pollster rule still applies.

The feed does not consistently distinguish undecided voters from omitted minor candidates. The adapter conservatively stores `100 - D - R` as an **upper bound including undecided**, not a measured minor-party share. The existing >5% other-share filter consequently excludes many of these inputs. This deliberately limits coverage until richer original-source details are reviewed; it does not renormalize two-party shares or manufacture zero third-party support. All coefficients remain provisional, and no independent-model buy/sell route is enabled.

The first live API check returned 95 Senate, 92 governor and 42 House rows. Thirteen Senate inputs passed normalization across three bound races. Of 82 quarantined Senate rows, 77 were outside the reviewed race scope, one used an unreviewed pollster, one was partisan, two exceeded the percentage-total limit, and one was a superseded RV subsample. Model eligibility is stricter than ingestion: the pre-existing NH seed supplies additional source-checked inputs; NC still needs enough eligible independent polling. Counts will change as the public feed updates.
