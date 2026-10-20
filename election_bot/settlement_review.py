"""Read-only replay: what the bot looks like when valued at settlement, not SIG bid.

Three views from the local journal (no network, no writes):
1. current holdings at SIG bid, SIG midpoint and reference fair value;
2. past sales versus reference fair value at the time, and whether the
   settlement-exit gate would have blocked each one;
3. hourly portfolio P&L under both valuations, with the drawdown pause each
   valuation would have produced.
"""
from bisect import bisect_right
from collections import defaultdict
from pathlib import Path
import sqlite3
import time

from .leadership import load_events, number
from .ledger import inventory_from_executions
from .strategy import D

MAX_SNAPSHOT_AGE = 2*3600     # A holding needs a quote this recent to be valued.
MAX_UNVALUED_FRACTION = D('.10')  # Holdings without one are carried at cost, up to this share count.
SALE_LOOKBACK = 120           # A sale is compared with references seen just before it.


def side_values(row, side):
    """(SIG bid, SIG mid, reference fair) for one side of an aligned snapshot."""
    sig = row['SIG']
    fair = (row['Kalshi']['mid']+row['Polymarket']['mid'])/2
    bid = sig['bid'] if side == 'yes' else 1-sig['ask']
    mid = sig['mid'] if side == 'yes' else 1-sig['mid']
    return D(bid), D(str(mid)), D(str(fair if side == 'yes' else 1-fair))


def plain(rows):
    return [dict(dict(r), buffer='0') for r in rows]


def latest_before(snaps, stamps, at, max_age):
    i = bisect_right(stamps, at)
    if not i or at-stamps[i-1] > max_age:
        return None
    return snaps[i-1]


def valuation(executions, snaps, stamps, at):
    """Unbuffered portfolio P&L at time at; holdings without quotes are carried at cost.

    Returns None when unquoted holdings exceed MAX_UNVALUED_FRACTION of shares.
    """
    holdings, realized = inventory_from_executions(plain(r for r in executions if r['at'] <= at))
    totals = {'sig_bid': D(0), 'sig_mid': D(0), 'reference_fair': D(0)}
    shares = sum((v['quantity'] for v in holdings.values()), D(0))
    unvalued = D(0)
    for (ex, side), v in holdings.items():
        row = latest_before(snaps.get(ex, []), stamps.get(ex, []), at, MAX_SNAPSHOT_AGE)
        if row is None:
            unvalued += v['quantity']
            continue
        bid, mid, fair = side_values(row, side)
        for key, price in (('sig_bid', bid), ('sig_mid', mid), ('reference_fair', fair)):
            totals[key] += v['quantity']*price-v['cost']
    if shares and unvalued/shares > MAX_UNVALUED_FRACTION:
        return None
    base = sum(realized.values(), D(0))
    return dict({k: base+v for k, v in totals.items()}, unvalued_shares=unvalued)


def pause_path(series, pause, resume):
    """Apply the drawdown pause rule to a P&L series; returns the first pause and the final state."""
    peak, paused, first, worst = D(0), False, None, D(0)
    for at, pnl in series:
        peak = max(peak, pnl)
        drop = peak-pnl
        worst = max(worst, drop)
        if drop >= pause:
            paused = True
            first = first or at
        elif paused and drop < resume:
            paused = False
    return {'first_pause_at': first, 'paused_at_end': paused, 'max_drawdown': str(worst),
            'final_pnl': str(series[-1][1]) if series else None, 'peak_pnl': str(peak)}


def report(path, hours=72, now=None, margin='0.01', pause='2500', resume='1250'):
    now = time.time() if now is None else number(now)
    hours = number(hours)
    if not 0 < hours <= 720:
        raise ValueError('Hours must be greater than zero and at most 720')
    margin, pause, resume = D(margin), D(pause), D(resume)
    path = Path(path).resolve()
    result = {'as_of': now, 'hours': hours, 'status': 'No journal yet',
              'limitations': [
                  'Reference fair value is the Kalshi/Polymarket midpoint average: an estimate, not a settlement guarantee.',
                  'Positions are unbuffered (the platform charges no fees); realized P&L uses actual fill prices.',
                  'Checkpoints use the latest aligned scan snapshot per holding within two hours; others are incomplete.',
                  'Replays pause rules on hourly samples; the live gate evaluates every cycle.']}
    if not path.exists():
        return result
    db = sqlite3.connect(path.as_uri()+'?mode=ro', uri=True)
    db.row_factory = sqlite3.Row
    try:
        db.execute('BEGIN')
        executions = [dict(r) for r in db.execute(
            'SELECT * FROM executions WHERE CAST(quantity AS REAL)>0 ORDER BY at,rowid')]
        since = now-hours*3600
        rows, _ = load_events(db, since-MAX_SNAPSHOT_AGE, now)
    finally:
        db.close()
    snaps = defaultdict(list)
    for row in rows:
        if row['aligned']:
            snaps[row['exchange']].append(row)
    stamps = {ex: [r['at'] for r in rs] for ex, rs in snaps.items()}

    # 1. Current holdings.
    current = valuation(executions, snaps, stamps, now)
    holdings, _ = inventory_from_executions(plain(executions))
    result['current'] = {'positions': len(holdings),
                         'shares': str(sum((v['quantity'] for v in holdings.values()), D(0))),
                         'cost_without_buffer': str(sum((v['cost'] for v in holdings.values()), D(0))),
                         'unvalued_shares_at_cost': str(current.pop('unvalued_shares')) if current else None,
                         'pnl': {k: str(v) for k, v in current.items()} if current else None}

    # 2. Past sales inside the window.
    sales, blocked, given_up = [], 0, D(0)
    for e in executions:
        if e['action'] != 'sell' or e['at'] < since:
            continue
        row = latest_before(snaps.get(e['exchange'], []), stamps.get(e['exchange'], []), e['at'], SALE_LOOKBACK)
        if row is None:
            sales.append({'at': e['at'], 'exchange': e['exchange'], 'status': 'no_reference_snapshot'})
            continue
        _, _, fair = side_values(row, e['side'])
        price, qty = D(e['price']), D(e['quantity'])
        would_block = price-fair < margin
        blocked += would_block
        given_up += (fair-price)*qty if would_block else 0
        sales.append({'at': e['at'], 'exchange': e['exchange'], 'side': e['side'], 'quantity': str(qty),
                      'price': str(price), 'fair_value': str(fair), 'price_minus_fair': str(price-fair),
                      'settlement_gate_would_block': would_block})
    result['sales'] = {'count': len(sales), 'with_reference': sum('fair_value' in s for s in sales),
                       'would_block': blocked,
                       'settlement_value_given_up_by_blocked_sales': str(given_up), 'detail': sales}

    # 3. Hourly P&L and pause behaviour.
    series, incomplete = defaultdict(list), 0
    t = since
    while t <= now:
        values = valuation(executions, snaps, stamps, t)
        if values is None:
            incomplete += 1
        else:
            values.pop('unvalued_shares')
            for key, value in values.items():
                series[key].append((t, value))
        t += 3600
    result['checkpoints'] = {'complete': len(series['sig_bid']), 'incomplete': incomplete}
    result['pause_replay'] = {key: pause_path(series[key], pause, resume)
                              for key in ('sig_bid', 'reference_fair')}
    result['status'] = 'ok'
    return result


def format_report(r):
    lines = ['SETTLEMENT-VALUE REVIEW — read-only; the competition scores balances after settlement']
    if r['status'] != 'ok':
        return '\n'.join(lines+[r['status']])
    c = r['current']
    lines.append(f"Holdings: {c['positions']} positions, {float(D(c['shares'])):,.0f} shares, "
                 f"cost {float(D(c['cost_without_buffer'])):,.0f} (no buffer)")
    if c['pnl']:
        lines.append('Unbuffered P&L now:  ' + '  '.join(f"{k} {float(D(v)):+,.0f}" for k, v in c['pnl'].items()))
        lines.append(f"  ({float(D(c['unvalued_shares_at_cost'])):,.0f} shares without a recent aligned quote carried at cost)")
    else:
        lines.append('Current valuation incomplete: a holding has no recent aligned snapshot.')
    s = r['sales']
    lines.append(f"Sales in window: {s['count']} ({s['with_reference']} with references); settlement gate "
                 f"would block {s['would_block']}, keeping {float(D(s['settlement_value_given_up_by_blocked_sales'])):+,.0f} "
                 'of settlement value')
    lines.append(f"Hourly checkpoints: {r['checkpoints']['complete']} complete, {r['checkpoints']['incomplete']} incomplete")
    for key, p in r['pause_replay'].items():
        first = 'never' if p['first_pause_at'] is None else time.strftime('%m-%d %H:%M', time.localtime(p['first_pause_at']))
        final = '—' if p['final_pnl'] is None else f"{float(D(p['final_pnl'])):+,.0f}"
        lines.append(f"  {key:15} final {final:>8}  max drawdown {float(D(p['max_drawdown'])):,.0f}  "
                     f"first pause {first}  paused now {p['paused_at_end']}")
    lines.append('Use --json for each sale and the full limitations.')
    return '\n'.join(lines)
