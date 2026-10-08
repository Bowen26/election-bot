"""Prospective exit-depth experiments. Reporting never submits or reserves orders."""
from collections import Counter, defaultdict
import json
from pathlib import Path
import sqlite3
import time

from .leadership import HORIZONS, future_quote, load_events
from .strategy import D, choose_exit
from .profit_exit import profit_target
from .exit_trend import settings as trend_settings
from dataclasses import replace

POLICIES = ('legacy', 'route_depth', 'sig_depth')


def checked_exit(book, refs, settings, execution, held, cost, per_order, sale_basis,
                 detail, quantity_cap=None, policy='legacy', trend_check=None):
    """Shared FIFO check; optional live profit target preserves legacy experiments."""
    if not execution['sell_enabled']:
        detail.update(status='blocked', reason='selling_disabled')
        return None
    signal = choose_exit(book, refs, settings, execution, held, cost, per_order,
                         detail, quantity_cap, reference_policy=policy)
    trend = (policy == 'legacy' and execution.get('profit_target_enabled', False)
             and execution.get('profit_exit_mode', 'fixed') == 'trend')
    # Overpricing remains an independent risk exit. All profitable exits use the
    # trend gate in this mode, including a legacy convergence proposal.
    if trend and signal and signal.reason == 'convergence_take_profit':
        detail['legacy_reason'] = signal.reason
        signal = None
    if signal and signal.reason == 'convergence_take_profit':
        basis = sale_basis(signal.side, signal.quantity)
        net = signal.price-D(settings['cost_buffer_per_share'])
        route = detail['routes']['convergence_take_profit']
        route.update(fifo_cost_per_share=str(basis/signal.quantity),
                     net_profit_per_share_fifo=str(net-basis/signal.quantity),
                     fifo_profit_passed=net-basis/signal.quantity >= D(execution['take_profit_min']))
        if net-basis/signal.quantity < D(execution['take_profit_min']):
            detail.update(status='blocked', reason='fifo_profit_below_minimum')
            signal = None
    if signal is None and policy == 'legacy' and execution.get('profit_target_enabled', False):
        detail['legacy_reason'] = detail.get('reason')
        cap = quantity_cap
        if trend and held:
            partial = max(1, int(abs(D(held))*D(trend_settings(execution)['partial_fraction'])))
            cap = partial if cap is None else min(D(cap), partial)
            detail['partial_quantity_cap'] = str(cap)
        signal = profit_target(book, settings, execution, held, cost, per_order,
                               sale_basis, detail, cap)
        if trend and signal:
            check = trend_check(signal.quantity) if trend_check else {
                'allowed': False, 'reason': 'trend_history_unavailable'}
            detail['trend'] = check
            if check['allowed']:
                signal = replace(signal, reason='trend_profit_target')
                detail.update(reason=signal.reason, trend_trigger=check['reason'])
            else:
                detail.update(status='blocked', reason=check['reason'])
                signal = None
    return signal


def experiment(book, refs, settings, execution, held, cost, per_order, sale_basis,
               baseline, baseline_detail, quantity_cap=None):
    fee = D(settings['cost_buffer_per_share'])
    live_profit_target = execution.get('profit_target_enabled', False)
    # Never relabel a new live profit-target proposal as historical "legacy".
    study_execution = dict(execution, profit_target_enabled=False)
    decisions = []
    for policy in POLICIES:
        detail = dict(baseline_detail) if policy == 'legacy' and not live_profit_target else {}
        if not execution['sell_enabled']:
            signal = None
            detail.update(status='blocked', reason='selling_disabled')
        elif policy == 'legacy' and not live_profit_target:
            signal = baseline
        else:
            signal = checked_exit(book, refs, settings, study_execution, held, cost,
                                  per_order, sale_basis, detail, quantity_cap, policy)
        candidate = None
        if signal:
            basis = sale_basis(signal.side, signal.quantity)
            candidate = {**vars(signal), 'fifo_basis': basis,
                         'hypothetical_realized_pnl_after_buffers':
                             signal.quantity*(signal.price-fee)-basis}
        decisions.append({'policy': policy, 'candidate': candidate, 'check': detail})
    return {'version': 1, 'experiment': 'exit_depth_v1', 'simulation_only': True,
            'held': held, 'cost': cost, 'quantity_cap': quantity_cap, 'per_order': per_order,
            'buffer': fee, 'settings': dict(settings), 'execution': dict(execution),
            'decisions': decisions}


def _read_studies(db, since, now):
    records, conflicts, counts = {}, set(), Counter()
    for at, text in db.execute("SELECT at,detail FROM events WHERE kind='exit_shadow' AND at>=? AND at<=? ORDER BY at,rowid", (since, now)):
        counts['events'] += 1
        try:
            data = json.loads(text)
            sid = data['snapshot_id']
            if (data['version'] != 1 or data['phase'] != 'scan' or data['experiment'] != 'exit_depth_v1'
                    or not isinstance(sid, str) or not sid or data['simulation_only'] is not True):
                raise ValueError('Invalid experiment identity')
            if sid in records:
                counts['duplicate_ids'] += 1
                if data != records[sid][1]:
                    conflicts.add(sid)
            else:
                records[sid] = (at, data)
        except (KeyError, ValueError, TypeError):
            counts['malformed'] += 1
    for sid in conflicts:
        records.pop(sid, None)
    counts['conflicting_ids'] = len(conflicts)
    return records, counts


def _candidates(data, start):
    """Check stored economics against original SIG top level; no inferred fills."""
    rows = data['decisions']
    if len(rows) != 3 or {r['policy'] for r in rows} != set(POLICIES):
        raise ValueError('Missing or duplicate policies')
    held, fee = D(data['held']), D(data['buffer'])
    if not held or fee < 0:
        raise ValueError('Invalid holdings or buffer')
    side = 'yes' if held > 0 else 'no'
    book = start['SIG']
    bid, size = (book['bid'], book['bid_size']) if side == 'yes' else (1-book['ask'], book['ask_size'])
    result = {}
    for row in rows:
        c = row['candidate']
        if c is not None:
            q, price, basis = D(c['quantity']), D(c['price']), D(c['fifo_basis'])
            if (c['action'] != 'sell' or c['side'] != side or price != bid or price % D('.005')
                    or q <= 0 or q != int(q) or q > min(abs(held), size, D(data['settings']['max_shares_per_order']))
                    or q*price > D(data['per_order']) or basis < 0 or basis > D(data['cost'])
                    or D(c['hypothetical_realized_pnl_after_buffers']) != q*(price-fee)-basis):
                raise ValueError('Invalid candidate economics')
            if data['quantity_cap'] is not None and q > D(data['quantity_cap']):
                raise ValueError('Candidate exceeds exposure headroom')
            c = {**c, 'quantity': q, 'price': price}
        result[row['policy']] = c
    return result


def report(path, now=None, hours=24):
    now = time.time() if now is None else float(D(now))
    hours = float(D(hours))
    if not 0 < hours <= 720:
        raise ValueError('Hours must be greater than zero and at most 720')
    base = {'status': 'No exit study journal yet', 'as_of': now, 'hours': hours,
            'note': 'Simulated exits on real holdings, not independent portfolios or actual fills. '
                    'Immediate P&L includes FIFO entry cost and the exit buffer. Sell-now advantage '
                    'compares the same quantity sold now versus at the first valid future SIG bid, '
                    'with the same exit buffer on both. Positive favors selling sooner. Missing or thin '
                    'future quotes are not zero returns. Repeated races and days are correlated; '
                    'do not add these overlapping proposals or different horizons into a profit total.'}
    path = Path(path).resolve()
    if not path.exists():
        return base
    db = sqlite3.connect(path.as_uri()+'?mode=ro', uri=True)
    try:
        db.execute('BEGIN')
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='events' AND type='table'").fetchone():
            return base
        since = now-hours*3600
        quotes, quote_counts = load_events(db, since, now)
        studies, counts = _read_studies(db, since, now)
    finally:
        db.close()
    by_id = {r['id']: r for r in quotes}
    by_race = defaultdict(list)
    for row in quotes:
        by_race[row['exchange']].append(row)
    rows = []
    proposal_counts = {p: Counter() for p in POLICIES}
    latest = []
    for sid, (at, study) in studies.items():
        start = by_id.get(sid)
        if not start or study.get('exchange') != start['exchange'] or not 0 <= at-start['at'] <= 5:
            counts['unmatched_or_invalid_SIG_snapshot'] += 1
            continue
        if study.get('status') == 'unavailable':
            counts['measurement_unavailable'] += 1
            continue
        try:
            candidates = _candidates(study, start)
            counts['valid_comparisons'] += 1
            for entry in study['decisions']:
                policy, c = entry['policy'], candidates[entry['policy']]
                proposal_counts[policy][entry['check'].get('reason', 'unknown')] += 1
                if c:
                    proposal_counts[policy]['eligible_proposals'] += 1
                    legacy = candidates['legacy']
                    if policy != 'legacy' and legacy is None:
                        proposal_counts[policy]['additional_vs_legacy'] += 1
                    elif policy != 'legacy' and c['quantity'] > legacy['quantity']:
                        proposal_counts[policy]['larger_than_legacy'] += 1
            rows.append((start, candidates))
            latest.append({'at': start['at'], 'exchange': start['exchange'], 'decisions': study['decisions']})
        except (KeyError, ValueError, TypeError, ArithmeticError):
            counts['invalid_economics'] += 1
    horizons = {}
    for horizon, window in HORIZONS.items():
        next_at = {}
        statuses = {p: Counter() for p in POLICIES}
        measured = {p: [] for p in POLICIES}
        for start, candidates in sorted(rows, key=lambda r: (r[0]['at'], r[0]['id'])):
            ex = start['exchange']
            if start['at'] < next_at.get(ex, float('-inf')):
                continue
            next_at[ex] = start['at']+horizon+window
            scans = by_race[ex]
            future = future_quote(scans, [r['at'] for r in scans], start, horizon, window)
            for policy, c in candidates.items():
                if c is None:
                    statuses[policy]['no_candidate'] += 1
                    continue
                status = ('measured' if future else 'not_due' if now < start['at']+horizon
                          else 'awaiting_quote' if now <= start['at']+horizon+window else 'missed')
                if future:
                    sig = future['SIG']
                    bid, depth = (sig['bid'], sig['bid_size']) if c['side'] == 'yes' else (1-sig['ask'], sig['ask_size'])
                    if depth < c['quantity']:
                        status = 'insufficient_future_depth'
                    else:
                        measured[policy].append({'exchange': ex, 'quantity': c['quantity'],
                            'advantage': c['price']-bid,
                            'immediate_pnl': D(c['hypothetical_realized_pnl_after_buffers']),
                            'delay': future['at']-start['at']-horizon})
                statuses[policy][status] += 1
        horizons[str(horizon)] = {}
        for policy in POLICIES:
            items = measured[policy]
            quantity = sum((r['quantity'] for r in items), D(0))
            horizons[str(horizon)][policy] = {'statuses': dict(statuses[policy]), 'measured': len(items),
                'races': len({r['exchange'] for r in items}),
                'sell_now_advantage_per_share': str(sum((r['quantity']*r['advantage'] for r in items), D(0))/quantity) if quantity else None,
                'immediate_hypothetical_pnl_per_share': str(sum((r['immediate_pnl'] for r in items), D(0))/quantity) if quantity else None,
                'mean_followup_delay_seconds': sum(r['delay'] for r in items)/len(items) if items else None}
    base.update(status='ok', events=dict(counts), quote_events=quote_counts,
                proposals={p: dict(c) for p, c in proposal_counts.items()}, horizons=horizons,
                latest=sorted(latest, key=lambda r: r['at'], reverse=True)[:5])
    return base


def format_report(result):
    lines = ['EXIT DEPTH STUDY — legacy depth comparisons; excludes the optional profit-target route']
    if result['status'] != 'ok':
        return '\n'.join(lines+[result['status']])
    lines.append('Records: '+str(result['events']))
    for policy, counts in result['proposals'].items():
        lines.append(policy+': '+str(counts))
    for horizon, policies in result['horizons'].items():
        lines.append('\n'+str(int(horizon)//60)+' MINUTES: sell-now advantage (positive favors earlier sale)')
        for policy, r in policies.items():
            value = r['sell_now_advantage_per_share']
            formatted = '—' if value is None else f'{float(value)*100:.3f} cents/share'
            lines.append(f'{policy}: {r["measured"]} measured; {formatted}; {r["statuses"]}')
    lines.append('\nMissing outcomes are not zero. Proposals are correlated and fills are assumptions. Use --json for details.')
    return '\n'.join(lines)
