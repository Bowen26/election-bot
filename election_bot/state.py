"""Durable reservations: a network error never makes an order disappear."""
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import sqlite3
import time

from .strategy import D


@contextmanager
def exclusive_lock(directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (directory / 'bot.lock').open('a') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Another bot process is running in this runtime directory") from None
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


class Journal:
    def __init__(self, path, binding):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(str(path))
        os.chmod(path, 0o600)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS identity (binding TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS orders (
            key TEXT PRIMARY KEY, market TEXT NOT NULL, amount TEXT NOT NULL,
            day TEXT NOT NULL, created REAL NOT NULL, state TEXT NOT NULL,
            payload TEXT NOT NULL, response TEXT);
          CREATE TABLE IF NOT EXISTS events (
            at REAL NOT NULL, kind TEXT NOT NULL, detail TEXT NOT NULL);
          CREATE INDEX IF NOT EXISTS events_at ON events(at);
          CREATE INDEX IF NOT EXISTS orders_market_created ON orders(market,created);
        ''')
        row = self.db.execute('SELECT binding FROM identity').fetchone()
        if row and row['binding'] != binding:
            raise ValueError("Runtime belongs to another key/tournament; do not reuse its journal")
        if not row:
            with self.db:
                self.db.execute('INSERT INTO identity VALUES (?)', (binding,))

    def close(self):
        self.db.close()

    def event(self, kind, detail):
        with self.db:
            self.db.execute('INSERT INTO events VALUES (?,?,?)',
                            (time.time(), kind, json.dumps(detail, default=str, allow_nan=False)))

    def reserve(self, payload, amount, guard=None):
        if self.db.in_transaction:
            raise RuntimeError("Reservation requires its own transaction")
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            if self.pending():
                raise RuntimeError("Unresolved order blocks new submissions")
            if guard is not None:
                guard()
            self.db.execute('INSERT INTO orders VALUES (?,?,?,?,?,?,?,NULL)',
                            (payload['idempotencyKey'], payload['exchangeId'], str(amount),
                             datetime.now(timezone.utc).date().isoformat(), time.time(),
                             'pending', json.dumps(payload)))

    def response(self, key, response):
        with self.db:
            self.db.execute('UPDATE orders SET response=? WHERE key=?', (json.dumps(response), key))

    def complete(self, key, amount):
        if D(amount) < 0:
            raise ValueError("Negative order cost")
        with self.db:
            self.db.execute("UPDATE orders SET state='closed', amount=? WHERE key=?", (str(amount), key))

    def pending(self):
        return self.db.execute("SELECT * FROM orders WHERE state='pending'").fetchall()

    def used(self, market=None, today=False):
        query, args = 'SELECT amount FROM orders WHERE 1=1', []
        if market is not None:
            query += ' AND market=?'
            args.append(market)
        if today:
            query += ' AND day=?'
            args.append(datetime.now(timezone.utc).date().isoformat())
        return sum((D(r['amount']) for r in self.db.execute(query, args)), D(0))

    def last_order(self, market):
        row = self.db.execute('SELECT MAX(created) AS t FROM orders WHERE market=?', (market,)).fetchone()
        return row['t'] or 0

    def summary(self):
        return {'spent_or_reserved': str(self.used()), 'today': str(self.used(today=True)),
                'unresolved_orders': len(self.pending()),
                'orders': self.db.execute('SELECT COUNT(*) FROM orders').fetchone()[0]}
