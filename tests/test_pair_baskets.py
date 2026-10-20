import copy
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

from election_bot import pair_baskets as pb
from election_bot.active_engine import ActiveEngine
from election_bot.clients import contract_record, fingerprint
from election_bot.demo import fixture
from election_bot.engine import validate_config
from election_bot.state import Journal
from election_bot.strategy import Book, D, Signal

import test_active


def book(bid, ask, size=200):
    return Book.make([(bid, size)], [(ask, size)], venue='SIG')


def configured(enabled=True):
    config, original, refs = fixture()
    config['execution'] = dict(test_active.EXECUTION)
    anchor = config['markets'][0]
    anchor.update(race_key='2026:senate:NH', sig_market_id='2')
    leg = {k: v for k, v in anchor.items() if k != 'contract_fingerprint'}
    leg.update(enabled=False, basket_leg=True, name='nh-senate-republican-basket-leg', sig_market_id='3',
               sig_exchange_id='3', exposure_sign=-1, kalshi_ticker='SENATENH-26-R',
               basket_sig_fingerprint='pinned')
    config['markets'].append(leg)
    config['pair_baskets'] = {'enabled': enabled, 'min_edge': '0.02'}
    return config, original, refs


class SettingsAndPairsTests(unittest.TestCase):
    def test_off_by_default_and_validated(self):
        config, _, _ = configured()
        self.assertFalse(pb.settings({'markets': []})['enabled'])
        self.assertEqual(len(pb.pairs(config)), 1)
        validate_config(config)
        for change, message in ((dict(enabled=True), 'stay disabled'), (dict(exposure_sign=1), 'opposite'),
                                (dict(basket_sig_fingerprint=None), 'pinned'), (dict(office='house'), 'share office')):
            bad = copy.deepcopy(config)
            bad['markets'][-1].update(change)
            with self.assertRaisesRegex(ValueError, message):
                pb.settings(bad)
        for bad in ({'min_edge': '0'}, {'pairs_per_cycle': 0}, {'races': 'x'}, {'other': 1}):
            with self.assertRaises(ValueError):
                pb.settings({'markets': [], 'pair_baskets': bad})


class PlanTests(unittest.TestCase):
    policy = pb.settings({'markets': [], 'pair_baskets': {'min_edge': '0.02'}})

    def test_edge_sizing_and_thinner_leg_first(self):
        plan, reason = pb.plan(book('.57', '.60', 400), book('.475', '.50', 100), self.policy, D(1000), D('.01'))
        self.assertIsNone(reason)
        self.assertEqual(plan['edge'], D('.045'))
        self.assertEqual(plan['prices'], {'anchor': D('.43'), 'leg': D('.525')})
        self.assertEqual(plan['sets'], 50)                         # half of the thinner 100
        self.assertEqual(plan['order'], ['leg', 'anchor'])
        small, _ = pb.plan(book('.57', '.60', 400), book('.475', '.50', 400), self.policy, D(25), D('.01'))
        self.assertEqual(small['sets'], 25)                        # 25 / (.955 + .02)

    def test_blocks_and_break_even(self):
        self.assertEqual(pb.plan(book('.50', '.52'), book('.50', '.52'), self.policy, D(100))[1], 'edge_below_minimum')
        self.assertEqual(pb.plan(Book.make([], [('.5', 1)]), book('.6', '.62'), self.policy, D(100))[1], 'no_yes_bid')
        self.assertEqual(pb.plan(book('.57', '.60'), book('.475', '.50'), self.policy, D(0))[1], 'size_below_one_set')
        self.assertEqual(pb.second_leg_price(D('.43'), D('.55')), D('.55'))
        self.assertEqual(pb.second_leg_price(D('.43'), D('.57')), D('.57'))
        self.assertIsNone(pb.second_leg_price(D('.43'), D('.575')))


class AttributionTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.addCleanup(self.db.close)
        self.db.execute('CREATE TABLE executions (key TEXT PRIMARY KEY, exchange TEXT, action TEXT, side TEXT, '
                        'quantity TEXT, price TEXT, buffer TEXT, at REAL)')
        pb.initialize(self.db)

    def fill(self, key, exchange, quantity, price, basket=True):
        self.db.execute('INSERT INTO executions VALUES (?,?,?,?,?,?,?,?)',
                        (key, exchange, 'buy', 'no', str(quantity), price, '.01', 0))
        if basket:
            self.db.execute('INSERT INTO basket_orders VALUES (?,?,?,?)', (key, 'r', exchange, 'x'))

    def test_locked_unmatched_and_cost(self):
        anchor, leg = {'sig_exchange_id': '2'}, {'sig_exchange_id': '3'}
        self.fill('a', '2', 100, '.43')
        self.fill('pre', '2', 50, '.40', basket=False)             # pre-existing NO is not locked
        self.assertEqual(pb.locked(self.db, '2'), 100)
        self.assertEqual(pb.unmatched(self.db, anchor, leg), ('leg', 100, D('.57')))
        self.fill('b', '3', 60, '.525')
        role, quantity, cap = pb.unmatched(self.db, anchor, leg)
        self.assertEqual((role, quantity), ('leg', 40))
        self.fill('c', '3', 40, '.55')
        self.assertIsNone(pb.unmatched(self.db, anchor, leg))
        self.assertEqual(pb.basket_cost(self.db).quantize(D('.01')), D('96.50'))


class VerifyPairTests(unittest.TestCase):
    def market(self, mid, exchange, party, status='open', settle='2026-11-04T17:00:00.000Z'):
        return {'id': mid, 'title': 't', 'status': status, 'settlementDate': settle,
                'exchanges': [{'id': exchange, 'option': 'YES'}],
                'resolution_tree': {'contract_details': {'resolutionType': 'Party Winner', 'winnerName': party}}}

    def test_identity_rules(self):
        from election_bot.clients import sig_contract_record
        anchor = {'sig_market_id': '1', 'sig_exchange_id': '2', 'exposure_sign': 1}
        leg = {'sig_market_id': '5', 'sig_exchange_id': '6', 'exposure_sign': -1}
        a, l = self.market('1', '2', 'Democratic Party'), self.market('5', '6', 'Republican Party')
        leg['basket_sig_fingerprint'] = fingerprint(sig_contract_record(l, leg))
        self.assertTrue(pb.verify_pair(a, l, anchor, leg))
        for bad_leg, message in ((self.market('5', '6', 'Democratic Party'), 'Party Winner'),
                                 (self.market('5', '6', 'Republican Party', status='closed'), 'not open'),
                                 (self.market('5', '6', 'Republican Party', settle='2026-12-01'), 'different dates|changed')):
            with self.assertRaisesRegex(ValueError, message):
                pb.verify_pair(a, bad_leg, anchor, leg)


class EngineTests(unittest.TestCase):
    def engine(self, enabled=True, leg_prices=('.475', '.50')):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        config, original, refs = configured(enabled)
        sig = test_active.Broker(original)
        anchor = config['markets'][0]
        anchor['contract_fingerprint'] = fingerprint(contract_record(sig.market('2'), anchor, refs.metadata(anchor)))
        sig.prices.update({'2': ('.57', '.60'), '3': leg_prices})
        journal = Journal(Path(temp.name)/'j.db', 'test')
        self.addCleanup(journal.close)
        engine = ActiveEngine(config, sig, refs, journal, temp.name, live=True)
        return engine, sig, journal

    def test_buys_both_no_legs_and_tags_them(self):
        engine, sig, journal = self.engine()
        with patch.object(pb, 'verify_pair', return_value=True):
            engine.pair_basket_cycle(sig.account(), sig.positions())
        legs = [(o['exchangeId'], o['side'], o['action'], o['quantity']) for o in sig.orders.values()]
        self.assertEqual(legs, [('2', 'no', 'buy', 25), ('3', 'no', 'buy', 25)])
        self.assertEqual(pb.locked(journal.db, '2'), 25)
        self.assertEqual(pb.locked(journal.db, '3'), 25)
        self.assertIsNone(pb.unmatched(journal.db, engine.pair_list[0][0], engine.pair_list[0][1]))

    def test_disabled_or_no_edge_does_nothing(self):
        engine, sig, _ = self.engine(enabled=False)
        engine.cycle()
        self.assertFalse(any(o['side'] == 'no' and o['exchangeId'] == '3' for o in sig.orders.values()))
        engine, sig, _ = self.engine(leg_prices=('.40', '.42'))
        with patch.object(pb, 'verify_pair', return_value=True):
            engine.pair_basket_cycle(sig.account(), sig.positions())
        self.assertEqual(sig.orders, {})

    def test_basket_shares_are_not_sold_by_other_routes(self):
        engine, sig, journal = self.engine()
        with patch.object(pb, 'verify_pair', return_value=True):
            engine.pair_basket_cycle(sig.account(), sig.positions())
        sale = Signal('no', D('.45'), 25, D('.4'), D('.05'), 'sell', 'convergence_take_profit')
        detail = {}
        self.assertIsNone(engine.cap_basket_sale(sale, '2', -25, detail))
        self.assertEqual(detail['reason'], 'basket_shares_held_to_settlement')
        self.assertEqual(engine.cap_basket_sale(sale, '2', -30, {}).quantity, 5)

    def test_skips_race_where_yes_is_held(self):
        engine, sig, _ = self.engine()
        sig.inventory['2'] = D(10)
        engine.held = Mock(side_effect=lambda ex, positions: (D(10) if ex == '2' else D(0), D(6)))
        with patch.object(pb, 'verify_pair', return_value=True):
            engine.pair_basket_cycle(sig.account(), sig.positions())
        self.assertEqual(sig.orders, {})


if __name__ == '__main__':
    unittest.main()


class AccountingTests(unittest.TestCase):
    def test_matched_basket_nets_to_zero_exposure_and_leg_gets_fair_marks(self):
        from election_bot.drawdown import DEFAULTS
        from election_bot.risk import Exposure
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        config, original, refs = configured()
        config['limits'].update(net_shares_total='1000', net_shares_per_office='1000')
        config['drawdown_control'] = dict(DEFAULTS, enabled=True, valuation='reference_fair')
        sig = test_active.Broker(original)
        anchor = config['markets'][0]
        anchor['contract_fingerprint'] = fingerprint(contract_record(sig.market('2'), anchor, refs.metadata(anchor)))
        leg = config['markets'][-1]
        leg['contract_fingerprint'] = fingerprint(contract_record(sig.market('3'), leg, refs.metadata(leg)))
        sig.prices.update({'2': ('.57', '.60'), '3': ('.475', '.50')})
        journal = Journal(Path(temp.name)/'j.db', 'test')
        self.addCleanup(journal.close)
        engine = ActiveEngine(config, sig, refs, journal, temp.name, live=True)
        with patch.object(pb, 'verify_pair', return_value=True):
            engine.pair_basket_cycle(sig.account(), sig.positions())
        exposure = Exposure(engine.ledger, config)
        self.assertEqual(exposure.net['total'], 0)                 # NO-D (-25) + NO-R (+25)
        engine.refresh_drawdown()
        marks = {r[0] for r in journal.db.execute('SELECT exchange FROM drawdown_fair_marks')}
        self.assertEqual(marks, {'2', '3'})


class RepairTests(unittest.TestCase):
    def test_second_leg_above_break_even_is_repaired_later(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        config, original, refs = configured()
        sig = test_active.Broker(original)
        anchor = config['markets'][0]
        anchor['contract_fingerprint'] = fingerprint(contract_record(sig.market('2'), anchor, refs.metadata(anchor)))
        sig.prices.update({'2': ('.57', '.60'), '3': ('.475', '.50')})
        journal = Journal(Path(temp.name)/'j.db', 'test')
        self.addCleanup(journal.close)
        engine = ActiveEngine(config, sig, refs, journal, temp.name, live=True)
        original_book, reads = sig.book, {'3': 0}
        def moving(exchange):
            if exchange == '3':
                reads['3'] += 1
                if reads['3'] == 2:                               # moves away right after leg one
                    return book('.42', '.45')
            return original_book(exchange)
        sig.book = moving
        with patch.object(pb, 'verify_pair', return_value=True):
            engine.pair_basket_cycle(sig.account(), sig.positions())
            self.assertEqual([o['exchangeId'] for o in sig.orders.values()], ['2'])
            leg_pair = engine.pair_list[0]
            self.assertEqual(pb.unmatched(journal.db, *leg_pair)[:2], ('leg', 25))
            engine.pair_basket_cycle(sig.account(), sig.positions())   # price back at .475 bid
        self.assertEqual([o['exchangeId'] for o in sig.orders.values()], ['2', '3'])
        self.assertIsNone(pb.unmatched(journal.db, *engine.pair_list[0]))


class UnresolvedLegTests(unittest.TestCase):
    def test_error_after_sending_a_leg_propagates(self):
        from election_bot.clients import APIError
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        config, original, refs = configured()
        sig = test_active.Broker(original)
        anchor = config['markets'][0]
        anchor['contract_fingerprint'] = fingerprint(contract_record(sig.market('2'), anchor, refs.metadata(anchor)))
        sig.prices.update({'2': ('.57', '.60'), '3': ('.475', '.50')})
        journal = Journal(Path(temp.name)/'j.db', 'test')
        self.addCleanup(journal.close)
        engine = ActiveEngine(config, sig, refs, journal, temp.name, live=True)
        sig.order = Mock(side_effect=APIError('GET /orders/1 returned HTTP 503', status=503, method='GET', venue='SIG'))
        with patch.object(pb, 'verify_pair', return_value=True), self.assertRaises(APIError) as caught:
            engine.pair_basket_cycle(sig.account(), sig.positions())
        self.assertEqual(caught.exception.status, 503)          # original error, for supervisor handling
        self.assertEqual(len(sig.orders), 1)                     # no second leg after an unresolved first
        self.assertEqual(len(journal.pending()), 1)              # reservation kept for reconciliation
