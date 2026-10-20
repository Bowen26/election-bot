"""Read-only stress test: settlement value of current holdings under a national swing.

Each held race's reference fair probability p is turned into an implied
Democratic margin m = sigma * Phi^-1(p) (in points, oriented to the Democratic
candidate). A uniform national swing s moves every race to Phi((m + s) / sigma).
The same swing hits every race at once, which is the correlated risk that
per-race caps do not show. sigma is an assumption (default 6 points), not an
estimate; the report prints several.
"""
from collections import defaultdict
from pathlib import Path
from statistics import NormalDist
import sqlite3
import time

from .leadership import load_events
from .ledger import inventory_from_executions
from .settlement_review import latest_before, plain, side_values
from .strategy import D

SWINGS = (-6, -4, -2, 0, 2, 4, 6)
SIGMAS = (4, 6, 8)
MAX_SNAPSHOT_AGE = 6*3600
NORMAL = NormalDist()


def dem_probability(fair_yes, exposure_sign):
    """Probability the Democratic candidate wins, from the YES fair value of a party contract."""
    return fair_yes if exposure_sign == 1 else 1-fair_yes


def shifted(p_dem, swing, sigma):
    p = min(max(float(p_dem), 1e-6), 1-1e-6)
    return NORMAL.cdf(NORMAL.inv_cdf(p)+swing/sigma)


def positions(executions, snaps, stamps, markets, now):
    """Held positions with fair value and Democratic orientation; unquoted ones listed separately."""
    holdings, realized = inventory_from_executions(plain(executions))
    rows, unvalued = [], []
    for (ex, side), v in sorted(holdings.items()):
        mapping = markets.get(ex)
        row = latest_before(snaps.get(ex, []), stamps.get(ex, []), now, MAX_SNAPSHOT_AGE)
        if mapping is None or row is None or mapping.get('exposure_sign') not in (1, -1):
            unvalued.append({'exchange': ex, 'side': side, 'shares': str(v['quantity']), 'cost': str(v['cost'])})
            continue
        _, _, fair_side = side_values(row, side)
        fair_yes = fair_side if side == 'yes' else 1-fair_side
        rows.append({'exchange': ex, 'market': mapping.get('name'), 'side': side, 'shares': v['quantity'],
                     'cost': v['cost'], 'fair_side': fair_side,
                     'p_dem': dem_probability(fair_yes, mapping['exposure_sign']),
                     # +1 if this position pays when the Democrat wins.
                     'dem_direction': mapping['exposure_sign']*(1 if side == 'yes' else -1),
                     'quote_age_seconds': now-row['at']})
    return rows, unvalued, sum(realized.values(), D(0))


def value(rows, swing, sigma):
    total = D(0)
    for r in rows:
        p = shifted(r['p_dem'], swing, sigma)
        p_pays = p if r['dem_direction'] == 1 else 1-p
        total += r['shares']*D(str(p_pays))
    return total


def report(path, config, now=None):
    now = time.time() if now is None else float(now)
    path = Path(path).resolve()
    result = {'as_of': now, 'status': 'No journal yet', 'method': {
        'swings_points': list(SWINGS), 'sigmas_points': list(SIGMAS),
        'note': 'Uniform national swing applied to every race; sigma converts probability to margin and is assumed.'}}
    if not path.exists():
        return result
    db = sqlite3.connect(path.as_uri()+'?mode=ro', uri=True)
    db.row_factory = sqlite3.Row
    try:
        db.execute('BEGIN')
        executions = [dict(r) for r in db.execute(
            'SELECT * FROM executions WHERE CAST(quantity AS REAL)>0 ORDER BY at,rowid')]
        snapshots, _ = load_events(db, now-MAX_SNAPSHOT_AGE, now)
    finally:
        db.close()
    snaps = defaultdict(list)
    for row in snapshots:
        if row['aligned']:
            snaps[row['exchange']].append(row)
    stamps = {ex: [r['at'] for r in rs] for ex, rs in snaps.items()}
    markets = {m['sig_exchange_id']: m for m in config['markets']}
    rows, unvalued, realized = positions(executions, snaps, stamps, markets, now)
    cost = sum((r['cost'] for r in rows), D(0))
    base = value(rows, 0, SIGMAS[1])
    result['positions'] = len(rows)
    result['unvalued_positions'] = unvalued
    result['valued_cost'] = str(cost)
    result['realized_pnl'] = str(realized)
    result['expected_pnl_no_swing'] = str(base-cost)
    result['scenarios'] = {str(sigma): {str(s): str(value(rows, s, sigma)-cost) for s in SWINGS} for sigma in SIGMAS}
    dem_shares = sum((r['shares'] for r in rows if r['dem_direction'] == 1), D(0))
    rep_shares = sum((r['shares'] for r in rows if r['dem_direction'] == -1), D(0))
    result['direction'] = {'shares_paying_if_democrat_wins': str(dem_shares),
                           'shares_paying_if_republican_wins': str(rep_shares)}
    # Biggest single-race risks: loss if this race alone goes the other way.
    risks = sorted(({'market': r['market'], 'side': r['side'], 'shares': str(r['shares']),
                     'fair': str(round(r['fair_side'], 4)), 'loss_if_wrong': str(r['cost']),
                     'expected_loss': str(round((1-r['fair_side'])*r['shares'], 2)),
                     'pnl_at_minus6': str(round(value([r], -6*r['dem_direction'], SIGMAS[1])-r['cost'], 2))}
                    for r in rows), key=lambda x: -D(x['loss_if_wrong']))
    result['largest_positions'] = risks[:10]
    result['status'] = 'ok'
    return result


def format_report(r):
    lines = ['NATIONAL SWING STRESS TEST — settlement value at reference fair value; no orders']
    if r['status'] != 'ok':
        return '\n'.join(lines+[r['status']])
    lines.append(f"{r['positions']} valued positions, cost {float(D(r['valued_cost'])):,.0f}; "
                 f"{len(r['unvalued_positions'])} without a recent reference quote (excluded)")
    d = r['direction']
    lines.append(f"Shares paying if Democrat wins: {float(D(d['shares_paying_if_democrat_wins'])):,.0f}; "
                 f"if Republican wins: {float(D(d['shares_paying_if_republican_wins'])):,.0f}")
    lines.append('Expected P&L by uniform swing toward Democrats (points):')
    lines.append('  sigma ' + ''.join(f'{s:>+9}' for s in SWINGS))
    for sigma, row in r['scenarios'].items():
        lines.append(f'  {sigma:>5} ' + ''.join(f"{float(D(row[str(s)])):>+9,.0f}" for s in SWINGS))
    lines.append('Largest positions (loss if that race alone goes the other way):')
    for x in r['largest_positions']:
        lines.append(f"  {str(x['market']):28} {x['side']:3} {float(D(x['shares'])):>7,.0f} sh  fair {x['fair']:>6}  "
                     f"lose {float(D(x['loss_if_wrong'])):>7,.0f}  expected loss {float(D(x['expected_loss'])):>6,.0f}")
    lines.append('Sigma is an assumption; compare rows rather than trusting one number. See --json for detail.')
    return '\n'.join(lines)
