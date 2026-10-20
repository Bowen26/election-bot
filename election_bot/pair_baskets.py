"""Risk-free party-pair NO baskets inside SIG. Off unless pair_baskets.enabled.

For a two-party race with a Democratic contract (the existing pinned mapping)
and a Republican contract (a disabled mapping marked basket_leg), at most one
can resolve YES. Buying NO on both costs (1-bid_D) + (1-bid_R) per set and pays
at least 1, so the guaranteed edge is bid_D + bid_R - 1.

Accounting: the leg carries exposure_sign = -anchor sign, so a matched set nets
to zero directional exposure in the existing signed model. Shares bought for
baskets are tagged in basket_orders and are never sold by other exit routes.
Races where the bot holds YES on either contract are skipped (a NO buy there
would oppose inventory). The second leg is capped at break-even; an unmatched
remainder is repaired later at break-even or better, or left as a normal,
fully accounted position.
"""
import math
import time

from .clients import fingerprint, sig_contract_record
from .strategy import D

REASON = 'pair_basket'
SWAP = 'pair_swap'
DEFAULTS = dict(enabled=False, min_edge='0.02', depth_fraction='0.5', max_sets_per_order=2000,
                total_coins='10000', pairs_per_cycle=3, verify_seconds=900, races=None,
                swap_enabled=False)


def settings(config):
    raw = config.get('pair_baskets', {})
    if not isinstance(raw, dict) or set(raw)-set(DEFAULTS):
        raise ValueError('Invalid pair_baskets configuration')
    p = dict(DEFAULTS, **raw)
    for key in ('enabled', 'swap_enabled'):
        if type(p[key]) is not bool:
            raise ValueError('pair_baskets %s must be boolean' % key)
    for key in ('min_edge', 'depth_fraction', 'total_coins'):
        p[key] = D(p[key])
    if not D('.005') <= p['min_edge'] <= D('.5') or not 0 < p['depth_fraction'] <= 1 or p['total_coins'] <= 0:
        raise ValueError('Invalid pair_baskets min_edge, depth_fraction or total_coins')
    for key, low, high in (('max_sets_per_order', 1, 100000), ('pairs_per_cycle', 1, 20), ('verify_seconds', 60, 3600)):
        if type(p[key]) is not int or not low <= p[key] <= high:
            raise ValueError('pair_baskets %s must be an integer from %d to %d' % (key, low, high))
    if p['races'] is not None and (not isinstance(p['races'], list) or not all(isinstance(r, str) for r in p['races'])):
        raise ValueError('pair_baskets races must be a list of race_key strings')
    for pair in pairs(config, check=True):
        pass
    return p


def pairs(config, check=False):
    """(anchor, leg) for each disabled basket_leg mapping and its enabled signed anchor."""
    result = []
    for leg in config['markets']:
        if not leg.get('basket_leg'):
            continue
        anchors = [m for m in config['markets'] if m.get('race_key') == leg.get('race_key')
                   and m is not leg and m.get('enabled') and not m.get('basket_leg')]
        problem = None
        if leg.get('enabled'):
            problem = 'basket_leg mappings must stay disabled'
        elif len(anchors) != 1:
            problem = 'basket_leg needs exactly one enabled anchor in its race'
        elif anchors[0].get('exposure_mode', 'signed') != 'signed' or anchors[0].get('exposure_sign') not in (1, -1):
            problem = 'basket anchor must be a signed contract'
        elif leg.get('exposure_sign') != -anchors[0]['exposure_sign']:
            problem = 'basket_leg exposure_sign must be the opposite of its anchor'
        elif any(leg.get(k) != anchors[0].get(k) for k in ('office', 'state_code', 'region')):
            problem = 'basket_leg must share office, state and region with its anchor'
        elif not isinstance(leg.get('basket_sig_fingerprint'), str):
            problem = 'basket_leg requires a pinned basket_sig_fingerprint'
        if problem:
            if check:
                raise ValueError(problem + ': ' + str(leg.get('name')))
            continue
        result.append((anchors[0], leg))
    return result


def party(mapping):
    """Party named by the contract's YES side, from its signed orientation."""
    return 'Democratic Party' if mapping['exposure_sign'] == 1 else 'Republican Party'


def verify_pair(anchor_market, leg_market, anchor, leg):
    """SIG-side identity: both open Party Winner contracts, different parties, same race date."""
    details = []
    for market, mapping in ((anchor_market, anchor), (leg_market, leg)):
        if market.get('status') != 'open':
            raise ValueError('Basket contract is not open')
        exchanges = [x for x in market['exchanges'] if str(x['id']) == mapping['sig_exchange_id']]
        if str(market['id']) != mapping['sig_market_id'] or len(exchanges) != 1:
            raise ValueError('Basket market/exchange identity mismatch')
        d = market['resolution_tree']['contract_details']
        if d.get('resolutionType') != 'Party Winner' or d.get('winnerName') != party(mapping):
            raise ValueError('Basket contract is not the expected Party Winner contract')
        details.append(d)
    if fingerprint(sig_contract_record(leg_market, leg)) != leg['basket_sig_fingerprint']:
        raise ValueError('Basket leg SIG contract changed; rediscover before trading')
    if anchor_market.get('settlementDate') != leg_market.get('settlementDate'):
        raise ValueError('Basket contracts settle on different dates')
    return True


def plan(anchor_book, leg_book, policy, coins, fee=D(0)):
    """Sets, prices and order for one basket, or (None, reason). Both legs buy NO.

    coins bounds the pair's combined reservation, including the per-share cost buffer on each leg.
    """
    if not anchor_book.bids or not leg_book.bids:
        return None, 'no_yes_bid'
    (bid_a, size_a), (bid_l, size_l) = anchor_book.bids[0], leg_book.bids[0]
    edge = bid_a+bid_l-1
    if edge < policy['min_edge']:
        return None, 'edge_below_minimum'
    price_a, price_l = 1-bid_a, 1-bid_l
    cost = price_a+price_l
    sets = int(min(policy['depth_fraction']*min(size_a, size_l), D(policy['max_sets_per_order']),
                   max(D(0), D(coins))/(cost+2*D(fee)) if coins is not None else D(policy['max_sets_per_order'])))
    if sets < 1:
        return None, 'size_below_one_set'
    # Thinner book first: it is the leg most likely to disappear.
    legs = [('anchor', price_a, size_a), ('leg', price_l, size_l)]
    legs.sort(key=lambda item: item[2])
    return {'sets': sets, 'edge': edge, 'cost_per_set': cost,
            'order': [name for name, _, _ in legs], 'prices': {'anchor': price_a, 'leg': price_l}}, None


def second_leg_price(first_price, current_price):
    """Never pay more than break-even for the completing leg."""
    cap = 1-first_price
    return current_price if current_price <= cap else None


def initialize(db):
    with db:
        db.execute('CREATE TABLE IF NOT EXISTS basket_orders (key TEXT PRIMARY KEY, race TEXT NOT NULL, '
                   'exchange TEXT NOT NULL, role TEXT NOT NULL)')


def attributed(db, roles=('anchor', 'leg')):
    """Filled NO shares and cost per exchange bought for baskets, for the given roles."""
    result = {}
    for exchange, quantity, cost in db.execute(
            'SELECT b.exchange, SUM(CAST(e.quantity AS REAL)), SUM(CAST(e.quantity AS REAL)*CAST(e.price AS REAL)) '
            'FROM basket_orders b JOIN executions e ON e.key=b.key WHERE e.action=\'buy\' AND b.role IN (%s) '
            'GROUP BY b.exchange' % ','.join('?'*len(roles)), roles):
        result[exchange] = (D(str(int(quantity or 0))), D(str(cost or 0)))
    return result


def locked(db, exchange):
    """Basket and swap NO shares on this exchange that other routes must not sell."""
    return attributed(db, ('anchor', 'leg', 'swap_buy')).get(exchange, (D(0), D(0)))[0]


def swap_unmatched(db, anchor, leg):
    """(NO-Republican shares still owed for completed YES-Democratic swap sales, break-even cap) or None."""
    sold, proceeds = db.execute(
        'SELECT SUM(CAST(e.quantity AS REAL)), SUM(CAST(e.quantity AS REAL)*CAST(e.price AS REAL)) '
        'FROM basket_orders b JOIN executions e ON e.key=b.key '
        'WHERE b.role=\'swap_sell\' AND e.action=\'sell\' AND b.exchange=?', (anchor['sig_exchange_id'],)).fetchone()
    sold, proceeds = D(str(int(sold or 0))), D(str(proceeds or 0))
    bought = attributed(db, ('swap_buy',)).get(leg['sig_exchange_id'], (D(0), D(0)))[0]
    if sold <= bought:
        return None
    # Break-even: never pay more for NO-Republican than the average YES-Democratic sale price.
    return int(sold-bought), proceeds/sold


def swap_plan(anchor_book, leg_book, policy, held_yes, coins, fee=D(0)):
    """Sell YES-Democratic at its bid, then buy NO-Republican at 1-bid_R: the NO-basket edge."""
    if not anchor_book.bids or not leg_book.bids:
        return None, 'no_yes_bid'
    (bid_a, size_a), (bid_l, size_l) = anchor_book.bids[0], leg_book.bids[0]
    edge = bid_a+bid_l-1
    if edge < policy['min_edge']:
        return None, 'edge_below_minimum'
    price_l = 1-bid_l
    sets = int(min(policy['depth_fraction']*min(size_a, size_l), D(policy['max_sets_per_order']), D(held_yes),
                   max(D(0), D(coins))/(price_l+D(fee)) if coins is not None else D(policy['max_sets_per_order'])))
    if sets < 1:
        return None, 'size_below_one_set'
    return {'sets': sets, 'edge': edge, 'sell_price': bid_a, 'buy_price': price_l}, None


def unmatched(db, anchor, leg):
    """(role needing more shares, quantity, break-even price cap) or None."""
    got = attributed(db)
    qa, ca = got.get(anchor['sig_exchange_id'], (D(0), D(0)))
    ql, cl = got.get(leg['sig_exchange_id'], (D(0), D(0)))
    if qa == ql:
        return None
    if qa > ql:
        extra = qa-ql
        # Break-even on the unmatched anchor shares: their average price.
        return 'leg', int(extra), 1-ca/qa
    extra = ql-qa
    return 'anchor', int(extra), 1-cl/ql


def basket_cost(db):
    """Coins tied up in basket and swap NO shares (counts toward total_coins)."""
    return sum((cost for _, cost in attributed(db, ('anchor', 'leg', 'swap_buy')).values()), D(0))


def discover(config, sig, refs, races=None, limit=None, interval=3.0, sleep=time.sleep, clock=time.monotonic):
    """GET-only: build disabled basket_leg mappings for two-party races. Nothing is enabled or traded.

    The Republican contract must pass the same strict SIG/Kalshi/Polymarket identity
    check as configured contracts, and both reference books must be available now,
    so fair-value drawdown marks can always value the leg.
    """
    import re
    import urllib.parse
    from .catalog import verify_candidate
    from .clients import APIError, contract_record
    from .contract_review import event_markets, identity
    existing = {m.get('race_key') for m in config['markets'] if m.get('basket_leg')}
    anchors = {m['race_key']: m for m in config['markets']
               if m.get('enabled') and not m.get('basket_leg') and m.get('race_key')
               and m.get('exposure_mode', 'signed') == 'signed' and m.get('exposure_sign') == 1}
    by_race = {}
    for market in sig.markets():
        found = identity(market.get('title', ''))
        if found and market.get('status') == 'open':
            by_race.setdefault(found['race_key'], {})[found['party']] = market
    legs, skipped, last = [], {}, None
    targets = sorted(r for r in anchors if races is None or r in races)
    for race in targets[:limit] if limit else targets:
        parties = by_race.get(race, {})
        if race in existing:
            skipped[race] = 'leg_already_configured'
            continue
        if 'Independent Party' in parties:
            skipped[race] = 'three_party_race_not_supported'
            continue
        if 'Republican Party' not in parties or 'Democratic Party' not in parties:
            skipped[race] = 'missing_party_contract'
            continue
        anchor, listing = anchors[race], parties['Republican Party']
        try:
            # sig.market makes two SIG reads (market and rules); pace both.
            if last is not None:
                sleep(max(0, last+2*interval-clock()))
            last = clock()
            sm = sig.market(listing['id'])
            yes = [x for x in sm['exchanges'] if x['option'].upper() == 'YES']
            if len(yes) != 1:
                raise ValueError('Republican contract needs one YES exchange')
            kbase = refs.kalshi.request('/markets/'+urllib.parse.quote(anchor['kalshi_ticker'], safe=''))['market']
            ks = [m for m in event_markets(refs.kalshi, kbase['event_ticker'])
                  if re.search(r'\brepublican party\b', m.get('rules_primary', '').casefold())]
            event = refs.gamma.request('/events/slug/'+urllib.parse.quote(anchor['polymarket_event'], safe=''))
            ps = [m for m in event['markets'] if m['question'].startswith('Will the Republicans win the ')
                  or m['question'].startswith('Will the Republican Party win the ')]
            if len(ks) != 1 or len(ps) != 1:
                raise ValueError('Republican reference markets absent or ambiguous')
            leg = {k: v for k, v in anchor.items() if k not in ('contract_fingerprint', 'settlement_review')}
            leg.update(enabled=False, basket_leg=True, name=anchor['name'].replace('-democratic', '')+'-republican-basket-leg',
                       sig_market_id=str(sm['id']), sig_exchange_id=str(yes[0]['id']),
                       kalshi_ticker=ks[0]['ticker'], polymarket_market=ps[0]['slug'],
                       kalshi_yes_matches_sig_yes=True, polymarket_yes_matches_sig_yes=True,
                       exposure_sign=-anchor['exposure_sign'])
            metadata = refs.metadata(leg)
            record = contract_record(sm, leg, metadata)
            verify_candidate({'mapping': leg, 'contract': record}, 'Republican Party')
            refs.books(leg, metadata)  # References must be live for drawdown valuation.
            leg['contract_fingerprint'] = fingerprint(record)
            leg['basket_sig_fingerprint'] = fingerprint(sig_contract_record(sm, leg))
            legs.append(leg)
        except APIError as error:
            if error.status in (401, 403, 429):
                skipped[race] = 'stopped: '+str(error)[:80]
                break
            skipped[race] = str(error)[:120]
        except (ValueError, KeyError, TypeError, StopIteration, IndexError) as error:
            skipped[race] = str(error)[:120] or type(error).__name__
    return legs, skipped
