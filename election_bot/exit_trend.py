"""Spaced, position-specific quote history for confirmed partial profit exits."""
import hashlib
import json
import time

from .strategy import D, choose

DEFAULTS = {
    'window_seconds': 1800, 'sample_spacing_seconds': 60,
    'max_sample_gap_seconds': 600, 'minimum_samples': 3,
    'confirmation_samples': 2, 'minimum_span_seconds': 120,
    'trailing_drop': '0.02', 'reference_weakening': '0.02',
    'remaining_gap': '0.02', 'partial_fraction': '0.25',
}


def settings(execution):
    supplied = execution.get('exit_trend', {})
    if not isinstance(supplied, dict) or set(supplied)-set(DEFAULTS):
        raise ValueError('exit_trend must contain only supported settings')
    result = dict(DEFAULTS, **supplied)
    for key, low, high in (
        ('window_seconds', 180, 7200), ('sample_spacing_seconds', 15, 300),
        ('max_sample_gap_seconds', 30, 1800), ('minimum_samples', 3, 30),
        ('confirmation_samples', 2, 10), ('minimum_span_seconds', 30, 3600)):
        value = result[key]
        if type(value) is not int or not low <= value <= high:
            raise ValueError('Invalid exit_trend '+key)
    if not (result['sample_spacing_seconds'] <= result['max_sample_gap_seconds'] <= result['window_seconds']):
        raise ValueError('Invalid exit trend spacing/gap/window relationship')
    if (result['confirmation_samples'] >= result['minimum_samples'] or
        result['minimum_span_seconds'] > result['window_seconds'] or
        (result['minimum_samples']-1)*result['sample_spacing_seconds'] > result['window_seconds']):
        raise ValueError('Exit trend window cannot support the required observations')
    for key in ('trailing_drop', 'reference_weakening', 'remaining_gap'):
        if not 0 < D(result[key]) < 1:
            raise ValueError('Invalid exit_trend '+key)
    if not 0 < D(result['partial_fraction']) <= D('.5'):
        raise ValueError('Exit trend partial_fraction must be in (0, 0.5]')
    return result


class ExitTrend:
    def __init__(self, db):
        self.db = db
        with db:
            db.execute('''CREATE TABLE IF NOT EXISTS exit_trend_samples (
                exchange TEXT NOT NULL, epoch INTEGER NOT NULL, policy TEXT NOT NULL,
                at REAL NOT NULL, detail TEXT NOT NULL,
                PRIMARY KEY(exchange,epoch,policy,at))''')
            db.execute('CREATE INDEX IF NOT EXISTS exit_trend_at ON exit_trend_samples(at)')

    def epoch(self, exchange):
        row = self.db.execute('SELECT MAX(rowid) FROM executions WHERE exchange=? '
            'AND CAST(quantity AS REAL)>0', (exchange,)).fetchone()
        return row[0] or 0

    def context(self, mapping, book, refs, held, strategy, execution, phase, now=None):
        """Only valid normal scans can add samples; preflight cannot add confirmations."""
        choose(book, refs, strategy, 0)  # Validate immediately before persisting evidence.
        now = time.time() if now is None else now
        cfg = settings(execution)
        exchange = mapping['sig_exchange_id']
        epoch = self.epoch(exchange)
        side = 'yes' if held > 0 else 'no'
        oriented = [book]+list(refs) if held > 0 else [q.complement() for q in [book]+list(refs)]
        signature = {'contract': mapping['contract_fingerprint'], 'side': side,
                     'strategy': strategy, 'trend': cfg, 'take_profit_min': execution['take_profit_min']}
        policy = hashlib.sha256(json.dumps(signature,sort_keys=True).encode()).hexdigest()
        current = {'at': now, 'bid': str(oriented[0].bids[0][0]),
                   'depth': str(oriented[0].bids[0][1]),
                   'reference_bids': [str(b.bids[0][0]) for b in oriented[1:]],
                   'reference_depths': [str(b.bids[0][1]) for b in oriented[1:]]}
        rows = [json.loads(r[0]) for r in self.db.execute(
            'SELECT detail FROM exit_trend_samples WHERE exchange=? AND epoch=? AND policy=? '
            'AND at>=? ORDER BY at', (exchange,epoch,policy,now-cfg['window_seconds']))]
        if phase == 'scan' and (not rows or now-rows[-1]['at'] >= cfg['sample_spacing_seconds']):
            with self.db:
                self.db.execute('DELETE FROM exit_trend_samples WHERE at<?', (now-cfg['window_seconds'],))
                self.db.execute('DELETE FROM exit_trend_samples WHERE exchange=? AND (epoch<>? OR policy<>?)',
                                (exchange,epoch,policy))
                self.db.execute('INSERT INTO exit_trend_samples VALUES (?,?,?,?,?)',
                                (exchange,epoch,policy,now,json.dumps(current)))
            rows.append(current)
        return {'samples': rows, 'current': current, 'settings': cfg, 'epoch': epoch,
                'min_reference_depth': strategy['min_reference_depth']}


def assess(context, quantity):
    cfg, current = context['settings'], context['current']
    result = {'allowed': False, 'reason': 'trend_history_insufficient',
              'position_epoch': context['epoch'], 'required_samples': cfg['minimum_samples'],
              'confirmation_samples': cfg['confirmation_samples'], 'samples': 0,
              'window_seconds': cfg['window_seconds']}
    rows = []
    # An unusably thin point or a long outage breaks the chain, rather than being skipped.
    def usable(row):
        return (D(row['depth']) >= quantity and len(row['reference_bids']) == 2 and
                len(row['reference_depths']) == 2 and
                all(D(q) >= D(context['min_reference_depth']) for q in row['reference_depths']))
    for row in context['samples']:
        if row['at'] > current['at']:
            result['reason'] = 'trend_clock_reversal'
            return result
        if not usable(row):
            rows = []
            continue
        if rows and row['at']-rows[-1]['at'] > cfg['max_sample_gap_seconds']:
            rows = []
        rows.append(row)
    result['samples'] = len(rows)
    if not usable(current):
        result['reason'] = 'trend_depth_insufficient'
        return result
    if (not rows or current['at']-rows[-1]['at'] > cfg['max_sample_gap_seconds'] or
        len(rows) < cfg['minimum_samples'] or rows[-1]['at']-rows[0]['at'] < cfg['minimum_span_seconds']):
        return result
    baseline = [D(v) for v in rows[0]['reference_bids']]
    peak = D(rows[0]['bid'])
    checks = []
    def predicates(row, high):
        bid = D(row['bid']); references = [D(v) for v in row['reference_bids']]
        return {'gap_narrowed': max(references)-bid <= D(cfg['remaining_gap']),
                'references_weakened': all(a-b >= D(cfg['reference_weakening']) for a,b in zip(baseline,references)),
                'trailing_pullback': high-bid >= D(cfg['trailing_drop'])}
    for row in rows:
        checks.append(predicates(row,peak))
        peak = max(peak,D(row['bid']))
    current_checks = predicates(current,peak)
    confirmed = {key: current_checks[key] and all(c[key] for c in checks[-cfg['confirmation_samples']:])
                 for key in current_checks}
    result.update(span_seconds=round(rows[-1]['at']-rows[0]['at'],1),
                  peak_bid=str(peak), current_bid=current['bid'],
                  remaining_reference_gap=str(max(D(v) for v in current['reference_bids'])-D(current['bid'])),
                  pullback=str(max(D(0),peak-D(current['bid']))), confirmed=confirmed,
                  current_checks=current_checks)
    for reason in ('trailing_pullback','references_weakened','gap_narrowed'):
        if confirmed[reason]:
            result.update(allowed=True,reason=reason)
            return result
    rising = D(current['bid']) > D(rows[0]['bid'])
    supported = min(D(v) for v in current['reference_bids'])-D(current['bid']) > D(cfg['remaining_gap'])
    result['reason'] = 'hold_supported_rise' if rising and supported else 'hold_no_confirmed_exit'
    return result
