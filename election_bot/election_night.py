"""Election-night entry route: buy races the reference markets treat as decided.

After polls close, Kalshi and Polymarket reprice called races within seconds,
while SIG books can lag until trading ends at noon ET on November 4. When both
references bid one side at or above min_reference_bid and SIG still offers it
at least min_edge below their midpoint, this route proposes a buy.

Only inside [start_at, end_at). Existing limits, exposure caps, the drawdown
gate and the position-entry guard still apply, and held inventory is never
flipped. During the window, generic news-uncertainty pauses (and optionally the
post-sale reentry cooldown) are overridden for this route only; disputed
results and feed-health blocks are never overridden. Disabled by default.
"""
from datetime import datetime
import time

from .strategy import D, Signal

REASON = 'election_night'
DEFAULTS = dict(enabled=False, start_at='2026-11-03T18:00:00-05:00', end_at='2026-11-04T12:00:00-05:00',
                min_reference_bid='0.95', min_edge='0.02', max_price='0.99', depth_fraction='0.5',
                max_shares=2000, ignore_reentry_cooldown=True, override_news_pauses=True)
OVERRIDABLE_NEWS = ('News uncertainty pause',)


def timestamp(value):
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError('election_night times need an explicit UTC offset')
    return parsed.timestamp()


def settings(config):
    raw = config.get('election_night', {})
    if not isinstance(raw, dict) or set(raw)-set(DEFAULTS):
        raise ValueError('Invalid election_night configuration')
    p = dict(DEFAULTS, **raw)
    for key in ('enabled', 'ignore_reentry_cooldown', 'override_news_pauses'):
        if type(p[key]) is not bool:
            raise ValueError('election_night %s must be boolean' % key)
    p['start'], p['end'] = timestamp(p['start_at']), timestamp(p['end_at'])
    if not p['start'] < p['end'] or p['end']-p['start'] > 3*86400:
        raise ValueError('election_night window must be positive and at most three days')
    for key in ('min_reference_bid', 'min_edge', 'max_price', 'depth_fraction'):
        p[key] = D(p[key])
    if not D('.80') <= p['min_reference_bid'] < 1:
        raise ValueError('election_night min_reference_bid must be from 0.80 to below 1')
    if not D('.005') <= p['min_edge'] <= D('.50'):
        raise ValueError('election_night min_edge must be from 0.005 to 0.50')
    if not 0 < p['max_price'] < 1 or not 0 < p['depth_fraction'] <= 1:
        raise ValueError('Invalid election_night max_price or depth_fraction')
    if type(p['max_shares']) is not int or not 1 <= p['max_shares'] <= 100000:
        raise ValueError('election_night max_shares must be an integer from 1 to 100000')
    return p


class ElectionNight:
    def __init__(self, config):
        self.policy = settings(config)
        self.strategy = config['strategy']

    def active(self, now=None):
        now = time.time() if now is None else now
        return self.policy['enabled'] and self.policy['start'] <= now < self.policy['end']

    def overrides(self, news_reason, now=None):
        """True only for generic uncertainty pauses, inside the window."""
        return (bool(news_reason) and self.active(now) and self.policy['override_news_pauses']
                and news_reason.startswith(OVERRIDABLE_NEWS))

    def called_side(self, refs):
        """Side both fresh references bid at or above the threshold, else None."""
        if len(refs) != 2:
            return None
        try:
            for venue, b in zip(('Kalshi', 'Polymarket'), refs):
                b.check(self.strategy['max_age_seconds'], venue=venue)
        except ValueError:
            return None
        for side in ('yes', 'no'):
            books = refs if side == 'yes' else [r.complement() for r in refs]
            if all(b.bids and b.asks and b.bids[0][0] >= self.policy['min_reference_bid'] for b in books):
                return side
        return None

    def select(self, book, refs, available, held, side_limits=None, detail=None):
        """One buy proposal or None; detail records why."""
        detail = {} if detail is None else detail
        detail.update(route=REASON, status='blocked')
        side = self.called_side(refs)
        if side is None:
            detail['reason'] = 'not_called_by_references'
            return None
        detail['called_side'] = side
        if held and ((held < 0) != (side == 'no')):
            detail['reason'] = 'opposite_inventory'
            return None
        book.check(self.strategy['max_age_seconds'], venue='SIG')
        target = book if side == 'yes' else book.complement()
        if not target.asks:
            detail['reason'] = 'no_sig_ask'
            return None
        ask, size = target.asks[0]
        aligned = refs if side == 'yes' else [r.complement() for r in refs]
        fair = sum(((b.bids[0][0]+b.asks[0][0])/2 for b in aligned), D(0))/2
        detail.update(sig_ask=str(ask), fair_value=str(fair), edge=str(fair-ask))
        if ask > self.policy['max_price'] or ask % D('.005'):
            detail['reason'] = 'price_cap_or_tick'
            return None
        if fair-ask < self.policy['min_edge']:
            detail['reason'] = 'edge_below_minimum'
            return None
        caps = [D(available)/(ask+D(self.strategy['cost_buffer_per_share'])),
                D(self.policy['max_shares']), D(self.strategy['max_shares_per_order']),
                self.policy['depth_fraction']*size]
        if side_limits is not None:
            caps.append(D(side_limits[side]))
        quantity = int(min(caps))
        if quantity < 1:
            detail['reason'] = 'size_below_one_share'
            return None
        detail.update(status='eligible', reason=REASON, quantity=quantity)
        return Signal(side, ask, quantity, fair, fair-ask, 'buy', REASON)


def rehearse(path, config, hours=24, now=None):
    """Read-only: what the route would have proposed on recorded aligned snapshots.

    Ignores the window (it has not happened yet), budgets, holdings and caps;
    reports proposals and the reference-implied edge per share only.
    """
    import sqlite3
    from pathlib import Path
    from .leadership import load_events
    policy = settings(config)
    now = time.time() if now is None else now
    path = Path(path).resolve()
    result = {'status': 'No journal yet', 'hours': hours,
              'policy': {k: str(v) for k, v in policy.items() if k not in ('start', 'end')}}
    if not path.exists():
        return result
    db = sqlite3.connect(path.as_uri()+'?mode=ro', uri=True)
    try:
        db.execute('BEGIN')
        rows, _ = load_events(db, now-hours*3600, now)
    finally:
        db.close()
    names = {m['sig_exchange_id']: m.get('name') for m in config['markets']}
    best, called = {}, 0
    for row in rows:
        if not row['aligned']:
            continue
        for side in ('yes', 'no'):
            def oriented(q):
                return (q['bid'], q['ask']) if side == 'yes' else (1-q['ask'], 1-q['bid'])
            refs = [oriented(row[v]) for v in ('Kalshi', 'Polymarket')]
            if not all(bid >= policy['min_reference_bid'] for bid, _ in refs):
                continue
            called += 1
            fair = sum((b+a)/2 for b, a in refs)/2
            ask = oriented(row['SIG'])[1]
            edge = fair-ask
            if ask <= policy['max_price'] and edge >= policy['min_edge']:
                key = (row['exchange'], side)
                if key not in best or edge > best[key]['edge']:
                    best[key] = {'exchange': row['exchange'], 'market': names.get(row['exchange']), 'side': side,
                                 'ask': str(ask), 'fair_value': str(fair), 'edge': edge, 'at': row['at']}
    result.update(status='ok', aligned_snapshots=sum(r['aligned'] for r in rows), called_side_snapshots=called,
                  races_with_proposals=len(best),
                  proposals=sorted(({**v, 'edge': str(v['edge'])} for v in best.values()),
                                   key=lambda v: -D(v['edge'])))
    return result


def format_rehearsal(r):
    lines = ['ELECTION-NIGHT REHEARSAL — recorded quotes, window ignored, no orders']
    if r['status'] != 'ok':
        return '\n'.join(lines+[r['status']])
    p = r['policy']
    lines.append(f"Rule: both reference bids >= {p['min_reference_bid']}, SIG ask <= {p['max_price']}, "
                 f"edge to reference midpoint >= {p['min_edge']}")
    lines.append(f"{r['aligned_snapshots']} aligned snapshots; {r['called_side_snapshots']} looked called; "
                 f"{r['races_with_proposals']} race sides would have produced a buy")
    for v in r['proposals'][:20]:
        lines.append(f"  {str(v['market']):28} {v['side']:3} ask {v['ask']:>6} fair {float(D(v['fair_value'])):.3f} "
                     f"edge {float(D(v['edge']))*100:+.1f}c")
    lines.append('Today these are mostly safe favorites; election night adds newly called races.')
    return '\n'.join(lines)
