import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock

from election_bot.capital_rotation import REASON, Rotation, settings
from election_bot.strategy import Book, D, Signal

STRATEGY = {'max_age_seconds': 15, 'cost_buffer_per_share': '.005', 'max_shares_per_order': 1500,
            'max_reference_spread': '.08', 'max_reference_disagreement': '.08'}


def book(bid, ask, size=3000, venue='SIG'):
    return Book.make([(bid, size)], [(ask, size)], venue=venue)


def rotation(**changes):
    return Rotation({'strategy': STRATEGY, 'capital_rotation': dict({'enabled': True}, **changes)})


def target(edge='.10', ask='.87'):
    return Signal('yes', D(ask), 500, D('.975'), D(edge))


REFS = [book('.95', '.958', 500, 'Kalshi'), book('.95', '.958', 500, 'Polymarket')]   # fair .954


class SettingsTests(unittest.TestCase):
    def test_off_by_default_and_validated(self):
        self.assertFalse(settings({})['enabled'])
        self.assertEqual(settings({})['max_giveup'], D('.02'))
        self.assertEqual(settings({})['min_sale_coins'], D('25'))
        for bad in ({'enabled': 'yes'}, {'max_giveup': '.06'}, {'min_gain': '0'}, {'pending_seconds': 5},
                    {'depth_fraction': '0'}, {'max_shares': 0}, {'min_sale_coins': '-1'}, {'unknown': 1}):
            with self.assertRaises(ValueError):
                settings({'capital_rotation': bad})


class ProposeTests(unittest.TestCase):
    def test_sells_converged_holding_to_fund_better_entry(self):
        r = rotation()
        now = time.time()
        r.note('target', 'race:t', target(), D('1000'), now)
        detail = {}
        sale = r.propose(book('.94', '.95'), REFS, 5000, 'held', 'race:h', 1000, detail, now)
        self.assertEqual((sale.action, sale.side, sale.price, sale.reason), ('sell', 'yes', D('.94'), REASON))
        self.assertEqual(sale.quantity, 1063)                  # per-order coins / bid; need would be 1064
        self.assertEqual(D(detail['giveup_per_share']), D('.014'))
        self.assertGreaterEqual(D(detail['net_gain_per_share']), D('.03'))

    def test_no_holding_sells_the_no_side(self):
        r = rotation()
        r.note('target', 'race:t', target(), D('100'))
        yes_refs = [book('.04', '.05', 500, 'Kalshi'), book('.04', '.05', 500, 'Polymarket')]  # NO fair .955
        sale = r.propose(book('.05', '.06'), yes_refs, -500, 'held', 'race:h', 1000)
        self.assertEqual((sale.side, sale.price, sale.quantity), ('no', D('.94'), 107))   # ceil(100 / .94)

    def test_blocks(self):
        now = time.time()
        cases = [('disabled_or_no_position', rotation(enabled=False), book('.94', '.95'), 5000, {}),
                 ('giveup_above_maximum', rotation(), book('.93', '.94'), 5000, {}),          # 2.4c below fair
                 ('gain_below_minimum', rotation(), book('.94', '.95'), 5000, {'edge': '.02'}),
                 ('size_below_one_share', rotation(), book('.94', '.95', 1), 5000, {}),
                 ('sale_below_minimum', rotation(), book('.94', '.95', 4), 5000, {})]       # 2 shares = 1.88 coins
        for reason, r, sig, held, change in cases:
            r.note('target', 'race:t', target(**change), D('1000'), now)
            detail = {}
            self.assertIsNone(r.propose(sig, REFS, held, 'held', 'race:h', 1000, detail, now), reason)
            self.assertEqual(detail['reason'], reason)
        r = rotation()
        r.note('target', 'race:t', target(), D('1000'), now-1000)                # expired
        detail = {}
        self.assertIsNone(r.propose(book('.94', '.95'), REFS, 5000, 'held', 'race:h', 1000, detail, now))
        self.assertEqual(detail['reason'], 'no_pending_entry')
        r.note('target', 'race:h', target(), D('1000'), now)                     # same race is never sold
        self.assertIsNone(r.propose(book('.94', '.95'), REFS, 5000, 'held', 'race:h', 1000, {}, now))

    def test_sale_proceeds_shrink_every_pending_need(self):
        r = rotation()
        r.note('a', 'race:a', target(), D('500'))
        r.note('b', 'race:b', target(), D('1200'))
        r.sold(D('600'))
        self.assertEqual(set(r.pending), {'b'})
        self.assertEqual(r.pending['b']['need_coins'], D('600'))
        self.assertFalse(r.note('c', 'race:c', target(), D('0')))              # nothing needed: not pending
        self.assertNotIn('c', r.pending)


class EngineTests(unittest.TestCase):
    """End to end: a converged holding is sold, then the blocked entry buys with the proceeds."""
    def engine(self, enabled=True):
        import test_active
        from election_bot.active_engine import ActiveEngine
        from election_bot.clients import contract_record, fingerprint
        from election_bot.demo import fixture
        from election_bot.state import Journal
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        config, original, refs = fixture()
        config['execution'] = dict(test_active.EXECUTION)
        config['capital_rotation'] = {'enabled': enabled, 'min_sale_coins': '10'}  # demo per-order cap is 25 coins
        sig = test_active.Broker(original)
        held = config['markets'][0]
        held['sig_market_id'] = '2'
        held['contract_fingerprint'] = fingerprint(contract_record(sig.market('2'), held, refs.metadata(held)))
        better = dict(held, name='better', sig_market_id='3', sig_exchange_id='3')
        better['contract_fingerprint'] = fingerprint(contract_record(sig.market('3'), better, refs.metadata(better)))
        config['markets'].append(better)
        quotes = {'nh-senate-democratic': ('.95', '.958'), 'better': ('.975', '.985')}
        refs.books = Mock(side_effect=lambda m, md: [book(*quotes[m['name']], 500, 'Kalshi'),
                                                      book(*quotes[m['name']], 500, 'Polymarket')])
        sig.prices.update({'2': ('.94', '.95'), '3': ('.86', '.87')})
        journal = Journal(Path(temp.name)/'j.db', 'test')
        self.addCleanup(journal.close)
        engine = ActiveEngine(config, sig, refs, journal, temp.name, live=True)
        # A 100-share holding bought at .80, out of race cooldown; almost no cash left.
        seed = {'exchangeId': '2', 'side': 'yes', 'action': 'buy', 'quantity': 100, 'price': '.80', 'idempotencyKey': 'seed'}
        journal.reserve(seed, D('81'))
        engine.ledger.record(seed, 100, '.80', '.01')
        journal.db.execute('UPDATE orders SET created=0')
        journal.db.commit()
        sig.inventory['2'] = D(100)
        sig.cash = D('0.5')
        engine.scanner.batch = lambda *args: iter([better, held])   # target noted before the holding
        return engine, sig, journal

    def test_sells_converged_holding_then_buys_better_entry(self):
        engine, sig, journal = self.engine()
        engine.cycle()
        sale = sig.orders[1]
        self.assertEqual((sale['exchangeId'], sale['action'], sale['side']), ('2', 'sell', 'yes'))
        self.assertEqual(sale['quantity'], 26)                       # ceil(24.14 / .94), under 25 coins per order
        self.assertEqual(engine.rotation.pending, {})                 # proceeds covered the need
        engine.cycle()
        buy = sig.orders[2]
        self.assertEqual((buy['exchangeId'], buy['action'], buy['side']), ('3', 'buy', 'yes'))
        kinds = [r[0] for r in journal.db.execute(
            "SELECT kind FROM events WHERE kind LIKE 'capital_rotation%' ORDER BY at")]
        self.assertEqual(kinds[:2], ['capital_rotation_pending', 'capital_rotation_decision'])

    def test_disabled_rotation_never_sells(self):
        engine, sig, _ = self.engine(enabled=False)
        engine.cycle()
        engine.cycle()
        self.assertEqual(sig.orders, {})

    def test_no_rotation_during_election_night(self):
        engine, sig, _ = self.engine()
        engine.election_night.active = lambda now=None: True
        engine.cycle()
        self.assertFalse(any(o['action'] == 'sell' and o['exchangeId'] == '2' for o in sig.orders.values()))
