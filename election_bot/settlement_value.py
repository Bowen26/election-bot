"""Settlement-value view of positions. The competition scores the final SUSQie
balance after every market resolves, so a held share is worth its probability of
winning, not today's SIG bid. Kalshi and Polymarket midpoints are the estimate.

Pure functions; no network, database or order access.
"""
from .strategy import D

EXIT_DEFAULTS = dict(enabled=False, margin='0.01', block_risk_exits=True)


def exit_settings(config):
    raw = config.get('settlement_exits', {})
    if not isinstance(raw, dict) or set(raw)-set(EXIT_DEFAULTS):
        raise ValueError('Invalid settlement_exits configuration')
    p = dict(EXIT_DEFAULTS, **raw)
    for key in ('enabled', 'block_risk_exits'):
        if type(p[key]) is not bool:
            raise ValueError('settlement_exits %s must be boolean' % key)
    p['margin'] = D(p['margin'])
    if not 0 <= p['margin'] <= D('.10'):
        raise ValueError('settlement_exits margin must be between 0 and 0.10')
    return p


def fair_value(refs, side, strategy):
    """Average of the two reference midpoints for one side, or ValueError.

    Requires both venues fresh, within the configured spread, and agreeing.
    """
    if len(refs) != 2:
        raise ValueError('Fair value requires both reference venues')
    mids = []
    for venue, ref in zip(('Kalshi', 'Polymarket'), refs):
        ref.check(strategy['max_age_seconds'], venue=venue)
        oriented = ref if side == 'yes' else ref.complement()
        if not oriented.bids or not oriented.asks:
            raise ValueError('Fair value requires two-sided reference books')
        bid, ask = oriented.bids[0][0], oriented.asks[0][0]
        if not 0 < bid < ask < 1 or ask-bid > D(strategy['max_reference_spread']):
            raise ValueError('Reference spread too wide for fair value')
        mids.append((bid+ask)/2)
    if abs(mids[0]-mids[1]) > D(strategy['max_reference_disagreement']):
        raise ValueError('References disagree too much for fair value')
    return sum(mids)/2


def gate_exit(signal, refs, strategy, policy, detail):
    """Keep a sale only if it beats holding to settlement by the margin."""
    if signal is None or signal.action != 'sell' or not policy['enabled']:
        return signal
    try:
        fair = fair_value(refs, signal.side, strategy)
    except ValueError as error:
        detail.update(status='blocked', reason='settlement_value_unavailable', settlement_error=str(error))
        return None
    detail['settlement_value'] = {'fair_value': str(fair), 'bid': str(signal.price),
                                  'required_bid': str(fair+policy['margin']), 'route': signal.reason}
    if signal.price-fair < policy['margin']:
        detail.update(status='blocked', reason='below_settlement_value', legacy_reason=signal.reason)
        return None
    return signal
