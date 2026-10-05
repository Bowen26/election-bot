# Initial five-race review

The configuration has since expanded to 107 races. See [the current complete coverage report](COVERAGE.md). This document preserves the initial five-race audit.

Reviewed on October 4, 2026 against SIG's tournament-scoped resolution trees and the public Kalshi/Polymarket contract metadata. The user's `config.json` enables all five rows. The example configuration remains a single disabled starter mapping.

| Race | SIG Democratic-winner market / exchange | Kalshi reference | Polymarket event |
| --- | --- | --- | --- |
| New Hampshire | 381 / 1070 | SENATENH-26-D | [New Hampshire](https://polymarket.com/event/new-hampshire-senate-election-winner) |
| Maine | 270 / 959 | SENATEME-26-D | [Maine](https://polymarket.com/event/maine-senate-election-winner) |
| Texas | 293 / 982 | SENATETX-26-D | [Texas](https://polymarket.com/event/texas-senate-election-winner) |
| Michigan | 358 / 1047 | SENATEMI-26-D | [Michigan](https://polymarket.com/event/michigan-senate-election-winner) |
| Iowa | 260 / 949 | SENATEIA-26-D | [Iowa](https://polymarket.com/event/iowa-senate-election-winner) |

Each mapping uses the external Democratic-party YES contract as the reference for SIG YES. The strategy can buy YES or NO; NO means the Democratic-party contract does not resolve YES, not necessarily a guaranteed Republican victory. No second market for the same race has been added.

All four new SIG resolution trees specify Federal / General / Party Winner / Democratic Party, for the November 3, 2026 election. Polymarket's rules cover the Democratic nominee, including a replacement nominee, and exclude independents from the party outcome. Kalshi's primary criterion is a Democratic representative being sworn in for the term beginning in 2027, with accelerated determination available. These are useful references, **not identical settlement contracts**. SIG's scheduled settlement is November 4, while external certification, runoff and swearing-in timelines can differ. The fingerprints pin these reviewed texts and identities; later changes block the affected mapping.

All four additional races had active external markets, two-sided books, adequate reference depth, and acceptable spread/agreement/freshness in the audit. None met the bot's entry threshold in that snapshot. This is a connectivity and mapping check, not evidence of profitability or a promise of immediate trades. The saved audit is `.runtime/additional-races-review.json`; `python3 scripts/review_races.py` repeats the read-only audit without changing the configuration or submitting orders.

Limits remain 25 SUSQies per order, 100 cumulative per exchange, 200 per UTC day across the account's bot orders, and 500 cumulative total. Existing spending remains in the live journal. Positions in several Senate races can move together; adding races does not eliminate correlated election risk.

The national news feeds now match each added state plus Senate/Senator, excluding “state senate.” This does not add a local publisher for every state or guarantee comprehensive candidate coverage. Reload code and configuration by stopping the current process with Ctrl+C and restarting `python3 -m election_bot run --live`.
