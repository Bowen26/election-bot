"""Pre-election capital rotation: sell part of a nearly converged holding to fund a
clearly better entry that is blocked only by coins.

Before election night the bot can be fully invested while some holdings trade within a
cent or two of reference fair value and other races still offer much larger gaps. Per
share sold, rotation requires

    (sale bid / target ask) * target edge - (fair value - sale bid) >= min_gain

with the sale bid no more than max_giveup below fair value. Target edges use the
conservative entry measure (lower reference bid - ask - buffer); the give-up uses the
reference midpoint fair value. During the election-night window, night recycling does
this job instead, so rotation is off there.
"""
import math
import time

from .settlement_value import fair_value
from .strategy import D, Signal

REASON = 'capital_rotation'
DEFAULTS = dict(enabled=False, max_giveup='0.02', min_gain='0.03', pending_seconds=900,
                depth_fraction='0.5', max_shares=2000, min_sale_coins='25')


def settings(config):
    raw = config.get('capital_rotation', {})
    if not isinstance(raw, dict) or set(raw)-set(DEFAULTS):
        raise ValueError('Invalid capital_rotation configuration')
    p = dict(DEFAULTS, **raw)
    if type(p['enabled']) is not bool:
        raise ValueError('capital_rotation enabled must be boolean')
    for key in ('max_giveup', 'min_gain', 'depth_fraction', 'min_sale_coins'):
        p[key] = D(p[key])
    if not 0 <= p['max_giveup'] <= D('.05'):
        raise ValueError('capital_rotation max_giveup must be from 0 to 0.05')
    if not D('.005') <= p['min_gain'] <= D('.50'):
        raise ValueError('capital_rotation min_gain must be from 0.005 to 0.50')
    if not 0 <= p['min_sale_coins'] <= 1000:
        raise ValueError('capital_rotation min_sale_coins must be from 0 to 1000')
    if not 0 < p['depth_fraction'] <= 1:
        raise ValueError('capital_rotation depth_fraction must be above 0 and at most 1')
    if type(p['pending_seconds']) is not int or not 60 <= p['pending_seconds'] <= 3600:
        raise ValueError('capital_rotation pending_seconds must be an integer from 60 to 3600')
    if type(p['max_shares']) is not int or not 1 <= p['max_shares'] <= 100000:
        raise ValueError('capital_rotation max_shares must be an integer from 1 to 100000')
    return p


class Rotation:
    def __init__(self, config):
        self.policy = settings(config)
        self.strategy = config['strategy']
        self.pending = {}  # exchange -> entry blocked only by coins

    @property
    def enabled(self):
        return self.policy['enabled']

    def note(self, exchange, race, entry, need_coins, now=None):
        """Remember an entry that only coins block. entry is the would-be buy Signal."""
        now = time.time() if now is None else now
        if need_coins <= 0:
            return self.clear(exchange)
        new = exchange not in self.pending
        self.pending[exchange] = {'race': race, 'side': entry.side, 'ask': entry.price,
                                  'edge': entry.edge, 'need_coins': D(need_coins), 'at': now}
        return new

    def clear(self, exchange):
        return self.pending.pop(exchange, None) is not None

    def expire(self, now=None):
        now = time.time() if now is None else now
        self.pending = {ex: v for ex, v in self.pending.items()
                        if now-v['at'] <= self.policy['pending_seconds'] and v['need_coins'] > 0}

    def propose(self, book, refs, held, exchange, race, per_order, detail=None, now=None):
        """A sale of part of this holding that funds the best pending entry, or None."""
        p = self.policy
        detail = {} if detail is None else detail
        detail.update(route=REASON, status='blocked')
        if not p['enabled'] or not held:
            detail['reason'] = 'disabled_or_no_position'
            return None
        self.expire(now)
        options = [(ex, v) for ex, v in self.pending.items() if ex != exchange and v['race'] != race]
        if not options:
            detail['reason'] = 'no_pending_entry'
            return None
        side = 'yes' if held > 0 else 'no'
        book.check(self.strategy['max_age_seconds'], venue='SIG')
        own = book if side == 'yes' else book.complement()
        if not own.bids:
            detail['reason'] = 'no_sig_bid'
            return None
        bid, depth = own.bids[0]
        try:
            fair = fair_value(refs, side, self.strategy)
        except ValueError as error:
            detail.update(reason='fair_value_unavailable', error=str(error))
            return None
        giveup = fair-bid
        target_ex, target = max(options, key=lambda item: item[1]['edge']/item[1]['ask'])
        gain = bid/target['ask']*target['edge']-giveup
        detail.update(target_exchange=target_ex, target_ask=str(target['ask']), target_edge=str(target['edge']),
                      sig_bid=str(bid), fair_value=str(fair), giveup_per_share=str(giveup),
                      net_gain_per_share=str(gain), need_coins=str(target['need_coins']))
        if giveup > p['max_giveup']:
            detail['reason'] = 'giveup_above_maximum'
            return None
        if gain < p['min_gain']:
            detail['reason'] = 'gain_below_minimum'
            return None
        needed = math.ceil(target['need_coins']/bid)  # Round up; Decimal // truncates.
        quantity = int(min(D(abs(held)), D(needed), p['depth_fraction']*depth, D(p['max_shares']),
                           D(self.strategy['max_shares_per_order']), D(per_order)/bid))
        if quantity < 1:
            detail['reason'] = 'size_below_one_share'
            return None
        if quantity*bid < p['min_sale_coins']:
            # Tiny sales add orders (and failure points) without moving meaningful capital.
            detail.update(reason='sale_below_minimum', quantity=quantity)
            return None
        detail.update(status='eligible', reason=REASON, quantity=quantity)
        return Signal(side, bid, quantity, fair, -giveup, 'sell', REASON)

    def sold(self, proceeds):
        """Sale proceeds add to the shared cash pool: every pending need shrinks by them."""
        for entry in self.pending.values():
            entry['need_coins'] -= D(proceeds)
        self.pending = {ex: v for ex, v in self.pending.items() if v['need_coins'] > 0}
