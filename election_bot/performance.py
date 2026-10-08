"""Read-only local reporting. Never opens a broker connection or submits orders."""
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
import json
import math
import sqlite3
import time

from .ledger import inventory_from_executions
from .news import position_dispute_report
from .diagnostics import quote_summary, exit_summary
from .measurement import WINDOWS, eligible, tracking_start
from .strategy import D


def interval_summary(values):
    values = sorted(v for v in values if type(v) in (int, float) and math.isfinite(v) and v >= 0)
    if not values:
        return {'samples': 0, 'median_seconds': None, 'p95_seconds': None, 'max_seconds': None}
    middle = len(values)//2
    median = values[middle] if len(values)%2 else (values[middle-1]+values[middle])/2
    return {'samples': len(values), 'median_seconds': round(median, 3),
            'p95_seconds': round(values[math.ceil(.95*len(values))-1], 3),
            'max_seconds': round(values[-1], 3)}


def scan_summary(visits, quotes):
    result = {}
    for policy in sorted({v.get('scan_policy', 'unknown') for v in visits+quotes}):
        attempts = [v for v in visits if v.get('scan_policy', 'unknown') == policy]
        successes = [v for v in quotes if v.get('scan_policy', 'unknown') == policy]
        reasons = Counter(r for v in attempts if v.get('lane') == 'priority' for r in v.get('priority_reasons', []))
        result[policy] = {'scheduled_visits': len(attempts), 'successful_quote_checks': len(successes),
            'unique_markets_visited': len({v['market'] for v in attempts}),
            'unique_markets_with_quotes': len({v['market'] for v in successes}),
            'priority_reasons': dict(reasons),
            'visit_intervals': interval_summary([v.get('revisit_seconds') for v in attempts]),
            'quote_intervals': interval_summary([v.get('quote_revisit_seconds') for v in successes]),
            'lanes': {lane: {'visits': sum(v.get('lane') == lane for v in attempts),
                'quote_intervals': interval_summary([v.get('quote_revisit_seconds') for v in successes
                                                    if v.get('lane') == lane])}
                for lane in ('regular', 'priority')},
            'by_market': [{'market': name,
                'visits': sum(v['market'] == name for v in attempts),
                'quote_checks': sum(v['market'] == name for v in successes),
                'quote_intervals': interval_summary([v.get('quote_revisit_seconds') for v in successes if v['market'] == name])}
                for name in sorted({v['market'] for v in attempts+successes})]}
    return {'policies': result,
        'note': 'Last 24 hours. Visits include cooldowns, failures and news pauses; quote checks require valid '
                'SIG and reference books and a completed decision. Final preflight and performance-only reads '
                'are excluded. First visits have no interval. Revisit times span restarts/downtime; comparisons '
                'between policies are descriptive, not a controlled profitability test.'}


def markout_summary(rows, horizon, now):
    measured = [r for r in rows if r['pnl'] is not None]
    matured = [r for r in rows if now >= r['at'] + horizon]
    missed = sum(r['sample_at'] is not None and r['pnl'] is None or
                 r['sample_at'] is None and now > r['at'] + horizon + WINDOWS[horizon]
                 for r in matured)
    pnl = sum((D(r['pnl']) for r in measured), D(0))
    shares = sum((D(r['quantity']) for r in measured), D(0))
    cost = sum((D(r['quantity'])*(D(r['price'])+D(r['buffer'])) for r in measured), D(0))
    delays = [r['sample_at']-r['at']-horizon for r in measured]
    positive = sum(D(r['pnl']) > 0 for r in measured)
    return {'eligible_buys': len(rows), 'matured': len(matured),
        'observations': len(measured), 'missed': missed,
        'awaiting_quote': len(matured)-len(measured)-missed,
        'not_due': len(rows)-len(matured), 'positive': positive,
        'positive_percent': round(100*positive/len(measured), 2) if measured else None,
        'coverage_percent': round(100*len(measured)/len(matured), 2) if matured else None,
        'sum_pnl_after_buffers': str(pnl),
        'pnl_per_share_after_buffers': str(pnl/shares) if shares else None,
        'return_on_entry_cost_percent': str(100*pnl/cost) if cost else None,
        'average_delay_seconds': round(sum(delays)/len(delays), 2) if delays else None,
        'max_delay_seconds': round(max(delays), 2) if delays else None,
        'window_seconds': WINDOWS[horizon],
        'note': 'Hypothetical full-size liquidation at first qualifying observation after horizon; not realized profit'}


def report(path):
    if not path.exists():
        return {'status': 'No execution journal yet'}
    db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
    db.row_factory = sqlite3.Row
    try:
        db.execute('BEGIN')  # One consistent read snapshot while the bot keeps writing.
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        from .maintenance import retention_info
        now = time.time()
        result = {'as_of': now, 'unresolved_orders': db.execute(
            "SELECT COUNT(*) FROM orders WHERE state='pending'").fetchone()[0]}
        result['event_retention'] = retention_info(db)
        reasons, checks = Counter(), Counter()
        scans, visits, quotes = [], [], []
        quote_failures, feed_errors, exits = [], [], []
        for row in db.execute('SELECT * FROM events WHERE at>=? ORDER BY at', (time.time()-86400,)):
            detail = json.loads(row['detail'])
            if row['kind'] in ('portfolio_risk', 'risk_stop', 'exit_coverage'):
                result['latest_' + row['kind']] = {'at': row['at'], **detail}
            if row['kind'] == 'skip':
                reasons[detail.get('reason', 'unknown')] += 1
            if row['kind'] == 'decision':
                for check in detail.get('checks', []):
                    checks[check.get('reason', 'unknown')] += 1
                exit_check = detail.get('exit_check')
                if exit_check and exit_check.get('status') != 'not_applicable':
                    exits.append({'at': row['at'], 'exchange': detail['exchange'],
                                  'phase': detail.get('phase', 'unknown'), **exit_check})
            if row['kind'] == 'scan_complete':
                scans.append(detail)
            if row['kind'] == 'scan_visit':
                visits.append(detail)
            if row['kind'] == 'scan_quote':
                quotes.append(detail)
            if row['kind'] == 'quote_diagnostics':
                quote_failures.append({'at': row['at'], **detail})
            if row['kind'] == 'feed_error':
                feed_errors.append({'at': row['at'], **detail})
            if row['kind'] == 'exit_blocked':
                exits.append({'at': row['at'], **detail})
        result.update(skip_reasons_last_24h=dict(reasons.most_common(15)),
                      side_checks_last_24h=dict(checks), recent_scans=scans[-5:],
                      scan_performance_last_24h=scan_summary(visits, quotes),
                      quote_diagnostics_last_24h=quote_summary(quote_failures, feed_errors),
                      exit_diagnostics_last_24h=exit_summary(exits))
        if 'executions' not in tables:
            result['status'] = 'Restart the updated bot to import confirmed fills and start tracking'
            return result
        result['orders_awaiting_fill_import'] = db.execute('''SELECT COUNT(*) FROM orders o
            LEFT JOIN executions e ON e.key=o.key WHERE o.state='closed' AND e.key IS NULL''').fetchone()[0]
        result['executions'] = [dict(r) for r in db.execute('''SELECT action,side,COUNT(*) AS orders,
            SUM(CAST(quantity AS REAL)) AS shares FROM executions WHERE CAST(quantity AS REAL)>0
            GROUP BY action,side''')]
        holdings, realized = inventory_from_executions(
            db.execute('SELECT * FROM executions ORDER BY at,rowid'))
        result['realized_pnl_after_buffers'] = str(sum(realized.values(), D(0)))
        result['open_cost_with_entry_buffer'] = str(sum((v['cost'] for v in holdings.values()), D(0)))
        open_exchanges = {ex for ex, side in holdings}
        result['open_contracts'] = len(open_exchanges)
        result['open_races'] = len(open_exchanges)
        result['race_count_basis'] = 'legacy_exchange_count'
        if 'race_bindings' in tables:
            assignments = dict(db.execute('SELECT exchange,race FROM race_bindings'))
            missing = sorted(open_exchanges-set(assignments))
            result['open_races'] = len({assignments[ex] for ex in open_exchanges}) if not missing else None
            result['race_count_basis'] = 'persisted_race_bindings'
            result['unmapped_race_exchanges'] = missing
        result['position_news_risk'] = position_dispute_report(Path(path).parent / 'news.sqlite3', holdings)
        pending = sum((D(r[0]) for r in db.execute("SELECT amount FROM orders WHERE state='pending'")), D(0))
        result['risk_committed'] = str(D(result['open_cost_with_entry_buffer']) +
                                      max(D(0), -D(result['realized_pnl_after_buffers'])) + pending)
        result['gross_buy_spending_today'] = str(sum((D(r[0]) for r in db.execute(
            'SELECT amount FROM orders WHERE day=?', (datetime.now(timezone.utc).date().isoformat(),))), D(0)))
        marks, groups = {}, {}
        started = tracking_start(db)
        for horizon in WINDOWS:
            rows = [dict(r) for r in db.execute('''SELECT e.*,m.at AS sample_at,m.pnl,m.reason
                FROM executions e LEFT JOIN markouts m ON e.key=m.key AND m.horizon=?
                WHERE e.action='buy' AND CAST(e.quantity AS REAL)>0''', (horizon,))
                    if eligible(r['at'], horizon, started)]
            marks[str(horizon)] = markout_summary(rows, horizon, now)
            assets = {(r['exchange'], r['side']) for r in rows}
            for exchange, side in sorted(assets):
                groups.setdefault((exchange, side), {})[str(horizon)] = markout_summary(
                    [r for r in rows if (r['exchange'], r['side']) == (exchange, side)], horizon, now)
        result['buy_markouts_seconds'] = marks
        result['buy_markouts_by_market'] = [dict(exchange=exchange, side=side, horizons=values)
            for (exchange, side), values in sorted(groups.items())]
        result['short_horizons_started_at'] = started
        result['measurement_note'] = (
            '1/5/15-minute tracking covers buys filled after this upgrade was first started. '
            '1-hour and 24-hour history is preserved. Missing observations include outages, stale '
            'quotes and insufficient full-size exit depth. Measured results may be biased toward '
            'liquid markets; compare coverage and sampling delays. Repeated buys in one race are '
            'not independent evidence. Never add results across horizons or call these realized returns.')
        result['valuations'] = [{**dict(r), 'age_seconds': round(time.time()-r['at'], 1)}
                                for r in db.execute('SELECT * FROM valuations')]
        result['valuation_note'] = ('Bid-depth liquidation value; pnl is null when full exit depth is unavailable. '
                                   'Quotes are historical snapshots; inspect age_seconds. '
                                   'Entry buffer is included in cost; exit buffer is not deducted here.')
        result['cost_note'] = 'PnL uses configured entry/exit buffers; these are estimates, not verified transaction fees.'
        result['budget_note'] = 'Risk capital is open cost plus net realized losses; profits do not increase the cap. Daily gross purchases do not recycle.'
        return result
    finally:
        db.close()
