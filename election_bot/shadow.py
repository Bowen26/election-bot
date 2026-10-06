"""Entry-only experiments. Pure calculations; no broker, ledger or network access."""
from .strategy import D, choose

VARIANTS = ('both_bids', 'kalshi_bid', 'polymarket_bid')


def decisions(book, refs, settings, available, held, side_limits=None, exit_selected=False):
    """Change reference valuation only; keep both venues' quality/depth gates.

    Budgets and holdings are the real portfolio's contemporaneous constraints,
    not three independent simulated portfolios. No signal returned here is traded.
    """
    # Validate both venues, including agreement, even for a single-bid variant.
    choose(book, refs, settings, 0)
    candidates = []
    for name, selected in zip(VARIANTS, (refs, [refs[0]] * 2, [refs[1]] * 2)):
        checks = []
        # Preserve BOTH venues' side-specific depth eligibility and sizing.
        limits = {}
        for side in ('yes', 'no'):
            aligned = refs if side == 'yes' else [r.complement() for r in refs]
            depth = min(r.bids[0][1] for r in aligned)
            cap = D(settings['max_shares_per_order']) if side_limits is None else D(side_limits[side])
            limits[side] = min(cap, depth) if depth >= D(settings['min_reference_depth']) else D(0)
        signal = choose(book, selected, settings, available, checks, limits)
        reason = 'eligible' if signal else 'no_eligible_entry'
        if exit_selected:
            signal, reason = None, 'live_exit_has_priority'
        elif signal and held and ((held < 0) != (signal.side == 'no')):
            signal, reason = None, 'opposite_inventory'
        candidates.append({'variant': name, 'reason': reason,
                           'candidate': vars(signal) if signal else None})
    return {'version': 1, 'experiment': 'reference_bid_v1',
            'entry_only': True, 'portfolio_simulation': False,
            'available': available, 'held': held, 'side_limits': side_limits,
            'settings': dict(settings), 'decisions': candidates}
