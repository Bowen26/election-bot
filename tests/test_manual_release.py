import json
from pathlib import Path
import tempfile
import time
import unittest

from election_bot.ledger import Ledger
from election_bot.manual_release import ReleaseRefused, release_unplaced
from election_bot.state import Journal
from election_bot.strategy import D

TID = 'tournament'
FEE = D('0.005')


def iso(t):
    return time.strftime('%Y-%m-%dT%H:%M:%S.000Z', time.gmtime(t))


class FakeSig:
    """SIG reads only: history with incomplete coverage, positions, cash, open orders."""
    tid = TID

    def __init__(self, rows, positions, cash, open_orders=()):
        self.rows, self.positions_list, self.cash, self.open = rows, positions, cash, list(open_orders)

    def read(self, path, params=None):
        assert path == '/orders' and params['exchangeId'] == '932'
        return {'data': list(self.rows), 'coverage': {'complete': False, 'projectedThroughSequence': 1},
                'pagination': {'hasMore': False, 'nextCursor': None}}

    def open_orders(self):
        return self.open

    def positions(self):
        return self.positions_list

    def account(self):
        return {'myBalance': self.cash}


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.journal = Journal(Path(temp.name)/'j.db', 'test')
        self.addCleanup(self.journal.close)
        self.ledger = Ledger(self.journal)
        self.now = time.time()
        # One acknowledged, filled buy of 10 at .80 on exchange 932.
        buy = {'exchangeId': '932', 'side': 'yes', 'action': 'buy', 'quantity': 10, 'price': 0.8,
               'tournamentId': TID, 'idempotencyKey': 'k-buy', 'expirationDate': iso(self.now-7200)}
        self.journal.reserve(buy, D(10)*(D('0.8')+FEE))
        self.journal.response('k-buy', {'orderId': 101})
        self.ledger.record(buy, 10, '0.8', FEE)
        self.sig_row = {'id': 101, 'exchangeId': '932', 'tournamentId': TID, 'open': False, 'action': 'buy',
                        'side': 'yes', 'priceLimit': 0.8, 'quantity': 10, 'quantityFilled': 10,
                        'expirationDate': buy['expirationDate']}
        # The stuck sale: reserved, never acknowledged, expired an hour ago.
        self.stuck = {'exchangeId': '932', 'side': 'yes', 'action': 'sell', 'quantity': 1, 'price': 0.86,
                      'tournamentId': TID, 'idempotencyKey': 'k-stuck', 'expirationDate': iso(self.now-3600)}
        self.journal.reserve(self.stuck, D('0.865'))
        self.cash = D(100000)-D(8)   # starting cash minus the recorded buy

    def sig(self, **changes):
        state = dict(rows=[self.sig_row], positions=[{'exchangeId': '932', 'quantity': 10}], cash=self.cash)
        state.update(changes)
        return FakeSig(state['rows'], state['positions'], state['cash'], state.get('open_orders', ()))

    def release(self, sig, key='k-stuck', now=None):
        return release_unplaced(self.journal, self.ledger, sig, key, D(100000), FEE,
                                now=self.now if now is None else now, sleep=lambda s: None)

    def test_releases_when_every_check_passes(self):
        result = self.release(self.sig())
        self.assertTrue(result['released'])
        self.assertEqual(self.journal.pending(), [])
        row = self.journal.db.execute("SELECT quantity FROM executions WHERE key='k-stuck'").fetchone()
        self.assertEqual(D(row['quantity']), 0)
        event = self.journal.db.execute("SELECT detail FROM events WHERE kind='operator_released_unplaced'").fetchone()
        self.assertEqual(json.loads(event['detail'])['coverage'][0]['complete'], False)

    def test_refusals_change_nothing(self):
        filled = dict(self.sig_row, id=102, action='sell', quantity=1, quantityFilled=1, priceLimit=0.86,
                      expirationDate=self.stuck['expirationDate'])
        cases = {
            'wrong key': (self.sig(), 'other-key', None),
            'not expired long enough': (self.sig(), 'k-stuck', self.now-3600+60),
            'request filled on SIG': (self.sig(rows=[self.sig_row, filled]), 'k-stuck', None),
            'unknown SIG order': (self.sig(rows=[self.sig_row, dict(filled, priceLimit=0.5, quantityFilled=0)]), 'k-stuck', None),
            'filled local order omitted': (self.sig(rows=[]), 'k-stuck', None),
            'positions differ': (self.sig(positions=[{'exchangeId': '932', 'quantity': 9}]), 'k-stuck', None),
            'cash shows a fill': (self.sig(cash=self.cash+D('0.86')), 'k-stuck', None),
            'open orders': (self.sig(open_orders=[{'id': 7}]), 'k-stuck', None),
        }
        for name, (sig, key, now) in cases.items():
            with self.assertRaises(ReleaseRefused, msg=name):
                self.release(sig, key, now)
            self.assertEqual([r['key'] for r in self.journal.pending()], ['k-stuck'], name)
        self.assertIsNone(self.journal.db.execute("SELECT 1 FROM executions WHERE key='k-stuck'").fetchone())

    def test_tolerates_sig_omitting_unfilled_orders_or_listing_the_unfilled_request(self):
        unfilled = {'exchangeId': '932', 'side': 'yes', 'action': 'buy', 'quantity': 30, 'price': 0.76,
                    'tournamentId': TID, 'idempotencyKey': 'k-old', 'expirationDate': iso(self.now-86400)}
        self.journal.db.execute("UPDATE orders SET state='closed' WHERE key='k-stuck'")   # reserve needs no pending
        self.journal.db.commit()
        self.journal.reserve(unfilled, D('22.95'))
        self.journal.response('k-old', {'orderId': 99})
        self.ledger.record(unfilled, 0, 0, FEE)
        self.journal.db.execute("UPDATE orders SET state='pending' WHERE key='k-stuck'")
        self.journal.db.commit()
        listed = dict(self.sig_row, id=104, action='sell', quantity=1, quantityFilled=0, priceLimit=0.86,
                      expirationDate=self.stuck['expirationDate'])
        result = self.release(self.sig(rows=[self.sig_row, listed]))       # SIG omits order 99, lists the request
        self.assertTrue(result['released'])
        self.assertEqual(result['local_unfilled_orders_sig_omits'], ['99'])
        self.assertEqual(result['request_listed_on_sig'], '104')

    def test_dry_run_changes_nothing(self):
        result = release_unplaced(self.journal, self.ledger, self.sig(), 'k-stuck', D(100000), FEE,
                                  now=self.now, sleep=lambda s: None, dry_run=True)
        self.assertTrue(result['dry_run'])
        self.assertEqual([r['key'] for r in self.journal.pending()], ['k-stuck'])

    def test_acknowledged_request_needs_normal_recovery(self):
        self.journal.response('k-stuck', {'orderId': 103})
        with self.assertRaisesRegex(ReleaseRefused, 'acknowledged'):
            self.release(self.sig())
