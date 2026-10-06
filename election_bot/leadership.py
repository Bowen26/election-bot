"""Read-only, descriptive lead/lag and prospective entry experiment reports."""
from bisect import bisect_left
from collections import Counter, defaultdict
from datetime import datetime, timezone
from decimal import InvalidOperation
import json
import math
from pathlib import Path
import sqlite3
import time

from .shadow import VARIANTS
from .strategy import D

HORIZONS = {300: 120, 900: 180}
MAX_AGE = 15
MAX_ALIGNMENT = 5
MOVE_MIN = .01
VENUES = ('SIG', 'Kalshi', 'Polymarket')


def number(value):
    if isinstance(value, bool):
        raise ValueError('Boolean numeric value')
    result = float(value)
    if not math.isfinite(result):
        raise ValueError('Nonfinite value')
    return result


def quote(raw, captured):
    """Require recorded quality AND independently check top level and timestamps."""
    if raw['quality']['issues'] != []:
        raise ValueError('Recorded quote issues')
    bid, bq = [D(x) for x in raw['bid']]
    ask, aq = [D(x) for x in raw['ask']]
    observed, source = number(raw['observed_at']), number(raw['source_at'])
    if not (0 < bid < ask < 1 and bq > 0 and aq > 0):
        raise ValueError('Invalid top of book')
    if any(not 0 <= captured-stamp <= MAX_AGE for stamp in (observed, source)):
        raise ValueError('Stale or future quote')
    return {'bid': bid, 'ask': ask, 'bid_size': bq, 'ask_size': aq,
            'mid': float((bid+ask)/2), 'observed': observed, 'source': source,
            'timestamp_basis': str(raw['timestamp_basis'])}


def load_events(db, since, now):
    """Exact snapshot identity only. Conflicting duplicates are all excluded."""
    snapshots, shadows = {}, {}
    conflicts, shadow_conflicts = set(), set()
    counts = Counter()
    for at, kind, text in db.execute(
            "SELECT at,kind,detail FROM events WHERE at>=? AND at<=? "
            "AND kind IN ('quote_snapshot','shadow_decision') ORDER BY at,rowid", (since-300, now)):
        counts[kind+'_events'] += 1
        try:
            item = json.loads(text)
            if item['version'] != 1 or item['phase'] != 'scan':
                counts['excluded_version_or_preflight'] += 1
                continue
            sid = item['snapshot_id']
            if not isinstance(sid, str) or not sid or not isinstance(item['exchange'], str):
                raise ValueError('Invalid identity')
            dest, bad = (snapshots, conflicts) if kind == 'quote_snapshot' else (shadows, shadow_conflicts)
            if sid in dest:
                counts[kind+'_duplicate_ids'] += 1
                if dest[sid][1] != item:
                    bad.add(sid)
            else:
                dest[sid] = (at, item)
        except (KeyError, TypeError, ValueError):
            counts['malformed_events'] += 1
    for sid in conflicts:
        snapshots.pop(sid, None)
    for sid in shadow_conflicts:
        shadows.pop(sid, None)
    counts['conflicting_snapshot_ids'] = len(conflicts)
    counts['conflicting_shadow_ids'] = len(shadow_conflicts)
    rows = []
    for sid, (event_at, raw) in snapshots.items():
        try:
            captured = number(raw['captured_at'])
            if abs(captured-event_at) > 5 or captured > now:
                raise ValueError('Capture/event time mismatch')
            raw_quotes = {q['venue']: q for q in raw['quotes']}
            if len(raw['quotes']) != 3 or set(raw_quotes) != set(VENUES):
                raise ValueError('Quote identity mismatch')
            sig = quote(raw_quotes['SIG'], captured)
            row = {'id': sid, 'exchange': raw['exchange'], 'at': captured, 'SIG': sig,
                   'aligned': False}
            try:
                row.update({v: quote(raw_quotes[v], captured) for v in VENUES[1:]})
                for clock in ('observed', 'source'):
                    stamps = [row[v][clock] for v in VENUES]
                    if max(stamps)-min(stamps) > MAX_ALIGNMENT:
                        raise ValueError('Quotes not aligned')
                row['aligned'] = True
            except (KeyError, ValueError, TypeError, InvalidOperation):
                counts['invalid_or_unaligned_references'] += 1
            if sid in shadows:
                at, shadow = shadows[sid]
                if shadow['exchange'] == row['exchange'] and 0 <= at-captured <= 5:
                    row['shadow'] = shadow
                else:
                    counts['shadow_identity_or_time_mismatch'] += 1
            rows.append(row)
        except (KeyError, ValueError, TypeError, InvalidOperation):
            counts['invalid_SIG_or_snapshot'] += 1
    rows.sort(key=lambda r: (r['at'], r['id']))
    counts['valid_SIG_scans'] = len(rows)
    counts['aligned_three_venue_scans'] = sum(r['aligned'] for r in rows)
    return rows, dict(counts)


def correlation(pairs):
    if len(pairs) < 3:
        return None
    mx = sum(x for x, y in pairs)/len(pairs)
    my = sum(y for x, y in pairs)/len(pairs)
    xx = sum((x-mx)**2 for x, y in pairs)
    yy = sum((y-my)**2 for x, y in pairs)
    if xx <= 1e-20 or yy <= 1e-20:
        return None
    return max(-1., min(1., sum((x-mx)*(y-my) for x, y in pairs)/math.sqrt(xx*yy)))


def predictive_summary(rows, feature, movement=False):
    selected = [r for r in rows if r.get(feature) is not None
                and (not movement or abs(r[feature])+1e-12 >= MOVE_MIN)]
    directional = [r for r in selected if abs(r[feature]) > 1e-12]
    nonflat = [r for r in directional if abs(r['sig_change']) > 1e-12]
    return {'samples': len(selected), 'races': len({r['exchange'] for r in selected}),
            'UTC_days': len({r['day'] for r in selected}),
            'correlation_with_future_SIG_change': correlation([(r[feature], r['sig_change']) for r in selected]),
            'directional_samples': len(directional),
            'flat_SIG_outcomes': len(directional)-len(nonflat),
            'follow_rate_including_flat': (sum(r[feature]*r['sig_change'] > 1e-12 for r in directional)/len(directional)
                                           if directional else None),
            'mean_signed_SIG_change': (sum((1 if r[feature] > 0 else -1)*r['sig_change'] for r in directional)/len(directional)
                                       if directional else None)}


def future_quote(rows, stamps, start, horizon, window):
    target = start['at']+horizon
    for index in range(bisect_left(stamps, target), len(rows)):
        row = rows[index]
        if row['at'] > target+window:
            break
        # A newly fetched old book must not count as a post-target observation.
        if min(row['SIG']['source'], row['SIG']['observed']) >= target:
            return row
    return None


def candidate(shadow, variant, start):
    """Validate prospective record against the exact original executable quote."""
    if shadow.get('experiment') != 'reference_bid_v1' or shadow.get('entry_only') is not True:
        raise ValueError('Unknown experiment')
    decisions = shadow['decisions']
    if len(decisions) != 3 or {r['variant'] for r in decisions} != set(VARIANTS):
        raise ValueError('Invalid experiment variants')
    record = next(r for r in decisions if r['variant'] == variant)
    c = record['candidate']
    if c is None:
        return None
    if record['reason'] != 'eligible' or c['action'] != 'buy' or c['side'] not in ('yes', 'no'):
        raise ValueError('Invalid candidate identity')
    qty, price, fee = D(c['quantity']), D(c['price']), D(shadow['settings']['cost_buffer_per_share'])
    side = c['side']
    sig = start['SIG']
    ask, depth = (sig['ask'], sig['ask_size']) if side == 'yes' else (1-sig['bid'], sig['bid_size'])
    if qty <= 0 or qty != int(qty) or price != ask or qty > depth or fee < 0:
        raise ValueError('Invalid executable size/price')
    if qty*(price+fee) > D(shadow['available']):
        raise ValueError('Budget mismatch')
    held = D(shadow['held'])
    if held and ((held < 0) != (side == 'no')):
        raise ValueError('Opposite inventory')
    if shadow.get('side_limits') is not None and qty > D(shadow['side_limits'][side]):
        raise ValueError('Exposure mismatch')
    bids = [start[v]['bid'] if side == 'yes' else 1-start[v]['ask'] for v in VENUES[1:]]
    sizes = [start[v]['bid_size'] if side == 'yes' else start[v]['ask_size'] for v in VENUES[1:]]
    settings = shadow['settings']
    if (qty > min(sizes) or min(sizes) < D(settings['min_reference_depth'])
            or qty > D(settings['max_shares_per_order'])):
        raise ValueError('Reference depth mismatch')
    reference = {'both_bids': min(bids), 'kalshi_bid': bids[0], 'polymarket_bid': bids[1]}[variant]
    edge = reference-price-fee
    if D(c['reference']) != reference or D(c['edge']) != edge or edge < D(settings['minimum_edge']):
        raise ValueError('Reference/edge mismatch')
    return {'side': side, 'price': price, 'quantity': qty, 'buffer': fee}


def shadow_summary(rows):
    counts = Counter(r['status'] for r in rows)
    measured = [r for r in rows if r['status'] == 'measured']
    shares = sum((r['quantity'] for r in measured), D(0))
    return {'statuses': dict(counts), 'measured': len(measured),
            'proposed_entries': sum(r.get('proposed', False) for r in rows),
            'races': len({r['exchange'] for r in measured}),
            'UTC_days': len({r['day'] for r in measured}),
            'hypothetical_pnl_per_share_after_buffers': str(sum((r['pnl'] for r in measured), D(0))/shares) if shares else None,
            'positive_fraction': sum(r['pnl'] > 0 for r in measured)/len(measured) if measured else None,
            'mean_followup_delay_seconds': sum(r['delay'] for r in measured)/len(measured) if measured else None}


def analyze(rows, since, now):
    by_race = defaultdict(list)
    for row in rows:
        by_race[row['exchange']].append(row)
    horizons = {}
    for horizon, window in HORIZONS.items():
        results, counts = [], Counter()
        variants = {v: [] for v in VARIANTS}
        for exchange, scans in by_race.items():
            stamps = [r['at'] for r in scans]
            previous, next_anchor = None, float('-inf')
            for start in scans:
                if not start['aligned']:
                    continue
                prior, previous = previous, start
                if start['at'] < since:
                    continue
                if start['at'] < next_anchor:
                    counts['overlap_excluded'] += 1
                    continue
                # Fixed per-race grid determined only by information at entry.
                # Reserve the whole window even if its endpoint is missing.
                next_anchor = start['at']+horizon+window
                counts['anchors'] += 1
                future = future_quote(scans, stamps, start, horizon, window)
                status = ('measured' if future else 'not_due' if now < start['at']+horizon
                          else 'awaiting_quote' if now <= start['at']+horizon+window else 'missed')
                counts[status] += 1
                day = datetime.fromtimestamp(start['at'], timezone.utc).date().isoformat()
                common = {'id': start['id'], 'exchange': exchange, 'day': day}
                if future:
                    sample = {**common, 'sig_change': future['SIG']['mid']-start['SIG']['mid'],
                              'delay': future['at']-start['at']-horizon}
                    for v in VENUES[1:]:
                        sample[v+'_gap'] = start[v]['mid']-start['SIG']['mid']
                        sample[v+'_move'] = (start[v]['mid']-prior[v]['mid'] if prior
                            and 0 < start['at']-prior['at'] <= 300
                            and start[v]['source'] > prior[v]['source'] else None)
                    k, p = sample['Kalshi_gap'], sample['Polymarket_gap']
                    sample['agreement_gap'] = (k+p)/2 if k*p > 0 else None
                    results.append(sample)
                for variant in VARIANTS:
                    result = {**common, 'status': 'no_prospective_record'}
                    if start.get('shadow', {}).get('status') == 'unavailable':
                        result['status'] = 'measurement_unavailable'
                    elif start.get('shadow'):
                        try:
                            c = candidate(start['shadow'], variant, start)
                            result['proposed'] = c is not None
                            result['status'] = 'no_candidate' if c is None else status
                            if c and future:
                                sig = future['SIG']
                                bid, depth = ((sig['bid'], sig['bid_size']) if c['side'] == 'yes'
                                              else (1-sig['ask'], sig['ask_size']))
                                if depth < c['quantity']:
                                    result['status'] = 'insufficient_exit_depth'
                                else:
                                    per_share = bid-c['price']-2*c['buffer']
                                    result.update(pnl=per_share*c['quantity'], per_share=per_share,
                                                  quantity=c['quantity'], delay=future['at']-start['at']-horizon)
                        except (KeyError, ValueError, TypeError, InvalidOperation, StopIteration):
                            result['status'] = 'invalid_prospective_record'
                    variants[variant].append(result)
        leadership = {}
        for label, feature, movement in (
                ('Kalshi_gap', 'Kalshi_gap', False), ('Polymarket_gap', 'Polymarket_gap', False),
                ('both_same_direction_gap', 'agreement_gap', False),
                ('Kalshi_recent_move', 'Kalshi_move', True), ('Polymarket_recent_move', 'Polymarket_move', True)):
            summary = predictive_summary(results, feature, movement)
            # Breakdowns reveal concentration; no IID confidence or winner claims.
            summary['by_day'] = {day: predictive_summary([r for r in results if r['day'] == day], feature, movement)
                                 for day in sorted({r['day'] for r in results})}
            summary['by_race'] = {race: predictive_summary([r for r in results if r['exchange'] == race], feature, movement)
                                  for race in sorted({r['exchange'] for r in results})}
            leadership[label] = summary
        paired = {}
        baseline_all = {r['id']: r for r in variants['both_bids']}
        baseline = {r['id']: r for r in variants['both_bids'] if r['status'] == 'measured'}
        for variant in VARIANTS[1:]:
            matches = [r for r in variants[variant] if r['status'] == 'measured' and r['id'] in baseline]
            paired[variant] = {'paired_measured': len(matches),
                'additional_proposals_vs_baseline': sum(r.get('proposed', False) and baseline_all[r['id']]['status'] == 'no_candidate' for r in variants[variant]),
                'mean_pnl_per_share_difference_vs_both_bids': str(sum((r['per_share']-baseline[r['id']]['per_share'] for r in matches), D(0))/len(matches)) if matches else None}
        horizons[str(horizon)] = {'window_seconds': window, 'coverage': dict(counts),
            'mean_followup_delay_seconds': sum(r['delay'] for r in results)/len(results) if results else None,
            'leadership': leadership, 'shadow': {v: shadow_summary(rs) for v, rs in variants.items()},
            'paired_shadow_comparison': paired}
    return horizons


def report(path, now=None, hours=48):
    now = time.time() if now is None else number(now)
    hours = number(hours)
    if not 0 < hours <= 720:
        raise ValueError('Hours must be greater than zero and at most 720')
    path = Path(path).resolve()
    base = {'as_of': now, 'hours': hours, 'status': 'No quote journal yet',
        'method': {'version': 1, 'max_quote_age_seconds': MAX_AGE,
                   'max_cross_venue_timestamp_skew_seconds': MAX_ALIGNMENT,
                   'movement_minimum': MOVE_MIN, 'movement_lookback_max_seconds': 300,
                   'sampling': 'Non-overlapping per-race horizon plus full observation window; scan phase only'},
        'limitations': [
            'Descriptive associations, not causality or evidence of profitable execution.',
            'Fetch/server timestamps are proxies; polling cannot establish sub-second market leadership.',
            'Sampling depends on current scan priorities, news gates and available quotes; missing outcomes are not zero.',
            'Correlated races/days and repeated comparisons preclude IID significance or declaring a winning variant.',
            'Shadow entries assume immediate full fills at displayed SIG asks, then full-size liquidation at the first valid follow-up top bid.',
            'No queue, latency, partial-fill, market-impact or independent portfolio simulation; real budgets/holdings constrain every variant.',
            'Each horizon is a separate hypothetical outcome after two configured buffers; do not sum horizons or treat as realized profit.']}
    if not path.exists():
        return base
    db = sqlite3.connect(path.as_uri()+'?mode=ro', uri=True)
    try:
        db.execute('BEGIN')
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='events'").fetchone():
            return base
        rows, counts = load_events(db, now-hours*3600, now)
        base.update(status='ok', events=counts, horizons=analyze(rows, now-hours*3600, now))
        return base
    finally:
        db.close()


def format_report(result):
    lines = ['REFERENCE LEADERSHIP / SHADOW ENTRIES — descriptive, not realized P&L']
    if result['status'] != 'ok':
        return '\n'.join(lines+[result['status']])
    events = result['events']
    lines.append(f"Aligned scan snapshots: {events.get('aligned_three_venue_scans', 0)}; prospective records: {events.get('shadow_decision_events', 0)}")
    def fmt(value, scale=1):
        return '—' if value is None else f'{float(value)*scale:.3f}'
    for horizon, item in result['horizons'].items():
        lines.extend(['', f'{int(horizon)//60} MINUTES — {item["coverage"]}',
                      'Predictor                    Samples  Races  Days   Corr.  Follow%'])
        for label, stats in item['leadership'].items():
            lines.append(f'{label:29} {stats["samples"]:6} {stats["races"]:6} {stats["UTC_days"]:5} '
                         f'{fmt(stats["correlation_with_future_SIG_change"]):>7} {fmt(stats["follow_rate_including_flat"],100):>8}')
        lines.append('Shadow rule       Measured   P&L/share(c)   Status counts')
        for label, stats in item['shadow'].items():
            lines.append(f'{label:18} {stats["measured"]:7} {fmt(stats["hypothetical_pnl_per_share_after_buffers"],100):>14}   {stats["statuses"]}')
    lines.extend(['', 'Follow% includes flat SIG outcomes as non-following; recent moves require at least 1 cent.',
                  'Missing observations are not zero returns. Shadow fills are assumptions, not orders.',
                  'Use --json for paired comparisons, day/race breakdowns, delays and limitations.'])
    return '\n'.join(lines)
