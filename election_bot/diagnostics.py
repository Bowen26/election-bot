"""Aggregate recorded quote and exit diagnostics; no broker access or writes."""
from collections import Counter, defaultdict
import math

from .strategy import D


def quote_summary(events, errors):
    groups = {}
    for event in events:
        for book in event.get('books', []):
            if not book.get('issues'):
                continue
            key = (event.get('phase', 'unknown'), book.get('venue', 'unknown'))
            group = groups.setdefault(key, {'failures': 0, 'issues': Counter(), 'source_ages': [], 'local_ages': []})
            group['failures'] += 1
            group['issues'].update(book['issues'])
            for kind in ('source', 'local'):
                age = book.get(kind+'_age_seconds')
                if type(age) in (int, float) and math.isfinite(age):
                    group[kind+'_ages'].append(age)
    rows = []
    for (phase, venue), group in sorted(groups.items()):
        row = {'phase': phase, 'venue': venue, 'failed_checks': group['failures'], 'issues': dict(group['issues'])}
        for kind in ('source', 'local'):
            ages = group[kind+'_ages']
            row[kind+'_age_seconds'] = {'min': min(ages) if ages else None,
                'max': max(ages) if ages else None, 'latest': ages[-1] if ages else None}
        rows.append(row)
    error_counts = Counter((e.get('phase', 'unknown'), e.get('venue', 'unknown'), str(e.get('status')))
                           for e in errors)
    return {'by_venue_and_phase': rows,
        'request_errors': [dict(phase=phase, venue=venue, http_status=status, count=count)
                          for (phase, venue, status), count in sorted(error_counts.items())],
        'latest_failures': events[-5:],
        'note': 'Failures recorded after this upgrade only, not a failure rate or unique missed opportunities. '
                'Multiple venues may fail one check. Performance-only observations and final preflight are '
                'separate phases. Local age is time since the Book object was built; source age uses each '
                "venue's timestamp basis. An old source timestamp with recent retrieval does not prove a dead "
                'feed: it may describe an unchanged book. Freshness guards remain enforced.'}


def exit_summary(events):
    phases = {}
    latest = {}
    for event in events:
        phase = event.get('phase', 'unknown')
        group = phases.setdefault(phase, {'checks': 0, 'statuses': Counter(), 'reasons': Counter(),
                                         'route_blockers': Counter(), 'by_market': defaultdict(Counter)})
        reason = event['reason']
        group['checks'] += 1
        group['statuses'][event['status']] += 1
        group['reasons'][reason] += 1
        group['by_market'][event['exchange']][reason] += 1
        routes = event.get('routes', {})
        if routes.get('overpriced_exit', {}).get('passes') is False:
            group['route_blockers']['overpriced_exit:bid_below_required'] += 1
        convergence = routes.get('convergence_take_profit', {})
        if convergence.get('price_converged') is False:
            group['route_blockers']['convergence_take_profit:price_not_converged'] += 1
        if 'net_profit_per_share_at_average_cost' in convergence:
            if D(convergence['net_profit_per_share_at_average_cost']) < D(convergence['minimum_profit_per_share']):
                group['route_blockers']['convergence_take_profit:profit_below_minimum'] += 1
        if convergence.get('fifo_profit_passed') is False:
            group['route_blockers']['convergence_take_profit:fifo_profit_below_minimum'] += 1
        latest[(event['exchange'], phase)] = event
    return {'by_phase': {phase: {'checks': value['checks'], 'statuses': dict(value['statuses']),
                'reasons': dict(value['reasons']), 'route_blockers': dict(value['route_blockers']),
                'by_market': {market: dict(reasons) for market, reasons in sorted(value['by_market'].items())}}
                for phase, value in sorted(phases.items())},
        'latest_checks': sorted(latest.values(), key=lambda e: e['at'], reverse=True)[:20],
        'note': 'Owned positions only, recorded after this upgrade. Scan, preflight and pre-submit counts '
                'are separate; they are repeated evaluations, not trades. Eligible means a proposed exit, '
                'not a fill. Not-evaluated means a guard prevented the price decision. The two exit routes '
                'are alternatives; both must fail to block on price. Profit numbers include configured '
                'buffers rather than verified fees. Historical snapshots may no longer be executable.'}
