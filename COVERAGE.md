# Competition coverage

Audited 2026-10-04T22:57:21.942986+00:00: 237 SIG contracts represent 117 contests (115 races plus House/Senate control). Enabled 107 races: {'senate': 30, 'governor': 31, 'house': 46}. Ten contests remain excluded.

One Democratic-party contract per race is enabled. The strategy can buy its YES or NO; NO includes every non-Democratic outcome and is not an exact Republican-party contract. Separate Republican/independent contracts are not independently traded. This prevents duplicate per-race allowances.

Limits: 50 coins/order; 250 risk capital/race; 5,000 gross buy coins/day (UTC); 25,000 total risk capital. In the active execution mode, confirmed sales release position cost while realized losses continue consuming capital. Daily purchases do not recycle. Additional controls: 5,000 net directional shares total; 2,500 per office; a stop at 2,500 coins of cumulative net realized losses after buffers. These are not maximum-loss or unrealized-drawdown measures. Existing bot trades are imported; the journal is not reset. See README for accounting and exit rules.

Rules differ across venues: SIG uses its election resolution tree; Kalshi generally uses the party sworn in/inaugurated; Polymarket uses election calls/certification. House party attribution can include caucusing independents. These are reference prices with settlement basis risk, not identical contracts or guaranteed arbitrage. Candidate-only and wrong-election-year contracts are excluded. Louisiana is excluded because the fetched Kalshi Louisiana contract names Kentucky in its primary rule.

All verified mappings are checked against their pinned rules before an actionable order. Scan-only metadata may be cached for up to 15 minutes; quotes are always fresh. Freshness, depth, spread, venue agreement, news pauses, existing positions and budget limits still gate trading. Enabled does not mean an executable trade exists.

The scanner processes up to twelve races and four sequential orders per cycle, alternating news priority with ordinary rotation. It persists its position after each selection, so orders/restarts do not keep sending it back to the first race. Concurrent public feed reads and metadata caching accelerate scans; SIG writes remain serial and paced. Full sweeps are not simultaneous streaming coverage. RSS sources remain NPR, PBS and NHPR and do not provide exhaustive local news for every race.

Run `python3 -m election_bot coverage` for the saved configuration. Restart the bot to load changes. New listings are not automatically enabled: rerun the read-only audit and review their rules.

| SIG contest | Status | Reference / exclusion |
| --- | --- | --- |
| Will the Democratic Party win the AL-02 House race? | Enabled | KXHOUSERACE-AL02-26-D |
| Will the Democratic Party win the AZ-01 House race? | Enabled | HOUSEAZ1-26-D |
| Will the Democratic Party win the AZ-06 House race? | Enabled | HOUSEAZ6-26-D |
| Will the Democratic Party win the Alabama Governor? | Enabled | GOVPARTYAL-26-D |
| Will the Democratic Party win the Alabama Senate? | Enabled | SENATEAL-26-D |
| Will the Democratic Party win the Alaska Governor? | Excluded | Polymarket party question absent or ambiguous: Will the Democrats win the Alaska governor race in 2026? |
| Will the Democratic Party win the Alaska Senate? | Excluded | Polymarket party question absent or ambiguous: Will the Democrats win the Alaska Senate race in 2026? |
| Will the Democratic Party win the Arizona Governor? | Enabled | GOVPARTYAZ-26-D |
| Will the Democratic Party win the Arkansas Governor? | Enabled | GOVPARTYAR-26-D |
| Will the Democratic Party win the Arkansas Senate? | Enabled | SENATEAR-26-D |
| Will the Democratic Party win the CA-22 House race? | Enabled | HOUSECA22-26-D |
| Will the Democratic Party win the CA-48 House race? | Enabled | KXHOUSERACE-CA48-26-D |
| Will the Democratic Party win the CO-03 House race? | Enabled | HOUSECO3-26-D |
| Will the Democratic Party win the CO-04 House race? | Enabled | KXHOUSERACE-CO04-26-D |
| Will the Democratic Party win the CO-08 House race? | Enabled | HOUSECO8-26-D |
| Will the Democratic Party win the California Governor? | Excluded | Polymarket event absent or ambiguous: California Governor Election Winner |
| Will the Democratic Party win the Colorado Governor? | Enabled | GOVPARTYCO-26-D |
| Will the Democratic Party win the Colorado Senate? | Enabled | SENATECO-26-D |
| Will the Democratic Party win the Connecticut Governor? | Enabled | GOVPARTYCT-26-D |
| Will the Democratic Party win the Delaware Senate? | Enabled | SENATEDE-26-D |
| Will the Democratic Party win the FL-07 House race? | Enabled | KXHOUSERACE-FL07-26-D |
| Will the Democratic Party win the FL-09 House race? | Enabled | KXHOUSERACE-FL09-26-D |
| Will the Democratic Party win the FL-13 House race? | Enabled | HOUSEFL13-26-D |
| Will the Democratic Party win the FL-16 House race? | Enabled | KXHOUSERACE-FL16-26-D |
| Will the Democratic Party win the FL-20 House race? | Enabled | KXHOUSERACE-FL20-26-D |
| Will the Democratic Party win the FL-22 House race? | Enabled | KXHOUSERACE-FL22-26-D |
| Will the Democratic Party win the Florida Governor? | Enabled | GOVPARTYFL-26-D |
| Will the Democratic Party win the Florida Senate? | Excluded | GET /markets/SENATEFL-26-D returned HTTP 404 |
| Will the Democratic Party win the Georgia Governor? | Enabled | GOVPARTYGA-26-D |
| Will the Democratic Party win the Georgia Senate? | Enabled | SENATEGA-26-D |
| Will the Democratic Party win the Hawaii Governor? | Enabled | GOVPARTYHI-26-D |
| Will the Democratic Party win the IA-01 House race? | Enabled | HOUSEIA1-26-D |
| Will the Democratic Party win the IA-02 House race? | Enabled | KXHOUSERACE-IA02-26-D |
| Will the Democratic Party win the IA-03 House race? | Enabled | HOUSEIA3-26-D |
| Will the Democratic Party win the Idaho Governor? | Enabled | GOVPARTYID-26-D |
| Will the Democratic Party win the Idaho Senate? | Enabled | SENATEID-26-D |
| Will the Democratic Party win the Illinois Governor? | Enabled | GOVPARTYIL-26-D |
| Will the Democratic Party win the Illinois Senate? | Enabled | SENATEIL-26-D |
| Will the Democratic Party win the Iowa Governor? | Enabled | GOVPARTYIA-26-D |
| Will the Democratic Party win the Iowa Senate? | Enabled | SENATEIA-26-D |
| Will the Democratic Party win the KY-04 House race? | Enabled | KXHOUSERACE-KY04-26-D |
| Will the Democratic Party win the Kansas Governor? | Excluded | GET /markets/GOVPARTYKS-26-D returned HTTP 404 |
| Will the Democratic Party win the Kansas Senate? | Enabled | SENATEKS-26-D |
| Will the Democratic Party win the Kentucky Senate? | Excluded | GET /markets/SENATEKY-26-D returned HTTP 404 |
| Will the Democratic Party win the Louisiana Senate? | Excluded | Kalshi settlement rule needs individual review |
| Will the Democratic Party win the ME-02 House race? | Enabled | HOUSEME2-26-D |
| Will the Democratic Party win the MI-07 House race? | Enabled | HOUSEMI7-26-D |
| Will the Democratic Party win the MI-10 House race? | Enabled | HOUSEMI10-26-D |
| Will the Democratic Party win the MN-01 House race? | Enabled | KXHOUSERACE-MN01-26-D |
| Will the Democratic Party win the MN-02 House race? | Enabled | HOUSEMN2-26-D |
| Will the Democratic Party win the MN-05 House race? | Enabled | KXHOUSERACE-MN05-26-D |
| Will the Democratic Party win the MT-01 House race? | Enabled | HOUSEMT1-26-D |
| Will the Democratic Party win the Maine Governor? | Enabled | GOVPARTYME-26-D |
| Will the Democratic Party win the Maine Senate? | Enabled | SENATEME-26-D |
| Will the Democratic Party win the Maryland Governor? | Enabled | GOVPARTYMD-26-D |
| Will the Democratic Party win the Massachusetts Governor? | Enabled | GOVPARTYMA-26-D |
| Will the Democratic Party win the Massachusetts Senate? | Enabled | SENATEMA-26-D |
| Will the Democratic Party win the Michigan Governor? | Enabled | GOVPARTYMI-26-D |
| Will the Democratic Party win the Michigan Senate? | Enabled | SENATEMI-26-D |
| Will the Democratic Party win the Minnesota Governor? | Enabled | GOVPARTYMN-26-D |
| Will the Democratic Party win the Minnesota Senate? | Enabled | SENATEMN-26-D |
| Will the Democratic Party win the Mississippi Senate? | Enabled | SENATEMS-26-D |
| Will the Democratic Party win the Montana Senate? | Enabled | SENATEMT-26-D |
| Will the Democratic Party win the NE-02 House race? | Enabled | HOUSENE2-26-D |
| Will the Democratic Party win the NH-01 House race? | Enabled | HOUSENH1-26-D |
| Will the Democratic Party win the NJ-07 House race? | Enabled | HOUSENJ7-26-D |
| Will the Democratic Party win the NY-13 House race? | Enabled | KXHOUSERACE-NY13-26-D |
| Will the Democratic Party win the NY-17 House race? | Enabled | HOUSENY17-26-D |
| Will the Democratic Party win the Nebraska Governor? | Enabled | GOVPARTYNE-26-D |
| Will the Democratic Party win the Nebraska Senate? | Enabled | SENATENE-26-D |
| Will the Democratic Party win the Nevada Governor? | Enabled | GOVPARTYNV-26-D |
| Will the Democratic Party win the New Hampshire Governor? | Excluded | GET /markets/GOVPARTYNH-26-D returned HTTP 404 |
| Will the Democratic Party win the New Hampshire Senate? | Enabled | SENATENH-26-D |
| Will the Democratic Party win the New Jersey Senate? | Enabled | SENATENJ-26-D |
| Will the Democratic Party win the New Mexico Governor? | Enabled | GOVPARTYNM-26-D |
| Will the Democratic Party win the New Mexico Senate? | Enabled | SENATENM-26-D |
| Will the Democratic Party win the New York Governor? | Enabled | GOVPARTYNY-26-D |
| Will the Democratic Party win the North Carolina Senate? | Enabled | SENATENC-26-D |
| Will the Democratic Party win the Oklahoma Governor? | Enabled | GOVPARTYOK-26-D |
| Will the Democratic Party win the Oklahoma Senate? | Enabled | SENATEOK-26-D |
| Will the Democratic Party win the Oregon Governor? | Enabled | GOVPARTYOR-26-D |
| Will the Democratic Party win the Oregon Senate? | Enabled | SENATEOR-26-D |
| Will the Democratic Party win the PA-01 House race? | Enabled | HOUSEPA1-26-D |
| Will the Democratic Party win the PA-07 House race? | Enabled | HOUSEPA7-26-D |
| Will the Democratic Party win the PA-08 House race? | Enabled | HOUSEPA8-26-D |
| Will the Democratic Party win the PA-10 House race? | Enabled | HOUSEPA10-26-D |
| Will the Democratic Party win the Pennsylvania Governor? | Enabled | GOVPARTYPA-26-D |
| Will the Democratic Party win the Rhode Island Governor? | Enabled | GOVPARTYRI-26-D |
| Will the Democratic Party win the Rhode Island Senate? | Enabled | SENATERI-26-D |
| Will the Democratic Party win the SC-01 House race? | Enabled | KXHOUSERACE-SC01-26-D |
| Will the Democratic Party win the South Carolina Governor? | Enabled | GOVPARTYSC-26-D |
| Will the Democratic Party win the South Carolina Senate? | Enabled | SENATESC-26-D |
| Will the Democratic Party win the South Dakota Governor? | Enabled | GOVPARTYSD-26-D |
| Will the Democratic Party win the South Dakota Senate? | Enabled | SENATESD-26-D |
| Will the Democratic Party win the TN-05 House race? | Enabled | KXHOUSERACE-TN05-26-D |
| Will the Democratic Party win the TX-15 House race? | Enabled | HOUSETX15-26-D |
| Will the Democratic Party win the TX-23 House race? | Enabled | KXHOUSERACE-TX23-26-D |
| Will the Democratic Party win the TX-28 House race? | Enabled | HOUSETX28-26-D |
| Will the Democratic Party win the Tennessee Governor? | Enabled | GOVPARTYTN-26-D |
| Will the Democratic Party win the Tennessee Senate? | Enabled | SENATETN-26-D |
| Will the Democratic Party win the Texas Governor? | Enabled | GOVPARTYTX-26-D |
| Will the Democratic Party win the Texas Senate? | Enabled | SENATETX-26-D |
| Will the Democratic Party win the U.S. House? | Excluded | Requires separate settlement review |
| Will the Democratic Party win the U.S. Senate? | Excluded | Requires separate settlement review |
| Will the Democratic Party win the VA-01 House race? | Enabled | HOUSEVA1-26-D |
| Will the Democratic Party win the VA-02 House race? | Enabled | HOUSEVA2-26-D |
| Will the Democratic Party win the VA-05 House race? | Enabled | KXHOUSERACE-VA05-26-D |
| Will the Democratic Party win the VA-07 House race? | Enabled | HOUSEVA7-26-D |
| Will the Democratic Party win the Vermont Governor? | Enabled | GOVPARTYVT-26-D |
| Will the Democratic Party win the Virginia Senate? | Enabled | SENATEVA-26-D |
| Will the Democratic Party win the WA-03 House race? | Enabled | HOUSEWA3-26-D |
| Will the Democratic Party win the WI-01 House race? | Enabled | HOUSEWI1-26-D |
| Will the Democratic Party win the WI-03 House race? | Enabled | HOUSEWI3-26-D |
| Will the Democratic Party win the West Virginia Senate? | Enabled | SENATEWV-26-D |
| Will the Democratic Party win the Wisconsin Governor? | Enabled | GOVPARTYWI-26-D |
| Will the Democratic Party win the Wyoming Governor? | Enabled | GOVPARTYWY-26-D |
| Will the Democratic Party win the Wyoming Senate? | Enabled | SENATEWY-26-D |
