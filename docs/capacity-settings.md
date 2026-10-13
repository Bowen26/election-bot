# Local competition capacity settings — October 11, 2026

The local deployment now has the limits below. `config.json` is intentionally ignored by Git; pulling this repository does not overwrite another deployment's settings. These are this user's virtual competition settings, not new defaults for the example configuration. Configuration validation and synthetic sizing checks passed; this is not a strategy-profitability test.

| Setting | October 10 | October 11 |
| --- | ---: | ---: |
| Overall committed risk (`total`) | 75,000 coins | 90,000 coins |
| Daily gross buy spending (`daily`) | 5,000 coins | 20,000 coins |
| Per race (`per_market`) | 2,000 coins | 5,000 coins |
| Per order (`per_order`) | 100 coins | 250 coins |
| Total directional exposure (`net_shares_total`) | 5,000 net shares | 50,000 net shares |
| Per office (`net_shares_per_office`) | 4,000 net shares | 25,000 net shares |
| Per Census region (`net_shares_per_region`) | 4,000 net shares | 25,000 net shares |
| Maximum order quantity (`strategy.max_shares_per_order`) | 200 shares | 500 shares |
| Cumulative net realized-loss stop | 7,500 coins | 9,000 coins |

The realized-loss stop remains `realized_loss_stop_fraction = 0.10` times the overall limit and excludes unrealized losses. The increase to 90,000 therefore increases that stop to 9,000. Net-share caps measure signed exposure with conservative pending-fill bounds, not gross turnover or a guarantee that positions hedge one another. These increases allow substantially more concentration than the October 10 settings.

At the October 11 inspection, 10,562.245 coins were committed across 19 races; Senate net exposure was -4,000 shares, exactly its old cap. Oklahoma and South Dakota Senate holdings were near their old 2,000-coin race caps. That day's recorded gross buy spending was 1,485.240 coins. No unresolved orders were recorded in the inspected snapshot. The new limits release capacity at these bottlenecks; they are not an instruction to buy any particular race.

The 90,000-coin limit is a ceiling on open cost, retained net realized losses and pending reservations. It is not a 90% marked-to-market allocation target or a guaranteed 10,000-coin cash reserve. Account cash is checked separately. On an initial 100,000-coin budget, it permits most of that budget to be deployed if enough qualifying opportunities and liquidity exist.

## Reusing capital and rotation

Confirmed sales already release sold FIFO cost from the committed-risk calculation, while realized net losses remain committed. Expected proceeds from a pending sale cannot fund a purchase. Sales do not reset the daily gross-buy counter, so the daily limit also bounds capital turnover after selling.

Existing trend/profit/convergence exit rules remain active. The bot does **not** yet automatically rank all holdings and sell a weaker holding specifically to fund a new market. That capital-rotation feature still requires implementation and validation. Any such route needs fresh executable bids/asks on both markets, a comparison of remaining opportunity after sale/re-entry costs, realistic partial-fill handling, exposure checks, and confirmed sale proceeds before buying. A purchase candidate must be repriced after the sale; the opportunity may disappear while selling. Increasing limits does not make held contracts immediately liquid.

Price-gap, freshness, liquidity, order reconciliation, cooldown and account checks remain unchanged. Actual order size is still bounded by SIG and reference depth as well as coin/share limits. The polling model remains research-only.

Restart a running worker to load these settings. The local `research_polling.enabled` flag remains true. The worker can be launched from the bot directory with:

```sh
caffeinate -i python3 -m election_bot watch --live
```

To reproduce the capacity settings on another deployment, update its existing `limits` object with the following values and set `strategy.max_shares_per_order` to 500. Preserve all other settings and reviewed market mappings.

```json
{
  "per_order": "250",
  "per_market": "5000",
  "total": "90000",
  "daily": "20000",
  "net_shares_total": "50000",
  "net_shares_per_office": "25000",
  "realized_loss_stop_fraction": "0.10",
  "net_shares_per_region": "25000"
}
```
