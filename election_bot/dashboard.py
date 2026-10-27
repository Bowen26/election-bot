"""One-screen status from local files only: process, P&L, baskets, orders, errors, election night."""
from collections import Counter
import json
from pathlib import Path
import sqlite3
import time

from .strategy import D


def _latest(db, kind):
    row = db.execute('SELECT at, detail FROM events WHERE kind=? ORDER BY at DESC LIMIT 1', (kind,)).fetchone()
    return (row[0], json.loads(row[1])) if row else (None, None)


def values(db):
    """Realized P&L plus open holdings at three bases, kept apart: SIG bid (what selling
    now would fetch), reference fair (Kalshi/Polymarket mid, the same prices the entries
    used) and cost. Uses the latest retained marks; a holding without a mark is counted."""
    from .drawdown import unbuffered
    db.row_factory = sqlite3.Row
    try:
        plain, realized = unbuffered(db.execute('SELECT * FROM executions ORDER BY at,rowid').fetchall())
        bids = {r['exchange']: r for r in db.execute('SELECT * FROM drawdown_marks')}
        fairs = {r['exchange']: r for r in db.execute('SELECT * FROM drawdown_fair_marks')}
    finally:
        db.row_factory = None
    out = {'realized': sum(realized.values(), D(0)), 'cost': D(0), 'sig_bid': D(0), 'reference_fair': D(0),
           'holdings': 0, 'no_bid_mark': 0, 'no_fair_mark': 0}
    for (exchange, side), v in plain.items():
        if v['quantity'] <= 0:
            continue
        out['holdings'] += 1
        out['cost'] += v['cost']
        for key, marks, column, missing in (('sig_bid', bids, 'bid_value', 'no_bid_mark'),
                                            ('reference_fair', fairs, 'fair_value', 'no_fair_mark')):
            mark = marks.get(exchange)
            if mark is None or mark['side'] != side or D(mark['quantity']) != v['quantity']:
                out[missing] += 1
                out[key] += v['cost']  # Unmarked: count at cost so totals stay comparable.
            else:
                out[key] += D(mark[column])
    out['pnl_at_sig_bid'] = out['realized']+out['sig_bid']-out['cost']
    out['pnl_at_reference_fair'] = out['realized']+out['reference_fair']-out['cost']
    return {k: (v if isinstance(v, int) else str(v)) for k, v in out.items()}


def report(runtime, config, now=None):
    now = time.time() if now is None else now
    runtime = Path(runtime)
    result = {'as_of': now}
    status_path = runtime/'supervisor'/'status.json'
    if status_path.exists():
        s = json.loads(status_path.read_text())
        result['watcher'] = {'status': s.get('status'), 'restarts': s.get('restarts'), 'reason': s.get('reason'),
                             'since_minutes': round((now-s['at'])/60, 1)}
    result['stop_flag'] = (runtime/'STOP').exists()
    path = runtime/'live.sqlite3'
    if not path.exists():
        result['status'] = 'no live journal'
        return result
    db = sqlite3.connect(path.resolve().as_uri()+'?mode=ro', uri=True)
    try:
        names = {m['sig_exchange_id']: m['name'] for m in config['markets']}
        scans = [json.loads(d) | {'_at': at} for at, d in db.execute(
            "SELECT at, detail FROM events WHERE kind='scan_complete' AND at>? ORDER BY at", (now-900,))]
        result['cycles_15m'] = {
            'count': len(scans), 'statuses': dict(Counter(s['status'] for s in scans)),
            'last_seconds_ago': round(now-scans[-1]['_at']) if scans else None,
            'mean_cycle_seconds': round(sum(s.get('seconds', 0) for s in scans)/len(scans), 1) if scans else None,
            'sig_reads_last_minute': scans[-1].get('sig_reads_last_minute') if scans else None}
        at, d = _latest(db, 'drawdown_control')
        if d:
            marks = d.get('marks', [])
            result['pnl'] = {'valuation': d.get('valuation'), 'portfolio_pnl': d.get('portfolio_pnl'),
                             'peak_pnl': d.get('peak_pnl'), 'buy_allowed': d.get('buy_allowed'),
                             'reason': d.get('reason'), 'missing': len(d.get('missing', [])),
                             'sig_bid_fallback_marks': sum(m.get('basis') == 'sig_bid_fallback' for m in marks),
                             'seconds_ago': round(now-at)}
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if {'executions', 'drawdown_marks', 'drawdown_fair_marks'} <= tables:
            result['values'] = values(db)
        if 'basket_orders' in tables:
            rows = db.execute('''SELECT b.race, b.exchange, b.role, e.action, CAST(e.quantity AS REAL), CAST(e.price AS REAL)
                FROM basket_orders b JOIN executions e ON e.key=b.key''').fetchall()
            races = {}
            for race, exchange, role, action, quantity, price in rows:
                r = races.setdefault(race, {'anchor': [0, 0.], 'leg': [0, 0.]})
                if role in r and action == 'buy':
                    r[role][0] += quantity
                    r[role][1] += quantity*price
            matched = {race: min(r['anchor'][0], r['leg'][0]) for race, r in races.items()}
            locked = sum(m-(races[race]['anchor'][1]/max(races[race]['anchor'][0], 1)*m
                            + races[race]['leg'][1]/max(races[race]['leg'][0], 1)*m)
                         for race, m in matched.items() if m)
            result['baskets'] = {'races': len(races), 'matched_sets': int(sum(matched.values())),
                                 'guaranteed_profit': round(locked, 2),
                                 'unmatched': {race: int(r['anchor'][0]-r['leg'][0]) for race, r in races.items()
                                               if r['anchor'][0] != r['leg'][0]}}
        checks = [json.loads(d) for (d,) in db.execute(
            "SELECT detail FROM events WHERE kind='pair_basket' AND at>?", (now-3600,))]
        result['basket_checks_1h'] = dict(Counter(
            (c.get('skip') or ('traded' if c.get('first_filled') is not None else 'repair' if 'repair_role' in c else 'other')).split(':')[0]
            for c in checks))
        orders = [json.loads(d) for (d,) in db.execute(
            "SELECT detail FROM events WHERE kind='order_closed' AND at>? ORDER BY at", (now-3600,))]
        result['orders_1h'] = [{'market': names.get(o['exchange'], o['exchange']), 'action': o['action'],
                                'side': o['side'], 'filled': o['filled'], 'price': o.get('average_fill_price')}
                               for o in orders][-10:]
        rotation = [json.loads(d) for (d,) in db.execute(
            "SELECT detail FROM events WHERE kind='capital_rotation_decision' AND at>?", (now-3600,))]
        result['rotation_1h'] = dict(Counter(c.get('reason') for c in rotation))
        radar = db.execute("SELECT at, detail FROM events WHERE kind='gap_radar' AND at>? ORDER BY at DESC LIMIT 1",
                           (now-900,)).fetchone()
        if radar:
            d = json.loads(radar[1])
            result['gap_radar'] = {'seconds_ago': round(now-radar[0]), 'screened': d.get('screened'),
                                   'flagged': [{k: f[k] for k in ('market', 'route', 'side', 'value')}
                                               for f in d.get('flagged', [])]}
        errors = Counter()
        for kind, d in db.execute("SELECT kind, detail FROM events WHERE at>? AND kind IN "
                                  "('feed_error','election_radar_skip','gap_radar_skip','drawdown_fair_mark_skip')",
                                  (now-900,)):
            errors[kind] += 1
        result['errors_15m'] = dict(errors)
    finally:
        db.close()
    night = config.get('election_night', {})
    result['election_night'] = {'enabled': night.get('enabled', False), 'recycle_enabled': night.get('recycle_enabled', False),
                                'window': [night.get('start_at', '2026-11-03T18:00:00-05:00'), night.get('end_at', '2026-11-04T12:00:00-05:00')]}
    result['features'] = {'capital_rotation': config.get('capital_rotation', {}).get('enabled', False),
                          'gap_radar': config.get('gap_radar', {}).get('enabled', False),
                          'pair_baskets': config.get('pair_baskets', {}).get('enabled', False),
                          'settlement_exits': config.get('settlement_exits', {}).get('enabled', False),
                          'alerts': config.get('alerts', {}).get('enabled', False),
                          'drawdown_valuation': config.get('drawdown_control', {}).get('valuation', 'sig_bid')}
    result['status'] = 'ok'
    return result


def format_report(r):
    lines = ['ELECTION BOT DASHBOARD — local files only, ' + time.strftime('%Y-%m-%d %H:%M', time.localtime(r['as_of']))]
    w = r.get('watcher', {})
    lines.append(f"Watcher: {w.get('status')} ({w.get('reason') or 'ok'}), restarts {w.get('restarts')}, "
                 f"STOP flag {'SET' if r.get('stop_flag') else 'clear'}")
    if r.get('status') != 'ok':
        return '\n'.join(lines+[r.get('status', '')])
    c = r['cycles_15m']
    lines.append(f"Cycles (15 min): {c['count']} {c['statuses']}; last {c['last_seconds_ago']}s ago; "
                 f"mean {c['mean_cycle_seconds']}s; SIG reads/min {c['sig_reads_last_minute']}")
    p = r.get('pnl')
    if p:
        lines.append(f"P&L ({p['valuation']}): {float(D(p['portfolio_pnl'] or 0)):+,.0f}  peak {float(D(p['peak_pnl'] or 0)):+,.0f}  "
                     f"buys {'allowed' if p['buy_allowed'] else 'PAUSED ('+str(p['reason'])+')'}; "
                     f"{p['sig_bid_fallback_marks']} holdings at SIG-bid fallback, {p['missing']} unvalued")
    v = r.get('values')
    if v:
        lines.append(f"Value: realized {float(D(v['realized'])):+,.0f} | open holdings ({v['holdings']}) cost {float(D(v['cost'])):,.0f}, "
                     f"at SIG bid {float(D(v['sig_bid'])):,.0f}, at reference fair {float(D(v['reference_fair'])):,.0f}")
        lines.append(f"P&L if sold now (SIG bid): {float(D(v['pnl_at_sig_bid'])):+,.0f}   "
                     f"P&L at settlement if references are right: {float(D(v['pnl_at_reference_fair'])):+,.0f}"
                     + (f"   (unmarked at cost: bid {v['no_bid_mark']}, fair {v['no_fair_mark']})"
                        if v['no_bid_mark'] or v['no_fair_mark'] else ''))
    b = r.get('baskets')
    if b:
        lines.append(f"Baskets: {b['matched_sets']:,} matched sets in {b['races']} races, guaranteed {b['guaranteed_profit']:+,.2f}; "
                     f"unmatched {b['unmatched'] or 'none'}")
    lines.append(f"Basket checks (1h): {r['basket_checks_1h']}")
    if r.get('rotation_1h'):
        lines.append(f"Rotation checks (1h): {r['rotation_1h']}")
    g = r.get('gap_radar')
    if g:
        lines.append(f"Gap radar: {g['seconds_ago']}s ago, {g['screened']} screened; checking first: "
                     + (', '.join(f"{x['market']} {x['route']} {x['side']} ({x['value']})" for x in g['flagged'][:4])
                        or 'nothing'))
    lines.append('Orders (1h): ' + ('; '.join(f"{o['market']} {o['action']} {o['side']} {o['filled']}@{o['price']}" for o in r['orders_1h']) or 'none'))
    lines.append(f"Errors (15 min): {r['errors_15m'] or 'none'}")
    n, f = r['election_night'], r['features']
    lines.append(f"Election night: {'ENABLED' if n['enabled'] else 'off'} {n['window'][0]} -> {n['window'][1]}; recycling {'on' if n['recycle_enabled'] else 'off'}")
    lines.append(f"Features: {f}")
    return '\n'.join(lines)
