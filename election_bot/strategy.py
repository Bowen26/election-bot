"""Pure price/depth checks shared by live execution, paper trading and tests."""
from dataclasses import dataclass
from decimal import Decimal, ROUND_FLOOR
import math
import time


def D(value):
    number = Decimal(str(value))
    if not number.is_finite():
        raise ValueError("Non-finite number")
    return number


@dataclass
class Book:
    bids: list
    asks: list
    observed_at: float
    source_at: float
    venue: str = 'unknown'
    timestamp_basis: str = 'unspecified'

    @classmethod
    def make(cls, bids, asks, source_at=None, venue='unknown', timestamp_basis='unspecified'):
        def levels(rows, reverse):
            merged = {}
            for price, size in rows:
                price, size = D(price), D(size)
                if not (0 < price < 1) or size < 0:
                    raise ValueError("Invalid order book level")
                if size:
                    merged[price] = merged.get(price, D(0)) + size
            return sorted(merged.items(), reverse=reverse)
        now = time.time()
        return cls(levels(bids, True), levels(asks, False), now,
                   now if source_at is None else float(source_at), venue, timestamp_basis)

    def complement(self):
        return Book([(1-p, q) for p, q in self.asks],
                    [(1-p, q) for p, q in self.bids],
                    self.observed_at, self.source_at, self.venue, self.timestamp_basis)

    def diagnostic(self, max_age, max_spread=None, venue=None, now=None):
        now = time.time() if now is None else now
        result = {'venue': venue or self.venue, 'timestamp_basis': self.timestamp_basis,
                  'max_age_seconds': max_age, 'issues': []}
        for name, stamp in (('local', self.observed_at), ('source', self.source_at)):
            if not math.isfinite(stamp):
                result[name+'_age_seconds'] = None
                result['issues'].append(name+'_timestamp_invalid')
                continue
            result[name+'_age_seconds'] = round(now-stamp, 3)
            if now-stamp > max_age:
                result['issues'].append(name+'_stale')
            elif stamp > now+5:
                result['issues'].append(name+'_future')
        if not self.bids or not self.asks:
            result['issues'].append('two_sided_book_required')
        else:
            spread = self.asks[0][0] - self.bids[0][0]
            result['spread'] = str(spread)
            if spread <= 0:
                result['issues'].append('locked_or_crossed')
            if max_spread is not None and spread > D(max_spread):
                result['issues'].append('reference_spread_too_wide')
        return result

    def check(self, max_age, max_spread=None, venue=None):
        detail = self.diagnostic(max_age, max_spread, venue)
        if not detail['issues']:
            return
        first = detail['issues'][0]
        if first.endswith('_invalid'):
            reason = 'Invalid book timestamp'
        elif first.endswith(('_stale', '_future')):
            reason = 'Stale or future-dated book'
        else:
            reason = {'two_sided_book_required': 'Two-sided book required',
                      'locked_or_crossed': 'Locked or crossed book',
                      'reference_spread_too_wide': 'Reference spread too wide'}[first]
        raise BookValidationError(reason, detail)


class BookValidationError(ValueError):
    def __init__(self, reason, detail):
        super().__init__(reason + ' [' + detail['venue'] + ']')
        self.detail = detail


@dataclass
class Signal:
    side: str
    price: Decimal
    quantity: int
    reference: Decimal
    edge: Decimal
    action: str = 'buy'
    reason: str = 'entry_gap'


def choose(sig, references, settings, available, diagnostics=None, side_limits=None):
    """Buy only when both reference bids support a conservative price gap.

    Reference bids are evidence, not a guaranteed probability or hedge.
    available is the smallest remaining cash/budget allowance.
    """
    if len(references) != 2:
        raise ValueError("Both Kalshi and Polymarket books are required")
    sig.check(settings['max_age_seconds'], venue='SIG')
    for venue, book in zip(('Kalshi', 'Polymarket'), references):
        book.check(settings['max_age_seconds'], settings['max_reference_spread'], venue=venue)
    mids = [(b.bids[0][0] + b.asks[0][0]) / 2 for b in references]
    if abs(mids[0] - mids[1]) > D(settings['max_reference_disagreement']):
        raise ValueError("Reference venues disagree too much")
    tick = D('0.005')
    fee = D(settings['cost_buffer_per_share'])
    threshold = D(settings['minimum_edge'])
    candidates = []
    for side in ('yes', 'no'):
        target = sig if side == 'yes' else sig.complement()
        refs = references if side == 'yes' else [b.complement() for b in references]
        check = {'side': side, 'available': str(available), 'ask': str(target.asks[0][0]),
                 'reference_bid': str(min(b.bids[0][0] for b in refs))}
        if diagnostics is not None:
            diagnostics.append(check)
        if any(b.bids[0][1] < D(settings['min_reference_depth']) for b in refs):
            check['reason'] = 'reference_depth'
            continue
        reference = min(b.bids[0][0] for b in refs)
        ceiling = ((reference - threshold - fee) / tick).to_integral_value(
            rounding=ROUND_FLOOR) * tick
        executable = [(p, q) for p, q in target.asks if p <= ceiling]
        if not executable:
            check['reason'] = 'price_gap'
            continue
        # Price only at the best available level; do not sweep a thin book.
        price, depth = executable[0]
        if price % tick != 0 or not (tick <= price <= 1-tick):
            raise ValueError("SIG price is not a valid limit-order tick")
        share_limit = (D(side_limits[side]) if side_limits is not None
                       else D(settings['max_shares_per_order']))
        if side_limits is not None:
            check['exposure_headroom'] = str(share_limit)
        quantity = int(min(share_limit, D(settings['max_shares_per_order']), depth,
                           min(b.bids[0][1] for b in refs),
                           D(available) / (price + fee)))
        if quantity > 0:
            check['reason'] = 'eligible'
            candidates.append(Signal(side, price, quantity, reference,
                                     reference-price-fee))
        else:
            check['reason'] = 'exposure_limit' if share_limit < 1 else 'budget_or_target_depth'
    return max(candidates, key=lambda s: s.edge) if candidates else None


def choose_exit(sig, references, settings, execution, held, cost, per_order, diagnostics=None, quantity_cap=None):
    """Sell owned shares when overpriced, or when a profitable gap has converged."""
    detail = diagnostics if diagnostics is not None else {}
    detail.update(status='not_applicable', reason='no_position')
    if not held:
        return None
    # Apply the same freshness, spread and agreement checks as entries.
    choose(sig, references, settings, 0)
    side = 'yes' if held > 0 else 'no'
    target = sig if held > 0 else sig.complement()
    refs = references if held > 0 else [r.complement() for r in references]
    price, depth = target.bids[0]
    detail.update(status='blocked', side=side, held=str(abs(D(held))), sig_bid=str(price),
                  sig_bid_depth=str(depth), min_reference_ask_depth=str(min(r.asks[0][1] for r in refs)),
                  required_reference_depth=str(settings['min_reference_depth']),
                  per_order_notional=str(per_order))
    tick, fee = D('.005'), D(settings['cost_buffer_per_share'])
    if price % tick or not tick <= price <= 1-tick:
        raise ValueError('SIG exit price is not a valid limit-order tick')
    if any(r.asks[0][1] < D(settings['min_reference_depth']) for r in refs):
        detail['reason'] = 'reference_ask_depth'
        return None
    reference = max(r.asks[0][0] for r in refs)
    overpriced = price - reference - fee >= D(execution['exit_edge'])
    average_cost = D(cost) / abs(D(held))  # Includes the entry buffer.
    converged = (price >= max(r.bids[0][0] for r in refs)
                 and price - fee - average_cost >= D(execution['take_profit_min']))
    detail['routes'] = {
        'overpriced_exit': {'passes': overpriced, 'reference_ask': str(reference),
            'required_bid': str(reference+fee+D(execution['exit_edge'])),
            'gap_to_required_bid': str(max(D(0), reference+fee+D(execution['exit_edge'])-price))},
        'convergence_take_profit': {'passes_average_cost_checks': converged,
            'reference_bid': str(max(r.bids[0][0] for r in refs)),
            'price_converged': price >= max(r.bids[0][0] for r in refs),
            'average_cost_with_entry_buffer': str(average_cost),
            'net_profit_per_share_at_average_cost': str(price-fee-average_cost),
            'minimum_profit_per_share': str(execution['take_profit_min']),
            'required_bid_for_average_profit': str(average_cost+fee+D(execution['take_profit_min']))}}
    if not (overpriced or converged):
        detail['reason'] = ('price_not_converged' if price < max(r.bids[0][0] for r in refs)
                            else 'profit_below_minimum')
        return None
    share_limit = abs(D(held)) if quantity_cap is None else D(quantity_cap)
    if quantity_cap is not None:
        detail['exposure_headroom'] = str(share_limit)
    quantity = int(min(share_limit, abs(D(held)), depth, D(settings['max_shares_per_order']),
                       min(r.asks[0][1] for r in refs), D(per_order) / price))
    if not quantity:
        detail['reason'] = 'exposure_limit' if share_limit < 1 else 'size_below_one_share'
        return None
    detail.update(status='eligible', reason='overpriced_exit' if overpriced else 'convergence_take_profit',
                  quantity=quantity)
    return Signal(side, price, quantity, reference, price-reference-fee, 'sell',
                  'overpriced_exit' if overpriced else 'convergence_take_profit')
