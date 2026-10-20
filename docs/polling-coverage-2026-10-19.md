# Polling coverage increment — October 19, 2026

Added reviewed candidate bindings for the Massachusetts Senate pairing Ed Markey (Democratic) / John Deaton (Republican), and Rhode Island Senate pairing Jack Reed (Democratic) / Raymond McKay (Republican). Primary publisher verification:

- UNH Survey Center, Massachusetts, September 24, 2026: https://scholars.unh.edu/survey_center_polls/1008/
- UNH Survey Center, Rhode Island, September 24, 2026: https://scholars.unh.edu/survey_center_polls/1009/

This expands the candidate catalog from six to eight states and current configured bindings from five to seven races. It does not certify every feed number against its original source. The existing reviewed pollster-family allowlist, LV/RV requirements, party orientation, timestamp checks, revision history and quarantine rules remain unchanged. A bound race may still have no forecast: the model requires independent pollsters, fresh evidence and limited residual other/undecided share. The current staged UNH polls for these new races have large residual shares, so binding alone does not authorize a model trade. Live model budgets and probability thresholds are unchanged.

Governor and House downloads are auxiliary staging feeds, not live-model inputs. An outage in either now records `source_failures` and `partial_collection` but does not invalidate a successful Senate ingestion. A Senate fetch failure, ingestion failure, stopped collection or backwards clock still prevents the model's success timestamp from advancing. The collector remains hourly; no retry loop or freshness relaxation was introduced.

The new bindings take effect on the next successful collection after a watcher restart. Evidence is received prospectively; historical source publication dates do not create retroactive availability. The collector preserves manually sourced priors on unchanged existing bindings.

Inspect collection results with:

```sh
python3 -m election_bot fair-value --collector-status
python3 -m election_bot fair-value --hours 24 --json
```

The first command shows per-source collection failures; the second shows which bound races can actually produce forecasts. Additional states need candidate, electoral-system and data review; the two-candidate plurality model is not automatically extended to runoff or ranked-choice contests.
