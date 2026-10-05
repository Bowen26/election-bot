"""Bounded priority scanning with a durable ordinary-coverage lane."""
import json
import math
from pathlib import Path
import time

from .strategy import D


PRIORITY_REVISIT_SECONDS = 30
INTEREST_TTL_SECONDS = 180
PRICE_COMPARISON_SECONDS = 300
REFERENCE_MOVE = D('.02')
NEAR_ENTRY_MARGIN = D('.02')
INTEREST_SCORES = {'near_entry': 200, 'reference_move': 180, 'owned_position': 100}


class ScanInterest:
    """Scheduling hints from validated observations; never authorize a trade."""
    def __init__(self):
        self.watches = {}
        self.previous = {}

    def invalidate(self, name):
        self.watches.pop(name, None)
        self.previous.pop(name, None)

    def observe(self, name, diagnostics, refs, settings, held, now=None):
        now = time.time() if now is None else now
        mids = tuple((b.bids[0][0]+b.asks[0][0])/2 for b in refs)
        previous = self.previous.get(name)
        watches = self.watches.setdefault(name, {})
        if (previous and 0 <= now-previous[0] <= PRICE_COMPARISON_SECONDS
                and any(abs(a-b) >= REFERENCE_MOVE for a, b in zip(mids, previous[1]))):
            watches['reference_move'] = now + INTEREST_TTL_SECONDS
        self.previous[name] = (now, mids)
        near = any(check.get('reason') in ('price_gap', 'eligible')
                   and D(check['available']) > 0
                   and (not held or (held < 0) == (check['side'] == 'no'))
                   and D(check['reference_bid'])-D(check['ask'])-D(settings['cost_buffer_per_share'])
                       >= max(D(0), D(settings['minimum_edge'])-NEAR_ENTRY_MARGIN)
                   for check in diagnostics)
        if near:
            watches['near_entry'] = now + INTEREST_TTL_SECONDS
        else:
            watches.pop('near_entry', None)

    def hints(self, owned, not_before, now=None):
        now = time.time() if now is None else now
        names = set(owned) | set(self.watches) | set(not_before)
        result = {}
        for name in names:
            watches = self.watches.get(name, {})
            reasons = [reason for reason, until in watches.items() if until > now]
            self.watches[name] = {reason: until for reason, until in watches.items() if until > now}
            if name in owned:
                reasons.append('owned_position')
            score = max((INTEREST_SCORES[r] for r in reasons), default=0)
            result[name] = {'score': score, 'reasons': sorted(reasons),
                            'expires': dict(self.watches[name]),
                            'not_before': not_before.get(name, 0)}
        return result


class Scanner:
    def __init__(self, markets, runtime):
        self.markets = markets
        self.path = Path(runtime) / 'scan_cursor.json'
        self.index = 0
        self.priority_turn = True
        self.last_visits = {}
        self.last_quotes = {}
        self.selection = {}
        self.policy = None
        if self.path.exists():
            try:
                saved = json.loads(self.path.read_text())
                self.index = next((i for i, m in enumerate(markets)
                                   if m['name'] == saved.get('next_market')), 0)
                self.priority_turn = saved.get('priority_turn') is True
                self.policy = saved.get('scan_policy')
                names, now = {m['name'] for m in markets}, time.time()
                for key in ('last_visits', 'last_quotes'):
                    values = saved.get(key, {})
                    if isinstance(values, dict):
                        setattr(self, key, {name: stamp for name, stamp in values.items()
                            if name in names and type(stamp) in (int, float)
                            and math.isfinite(stamp) and 0 <= stamp <= now+5})
            except (ValueError, TypeError, AttributeError):
                pass  # Losing a scan cursor cannot lose order/accounting state.

    def save(self):
        if not self.markets:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix('.tmp')
        temp.write_text(json.dumps({'next_market': self.markets[self.index]['name'],
                                   'priority_turn': self.priority_turn,
                                   'scan_policy': self.policy,
                                   'last_visits': self.last_visits, 'last_quotes': self.last_quotes},
                                  allow_nan=False))
        temp.replace(self.path)

    def set_policy(self, policy):
        if self.policy != policy:
            # Keep the ordinary cursor, but don't attribute an old policy's
            # elapsed interval to the first visit under the new policy.
            self.last_visits.clear()
            self.last_quotes.clear()
            self.policy = policy
            self.save()

    def record_quote(self, name, now=None):
        now = time.time() if now is None else now
        previous = self.last_quotes.get(name)
        self.last_quotes[name] = now
        self.save()
        return round(now-previous, 3) if previous is not None and now >= previous else None

    @staticmethod
    def hint(interests, name, now):
        hint = dict((interests or {}).get(name, {}))
        if 'expires' in hint:
            hint['reasons'] = [reason for reason in hint['reasons']
                               if reason not in hint['expires'] or hint['expires'][reason] > now]
            hint['score'] = max((INTEREST_SCORES[r] for r in hint['reasons']), default=0)
        return hint

    def batch(self, priority, size, interests=None):
        seen = set()
        for _ in range(min(size, len(self.markets))):
            now = time.time()
            candidates = []
            for m in self.markets:
                name = m['name']
                if name in seen:
                    continue
                hint = self.hint(interests, name, now)
                score = 300 if name in priority else hint.get('score', 0)
                if not score:
                    continue
                if interests is not None and (now < hint.get('not_before', 0)
                        or now-self.last_visits.get(name, 0) < PRIORITY_REVISIT_SECONDS):
                    continue
                candidates.append((score, -self.last_visits.get(name, 0), m))
            urgent = max(candidates, key=lambda r: (r[0], r[1]))[2] if candidates else None
            if urgent is not None and self.priority_turn:
                mapping = urgent
                self.priority_turn = False
                lane = 'priority'
            else:
                while self.markets[self.index]['name'] in seen:
                    self.index = (self.index + 1) % len(self.markets)
                mapping = self.markets[self.index]
                self.index = (self.index + 1) % len(self.markets)
                self.priority_turn = True
                lane = 'regular'
            name = mapping['name']
            hint = self.hint(interests, name, now)
            reasons = (['news'] if name in priority else []) + hint.get('reasons', [])
            previous = self.last_visits.get(name)
            self.selection = {'lane': lane, 'priority_reasons': reasons,
                'revisit_seconds': round(now-previous, 3) if previous is not None and now >= previous else None}
            self.last_visits[name] = now
            seen.add(name)
            # Persist before yielding: an order/early return cannot reset fairness.
            self.save()
            yield mapping
