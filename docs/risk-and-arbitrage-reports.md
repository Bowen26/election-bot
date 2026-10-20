# National swing stress test and party-pair scan

Both are read-only and place no orders.

## `python3 -m election_bot swing-risk [--json]`

This values current holdings at reference fair value (the Kalshi/Polymarket midpoint), then applies the same national swing to every race at once.

- **Method:** each race's Democratic win probability `p` becomes an implied margin `σ·Φ⁻¹(p)`. A swing of `s` points gives `Φ((m+s)/σ)`. The assumed `σ` (4, 6 or 8 points) is printed as rows, so compare rows rather than trusting one number.
- **Also shown:** shares paying if each party wins, and the ten largest positions with their loss if that race alone flips.
- **Excluded:** holdings without a reference quote in the last six hours are listed separately.

On 2026-10-19:
- **Direction:** 58,197 shares pay if Democrats win and 31,681 if Republicans win.
- **σ = 6:** expected P&L ranges from +7,495 with no swing to −2,193 at a 6-point Republican swing, and is +4,711 at a 6-point Democratic swing.
- **σ = 4:** a 6-point Republican swing gives −11,815.
- **Largest positions:** ten races each risk about 4,400–4,900 if they flip alone, with expected losses of 20–600 each. The book is mainly exposed to a correlated Republican overperformance.

## `python3 -m election_bot pair-arbitrage [--interval 2.5] [--limit N] [--threshold 0.01] [--json]`

SIG lists each race as separate per-party binary markets. At most one listed party wins.

- **NO basket (risk-free apart from execution):** if the YES bids sum to more than 1, buying NO on every party costs `n − Σbids` and pays at least `n − 1`. The edge is `Σbids − 1` per set.
- **YES basket:** if the YES asks sum to less than 1, buying YES on every party pays 1 unless an unlisted candidate wins.

Reads are paced one every `--interval` seconds (default 2.5) so the scan can run beside the live bot under the 100-reads-per-minute limit. A full scan of 117 races takes about 10 minutes. A rate-limit or API error stops the scan and reports what was read.

A 5-race sample on 2026-10-19 found AL-02 House NO basket at +1¢/set (2,205 sets) and Alabama Governor/Senate YES baskets at +1¢. Quotes are read seconds apart and both legs must fill, so these are leads, not executions. Trading them would need a separate two-leg order route.
