"""Read-only local reporting. Never opens a broker connection or submits orders."""
from collections import Counter
from datetime import datetime, timezone
import json
import sqlite3
import time

from .ledger import Ledger
from .strategy import D


def report(path):
    if not path.exists():
        return {'status': 'No execution journal yet'}
    db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
    db.row_factory = sqlite3.Row
    try:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        result = {'as_of': time.time(), 'unresolved_orders': db.execute(
            "SELECT COUNT(*) FROM orders WHERE state='pending'").fetchone()[0]}
        reasons, checks = Counter(), Counter()
        scans = []
        for row in db.execute('SELECT * FROM events WHERE at>=? ORDER BY at', (time.time()-86400,)):
            detail = json.loads(row['detail'])
            if row['kind'] == 'skip':
                reasons[detail.get('reason', 'unknown')] += 1
            if row['kind'] == 'decision':
                for check in detail.get('checks', []):
                    checks[check.get('reason', 'unknown')] += 1
            if row['kind'] == 'scan_complete':
                scans.append(detail)
        result.update(skip_reasons_last_24h=dict(reasons.most_common(15)),
                      side_checks_last_24h=dict(checks), recent_scans=scans[-5:])
        if 'executions' not in tables:
            result['status'] = 'Restart the updated bot to import confirmed fills and start tracking'
            return result
        result['orders_awaiting_fill_import'] = db.execute('''SELECT COUNT(*) FROM orders o
            LEFT JOIN executions e ON e.key=o.key WHERE o.state='closed' AND e.key IS NULL''').fetchone()[0]
        result['executions'] = [dict(r) for r in db.execute('''SELECT action,side,COUNT(*) AS orders,
            SUM(CAST(quantity AS REAL)) AS shares FROM executions WHERE CAST(quantity AS REAL)>0
            GROUP BY action,side''')]
        # Inventory reconstruction is pure; avoid schema creation on a read-only report.
        ledger = Ledger.__new__(Ledger)
        ledger.db, ledger._cache = db, None
        holdings, realized = ledger.inventory()
        result['realized_pnl_after_buffers'] = str(sum(realized.values(), D(0)))
        result['open_cost_with_entry_buffer'] = str(sum((v['cost'] for v in holdings.values()), D(0)))
        result['open_races'] = len({ex for ex, side in holdings})
        pending = sum((D(r[0]) for r in db.execute("SELECT amount FROM orders WHERE state='pending'")), D(0))
        result['risk_committed'] = str(D(result['open_cost_with_entry_buffer']) +
                                      max(D(0), -D(result['realized_pnl_after_buffers'])) + pending)
        result['gross_buy_spending_today'] = str(sum((D(r[0]) for r in db.execute(
            'SELECT amount FROM orders WHERE day=?', (datetime.now(timezone.utc).date().isoformat(),))), D(0)))
        marks = {}
        for horizon in (3600, 86400):
            rows = db.execute('SELECT * FROM markouts WHERE horizon=?', (horizon,)).fetchall()
            measured = [D(r['pnl']) for r in rows if r['pnl'] is not None]
            marks[str(horizon)] = {'observations': len(measured), 'missed': len(rows)-len(measured),
                'positive': sum(v > 0 for v in measured),
                'sum_pnl_after_buffers': str(sum(measured, D(0))),
                'note': 'Hypothetical full-size liquidation at first qualifying scan after horizon; not realized profit'}
        result['buy_markouts_seconds'] = marks
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
