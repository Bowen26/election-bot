"""Read-only A/B comparison for the patient entry pilot. No broker access, no writes.

The comparison answers three separate questions, because one number cannot:

  fill rate            did offering a worse price to the seller cost us trades?
  price improvement    when we did fill, how much better was the entry price?
  post-fill path       did the fills we won move against us afterwards?

The third is the one that can overturn the pilot. A patient buy fills when
someone chooses to sell into it, which is also when the price is most likely to
keep falling. A better entry price with a worse post-fill path is adverse
selection, not an edge, so the report separates the price we paid from where the
market went next instead of reporting only their sum.

Only improvable control orders are compared against improved patient orders.
The patient route declines narrow spreads, so comparing it against every
control fill would measure the spread filter rather than patience.
"""
from collections import defaultdict
from pathlib import Path
import sqlite3

from .strategy import D

HORIZONS = (60, 300, 900, 3600, 86400)


def mean(values):
    return str(sum(values, D(0))/len(values)) if values else None


def collect(db):
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if 'patient_orders' not in tables:
        return None
    rows = db.execute('''SELECT t.key, t.exchange, t.arm, t.priced, t.improvable,
                                e.quantity, e.price, e.buffer
                         FROM patient_orders t LEFT JOIN executions e ON e.key=t.key''').fetchall()
    marks = defaultdict(dict)
    if 'markouts' in tables:
        for mark in db.execute('SELECT key, horizon, pnl FROM markouts'):
            marks[mark['key']][mark['horizon']] = mark['pnl']
    return rows, marks


def report(source):
    """Accepts a journal path or an open connection; opens read-only and never writes."""
    if isinstance(source, (str, Path)):
        path = Path(source).resolve()
        if not path.exists():
            return {'status': 'no_journal', 'note': 'No execution journal yet.'}
        db = sqlite3.connect(path.as_uri()+'?mode=ro', uri=True)
        db.row_factory = sqlite3.Row
        try:
            return summarize(db)
        finally:
            db.close()
    return summarize(source)


def summarize(db):
    gathered = collect(db)
    if gathered is None:
        return {'status': 'not_started', 'note': 'The patient entry pilot has never run.'}
    rows, marks = gathered
    arms = {}
    unclassified = 0
    for row in rows:
        if row['arm'] == 'unknown':
            unclassified += 1
            continue
        # 'improved' means the order actually went out inside the spread.
        # An improvable control order is its matched comparison.
        if row['priced'] == 'improved':
            group = 'patient_improved'
        elif row['improvable'] == 1:
            group = 'control_improvable'
        elif row['improvable'] == 0:
            group = 'control_not_improvable'
        else:
            group = 'control_unknown_counterfactual'
        bucket = arms.setdefault(group, {'attempts': 0, 'fills': 0, 'shares': D(0),
                                         'prices': [], 'exits': defaultdict(list),
                                         'markouts': defaultdict(list)})
        bucket['attempts'] += 1
        quantity = D(row['quantity']) if row['quantity'] is not None else D(0)
        if quantity <= 0:
            continue                      # an expired order is an attempt, not a fill
        bucket['fills'] += 1
        bucket['shares'] += quantity
        price, buffer = D(row['price']), D(row['buffer'])
        bucket['prices'].append(price)
        for horizon, pnl in marks.get(row['key'], {}).items():
            if pnl is None:
                continue
            per_share = D(pnl)/quantity
            bucket['markouts'][horizon].append(per_share)
            # Where the market went, with what we paid added back in, so the two
            # arms are compared on the price path rather than on their entry price.
            bucket['exits'][horizon].append(per_share+price+2*buffer)

    summary = {}
    for name, bucket in sorted(arms.items()):
        summary[name] = {
            'attempts': bucket['attempts'], 'fills': bucket['fills'],
            'fill_rate': (str(D(bucket['fills'])/D(bucket['attempts']))
                          if bucket['attempts'] else None),
            'shares_filled': str(bucket['shares']),
            'average_entry_price': mean(bucket['prices']),
            'average_markout_per_share': {h: mean(bucket['markouts'][h]) for h in HORIZONS
                                          if bucket['markouts'][h]},
            'average_exit_value_per_share': {h: mean(bucket['exits'][h]) for h in HORIZONS
                                             if bucket['exits'][h]},
            'measured_fills_by_horizon': {h: len(bucket['markouts'][h]) for h in HORIZONS
                                          if bucket['markouts'][h]}}

    patient = summary.get('patient_improved', {})
    control = summary.get('control_improvable', {})
    comparison = {'matched_on': 'improved patient orders against improvable control orders'}
    for label, key in (('price_improvement_per_share', 'average_entry_price'),):
        if patient.get(key) and control.get(key):
            comparison[label] = str(D(control[key])-D(patient[key]))
    for horizon in HORIZONS:
        pair = [s.get('average_exit_value_per_share', {}).get(horizon) for s in (patient, control)]
        if all(pair):
            comparison.setdefault('exit_value_difference_per_share', {})[horizon] = \
                str(D(pair[0])-D(pair[1]))
        pair = [s.get('average_markout_per_share', {}).get(horizon) for s in (patient, control)]
        if all(pair):
            comparison.setdefault('markout_difference_per_share', {})[horizon] = \
                str(D(pair[0])-D(pair[1]))

    least = min((group.get('fills', 0) for group in (patient, control)), default=0)
    return {'status': 'measured' if least else 'no_matched_fills', 'by_group': summary,
            'comparison': comparison, 'unclassified_orders': unclassified,
            'note': 'Counts are attempts and fills, not a profit estimate. Markouts are per share '
                    'after two configured cost buffers and are not verified fees. A positive '
                    'price improvement only shows a better entry price; a negative exit value '
                    'difference at the same horizon means the patient fills were selected against, '
                    'and the two must be read together. Exit value adds the entry price and '
                    'buffers back so the arms are compared on the later price path alone. '
                    'Unmeasured horizons are omitted rather than treated as zero, so groups may '
                    'rest on different numbers of fills; measured_fills_by_horizon gives each '
                    'count. Arm assignment is deterministic, not randomized per order, so a '
                    'contract with few buys can land entirely in one arm. '
                    + ('Fewer than %d matched fills in the smaller group: differences here are '
                       'not yet evidence.' % 30 if least < 30 else
                       'Both groups have at least 30 matched fills.')}


def format_report(result):
    lines = ['PATIENT ENTRY PILOT — improved-price A/B against the current entry method']
    if result['status'] in ('not_started', 'no_journal'):
        return '\n'.join(lines+[result['note']])
    for name, group in result['by_group'].items():
        rate = group['fill_rate']
        lines.append('\n%s: %d attempts, %d fills%s' % (
            name.replace('_', ' '), group['attempts'], group['fills'],
            '' if rate is None else ' (%.1f%% filled)' % (float(rate)*100)))
        if group['average_entry_price'] is not None:
            lines.append('  average entry price: %.4f' % float(group['average_entry_price']))
        for horizon in HORIZONS:
            mark = group['average_markout_per_share'].get(horizon)
            exit_value = group['average_exit_value_per_share'].get(horizon)
            if mark is None:
                continue
            lines.append('  %6ds: markout %+.3f c/share; later bid %.4f (%d measured)' % (
                horizon, float(mark)*100, float(exit_value),
                group['measured_fills_by_horizon'][horizon]))
    comparison = result['comparison']
    improvement = comparison.get('price_improvement_per_share')
    if improvement is None:
        lines.append('\nNo matched fills in both arms yet; nothing to compare.')
    else:
        lines.append('\nPATIENT MINUS CONTROL (%s)' % comparison['matched_on'])
        lines.append('  price improvement won at entry: %+.3f cents/share' % (float(improvement)*100))
        for horizon in HORIZONS:
            later = comparison.get('exit_value_difference_per_share', {}).get(horizon)
            net = comparison.get('markout_difference_per_share', {}).get(horizon)
            if net is None:
                continue
            lines.append('  %6ds: later price %+.3f c/share, net of entry %+.3f c/share' % (
                horizon, float(later)*100, float(net)*100))
        lines.append('  A positive entry improvement with a negative later price is adverse\n'
                     '  selection: the better fills were the ones about to move against us.')
    if result['unclassified_orders']:
        lines.append('\nUnclassified orders: %d' % result['unclassified_orders'])
    lines.append('\n'+result['note'])
    return '\n'.join(lines)
