"""Shared coin accounting for contracts in one race; never assumes a hedge."""
from collections import defaultdict
import re

from .strategy import D


def configured_races(config):
    result = {}
    for mapping in config['markets']:
        exchange = mapping.get('sig_exchange_id')
        if not isinstance(exchange, str) or not re.fullmatch(r'[1-9][0-9]*', exchange):
            if mapping.get('enabled'):
                raise ValueError('Race accounting requires a numeric exchange ID')
            continue  # Disabled starter placeholders have no account inventory.
        if exchange in result:
            raise ValueError('Race accounting requires unique exchange IDs, including disabled mappings')
        race = mapping.get('race_key')
        if race is None:
            market = mapping.get('sig_market_id')
            if not isinstance(market, str) or not re.fullmatch(r'[1-9][0-9]*', market):
                raise ValueError('Race accounting requires race_key or numeric market ID')
            race = 'sig-market:' + market
        if not isinstance(race, str) or not race.strip() or race != race.strip():
            raise ValueError('Race accounting requires a nonempty race_key')
        result[exchange] = race
    return result


def committed_by_race(holdings, realized, pending, assignments):
    costs, pnl, reserved = (defaultdict(lambda: D(0)) for _ in range(3))
    def race(exchange):
        try:
            return assignments[str(exchange)]
        except KeyError:
            raise RuntimeError('Unmapped race accounting for exchange ' + str(exchange)) from None
    for (exchange, side), value in holdings.items():
        costs[race(exchange)] += value['cost']
    for exchange, value in realized.items():
        if value:
            pnl[race(exchange)] += value
    for row in pending:
        reserved[race(row['market'])] += D(row['amount'])
    return {key: costs[key] + max(D(0), -pnl[key]) + reserved[key]
            for key in set(assignments.values())}


class RaceGroups:
    def __init__(self, config, db=None):
        configured = configured_races(config)
        self.assignments = {}
        if db is not None and db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='race_bindings'").fetchone():
            self.assignments = dict(db.execute('SELECT exchange,race FROM race_bindings'))
        for exchange, race in configured.items():
            if exchange in self.assignments and self.assignments[exchange] != race:
                raise ValueError('Persisted race assignment changed for exchange ' + exchange + '; accounting review required')
            self.assignments[exchange] = race

    def bind(self, db):
        # Additive migration. Keep old bindings when mappings are disabled/removed.
        with db:
            db.execute('CREATE TABLE IF NOT EXISTS race_bindings (exchange TEXT PRIMARY KEY, race TEXT NOT NULL)')
            for exchange, race in self.assignments.items():
                prior = db.execute('SELECT race FROM race_bindings WHERE exchange=?', (exchange,)).fetchone()
                if prior and prior[0] != race:
                    raise RuntimeError('Race assignment changed during initialization')
                db.execute('INSERT OR IGNORE INTO race_bindings VALUES (?,?)', (exchange, race))

    def race(self, exchange):
        try:
            return self.assignments[exchange]
        except KeyError:
            raise RuntimeError('Unmapped race accounting for exchange ' + str(exchange)) from None

    def committed(self, ledger, exchange):
        holdings, realized = ledger.inventory()
        totals = committed_by_race(holdings, realized, ledger.journal.pending(), self.assignments)
        return totals[self.race(exchange)]

    def last_orders(self, journal):
        result = defaultdict(float)
        for row in journal.db.execute('SELECT market,MAX(created) AS last FROM orders GROUP BY market'):
            race = self.race(row['market'])
            result[race] = max(result[race], row['last'])
        return result

    def last_order(self, journal, exchange):
        race = self.race(exchange)
        members = [ex for ex, key in self.assignments.items() if key == race]
        row = journal.db.execute('SELECT MAX(created) FROM orders WHERE market IN (' +
                                 ','.join('?' for _ in members) + ')', members).fetchone()
        return row[0] or 0

    def summary(self, ledger):
        holdings, realized = ledger.inventory()
        totals = committed_by_race(holdings, realized, ledger.journal.pending(), self.assignments)
        return {'open_races': len({self.race(ex) for ex, side in holdings}),
                'open_contracts': len({ex for ex, side in holdings}),
                'race_risk_committed': {race: str(value) for race, value in sorted(totals.items()) if value}}
