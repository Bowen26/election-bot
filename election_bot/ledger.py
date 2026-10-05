"""Confirmed executions, FIFO inventory and conservative reusable capital."""
from collections import defaultdict, deque
import json
import time

from .strategy import D
from .measurement import WINDOWS, MAX_OBSERVATION_BOOKS, tracking_start


def liquidation(book, quantity):
    """Executable bid value across displayed depth; never invent missing liquidity."""
    remaining, value = D(quantity), D(0)
    for price, size in book.bids:
        take = min(remaining, size)
        value += take * price
        remaining -= take
        if remaining == 0:
            break
    return D(quantity) - remaining, value


class Ledger:
    def __init__(self, journal):
        self.journal, self.db = journal, journal.db
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS executions (
            key TEXT PRIMARY KEY, exchange TEXT NOT NULL, action TEXT NOT NULL,
            side TEXT NOT NULL, quantity TEXT NOT NULL, price TEXT NOT NULL,
            buffer TEXT NOT NULL, at REAL NOT NULL);
          CREATE TABLE IF NOT EXISTS markouts (
            key TEXT NOT NULL, horizon INTEGER NOT NULL, at REAL NOT NULL,
            pnl TEXT, reason TEXT, PRIMARY KEY(key,horizon));
          CREATE TABLE IF NOT EXISTS valuations (
            exchange TEXT PRIMARY KEY, at REAL NOT NULL, quantity TEXT NOT NULL,
            executable_quantity TEXT NOT NULL, bid_value TEXT NOT NULL,
            cost TEXT NOT NULL, pnl TEXT);
          CREATE TABLE IF NOT EXISTS performance_settings (
            name TEXT PRIMARY KEY, value TEXT NOT NULL);
          CREATE INDEX IF NOT EXISTS executions_exchange_action_at ON executions(exchange,action,at);
        ''')
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO performance_settings VALUES (?,?)',
                            ('short_horizons_started_at', str(time.time())))
        self.short_horizons_started_at = tracking_start(self.db)
        self._cache = None

    def missing(self):
        return self.db.execute('''SELECT o.* FROM orders o LEFT JOIN executions e ON o.key=e.key
            WHERE o.state='closed' AND e.key IS NULL ORDER BY o.created,o.rowid''').fetchall()

    def record(self, payload, quantity, price, buffer, at=None):
        quantity, price, buffer = D(quantity), D(price), D(buffer)
        if (quantity < 0 or quantity > D(payload['quantity']) or quantity != int(quantity)
                or (quantity and not 0 < price < 1) or buffer < 0):
            raise ValueError('Invalid confirmed execution')
        amount = quantity * (price + buffer) if payload['action'] == 'buy' else D(0)
        # Completion and accounting form one transaction: recovery cannot release twice.
        with self.db:
            prior = self.db.execute('SELECT * FROM executions WHERE key=?',
                                    (payload['idempotencyKey'],)).fetchone()
            if prior:
                if (D(prior['quantity']) != quantity or D(prior['price']) != price
                        or prior['action'] != payload['action'] or prior['side'] != payload['side']):
                    raise RuntimeError('Confirmed execution changed; accounting halted')
                return
            self.db.execute('INSERT INTO executions VALUES (?,?,?,?,?,?,?,?)',
                (payload['idempotencyKey'], payload['exchangeId'], payload['action'], payload['side'],
                 str(quantity), str(price), str(buffer), at if at is not None else time.time()))
            self.db.execute("UPDATE orders SET state='closed',amount=? WHERE key=?",
                            (str(amount), payload['idempotencyKey']))
            if quantity:
                self.db.execute('DELETE FROM valuations WHERE exchange=?', (payload['exchangeId'],))
        self._cache = None

    def inventory(self):
        if self._cache is not None:
            return self._cache
        lots, realized = defaultdict(deque), defaultdict(lambda: D(0))
        for row in self.db.execute('SELECT * FROM executions ORDER BY at,rowid'):
            asset = (row['exchange'], row['side'])
            quantity, price, fee = D(row['quantity']), D(row['price']), D(row['buffer'])
            if not quantity:
                continue
            if row['action'] == 'buy':
                lots[asset].append([quantity, price + fee])
            elif row['action'] == 'sell':
                remaining = quantity
                while remaining and lots[asset]:
                    lot = lots[asset][0]
                    take = min(remaining, lot[0])
                    realized[row['exchange']] += take * (price - fee - lot[1])
                    lot[0] -= take
                    remaining -= take
                    if not lot[0]:
                        lots[asset].popleft()
                if remaining:
                    raise RuntimeError('Sell exceeds bot-owned inventory; accounting halted')
            else:
                raise RuntimeError('Unknown execution action')
        holdings = {asset: {'quantity': sum((q for q, p in rows), D(0)), 'lots': list(rows),
                             'cost': sum((q*p for q, p in rows), D(0))}
                    for asset, rows in lots.items() if rows}
        self._cache = holdings, dict(realized)
        return self._cache

    def held(self, exchange):
        holdings, _ = self.inventory()
        sides = [(side, v) for (ex, side), v in holdings.items() if ex == exchange]
        if len(sides) > 1:
            raise RuntimeError('Opposing bot inventory on one exchange')
        if not sides:
            return D(0), D(0)
        side, values = sides[0]
        return values['quantity'] * (1 if side == 'yes' else -1), values['cost']

    def sale_basis(self, exchange, side, quantity):
        holdings, _ = self.inventory()
        remaining, cost = D(quantity), D(0)
        for size, price in holdings.get((exchange, side), {}).get('lots', []):
            take = min(remaining, size)
            cost += take * price
            remaining -= take
            if not remaining:
                return cost
        raise RuntimeError('Requested sale exceeds bot-owned inventory')

    def committed(self, exchange=None):
        holdings, realized = self.inventory()
        cost = sum((v['cost'] for (ex, side), v in holdings.items()
                    if exchange is None or ex == exchange), D(0))
        pnl = sum((v for ex, v in realized.items() if exchange is None or ex == exchange), D(0))
        pending = sum((D(r['amount']) for r in self.journal.pending()
                       if exchange is None or r['market'] == exchange), D(0))
        return cost + max(D(0), -pnl) + pending

    def observation_tasks(self, now, exchange=None):
        horizons = ','.join('(%d,%d)' % item for item in WINDOWS.items())
        sql = '''WITH horizons(horizon,window) AS (VALUES %s)
            SELECT e.*,h.horizon,e.at+h.horizon AS due,e.at+h.horizon+h.window AS deadline
            FROM executions e CROSS JOIN horizons h
            LEFT JOIN markouts m ON m.key=e.key AND m.horizon=h.horizon
            WHERE e.action='buy' AND CAST(e.quantity AS REAL)>0 AND m.key IS NULL
              AND (h.horizon>=3600 OR e.at>=?) AND e.at+h.horizon<=?''' % horizons
        args = [self.short_horizons_started_at, now]
        if exchange is not None:
            sql += ' AND e.exchange=?'
            args.append(exchange)
        return self.db.execute(sql + ' ORDER BY deadline,e.at,e.key', args).fetchall()

    def expire_observations(self, now=None):
        now = time.time() if now is None else now
        with self.db:
            for row in self.observation_tasks(now):
                if now > row['deadline']:
                    self.db.execute('INSERT OR IGNORE INTO markouts VALUES (?,?,?,?,?)',
                        (row['key'], row['horizon'], now, None, 'Observation window missed'))

    def due_exchanges(self, now=None, limit=MAX_OBSERVATION_BOOKS):
        now = time.time() if now is None else now
        self.expire_observations(now)
        # One book can measure several fills and horizons in the same market.
        return list(dict.fromkeys(row['exchange'] for row in self.observation_tasks(now)))[:limit]

    def observe(self, exchange, book, max_age, now=None):
        book.check(max_age)
        now = time.time() if now is None else now
        signed, cost = self.held(exchange)
        if signed:
            outcome = book if signed > 0 else book.complement()
            qty, value = liquidation(outcome, abs(signed))
            pnl = str(value - cost) if qty == abs(signed) else None
            with self.db:
                self.db.execute('INSERT OR REPLACE INTO valuations VALUES (?,?,?,?,?,?,?)',
                    (exchange, now, str(abs(signed)), str(qty), str(value), str(cost), pnl))
        else:
            with self.db:
                self.db.execute('DELETE FROM valuations WHERE exchange=?', (exchange,))
        for row in self.observation_tasks(now, exchange):
            if now > row['deadline']:
                with self.db:
                    self.db.execute('INSERT OR IGNORE INTO markouts VALUES (?,?,?,?,?)',
                        (row['key'], row['horizon'], now, None, 'Observation window missed'))
                continue
            if book.source_at < row['due'] or book.observed_at < row['due']:
                continue  # A recent quote can still predate this particular horizon.
            outcome = book if row['side'] == 'yes' else book.complement()
            qty, value = liquidation(outcome, D(row['quantity']))
            if qty < D(row['quantity']):
                continue  # No full-size exit available; retry within the window.
            pnl = str(value - qty * (D(row['price']) + 2*D(row['buffer'])))
            with self.db:
                self.db.execute('INSERT OR IGNORE INTO markouts VALUES (?,?,?,?,?)',
                                (row['key'], row['horizon'], now, pnl, None))

    def summary(self):
        holdings, realized = self.inventory()
        return {'risk_committed': str(self.committed()),
                'open_cost_with_entry_buffer': str(sum((v['cost'] for v in holdings.values()), D(0))),
                'realized_pnl_after_buffers': str(sum(realized.values(), D(0))),
                'open_races': len({ex for ex, side in holdings}),
                'gross_buy_spending': str(self.journal.used()),
                'gross_buy_spending_today': str(self.journal.used(today=True))}
