"""Gap radar: screen every enabled race at once and check the most valuable first.

One bulk SIG price read (up to 100 exchanges per read) and the bulk public Kalshi and
Polymarket reads used for election-night call detection give a top-of-book view of
every race. Each race gets a screening value, the coins one order could earn under the
bot's own rules, for the best of:

- entry: lower reference bid - SIG ask - buffer, with the entry rule's reference depth;
- election-night entry, inside the window: both references call the side and the SIG
  ask is at least min_edge below their average midpoint;
- exit: bot-owned, non-basket shares whose SIG bid exceeds the higher reference ask by
  the buffer plus exit_edge (a race called against a holding shows up here);
- funding: a holding whose sale could fund a pending coin-blocked buy under the
  capital-rotation rule (or, inside the window, the recycling rule).

Each threshold is relaxed by margin, because the quotes are cached and carry no SIG
depth. The max_flags most valuable races go to the front of the priority lane, best
first, for one check each until the next run. A flag only schedules a check: the visit
re-reads full books and applies every ordinary rule. Disabled by default.
"""
import math

from .strategy import D

DEFAULTS = dict(enabled=False, seconds=60, night_seconds=20, max_flags=8, margin='0.01',
                min_value='5', max_age_seconds=120)


def settings(config):
    raw = config.get('gap_radar', {})
    if not isinstance(raw, dict) or set(raw)-set(DEFAULTS):
        raise ValueError('Invalid gap_radar configuration')
    p = dict(DEFAULTS, **raw)
    if type(p['enabled']) is not bool:
        raise ValueError('gap_radar enabled must be boolean')
    for key, low, high in (('seconds', 10, 3600), ('night_seconds', 5, 600), ('max_flags', 1, 40),
                           ('max_age_seconds', 15, 600)):
        if type(p[key]) is not int or not low <= p[key] <= high:
            raise ValueError('gap_radar %s must be an integer from %d to %d' % (key, low, high))
    p['margin'], p['min_value'] = D(p['margin']), D(p['min_value'])
    if not 0 <= p['margin'] <= D('.05'):
        raise ValueError('gap_radar margin must be from 0 to 0.05')
    if not 0 <= p['min_value'] <= 1000:
        raise ValueError('gap_radar min_value must be from 0 to 1000 coins')
    return p


class Rules:
    """One run's thresholds, taken from the bot's own configuration."""
    def __init__(self, policy, config, night=None, funding=None):
        self.strategy = config['strategy']
        self.margin, self.max_age = policy['margin'], policy['max_age_seconds']
        self.buffer = D(self.strategy['cost_buffer_per_share'])
        self.minimum_edge = D(self.strategy['minimum_edge'])
        self.min_depth = D(self.strategy['min_reference_depth'])
        self.max_shares = D(self.strategy['max_shares_per_order'])
        self.exit_edge = D(config['execution']['exit_edge'])
        self.per_order = D(config['limits']['per_order'])
        self.night = night      # ElectionNight while its window is open, else None
        self.funding = funding  # {'targets', 'max_giveup', 'min_gain', 'called_only'} or None


def usable(refs, rules):
    """Both reference books fresh enough for detection, two-sided, tight and agreeing."""
    if len(refs) != 2:
        return False
    try:
        for venue, book in zip(('Kalshi', 'Polymarket'), refs):
            book.check(rules.max_age, rules.strategy['max_reference_spread'], venue=venue)
    except ValueError:
        return False
    mids = [(b.bids[0][0]+b.asks[0][0])/2 for b in refs]
    return abs(mids[0]-mids[1]) <= D(rules.strategy['max_reference_disagreement'])


def outcome(quote, side):
    """(bid, ask) for one outcome from a bulk SIG YES quote; a missing level is None."""
    bid, ask = (None if v is None else D(str(v)) for v in quote)
    bid, ask = (v if v is not None and 0 < v < 1 else None for v in (bid, ask))
    if side == 'yes':
        return bid, ask
    return (None if ask is None else 1-ask), (None if bid is None else 1-bid)


def screen(quote, refs, held, basket, room, headroom, rules, race):
    """The most valuable screening candidate for one race, or None.

    quote: (bid, ask) for SIG YES from the bulk read. refs: [Kalshi, Polymarket] books
    oriented to SIG YES. held: signed bot-owned shares (negative is NO). basket: shares
    locked in pair baskets. room: race coins left under per_market. headroom: per-side
    net-share room for buys, or None when exposure caps are off.
    """
    if not usable(refs, rules):
        return None
    best = None

    def consider(route, side, edge, shares):
        nonlocal best
        shares = D(int(max(D(0), shares)))
        value = edge*shares
        if value > 0 and (best is None or value > best['value']):
            best = {'route': route, 'side': side, 'edge': edge, 'shares': int(shares), 'value': value}

    owned = 'yes' if held > 0 else 'no' if held < 0 else None
    called = rules.night.called_side(refs, rules.max_age) if rules.night is not None else None
    for side in ('yes', 'no'):
        _, ask = outcome(quote, side)
        if (owned is not None and side != owned) or ask is None or room <= 0:
            continue  # Never a buy against held inventory.
        aligned = refs if side == 'yes' else [r.complement() for r in refs]
        cap = min(room, rules.per_order)/(ask+rules.buffer)
        limit = cap if headroom is None or headroom.get(side) is None else min(cap, D(headroom[side]))
        edge = min(b.bids[0][0] for b in aligned)-ask-rules.buffer
        depth = min(b.bids[0][1] for b in aligned)
        if edge >= rules.minimum_edge-rules.margin and depth >= rules.min_depth:
            consider('entry', side, edge, min(limit, depth, rules.max_shares))
        if called == side:
            p = rules.night.policy
            fair = sum((b.bids[0][0]+b.asks[0][0])/2 for b in aligned)/2
            if ask <= p['max_price'] and fair-ask >= p['min_edge']-rules.margin:
                consider('night_entry', side, fair-ask,
                         min(cap if p['ignore_exposure_caps'] else limit, D(p['max_shares']), rules.max_shares))
    if owned is None:
        return best
    free = abs(held)-basket
    bid, _ = outcome(quote, owned)
    if free < 1 or bid is None:
        return best
    aligned = refs if owned == 'yes' else [r.complement() for r in refs]
    size = min(free, rules.max_shares, rules.per_order/bid)
    edge = bid-max(b.asks[0][0] for b in aligned)-rules.buffer
    ask_depth = min(b.asks[0][1] for b in aligned)
    if edge >= rules.exit_edge-rules.margin and ask_depth >= rules.min_depth:
        consider('exit', owned, edge, min(size, ask_depth))
    f = rules.funding
    options = [t for t in (f['targets'] if f else []) if t['race'] != race]
    if options and (not f['called_only'] or called == owned):
        target = max(options, key=lambda t: t['edge']/t['ask'])
        giveup = sum((b.bids[0][0]+b.asks[0][0])/2 for b in aligned)/2-bid
        gain = bid/target['ask']*target['edge']-giveup
        if giveup <= f['max_giveup']+rules.margin and gain >= f['min_gain']-rules.margin:
            consider('funding', owned, gain, min(size, D(math.ceil(target['need_coins']/bid))))
    return best


def rank(candidates, policy):
    """[(name, candidate)] -> the max_flags most valuable at or above min_value, best first."""
    valued = [(name, c) for name, c in candidates
              if c is not None and c['value'] > 0 and c['value'] >= policy['min_value']]
    return sorted(valued, key=lambda item: (-item[1]['value'], item[0]))[:policy['max_flags']]


class GapRadar:
    def __init__(self, config):
        self.policy = settings(config)
        self.last_run = 0.

    def period(self, night):
        return self.policy['night_seconds'] if night else self.policy['seconds']

    def due(self, now, night):
        return self.policy['enabled'] and now-self.last_run >= self.period(night)

    def ttl(self, night):
        """A flag outlives a slow cycle; the next run replaces it either way."""
        return 2*self.period(night)+30
