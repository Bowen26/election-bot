"""Pure price/depth checks shared by live execution, paper trading and tests."""
from dataclasses import dataclass
from decimal import Decimal, ROUND_FLOOR
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

    @classmethod
    def make(cls, bids, asks, source_at=None):
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
                   now if source_at is None else float(source_at))

    def complement(self):
        return Book([(1-p, q) for p, q in self.asks],
                    [(1-p, q) for p, q in self.bids],
                    self.observed_at, self.source_at)

    def check(self, max_age, max_spread=None):
        now = time.time()
        if any(now - stamp > max_age or stamp > now + 5
               for stamp in (self.observed_at, self.source_at)):
            raise ValueError("Stale or future-dated book")
        if not self.bids or not self.asks:
            raise ValueError("Two-sided book required")
        spread = self.asks[0][0] - self.bids[0][0]
        if spread <= 0:
            raise ValueError("Locked or crossed book")
        if max_spread is not None and spread > D(max_spread):
            raise ValueError("Reference spread too wide")


@dataclass
class Signal:
    side: str
    price: Decimal
    quantity: int
    reference: Decimal
    edge: Decimal
    action: str = 'buy'
    reason: str = 'entry_gap'


def choose(sig, references, settings, available, diagnostics=None):
    """Buy only when both reference bids support a conservative price gap.

    Reference bids are evidence, not a guaranteed probability or hedge.
    available is the smallest remaining cash/budget allowance.
    """
    if len(references) != 2:
        raise ValueError("Both Kalshi and Polymarket books are required")
    sig.check(settings['max_age_seconds'])
    for book in references:
        book.check(settings['max_age_seconds'], settings['max_reference_spread'])
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
        quantity = int(min(D(settings['max_shares_per_order']), depth,
                           min(b.bids[0][1] for b in refs),
                           D(available) / (price + fee)))
        if quantity > 0:
            check['reason'] = 'eligible'
            candidates.append(Signal(side, price, quantity, reference,
                                     reference-price-fee))
        else:
            check['reason'] = 'budget_or_target_depth'
    return max(candidates, key=lambda s: s.edge) if candidates else None


def choose_exit(sig, references, settings, execution, held, cost, per_order):
    """Sell owned shares when overpriced, or when a profitable gap has converged."""
    if not held:
        return None
    # Apply the same freshness, spread and agreement checks as entries.
    choose(sig, references, settings, 0)
    side = 'yes' if held > 0 else 'no'
    target = sig if held > 0 else sig.complement()
    refs = references if held > 0 else [r.complement() for r in references]
    price, depth = target.bids[0]
    tick, fee = D('.005'), D(settings['cost_buffer_per_share'])
    if price % tick or not tick <= price <= 1-tick:
        raise ValueError('SIG exit price is not a valid limit-order tick')
    if any(r.asks[0][1] < D(settings['min_reference_depth']) for r in refs):
        return None
    reference = max(r.asks[0][0] for r in refs)
    overpriced = price - reference - fee >= D(execution['exit_edge'])
    average_cost = D(cost) / abs(D(held))  # Includes the entry buffer.
    converged = (price >= max(r.bids[0][0] for r in refs)
                 and price - fee - average_cost >= D(execution['take_profit_min']))
    if not (overpriced or converged):
        return None
    quantity = int(min(abs(D(held)), depth, D(settings['max_shares_per_order']),
                       min(r.asks[0][1] for r in refs), D(per_order) / price))
    if not quantity:
        return None
    return Signal(side, price, quantity, reference, price-reference-fee, 'sell',
                  'overpriced_exit' if overpriced else 'convergence_take_profit')
