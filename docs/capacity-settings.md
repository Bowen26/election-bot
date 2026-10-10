# Local competition capacity settings — October 10, 2026

The local deployment now has the following limits. `config.json` is intentionally ignored by Git; pulling this repository does not overwrite another deployment's live settings. These are the current user's competition settings, not new defaults for the example configuration.

| Setting | Current limit | Previous limit |
| --- | ---: | ---: |
| Per race (`per_market`) | 2,000 coins | 1,000 coins |
| Per office (`net_shares_per_office`) | 4,000 net shares | 2,500 net shares |
| Per Census region (`net_shares_per_region`) | 4,000 net shares | 2,500 net shares |
| Overall committed risk (`total`) | 75,000 coins | unchanged |
| Daily gross buy spending (`daily`) | 5,000 coins | unchanged |
| Per order (`per_order`) | 100 coins | unchanged |
| Total directional exposure (`net_shares_total`) | 5,000 net shares | unchanged |
| Maximum order quantity | 200 shares | unchanged |
| Cumulative net realized-loss stop | 7,500 coins | unchanged |

The realized-loss stop is `realized_loss_stop_fraction = 0.10` times the 75,000-coin overall limit; it excludes unrealized losses. Net-share caps measure signed exposure with conservative pending-fill bounds, not gross turnover or a guarantee that positions hedge one another.

The change addressed observed bottlenecks: Northeast net exposure was 2,500 shares, and four races were close to 1,000 coins of committed risk each. Approximately 7,421 coins were committed overall and 154 coins had been spent that UTC day, so the overall and daily limits had substantial room. At that snapshot, the regional change added 1,500 shares of headroom for Northeast exposure in the capped direction, subject to every other constraint.

These changes do not relax price gaps, freshness, liquidity, order reconciliation or account checks. Limits create capacity; they do not force a trade or imply profitability. Restart a running worker to load changed configuration. The local `research_polling.enabled` setting is also true; it starts hourly research collection after restart and does not enable model-driven orders.

To reproduce the three capacity changes on another deployment, edit only these entries inside its existing `limits` object, preserving its other settings and reviewed market mappings:

```json
{
  "per_market": "2000",
  "net_shares_per_office": "4000",
  "net_shares_per_region": "4000"
}
```
