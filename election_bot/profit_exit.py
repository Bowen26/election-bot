"""SIG profit-taking without requiring cross-venue price convergence."""
from .strategy import D, Signal


def profit_target(book, settings, execution, held, cost, per_order, sale_basis,
                  detail, quantity_cap=None):
    """Called only after normal SIG/reference quote validation by checked_exit."""
    if not held:
        return None
    book.check(settings['max_age_seconds'], venue='SIG')
    side = 'yes' if held > 0 else 'no'
    target = book if held > 0 else book.complement()
    price, depth = target.bids[0]
    tick, fee = D('.005'), D(settings['cost_buffer_per_share'])
    if price % tick or not tick <= price <= 1-tick:
        raise ValueError('SIG exit price is not a valid limit-order tick')
    average = D(cost)/abs(D(held))
    minimum = D(execution['take_profit_min'])
    route = {'average_cost_with_entry_buffer': str(average),
             'net_profit_per_share_at_average_cost': str(price-fee-average),
             'minimum_profit_per_share': str(minimum),
             'required_bid_for_average_profit': str(average+fee+minimum),
             'requires_reference_convergence': False,
             'quantity_source': 'SIG top bid, holdings and configured limits'}
    detail.setdefault('routes', {})['profit_target'] = route
    detail.update(status='blocked', reason='profit_target_below_minimum',
                  side=side, sig_bid=str(price), sig_bid_depth=str(depth))
    if price-fee-average < minimum:
        return None
    share_limit = abs(D(held)) if quantity_cap is None else D(quantity_cap)
    quantity = int(min(abs(D(held)), depth, share_limit,
                       D(settings['max_shares_per_order']), D(per_order)/price))
    if quantity <= 0:
        detail['reason'] = 'exposure_limit' if share_limit < 1 else 'size_below_one_share'
        return None
    basis = sale_basis(side, quantity)
    fifo_profit = price-fee-basis/quantity
    route.update(fifo_cost_per_share=str(basis/quantity),
                 net_profit_per_share_fifo=str(fifo_profit),
                 fifo_profit_passed=fifo_profit >= minimum)
    if fifo_profit < minimum:
        detail['reason'] = 'fifo_profit_below_minimum'
        return None
    detail.update(status='eligible', reason='profit_target', quantity=quantity,
                  reference_basis='position_average_cost')
    return Signal(side, price, quantity, average, price-fee-average, 'sell', 'profit_target')
