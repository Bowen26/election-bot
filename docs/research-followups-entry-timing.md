# Research follow-ups, entry timing and polling coverage — October 19, 2026

All three changes are research-only. Live sizing, entry thresholds, the drawdown buy pause, model budgets and probability thresholds are unchanged. A running watcher keeps the code it started with, so nothing here takes effect until it is restarted.

## 1. Research follow-up quotes

**Problem.** Studies need a SIG quote 5 and 15 minutes after each anchor. Ordinary scans revisit a race irregularly, so the leadership study missed 7,156 of 8,725 five-minute opportunities, and some simulated exits were measured late. The existing follow-up scheduler (`Ledger.observe_due`) covered only real executions.

**Change.** `election_bot/research_followups.py` keeps a `research_followups` table in the journal:

- Each scan-phase snapshot schedules *anchor* follow-ups at +300 s and +900 s on the same non-overlapping per-race grid the studies use.
- A scan with a shadow price-gap candidate or a held position schedules *position* follow-ups at +60, +150, +300 and +900 s. These are prioritized and spaced at least 60 s apart per race.
- Any later SIG quote of that race, from a scan or a risk check, satisfies due tasks without an extra request.
- Each cycle, after the existing execution follow-ups, the engine requests at most `max_books_per_cycle` SIG books, capped at `max_books_per_hour` over a rolling hour. Every attempt counts toward the cap. Positions go first, then the earliest deadline, with one book per race. An HTTP 429 pauses follow-ups for `backoff_seconds` and is re-raised as before.
- A follow-up is journaled as a SIG-only `quote_snapshot` with `phase: "research_followup"`. It never marks the ledger, valuations or drawdown estimate, and never places orders.
- The leadership, exit-depth and fair-value markout reports now accept these rows as outcome-only quotes. They are never entry anchors or reference inputs. The low-price replay is unchanged.

**Activation (affects API usage, so it is your call).** Follow-ups are disabled by default. To enable them, add this block to `config.json`, then restart the watcher:

```json
"research_followups": {"enabled": true, "max_books_per_cycle": 2, "max_books_per_hour": 120}
```

That adds at most 120 SIG GETs per hour. Check coverage with:

```sh
python3 -m election_bot research-followups
```

## 2. Entry-timing study

`python3 -m election_bot entry-timing-study [--hours 48] [--json] [--include-retrospective]`

For each recorded proposal from the live both-venue price-gap rule (`shadow_decision` → `both_bids`), the study compares two hypothetical entries:

| Policy | Entry |
|---|---|
| `immediate` | Displayed SIG ask at the proposal |
| `wait_for_bid` | Ask at the first snapshot completing a fresh, sustained bid improvement: proposed-side bid ≥ start bid + 1¢ on 2 consecutive fresh snapshots spanning ≥ 30 s, with gaps ≤ 120 s, within 300 s. A fresh snapshot needs both its source and fetch clocks to advance. Entry is skipped (`chased_away`) if the ask rose more than 3¢. |

Both policies are marked at the first valid SIG quote 15 minutes after the proposal (180 s window), with the same size. The exit needs full depth.

Each outcome is decomposed per share:

```
net = price_movement − entry_half_spread − exit_half_spread − cost_buffers
```

`price_movement` is mid-to-mid. Paired results (both entered) and the immediate outcome of proposals where waiting never entered are reported separately.

**Prospective.** The rule was fixed before collection, at `PREREGISTERED_AT` = 2026-10-20 00:00 UTC. Only later proposals count by default. `--include-retrospective` replays older proposals, labelled exploratory.

An exploratory 24-hour replay on 2026-10-19 found 43 measured immediate entries at −2.54¢/share. Movement was +0.02¢, half-spreads about 0.56¢ and buffers 2.00¢, so the losses come from costs, not adverse price movement. The waiting rule never confirmed, because there were not enough fresh quotes inside 5 minutes; enabling follow-ups supplies them. 49 of 104 exits were also missing an outcome quote.

## 3. Polling coverage

`python3 -m election_bot polling-coverage [--json]` replays the current adapter on the latest staged VoteHub batch. For every enabled Senate contract it shows the binding, accepted polls (live tier), rejection reasons, and whether research and live estimates are produced.

Changes in `votehub.py`:

- **Research-only race bindings** (`live_model_eligible: false`). All are two-party plurality general elections with an enabled "Democrats win" contract:
  - Arkansas: Shoffner / Cotton ([source](https://arkansasadvocate.com/2026/03/03/shoffner-cotton-win-primaries-for-u-s-senate-seat-in-arkansas/))
  - Minnesota: Flanagan / Tafoya ([source](https://www.mprnews.org/2026/10/12/peggy-flanagan-michele-tafoya-senate-race-stays-quiet-in-minnesota))
  - New Mexico: Luján / Marker ([source](https://sourcenm.com/2026/09/11/new-mexico-election-2026-us-senator/))
  - South Carolina: Andrews / Darline Graham ([source](https://www.live5news.com/2026/08/26/senate-candidates-dr-annie-andrews-kasie-whitener-react-result-republican-primary/))
- **Reviewed and deliberately not bound** (`EXCLUDED`):
  - Maine and Alaska: ranked-choice general elections
  - Georgia: majority runoff
  - Nebraska: independent major candidate
  - Montana: three-way contest
  - Mississippi: runoff rules not reviewed
  - Kansas: an earlier review kept it unbound, and the Democratic nominee is not confirmed from certified results
  - Ohio, Florida: no enabled contract
- **Research-tier pollster families.** These were reviewed for family only: SurveyUSA, Suffolk, Washington Post/George Mason, MassINC, Hendrix College, Mitchell Research, Texas Southern University and Stetson. Co-branded releases join an existing family so they can't count as an extra independent pollster (ReconMR/Siena → Siena, Bowling Green/YouGov → YouGov, Cygnal/Beacon → Beacon/Shaw). Partisan-aligned or opt-in sources (Guidant, Slingshot, ActiVote, Change Research, Quantus, Wedgewood) stay unreviewed.
- **Integer rounding.** A release that totals more than 100.5 only because of per-answer rounding (for example 53 + 46 + 2) is accepted as research-tier, not rejected.
- **Isolation.** Research polls carry `live_model_eligible: false` and `research_reasons`, and never supersede or conflict with live-tier rows. Live-tier records are byte-identical to before. `model_live.forecast` refuses research-only races and drops research-tier polls. Live forecasts and orders therefore see exactly the same inputs as before.

**Unchanged:** minimum independent pollsters (2), freshness, LV/RV and partisan rules, the 5-point residual (`max_other_pct`), live edge thresholds and budgets.

**Finding.** Even with more bindings, the new states produce no research estimate yet. The binding constraint is `max_other_pct = 5`: the residual counts undecided voters as "other", so most polls with undecideds are excluded (`material_other_candidate_share` in `--json`). Loosening this is a model change and needs its own review. It is not part of this increment.
