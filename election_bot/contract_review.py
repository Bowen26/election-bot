"""Read-only contract coverage and basis review. Never enables or routes trades."""
from collections import Counter, defaultdict
import json
import math
from pathlib import Path
import re
import sqlite3
import time
import urllib.parse

from .catalog import STATES, verify_candidate
from .clients import APIError, contract_record, fingerprint
from .ledger import inventory_from_executions
from .race_controls import RaceGroups, committed_by_race
from .strategy import D

PARTIES = ('Democratic Party', 'Republican Party', 'Independent Party')


def identity(title):
    """Titles are discovery hints only. Structured rules must verify identity."""
    match = re.fullmatch(r'Will the (Democratic Party|Republican Party|Independent Party) win the (.+)\?', title)
    if not match:
        return None
    party, label = match.groups()
    district = re.fullmatch(r'([A-Z]{2})-(\d{2}) House race', label)
    state = re.fullmatch(r'(.+) (Senate|Governor)', label)
    if district and district[1] in STATES.values():
        office, place = 'house', district[1]+'-'+district[2]
    elif state and state[1] in STATES:
        office, place = state[2].lower(), STATES[state[1]]
    else:
        return None
    return {'race_key': '2026:'+office+':'+place, 'party': party, 'discovery_only': True}


def assess(record, mapping, party):
    """Ordinal review score, not probability, loss estimate or an edge surcharge."""
    flags = []
    def flag(code, points, detail):
        flags.append({'code': code, 'review_points': points, 'detail': detail})
    try:
        verify_candidate({'mapping': mapping, 'contract': record}, party)
        structural = True
    except (KeyError, ValueError, TypeError, StopIteration, IndexError) as error:
        structural = False
        flag('identity_unverified', 50, str(error) or 'Incomplete identity evidence')
    sig = record.get('sig', {})
    details = sig.get('resolution_tree', {}).get('contract_details', {})
    kalshi = record.get('kalshi', {})
    primary = str(kalshi.get('rules_primary') or '').casefold()
    secondary = str(kalshi.get('rules_secondary') or '').casefold()
    poly = str(record.get('polymarket', {}).get('description') or '').casefold()
    in_office = 'sworn in' in primary or 'inaugurated' in primary
    election_winner = 'winner' in poly or 'candidate who wins' in poly
    if in_office and election_winner:
        flag('office_holder_vs_election_winner', 25,
             'Kalshi primary rule uses taking office; Polymarket description uses election winner. Secondary determination language requires review.')
    if not primary or not poly:
        flag('missing_external_rules', 40, 'One or both reference descriptions are unavailable.')
    if details.get('resolutionType') == 'Party Winner':
        flag('SIG_party_attribution_unspecified', 20,
             'Structured SIG fields identify a party winner but do not specify the nomination, caucus or party-switch conventions in the external text.')
    else:
        flag('SIG_resolution_unverified', 40, 'Party Winner structured resolution not verified.')
    if 'nominee' in poly and ('member' in primary or 'representative' in primary):
        flag('nominee_vs_party_membership', 15, 'Nominee-based attribution and party membership/representation may differ.')
    if 'caucus' in poly or 'caucus' in primary or 'caucus' in secondary:
        flag('caucus_attribution', 15, 'Caucusing language needs explicit cross-venue comparison.')
    if 'run-off' in poly or 'runoff' in poly:
        flag('runoff_scope', 10, 'Polymarket explicitly includes runoffs; SIG date/stage alone does not establish identical handling.')
    score = min(100, sum(f['review_points'] for f in flags))
    return {'structural_identity_verified': structural,
            'settlement_equivalence_verified': False,
            'review_score': score, 'review_priority': 'high' if score >= 40 else 'medium',
            'flags': flags, 'facts': {'SIG': details,
                'Kalshi_primary_uses_taking_office': in_office,
                'Kalshi_secondary_mentions_accelerated_determination': 'accelerated determination' in secondary,
                'Polymarket_mentions_media_calls': 'call the race' in poly or 'call this race' in poly,
                'Polymarket_mentions_certification': 'certification' in poly,
                'Polymarket_mentions_independents': 'independent' in poly},
            'record_fingerprint': fingerprint(record), 'additional_edge_required': None,
            'new_trading_authorized': False,
            'note': 'Heuristic triage, not a calibrated price adjustment. Structural identity is not settlement equivalence.'}


def race_budgets(config, journal):
    """Aggregate costs/losses/pending across every configured contract in a race."""
    mapping = RaceGroups(config).assignments
    result = {race: {'cap': str(config['limits']['per_market']), 'committed': None, 'remaining': None}
              for race in set(mapping.values())}
    journal = Path(journal).resolve()
    if not journal.exists():
        return {'status': 'journal_missing', 'races': result, 'note': 'Unknown holdings; no allowance inferred.'}
    db = sqlite3.connect(journal.as_uri()+'?mode=ro', uri=True); db.row_factory = sqlite3.Row
    try:
        db.execute('BEGIN')
        mapping = RaceGroups(config, db).assignments
        result = {race: {'cap': str(config['limits']['per_market']), 'committed': None, 'remaining': None}
                  for race in set(mapping.values())}
        holdings, realized = inventory_from_executions(db.execute('SELECT * FROM executions ORDER BY at,rowid'))
        pending = list(db.execute("SELECT market,amount FROM orders WHERE state='pending'"))
        if db.execute("SELECT COUNT(*) FROM orders o LEFT JOIN executions e ON e.key=o.key WHERE o.state='closed' AND e.key IS NULL").fetchone()[0]:
            return {'status': 'unimported_orders', 'races': result}
    except sqlite3.OperationalError:
        return {'status': 'journal_schema_unavailable', 'races': result}
    finally:
        db.close()
    exchanges = {ex for ex, side in holdings} | set(realized) | {str(r['market']) for r in pending}
    unknown = sorted(exchanges-set(mapping))
    if unknown:
        return {'status': 'unmapped_inventory_or_orders', 'unknown_exchanges': unknown, 'races': result}
    totals = committed_by_race(holdings, realized, pending, mapping)
    for race, row in result.items():
        used = totals[race]
        row.update(committed=str(used), remaining=str(max(D(0), D(row['cap'])-used)))
    return {'status': 'journal_snapshot_only', 'races': result,
            'note': 'One allowance per race across configured contracts, including disabled holdings and pending orders. '
                    'Not a live account reconciliation or trade authorization; other portfolio limits still apply.'}


def book_record(book, settings, venue):
    now = time.time()
    return {'at': now, 'source_at': book.source_at if math.isfinite(book.source_at) else None,
            'observed_at': book.observed_at if math.isfinite(book.observed_at) else None,
            'bid': [str(v) for v in book.bids[0]] if book.bids else None,
            'ask': [str(v) for v in book.asks[0]] if book.asks else None,
            'quality': book.diagnostic(settings['max_age_seconds'],
                         settings['max_reference_spread'] if venue != 'SIG' else None, venue, now)}


def event_markets(http, event_ticker):
    rows, cursor, seen, tickers = [], None, set(), set()
    for _ in range(100):
        params = {'event_ticker': event_ticker, 'limit': 100}
        if cursor:
            params['cursor'] = cursor
        page = http.request('/markets', params=params)
        for market in page['markets']:
            if market['event_ticker'] != event_ticker or market['ticker'] in tickers:
                raise ValueError('Kalshi event identity or pagination duplicate')
            rows.append(market); tickers.add(market['ticker'])
        cursor = page.get('cursor')
        if not cursor:
            return rows
        if not isinstance(cursor, str) or cursor in seen or not page['markets']:
            raise ValueError('Incomplete Kalshi event pagination')
        seen.add(cursor)
    raise ValueError('Kalshi event pagination limit exceeded')


def refresh_race(config, race, sig, refs):
    """Only GET methods. Discover actual sibling tickers, never synthesize one."""
    if sig.tid != config['tournament_id']:
        raise ValueError('Tournament identity mismatch')
    anchors = [m for m in config['markets'] if m.get('enabled') and m.get('race_key') == race]
    if len(anchors) != 1:
        raise ValueError('Choose one existing enabled race_key for discovery')
    anchor = anchors[0]
    catalog = sig.markets()
    siblings = [m for m in catalog if (identity(m['title']) or {}).get('race_key') == race]
    observed = time.time()
    kbase = refs.kalshi.request('/markets/'+urllib.parse.quote(anchor['kalshi_ticker'], safe=''))['market']
    if kbase['ticker'] != anchor['kalshi_ticker']:
        raise ValueError('Kalshi anchor identity mismatch')
    km = event_markets(refs.kalshi, kbase['event_ticker'])
    event = refs.gamma.request('/events/slug/'+urllib.parse.quote(anchor['polymarket_event'], safe=''))
    if event.get('slug') != anchor['polymarket_event']:
        raise ValueError('Polymarket event identity mismatch')
    results = []
    for market in siblings:
        found = identity(market['title']); party = found['party']
        entry = {'sig_market_id': str(market['id']), 'title': market['title'], 'party': party,
                 'race_key': race, 'observed_at': None, 'status': 'review_required', 'quotes': {},
                 'new_trading_authorized': False}
        results.append(entry)
        try:
            sm = sig.market(market['id'])
            ex = [x for x in sm['exchanges'] if x['option'].upper() == 'YES']
            if str(sm['id']) != str(market['id']) or sm['title'] != market['title'] or len(ex) != 1:
                raise ValueError('SIG market/exchange identity mismatch')
            exchange = str(ex[0]['id']);entry['exchange'] = exchange
            details = sm['resolution_tree']['contract_details']
            entry['SIG_details'] = details
            entry['observed_at'] = time.time()
            if details.get('winnerName') != party:
                raise ValueError('SIG winner identity mismatch')
            # SIG quotes are recorded even when external identity needs review.
            if sm.get('status') == 'open':
                entry['quotes']['SIG'] = book_record(sig.book(exchange), config['strategy'], 'SIG')
            if party == 'Independent Party':
                raise ValueError('Independent Party is not assumed to mean every independent candidate; individual attribution review required')
            adjective = party.removesuffix(' Party')
            # Use actual discovered contracts. The strict verifier below checks full rule identity.
            ks = [m for m in km if re.search(r'\b'+adjective.casefold()+r'(?: \(dfl\))? party\b', m.get('rules_primary','').casefold())]
            ps = [m for m in event['markets'] if m['question'].startswith(
                'Will the '+('Democrats' if adjective == 'Democratic' else 'Republicans')+' win the ')
                or m['question'].startswith('Will the '+party+' win the ')]
            if len(ks) != 1 or len(ps) != 1:
                raise ValueError('External party identity absent or ambiguous; candidate-only markets are not substitutes')
            mapping = {**anchor, 'enabled': False, 'name': race.replace(':','-')+'-'+adjective.lower(),
                'sig_market_id': str(sm['id']), 'sig_exchange_id': exchange,
                'kalshi_ticker': ks[0]['ticker'], 'polymarket_market': ps[0]['slug'],
                'kalshi_yes_matches_sig_yes': True, 'polymarket_yes_matches_sig_yes': True}
            # Do not imply that a Republican contract is a perfect inverse factor hedge.
            mapping.pop('contract_fingerprint', None);mapping.pop('exposure_sign', None)
            metadata = refs.metadata(mapping)
            record = contract_record(sm, mapping, metadata)
            entry.update(mapping=mapping, contract=record, observed_at=time.time(), basis=assess(record,mapping,party))
            entry['pin_matches_current_config'] = (fingerprint(record) == anchor.get('contract_fingerprint')
                if exchange == anchor['sig_exchange_id'] else None)
            if not entry['basis']['structural_identity_verified']:
                raise ValueError('Strict race/party identity failed; inspect basis flags')
            entry['status'] = 'identity_verified_basis_unresolved'
            kb, pb = refs.books(mapping, metadata)
            entry['quotes'].update(Kalshi=book_record(kb, config['strategy'], 'Kalshi'),
                                   Polymarket=book_record(pb, config['strategy'], 'Polymarket'))
        except (APIError, ValueError, KeyError, TypeError) as error:
            entry['reason'] = str(error)
    return {'version': 1, 'tournament_id': sig.tid, 'race_key': race,
            'started_at': observed, 'completed_at': time.time(), 'contracts': results,
            'note': 'GET-only review. No mappings enabled, orders placed or limits changed.'}


def report(config, runtime, race=None, now=None):
    now = time.time() if now is None else now
    runtime = Path(runtime)
    catalog_path = runtime/'catalog-review.json'
    audit = (json.loads(catalog_path.read_text()) if catalog_path.exists()
             else {'reviews': [], 'markets': []})
    by_id = {str(r['sig_market_id']): r for r in audit['reviews']}
    configured = {str(m['sig_market_id']): m for m in config['markets']}
    fresh = {}
    rejected = []
    for path in sorted((runtime/'contract-reviews').glob('*.json')):
        data = json.loads(path.read_text())
        if data.get('version') != 1 or data.get('tournament_id') != config['tournament_id']:
            rejected.append(path.name);continue
        for item in data['contracts']:
            if item.get('race_key') != data['race_key']:
                rejected.append(path.name);continue
            fresh[str(item['sig_market_id'])] = item
    if not catalog_path.exists() and not fresh:
        return {'status': 'No catalog evidence yet', 'rejected_refresh_files': rejected}
    grouped = defaultdict(list); excluded = []
    markets = {str(m['id']): m for m in audit['markets']}
    for mid, item in fresh.items():
        markets[mid] = {'id': mid, 'title': item['title']}
    for mid, market in markets.items():
        found = identity(market['title'])
        if not found:
            excluded.append({'id': mid, 'title': market['title']});continue
        if race is not None and found['race_key'] != race:
            continue
        item = {'sig_market_id': mid, 'title': market['title'], **found,
                'currently_enabled': configured.get(mid, {}).get('enabled', False),
                'evidence': 'title_only', 'status': 'rules_not_fetched', 'new_trading_authorized': False}
        if mid in by_id and 'contract' in by_id[mid]:
            saved = by_id[mid]
            item.update(evidence='historical_catalog', catalog_saved_at=audit.get('at'),
                        status='cached_rules_review', basis=assess(saved['contract'],saved['mapping'],found['party']))
            item['pin_matches_current_config'] = fingerprint(saved['contract']) == configured.get(mid, {}).get('contract_fingerprint') if mid in configured else None
        if mid in fresh:
            item.update(fresh[mid], evidence='race_refresh')
        item['new_trading_authorized'] = False
        grouped[found['race_key']].append(item)
    budgets = race_budgets(config, runtime/'live.sqlite3')
    races = []
    for key, contracts in sorted(grouped.items()):
        contracts.sort(key=lambda c: PARTIES.index(c['party']))
        views = []
        for item in contracts:
            quote = item.get('quotes', {}).get('SIG')
            if not quote:
                continue
            valid = (not quote['quality']['issues'] and
                     all(0 <= now-float(quote[t]) <= config['strategy']['max_age_seconds'] for t in ('at','source_at','observed_at')))
            external = item.get('quotes', {})
            comparable = item.get('basis', {}).get('structural_identity_verified', False)
            comparable = comparable and all(v in external for v in ('SIG', 'Kalshi', 'Polymarket'))
            if comparable:
                comparable = all(not external[v]['quality']['issues'] and
                    all(0 <= now-float(external[v][t]) <= config['strategy']['max_age_seconds']
                        for t in ('at','source_at','observed_at')) for v in external)
                if comparable:
                    comparable = all(max(external[v][t] for v in external)-min(external[v][t] for v in external) <= 5
                                     for t in ('observed_at','source_at'))
                    mids = [(D(external[v]['bid'][0])+D(external[v]['ask'][0]))/2 for v in ('Kalshi','Polymarket')]
                    comparable = comparable and abs(mids[0]-mids[1]) <= D(config['strategy'].get('max_reference_disagreement','.08'))
            for side in ('yes','no'):
                bid, ask = quote['bid'], quote['ask']
                reference = (min(D(external[v]['bid'][0]) if side == 'yes' else 1-D(external[v]['ask'][0])
                                 for v in ('Kalshi','Polymarket')) if comparable else None)
                entry_price = D(ask[0]) if side == 'yes' and ask else 1-D(bid[0]) if side == 'no' and bid else None
                views.append({'market_id': item['sig_market_id'], 'party_contract': item['party'], 'side': side,
                    'ask': ask[0] if side == 'yes' and ask else str(1-D(bid[0])) if side == 'no' and bid else None,
                    'ask_size': ask[1] if side == 'yes' and ask else bid[1] if side == 'no' and bid else None,
                    'quote_fresh_at_report': valid, 'quoted_at': quote['at'],
                    'references_fresh_aligned_agree': comparable,
                    'reference_bid_floor': str(reference) if reference is not None else None,
                    'diagnostic_gap_after_entry_buffer': str(reference-entry_price-D(config['strategy']['cost_buffer_per_share']))
                        if reference is not None and entry_price is not None else None,
                    'gap_is_trade_signal': False,
                    'meaning': ('Party wins' if side == 'yes' else 'Party does not win; includes all other outcomes under this contract')})
        races.append({'race_key': key, 'contracts': contracts, 'SIG_quote_views': views,
            'shared_race_budget': budgets['races'].get(key, {'cap': str(config['limits']['per_market']), 'remaining': None}),
            'relationship': 'Democratic NO and Republican YES are not verified equivalents; do not rank them as identical claims.',
            'new_trading_authorized': False})
    flags = Counter(f['code'] for r in races for c in r['contracts'] for f in c.get('basis',{}).get('flags',[]))
    return {'status': 'ok', 'as_of': now, 'catalog_saved_at': audit.get('at'), 'races': races,
        'summary': {'races':len(races),'party_contracts':dict(Counter(c['party'] for r in races for c in r['contracts'])),
                    'basis_flags':dict(flags)}, 'excluded_titles':excluded,'rejected_refresh_files':rejected,
        'budget_status':budgets['status'], 'budget_note':budgets.get('note'),
        'note': 'Read-only evidence review. Historical catalog save time is not proof every rule was fetched then. '
                'No calibrated basis surcharge or settlement equivalence is inferred. Quotes expire; no new trades are authorized.'}


def format_report(result):
    if result['status'] != 'ok':
        return result['status']
    lines=['CONTRACT / SETTLEMENT REVIEW — no new trading enabled',str(result['summary']),
           'Race                       Parties                              Shared budget remaining']
    for race in result['races']:
        parties=', '.join(c['party'].removesuffix(' Party') for c in race['contracts'])
        lines.append(f'{race["race_key"]:27} {parties:36} {race["shared_race_budget"].get("remaining")}')
        for view in race['SIG_quote_views']:
            lines.append(f'  {view["party_contract"]} {view["side"]}: ask {view["ask"]}, size {view["ask_size"]}, fresh={view["quote_fresh_at_report"]}')
    lines.extend(['Budget status: '+result['budget_status'], result['note'], 'Use --race and --json for rule flags and quote evidence.'])
    return '\n'.join(lines)
