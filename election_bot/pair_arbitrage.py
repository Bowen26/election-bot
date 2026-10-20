"""Read-only scan for party-pair mispricing inside SIG; never places orders.

SIG lists each race as separate binary markets, one per party ("Will the
Democratic Party win X?", "Will the Republican Party win X?", sometimes an
Independent one). At most one listed party wins, so for a race with n listed
parties:

* NO basket: buy NO on every party. Cost n - sum(YES bids); it pays at least
  n - 1 whatever happens. Guaranteed edge = sum(YES bids) - 1. Risk-free apart
  from execution (both legs must fill).
* YES basket: buy YES on every party. Cost sum(YES asks); pays 1 unless an
  unlisted candidate wins. Edge = 1 - sum(YES asks). Not risk-free.

The platform charges no fees. Reads are paced (default one SIG read every 2.5 s)
so the scan can run beside the live bot within the 100-reads-per-minute limit.
"""
from collections import defaultdict
import re
import time

from .clients import APIError
from .strategy import D

TITLE = re.compile(r'^Will the (Democratic|Republican|Independent) Party win (?:the )?(.+)\?$')


def groups(markets):
    """Race -> {party: (market id, exchange id)} for open single-exchange party markets."""
    races = defaultdict(dict)
    for m in markets:
        match = TITLE.match(m.get('title', ''))
        if (not match or m.get('status') != 'open' or m.get('isComposite') or m.get('isMultiOutcome')
                or len(m.get('exchanges', [])) != 1):
            continue
        races[match.group(2)][match.group(1)] = (str(m['id']), str(m['exchanges'][0]['id']))
    return {race: parties for race, parties in races.items() if len(parties) >= 2}


def evaluate(books):
    """books: {party: Book (YES orientation)}. Returns basket economics, or a reason."""
    n = len(books)
    if any(not b.bids or not b.asks for b in books.values()):
        return {'status': 'one_sided_book'}
    bids = {p: b.bids[0] for p, b in books.items()}
    asks = {p: b.asks[0] for p, b in books.items()}
    no_edge = sum((price for price, _ in bids.values()), D(0))-1
    yes_edge = 1-sum((price for price, _ in asks.values()), D(0))
    return {'status': 'ok', 'parties': n,
            'yes_bids': {p: str(v[0]) for p, v in bids.items()},
            'yes_asks': {p: str(v[0]) for p, v in asks.items()},
            'no_basket': {'guaranteed_edge_per_set': str(no_edge), 'cost_per_set': str(n-(no_edge+1)),
                          'sets_available': str(min(size for _, size in bids.values()))},
            'yes_basket': {'edge_per_set_if_a_listed_party_wins': str(yes_edge),
                           'cost_per_set': str(1-yes_edge),
                           'sets_available': str(min(size for _, size in asks.values()))}}


def scan(sig, interval=2.5, limit=None, sleep=time.sleep, clock=time.monotonic):
    """Read every party book with pacing. Returns per-race results."""
    found = groups(sig.markets())
    races = sorted(found.items())[:limit] if limit else sorted(found.items())
    results, last = [], None
    for race, parties in races:
        books, read_at, error = {}, [], None
        for party, (_, exchange) in sorted(parties.items()):
            if last is not None:
                sleep(max(0, last+interval-clock()))
            last = clock()
            try:
                books[party] = sig.book(exchange)
                read_at.append(time.time())
            except APIError as exc:
                # A rate limit or outage ends the scan; partial results are still reported.
                results.append({'race': race, 'status': 'api_error_scan_stopped', 'reason': str(exc)[:120]})
                return results
            except (ValueError, KeyError) as exc:
                error = str(exc)[:120]
                break
        if error:
            results.append({'race': race, 'status': 'read_error', 'reason': error})
            continue
        result = evaluate(books)
        result.update(race=race, exchanges={p: x for p, (_, x) in parties.items()},
                      read_spread_seconds=round(max(read_at)-min(read_at), 2))
        results.append(result)
    return results


def report(results, threshold='0.01'):
    threshold = D(threshold)
    ok = [r for r in results if r['status'] == 'ok']
    no = sorted((r for r in ok if D(r['no_basket']['guaranteed_edge_per_set']) >= threshold),
                key=lambda r: -D(r['no_basket']['guaranteed_edge_per_set']))
    yes = sorted((r for r in ok if D(r['yes_basket']['edge_per_set_if_a_listed_party_wins']) >= threshold),
                 key=lambda r: -D(r['yes_basket']['edge_per_set_if_a_listed_party_wins']))
    return {'status': 'ok', 'races_scanned': len(results), 'races_priced': len(ok), 'threshold': str(threshold),
            'no_basket_opportunities': no, 'yes_basket_opportunities': yes,
            'errors': [r for r in results if r['status'] != 'ok'],
            'note': 'Quotes are read seconds apart; both legs must fill. Read-only: nothing was traded.'}


def format_report(r):
    lines = ['SIG PARTY-PAIR SCAN — read-only; no orders',
             f"{r['races_priced']} of {r['races_scanned']} races priced; threshold {float(D(r['threshold']))*100:.1f}c per set"]
    lines.append(f"Risk-free NO baskets (sum of YES bids > 1): {len(r['no_basket_opportunities'])}")
    for x in r['no_basket_opportunities'][:15]:
        b = x['no_basket']
        lines.append(f"  {x['race']:32} edge {float(D(b['guaranteed_edge_per_set']))*100:+.1f}c/set  "
                     f"sets {float(D(b['sets_available'])):,.0f}  bids {x['yes_bids']}")
    lines.append(f"YES baskets (sum of YES asks < 1; loses if an unlisted candidate wins): {len(r['yes_basket_opportunities'])}")
    for x in r['yes_basket_opportunities'][:15]:
        b = x['yes_basket']
        lines.append(f"  {x['race']:32} edge {float(D(b['edge_per_set_if_a_listed_party_wins']))*100:+.1f}c/set  "
                     f"sets {float(D(b['sets_available'])):,.0f}  asks {x['yes_asks']}")
    if r['errors']:
        lines.append(f"{len(r['errors'])} races could not be read; see --json.")
    lines.append(r['note'])
    return '\n'.join(lines)
