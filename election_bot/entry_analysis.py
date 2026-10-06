"""Read-only entry-price/gap reports. No inferred historical order links or trades."""
from collections import Counter, defaultdict
from datetime import datetime, timezone
from decimal import InvalidOperation
import json
from pathlib import Path
import random
import sqlite3
import time

from .measurement import eligible, tracking_start
from .performance import markout_summary
from .strategy import D

PRICE_BUCKETS = ('0–0.15', '0.15–0.35', '0.35–0.65', '0.65–0.85', '0.85–1.00', 'unknown')
GAP_BUCKETS = ('below 0.05', '0.05–0.06', '0.06–0.08', '0.08–0.12', '0.12 and above', 'unknown')
HORIZONS = (300, 900)


def price_bucket(price):
    if price is None or not 0 < price < 1:
        return 'unknown'
    for index, upper in enumerate(('.15', '.35', '.65', '.85', '1')):
        if price < D(upper):
            return PRICE_BUCKETS[index]
    return 'unknown'


def gap_bucket(gap):
    if gap is None:
        return 'unknown'
    for index, upper in enumerate(('.05', '.06', '.08', '.12')):
        if gap < D(upper):
            return GAP_BUCKETS[index]
    return GAP_BUCKETS[4]


def linked_gaps(db, executions):
    """Only exact order_key links with matching order/execution identity qualify."""
    keys = {row['key'] for row in executions}
    signals = defaultdict(list)
    unkeyed = 0
    malformed = 0
    for event in db.execute("SELECT detail FROM events WHERE kind='signal'"):
        try:
            signal = json.loads(event['detail'])
            if not isinstance(signal, dict):
                raise ValueError('Signal is not an object')
            key = signal.get('order_key')
            if not isinstance(key, str) or not key:
                unkeyed += 1
            elif key in keys:
                signals[key].append(signal)
        except (ValueError, TypeError):
            malformed += 1
    gaps, reasons = {}, {}
    for execution in executions:
        key = execution['key']
        if key not in signals:
            reasons[key] = 'missing_order_link'
            continue
        try:
            payload = json.loads(execution['order_payload'])
            identity = (execution['exchange'], execution['side'], execution['action'])
            if ((str(payload['exchangeId']), payload['side'], payload['action']) != identity
                    or payload['idempotencyKey'] != key):
                raise ValueError('Order identity mismatch')
            candidates = set()
            for signal in signals[key]:
                if (str(signal['exchange']), signal['side'], signal['action']) != identity:
                    raise ValueError('Signal identity mismatch')
                reference, price, fee = D(signal['reference']), D(signal['price']), D(execution['buffer'])
                if (not 0 < reference < 1 or not 0 < price < 1 or fee < 0
                        or price != D(payload['price'])
                        or D(signal['quantity']) != D(payload['quantity'])
                        or D(signal['quantity']) < D(execution['quantity'])
                        or D(signal['edge']) != reference-price-fee):
                    raise ValueError('Inconsistent signal pricing or quantity')
                # Same pre-submission reference, but use actual average fill price.
                candidates.add(reference-D(execution['price'])-fee)
            if len(candidates) != 1:
                reasons[key] = 'conflicting_signals'
                continue
            gaps[key] = candidates.pop()
            reasons[key] = 'matched'
        except (KeyError, TypeError, ValueError, InvalidOperation):
            reasons[key] = 'invalid_linked_signal'
    return gaps, {'filled_buy_link_status': dict(Counter(reasons.values())),
                  'legacy_unkeyed_signal_events': unkeyed, 'malformed_signal_events': malformed}


def day_block_intervals(rows):
    """Exploratory uncertainty conditional on resampling whole UTC entry days.

    This preserves within-day clustering, not serial/cross-day election dependence.
    Five days is a reporting minimum, not an adequate-sample-size guarantee.
    """
    measured = [r for r in rows if r['pnl'] is not None]
    days = defaultdict(lambda: [D(0), D(0), 0, 0])
    for row in measured:
        day = datetime.fromtimestamp(row['at'], timezone.utc).date().isoformat()
        block = days[day]
        block[0] += D(row['pnl'])
        block[1] += D(row['quantity'])
        block[2] += D(row['pnl']) > 0
        block[3] += 1
    result = {'method': 'UTC entry-day block bootstrap', 'confidence_level': .95,
              'observed_entry_days': len(days), 'minimum_days_to_report': 5,
              'replicates': 0, 'pnl_per_share_after_buffers': None, 'positive_percent': None}
    if len(days) < 5:
        result['status'] = 'insufficient_entry_days'
        return result
    blocks = [days[day] for day in sorted(days)]
    rng = random.Random(20261005)
    pnl_samples, positive_samples = [], []
    for _ in range(1000):
        selected = [rng.choice(blocks) for _ in blocks]
        pnl = sum((b[0] for b in selected), D(0))
        shares = sum((b[1] for b in selected), D(0))
        pnl_samples.append(float(pnl/shares))
        positive_samples.append(100*sum(b[2] for b in selected)/sum(b[3] for b in selected))
    def interval(values):
        values.sort()
        def quantile(q):
            at = (len(values)-1)*q
            low = int(at)
            return round(values[low] + (values[min(low+1,len(values)-1)]-values[low])*(at-low), 6)
        return [quantile(.025), quantile(.975)]
    result.update(status='exploratory', replicates=1000,
                  pnl_per_share_after_buffers=interval(pnl_samples), positive_percent=interval(positive_samples))
    return result


def summarize_bucket(buys, marks, started, now):
    horizons = {}
    for horizon in HORIZONS:
        rows = []
        for buy in buys:
            if eligible(buy['at'], horizon, started):
                observation = marks.get((buy['key'], horizon), {})
                rows.append({**buy, 'sample_at': observation.get('at'), 'pnl': observation.get('pnl')})
        measured = [r for r in rows if r['pnl'] is not None]
        horizons[str(horizon)] = {**markout_summary(rows, horizon, now),
            'legacy_buys_excluded': len(buys)-len(rows),
            'distinct_observed_races': len({r['exchange'] for r in measured}),
            'uncertainty': day_block_intervals(rows)}
    return {'filled_buys': len(buys), 'filled_shares': str(sum((D(b['quantity']) for b in buys), D(0))),
            'distinct_races': len({b['exchange'] for b in buys}), 'horizons': horizons}


def report(path, now=None):
    path = Path(path).resolve()
    if not path.exists():
        return {'status': 'No execution journal yet'}
    db = sqlite3.connect(path.as_uri()+'?mode=ro', uri=True)
    db.row_factory = sqlite3.Row
    try:
        db.execute('BEGIN')
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {'executions', 'orders', 'events'}.issubset(tables):
            return {'status': 'Execution tracking is not available in this journal yet'}
        now = time.time() if now is None else now
        buys = [dict(r) for r in db.execute("SELECT e.*,o.payload AS order_payload FROM executions e "
            "LEFT JOIN orders o ON e.key=o.key WHERE e.action='buy' AND CAST(e.quantity AS REAL)>0 ORDER BY e.at,e.rowid")]
        marks = ({(r['key'],r['horizon']):dict(r) for r in db.execute(
            'SELECT * FROM markouts WHERE horizon IN (300,900)')} if 'markouts' in tables else {})
        started = tracking_start(db)
        gaps, linkage = linked_gaps(db, buys)
        prices, gap_groups = defaultdict(list), defaultdict(list)
        for buy in buys:
            prices[price_bucket(D(buy['price']))].append(buy)
            gap_groups[gap_bucket(gaps.get(buy['key']))].append(buy)
        return {'as_of': now, 'status': 'read_only', 'filled_buys': len(buys),
            'horizons_seconds': list(HORIZONS), 'short_horizons_started_at': started,
            'gap_linkage': linkage,
            'by_entry_price': [{'bucket': label, **summarize_bucket(prices[label], marks, started, now)}
                               for label in PRICE_BUCKETS],
            'by_entry_gap': [{'bucket': label, **summarize_bucket(gap_groups[label], marks, started, now)}
                             for label in GAP_BUCKETS],
            'notes': [
                'All filled buys appear in distributions. Five/fifteen-minute statistics include only buys eligible since short-horizon tracking began.',
                'Ranges include the lower endpoint and exclude the upper endpoint. Gap below 0.05 includes negative gaps.',
                'Entry price is the average fill price for the purchased YES or NO outcome. Gap uses the exactly linked pre-submission reference minus actual average fill price and one entry buffer.',
                'Legacy or invalid signal links remain unknown; no timestamp matching or reconstructed historical reference quotes are used.',
                'Markouts are hypothetical full-size liquidation values after entry/exit buffers, not realized P&L. Missing observations stay missing; inspect coverage and sampling delays.',
                'P&L per share is share-weighted; positive percentage counts measured buy orders. Repeated buys and correlated races are not independent trials.',
                'Intervals resample whole UTC entry days with 1,000 fixed-seed replicates and require five observed days. They ignore cross-day dependence; five days is not evidence of statistical adequacy or a trading edge.',
                'No thresholds, sizing or trading settings are changed by this report.']}
    finally:
        db.close()


def format_report(result):
    if result.get('status') != 'read_only':
        return result['status']
    lines = ['ENTRY ANALYSIS — historical observations; not realized profit',
             'Filled buys: ' + str(result['filled_buys']),
             'Gap links: ' + json.dumps(result['gap_linkage']['filled_buy_link_status'], sort_keys=True)]
    for field, title in (('by_entry_price','ENTRY PRICE'),('by_entry_gap','ENTRY GAP AFTER ENTRY BUFFER')):
        for horizon in HORIZONS:
            lines.extend(['', '%s — %s minutes' % (title,horizon//60),
                          'Bucket              Buys  Measured/matured  P&L/share(c)  Positive%  Missed'])
            for bucket in result[field]:
                stats=bucket['horizons'][str(horizon)]
                pnl=stats['pnl_per_share_after_buffers']
                positive=stats['positive_percent']
                lines.append('%-18s %5d %8s/%-7s %12s %10s %7d' % (
                    bucket['bucket'],bucket['filled_buys'],stats['observations'],stats['matured'],
                    '—' if pnl is None else '%.3f' % (100*D(pnl)),
                    '—' if positive is None else '%.1f' % positive,stats['missed']))
    lines.extend(['', 'Buys includes older fills excluded from short-horizon tracking; measured/matured counts only eligible buys.',
                  'Missing observations are not zero returns. Unknown gaps lack a reliable order-linked reference.',
                  'Repeated orders/races are correlated. Use --json for delays, distinct races and exploratory day-block intervals.'])
    return '\n'.join(lines)
