"""Bounded SIG-only follow-up quotes for research anchors and simulated positions.

Research studies (leadership, exit depth, fair-value markouts, entry timing) need a
SIG quote shortly after each anchor. Ordinary scans revisit a race irregularly, so
most five-minute outcomes were missing. This scheduler asks for at most a small,
configured number of extra SIG books per cycle and per hour. It never trades, never
marks the ledger or drawdown estimate, and is disabled unless configured.
"""
import math
import time

from .leadership import HORIZONS
from .measurement import WINDOWS

DEFAULTS = dict(enabled=False, max_books_per_cycle=2, max_books_per_hour=120,
                anchor_offsets_seconds=[300, 900], position_offsets_seconds=[60, 150, 300, 900],
                position_spacing_seconds=60, backoff_seconds=300)
KINDS = ('position', 'anchor')  # Priority order: simulated positions first.


def settings(config):
    raw = config.get('research_followups', {})
    if not isinstance(raw, dict) or set(raw)-set(DEFAULTS):
        raise ValueError('Invalid research_followups configuration')
    p = dict(DEFAULTS, **raw)
    if type(p['enabled']) is not bool:
        raise ValueError('research_followups enabled must be boolean')
    for key, low, high in (('max_books_per_cycle', 0, 8), ('max_books_per_hour', 0, 600),
                           ('position_spacing_seconds', 10, 3600), ('backoff_seconds', 60, 3600)):
        if type(p[key]) is not int or not low <= p[key] <= high:
            raise ValueError('research_followups %s must be an integer from %d to %d' % (key, low, high))
    for key in ('anchor_offsets_seconds', 'position_offsets_seconds'):
        offsets = p[key]
        if (not isinstance(offsets, list) or not 1 <= len(offsets) <= 8 or len(set(offsets)) != len(offsets)
                or any(type(o) is not int or not 30 <= o <= 3600 for o in offsets)):
            raise ValueError('research_followups %s must list 1-8 distinct integers from 30 to 3600' % key)
    return p


def window(offset):
    """Same observation windows the reports use, so a fetched quote is countable."""
    return HORIZONS.get(offset, WINDOWS.get(offset, max(30, min(180, offset//2))))


class ResearchFollowups:
    def __init__(self, db, config):
        self.db, self.policy = db, settings(config)
        self.grid = {}
        self.backoff_until = self.pruned_at = 0.
        with self.db:
            self.db.executescript('''
              CREATE TABLE IF NOT EXISTS research_followups (
                anchor_id TEXT NOT NULL, exchange TEXT NOT NULL, kind TEXT NOT NULL,
                offset INTEGER NOT NULL, due REAL NOT NULL, deadline REAL NOT NULL,
                status TEXT NOT NULL, resolved_at REAL, PRIMARY KEY(anchor_id, kind, offset));
              CREATE INDEX IF NOT EXISTS research_followups_pending
                ON research_followups(status, exchange, due);
              CREATE TABLE IF NOT EXISTS research_followup_fetches (
                at REAL NOT NULL, exchange TEXT NOT NULL);''')

    @property
    def enabled(self):
        return self.policy['enabled']

    def schedule(self, exchange, anchor_id, at, kind='anchor'):
        """Plain anchors follow the studies' non-overlapping per-race grid.

        Simulated positions are rarer and more valuable, so they get their own
        denser grid, limited by position_spacing_seconds per race and offset.
        """
        if not self.enabled:
            return 0
        if kind not in KINDS:
            raise ValueError('Unknown follow-up kind')
        offsets = self.policy['position_offsets_seconds' if kind == 'position' else 'anchor_offsets_seconds']
        added = 0
        with self.db:
            for offset in offsets:
                key = (exchange, kind, offset)
                if at < self.grid.get(key, float('-inf')):
                    continue
                self.grid[key] = at + (self.policy['position_spacing_seconds'] if kind == 'position'
                                       else offset + window(offset))
                added += self.db.execute('INSERT OR IGNORE INTO research_followups VALUES (?,?,?,?,?,?,?,NULL)',
                    (anchor_id, exchange, kind, offset, at+offset, at+offset+window(offset), 'pending')).rowcount
        return added

    def observed(self, exchange, book, now=None, status='observed_by_scan'):
        """Any SIG quote of the race fresh after the due time satisfies its tasks."""
        if not self.enabled:
            return 0
        now = time.time() if now is None else now
        if not all(math.isfinite(t) for t in (book.source_at, book.observed_at)):
            return 0
        fresh = min(book.source_at, book.observed_at)
        with self.db:
            return self.db.execute('''UPDATE research_followups SET status=?,resolved_at=?
                WHERE status='pending' AND exchange=? AND due<=? AND deadline>=?''',
                (status, now, exchange, fresh, now)).rowcount

    def expire(self, now):
        with self.db:
            self.db.execute('''UPDATE research_followups SET status='missed',resolved_at=?
                WHERE status='pending' AND deadline<?''', (now, now))

    def budget(self, now):
        used = self.db.execute('SELECT COUNT(*) FROM research_followup_fetches WHERE at>?',
                               (now-3600,)).fetchone()[0]
        return max(0, min(self.policy['max_books_per_cycle'], self.policy['max_books_per_hour']-used))

    def due(self, now=None):
        """Distinct races to quote now; one book serves every task in that race."""
        if not self.enabled:
            return []
        now = time.time() if now is None else now
        self.expire(now)
        if now-self.pruned_at > 3600:
            self.prune(now)
            self.pruned_at = now
        if now < self.backoff_until:
            return []
        limit = self.budget(now)
        if not limit:
            return []
        rows = self.db.execute('''SELECT exchange, MIN(CASE kind WHEN 'position' THEN 0 ELSE 1 END) AS priority,
                MIN(deadline) AS deadline FROM research_followups
            WHERE status='pending' AND due<=? AND deadline>=?
            GROUP BY exchange ORDER BY priority, deadline, exchange LIMIT ?''', (now, now, limit)).fetchall()
        return [r[0] for r in rows]

    def attempted(self, exchange, now=None):
        """Count every request, successful or not, against the hourly cap."""
        now = time.time() if now is None else now
        with self.db:
            self.db.execute('INSERT INTO research_followup_fetches VALUES (?,?)', (now, exchange))
            self.db.execute('DELETE FROM research_followup_fetches WHERE at<?', (now-7200,))

    def back_off(self, now=None):
        now = time.time() if now is None else now
        self.backoff_until = now + self.policy['backoff_seconds']

    def prune(self, now=None, keep_days=14):
        now = time.time() if now is None else now
        with self.db:
            self.db.execute("DELETE FROM research_followups WHERE status!='pending' AND resolved_at<?",
                            (now-keep_days*86400,))


def report(path, hours=48, now=None):
    """Read-only coverage of scheduled follow-ups; no network."""
    import sqlite3
    from pathlib import Path
    now = time.time() if now is None else float(now)
    path = Path(path).resolve()
    result = {'as_of': now, 'hours': hours, 'status': 'No journal yet', 'by_kind': {}}
    if not path.exists():
        return result
    db = sqlite3.connect(path.as_uri()+'?mode=ro', uri=True)
    try:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'research_followups' not in tables:
            result['status'] = 'Follow-ups not scheduled yet (disabled, or watcher not restarted)'
            return result
        for kind, offset, status, count in db.execute('''SELECT kind, offset, status, COUNT(*)
                FROM research_followups WHERE due>=? GROUP BY kind, offset, status''', (now-hours*3600,)):
            result['by_kind'].setdefault(kind, {}).setdefault(str(offset), {})[status] = count
        result['fetches_last_hour'] = db.execute('SELECT COUNT(*) FROM research_followup_fetches WHERE at>?',
                                                 (now-3600,)).fetchone()[0]
        result['status'] = 'ok'
        return result
    finally:
        db.close()


def format_report(result):
    lines = ['RESEARCH FOLLOW-UP QUOTES — SIG-only research reads; no orders']
    if result['status'] != 'ok':
        return '\n'.join(lines+[result['status']])
    lines.append('Extra SIG books requested in the last hour: %d' % result['fetches_last_hour'])
    for kind, offsets in sorted(result['by_kind'].items()):
        for offset, counts in sorted(offsets.items(), key=lambda x: int(x[0])):
            total = sum(counts.values())
            covered = counts.get('observed_by_scan', 0)+counts.get('fetched', 0)
            lines.append(f'{kind:9} +{int(offset):4}s  {covered}/{total} covered  {counts}')
    lines.append('"covered" means a fresh SIG quote arrived inside the window; studies still apply their own checks.')
    return '\n'.join(lines)
