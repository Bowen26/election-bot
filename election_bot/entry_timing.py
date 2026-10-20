"""Prospective entry-timing study. Simulation only; never changes live entry rules.

For each recorded price-gap proposal (the live both-venue reference rule, as
logged in shadow_decision events) compare two hypothetical entries:

* immediate: buy at the displayed SIG ask when the proposal was recorded;
* wait_for_bid: buy only after a fresh, sustained SIG bid improvement on the
  proposed side, at the ask displayed when that improvement is confirmed.

Both are marked at the same absolute time after the proposal, so they face the
same news and the same exit book. Each outcome is split into price movement
(mid to mid), half-spreads paid at entry and exit, and the configured cost
buffers, so a timing effect is not confused with spread or buffer costs.

The rule parameters below were fixed before collection began (PREREGISTERED_AT).
Proposals before that time are reported only with include_retrospective, and
are labelled exploratory.
"""
from collections import Counter, defaultdict
from datetime import datetime, timezone
from decimal import InvalidOperation
from pathlib import Path
import sqlite3
import time

from .leadership import candidate, future_quote, load_events, number
from .strategy import D

VERSION = 'entry_timing_v1'
PREREGISTERED_AT = datetime(2026, 10, 20, tzinfo=timezone.utc).timestamp()
VARIANT = 'both_bids'           # The live price-gap reference rule.
RULE = dict(max_wait_seconds=300, min_bid_improvement='0.01', sustain_snapshots=2,
            sustain_seconds=30, max_snapshot_gap_seconds=120, max_chase='0.03')
EXIT_HORIZONS = {900: 180}      # Seconds after the proposal -> observation window.
POLICIES = ('immediate', 'wait_for_bid')


def side_quote(sig, side):
    """Orient a SIG top of book to the proposed side."""
    if side == 'yes':
        return {'bid': sig['bid'], 'ask': sig['ask'], 'bid_size': sig['bid_size'], 'ask_size': sig['ask_size']}
    return {'bid': 1-sig['ask'], 'ask': 1-sig['bid'], 'bid_size': sig['ask_size'], 'ask_size': sig['bid_size']}


def mid(q):
    return (q['bid']+q['ask'])/2


def confirmation(rows, start, side, rule=RULE, until=None):
    """First snapshot completing a fresh, sustained bid improvement; else None.

    Only SIG quotes whose source and fetch clocks both advance count as fresh.
    A non-improving fresh quote or a long quote gap resets the streak.
    """
    base = side_quote(start['SIG'], side)['bid']
    need = base + D(rule['min_bid_improvement'])
    last = min(start['SIG']['source'], start['SIG']['observed'])
    deadline = start['at'] + rule['max_wait_seconds'] if until is None else until
    streak = []
    for row in rows:
        if row['at'] <= start['at']:
            continue
        if row['at'] > deadline:
            break
        clock = min(row['SIG']['source'], row['SIG']['observed'])
        if clock <= last:
            continue  # A refetched old book is not new evidence.
        last = clock
        if side_quote(row['SIG'], side)['bid'] < need:
            streak = []
            continue
        if streak and row['at']-streak[-1]['at'] > rule['max_snapshot_gap_seconds']:
            streak = []
        streak.append(row)
        if len(streak) >= rule['sustain_snapshots'] and streak[-1]['at']-streak[0]['at'] >= rule['sustain_seconds']:
            return row
    return None


def outcome(entry_row, exit_row, side, quantity, buffer, entry_price=None):
    entry = side_quote(entry_row['SIG'], side)
    exit_ = side_quote(exit_row['SIG'], side)
    if exit_['bid_size'] < quantity:
        return {'status': 'insufficient_exit_depth'}
    ask = entry['ask'] if entry_price is None else entry_price
    parts = {'price_movement': mid(exit_)-mid(entry),
             'entry_half_spread': ask-mid(entry),
             'exit_half_spread': mid(exit_)-exit_['bid'],
             'cost_buffers': 2*buffer}
    net = parts['price_movement']-parts['entry_half_spread']-parts['exit_half_spread']-parts['cost_buffers']
    if net != exit_['bid']-ask-2*buffer:
        raise ArithmeticError('Decomposition mismatch')
    return {'status': 'measured', 'quantity': quantity, 'entry_price': ask, 'entry_at': entry_row['at'],
            'net_per_share': net, **parts}


def evaluate(start, rows, stamps, c, horizon, window, now, rule=RULE):
    """Paired immediate/waiting outcomes for one proposal."""
    side, qty, fee = c['side'], c['quantity'], c['buffer']
    target_end = start['at']+horizon+window
    exit_row = future_quote(rows, stamps, start, horizon, window)
    pending = 'not_due' if now < start['at']+horizon else 'awaiting_quote' if now <= target_end else 'missed_exit_quote'
    result = {}
    result['immediate'] = (outcome(start, exit_row, side, qty, fee, c['price']) if exit_row
                           else {'status': pending})
    confirm = confirmation(rows, start, side, rule, until=min(start['at']+rule['max_wait_seconds'], start['at']+horizon))
    if confirm is None:
        waited = now >= start['at']+min(rule['max_wait_seconds'], horizon)
        result['wait_for_bid'] = {'status': 'no_confirmation' if waited else 'waiting'}
    else:
        entry = side_quote(confirm['SIG'], side)
        if entry['ask']-c['price'] > D(rule['max_chase']):
            result['wait_for_bid'] = {'status': 'chased_away', 'confirmed_at': confirm['at']}
        elif entry['ask_size'] < qty:
            result['wait_for_bid'] = {'status': 'insufficient_entry_depth', 'confirmed_at': confirm['at']}
        elif exit_row is None:
            result['wait_for_bid'] = {'status': pending, 'confirmed_at': confirm['at']}
        else:
            result['wait_for_bid'] = dict(outcome(confirm, exit_row, side, qty, fee),
                                          wait_seconds=confirm['at']-start['at'])
    if exit_row:
        for r in result.values():
            r['exit_delay_seconds'] = exit_row['at']-start['at']-horizon
    return result


COMPONENTS = ('net_per_share', 'price_movement', 'entry_half_spread', 'exit_half_spread', 'cost_buffers')


def mean_components(items):
    shares = sum((r['quantity'] for r in items), D(0))
    if not shares:
        return {k: None for k in COMPONENTS}
    return {k: str(sum((r[k]*r['quantity'] for r in items), D(0))/shares) for k in COMPONENTS}


def summarize(samples):
    summary = {}
    for policy in POLICIES:
        measured = [s[policy] for s in samples if s[policy]['status'] == 'measured']
        summary[policy] = {'statuses': dict(Counter(s[policy]['status'] for s in samples)),
                           'measured': len(measured), 'races': len({s['exchange'] for s in samples
                                                                     if s[policy]['status'] == 'measured'}),
                           'per_share_after_buffers': mean_components(measured),
                           'positive_fraction': (sum(r['net_per_share'] > 0 for r in measured)/len(measured)
                                                 if measured else None)}
    paired = [s for s in samples if all(s[p]['status'] == 'measured' for p in POLICIES)]
    immediate = mean_components([s['immediate'] for s in paired])
    waiting = mean_components([s['wait_for_bid'] for s in paired])
    summary['paired_both_entered'] = {
        'samples': len(paired), 'immediate': immediate, 'wait_for_bid': waiting,
        'wait_minus_immediate': {k: (str(D(waiting[k])-D(immediate[k])) if waiting[k] is not None else None)
                                 for k in COMPONENTS}}
    # What waiting skipped: immediate outcomes of proposals that never confirmed.
    skipped = [s['immediate'] for s in samples if s['immediate']['status'] == 'measured'
               and s['wait_for_bid']['status'] in ('no_confirmation', 'chased_away', 'insufficient_entry_depth')]
    summary['immediate_outcome_when_wait_skipped'] = dict(samples=len(skipped), **mean_components(skipped))
    return summary


def analyze(rows, now, include_retrospective=False, rule=RULE):
    by_race = defaultdict(list)
    for row in rows:
        by_race[row['exchange']].append(row)
    counts, horizons = Counter(), {}
    for horizon, window in EXIT_HORIZONS.items():
        samples = []
        for exchange, scans in sorted(by_race.items()):
            stamps = [r['at'] for r in scans]
            next_at = float('-inf')
            for start in scans:
                if not start['aligned'] or not start.get('shadow'):
                    continue
                if start['at'] < PREREGISTERED_AT and not include_retrospective:
                    counts['before_preregistration'] += 1
                    continue
                try:
                    c = candidate(start['shadow'], VARIANT, start)
                except (KeyError, ValueError, TypeError, InvalidOperation, StopIteration):
                    counts['invalid_prospective_record'] += 1
                    continue
                if c is None:
                    continue
                # One proposal per race per evaluation span keeps samples non-overlapping.
                if start['at'] < next_at:
                    counts['overlap_excluded'] += 1
                    continue
                next_at = start['at']+horizon+window
                try:
                    result = evaluate(start, scans, stamps, c, horizon, window, now, rule)
                except ArithmeticError:
                    counts['invalid_economics'] += 1
                    continue
                samples.append({'id': start['id'], 'exchange': exchange, 'at': start['at'],
                                'prospective': start['at'] >= PREREGISTERED_AT, **result})
        horizons[str(horizon)] = {'window_seconds': window, 'proposals': len(samples),
                                  'prospective_proposals': sum(s['prospective'] for s in samples),
                                  **summarize(samples)}
    return horizons, dict(counts)


def report(path, hours=48, now=None, include_retrospective=False):
    now = time.time() if now is None else number(now)
    hours = number(hours)
    if not 0 < hours <= 720:
        raise ValueError('Hours must be greater than zero and at most 720')
    path = Path(path).resolve()
    base = {'as_of': now, 'hours': hours, 'status': 'No quote journal yet', 'version': VERSION,
            'method': {'preregistered_at': PREREGISTERED_AT, 'rule': RULE, 'variant': VARIANT,
                       'exit_horizons_seconds': {str(k): v for k, v in EXIT_HORIZONS.items()},
                       'include_retrospective': include_retrospective},
            'limitations': [
                'Simulation only: no orders, queue position, latency or partial fills; displayed tops are assumed fillable.',
                'The waiting entry does not recheck the external reference gap at confirmation time.',
                'Both policies use the proposal size and are marked at the same absolute time after the proposal.',
                'Components are per share after buffers; net = movement - entry half-spread - exit half-spread - buffers.',
                'Proposals, races and days are correlated; counts are not independent trials. Missing outcomes are not zero.']}
    if not path.exists():
        return base
    db = sqlite3.connect(path.as_uri()+'?mode=ro', uri=True)
    try:
        db.execute('BEGIN')
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='events'").fetchone():
            return base
        since = now-hours*3600
        rows, quote_counts = load_events(db, since, now, include_followups=True)
    finally:
        db.close()
    horizons, counts = analyze(rows, now, include_retrospective)
    base.update(status='ok', quote_events=quote_counts, events=counts, horizons=horizons)
    return base


def format_report(result):
    lines = ['ENTRY TIMING STUDY — immediate vs wait for sustained SIG bid improvement; simulation only']
    if result['status'] != 'ok':
        return '\n'.join(lines+[result['status']])
    rule = result['method']['rule']
    lines.append(f"Rule: bid +{rule['min_bid_improvement']} on {rule['sustain_snapshots']} fresh snapshots over "
                 f"{rule['sustain_seconds']}s within {rule['max_wait_seconds']}s; max chase {rule['max_chase']}")
    if result['method']['include_retrospective']:
        lines.append('Includes proposals before pre-registration: EXPLORATORY, not a prospective test.')

    def cents(value):
        return '—' if value is None else f'{float(value)*100:+.2f}c'
    for horizon, item in result['horizons'].items():
        lines.extend(['', f"{int(horizon)//60} MINUTES after proposal — {item['proposals']} proposals "
                          f"({item['prospective_proposals']} prospective)",
                      'Policy         Measured   Net      Move     EntrySprd ExitSprd Buffers  Statuses'])
        for policy in POLICIES:
            s, c = item[policy], item[policy]['per_share_after_buffers']
            lines.append(f"{policy:14} {s['measured']:8} {cents(c['net_per_share']):>8} {cents(c['price_movement']):>8} "
                         f"{cents(c['entry_half_spread']):>9} {cents(c['exit_half_spread']):>8} {cents(c['cost_buffers']):>8}  {s['statuses']}")
        paired = item['paired_both_entered']
        lines.append(f"Paired (both entered): {paired['samples']}; wait minus immediate net "
                     f"{cents(paired['wait_minus_immediate']['net_per_share'])}, movement "
                     f"{cents(paired['wait_minus_immediate']['price_movement'])}")
        skipped = item['immediate_outcome_when_wait_skipped']
        lines.append(f"Immediate outcome where waiting never entered: {skipped['samples']} samples, "
                     f"net {cents(skipped['net_per_share'])}")
    lines.append('\nHypothetical outcomes, not realized P&L. Use --json for full statuses and limitations.')
    return '\n'.join(lines)
