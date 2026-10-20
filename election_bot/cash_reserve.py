"""Keep a cash floor for election night. Restricts buying only; never sells. Off by default.

Until `until` (default: the election-night window start), ordinary buys and new
pair baskets may not take account cash below `coins`. Election-night buys and
the completing legs of a basket are exempt.
"""
import time

from .election_night import timestamp, DEFAULTS as NIGHT
from .strategy import D

DEFAULTS = dict(enabled=False, coins='10000', until=None)


def settings(config):
    raw = config.get('cash_reserve', {})
    if not isinstance(raw, dict) or set(raw)-set(DEFAULTS):
        raise ValueError('Invalid cash_reserve configuration')
    p = dict(DEFAULTS, **raw)
    if type(p['enabled']) is not bool:
        raise ValueError('cash_reserve enabled must be boolean')
    p['coins'] = D(p['coins'])
    if p['coins'] < 0:
        raise ValueError('cash_reserve coins must be nonnegative')
    if p['until'] is None:
        p['until'] = config.get('election_night', {}).get('start_at', NIGHT['start_at'])
    p['until_at'] = timestamp(p['until'])
    return p


def floor(policy, now=None):
    """Coins to keep in cash right now (zero when inactive)."""
    now = time.time() if now is None else now
    return policy['coins'] if policy['enabled'] and now < policy['until_at'] else D(0)
