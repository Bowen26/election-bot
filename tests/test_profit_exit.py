import copy
import json
import time
import unittest
from unittest.mock import Mock, patch

import test_active as active
import test_race_controls as races
from test_exit_study import SETTINGS, EXECUTION, books
from election_bot.active_engine import ActiveEngine, ReservationRejected
from election_bot.engine import validate_config
from election_bot.exit_study import checked_exit, experiment
from election_bot.strategy import Book, D


ENABLED = dict(EXECUTION, profit_target_enabled=True, profit_exit_mode="fixed", reentry_cooldown_seconds=1800)


class ProfitTargetTests(unittest.TestCase):
    def evaluate(self, price='.65', cost=60, basis='.60', held=100, cap=None,
                 execution=None, book=None, refs=None, per_order=50, settings=None):
        default_book, default_refs = books(price=price)
        book = book or default_book
        refs = refs or default_refs
        detail = {}
        signal = checked_exit(book, refs, settings or SETTINGS, execution or ENABLED,
                              D(held), D(cost), D(per_order),
                              lambda side, qty: D(basis)*qty, detail, cap)
        return signal, detail

    def test_profitable_bid_can_sell_before_reference_convergence(self):
        old, _ = self.evaluate(execution=EXECUTION)
        self.assertIsNone(old)
        signal, detail = self.evaluate()
        self.assertEqual((signal.reason, signal.action, signal.price), ('profit_target', 'sell', D('.65')))
        self.assertEqual(signal.quantity, 76)
        self.assertEqual(detail['routes']['profit_target']['net_profit_per_share_fifo'], '0.04')

    def test_exact_buffered_threshold_and_one_tick_below(self):
        self.assertEqual(self.evaluate(price='.63')[0].reason, 'profit_target')
        signal, detail = self.evaluate(price='.625')
        self.assertIsNone(signal)
        self.assertEqual(detail['reason'], 'profit_target_below_minimum')

    def test_fifo_rejects_profitable_average_with_expensive_old_lots(self):
        signal, detail = self.evaluate(price='.65', basis='.64')
        self.assertIsNone(signal)
        self.assertEqual(detail['reason'], 'fifo_profit_below_minimum')
        self.assertFalse(detail['routes']['profit_target']['fifo_profit_passed'])

    def test_no_positions_are_native_sales(self):
        b, refs = books(price='.65')
        signal, _ = self.evaluate(held=-100, book=b.complement(), refs=[r.complement() for r in refs])
        self.assertEqual((signal.side, signal.action, signal.price), ('no', 'sell', D('.65')))

    def test_each_sizing_limit_and_zero_headroom(self):
        b, refs = books(price='.65'); b.bids[0] = (D('.65'), D(4))
        self.assertEqual(self.evaluate(book=b, refs=refs)[0].quantity, 4)
        self.assertEqual(self.evaluate(held=3, cost='1.8')[0].quantity, 3)
        self.assertEqual(self.evaluate(cap=D(2))[0].quantity, 2)
        self.assertIsNone(self.evaluate(cap=D(0))[0])
        self.assertEqual(self.evaluate(per_order=1)[0].quantity, 1)
        self.assertEqual(self.evaluate(settings=dict(SETTINGS,max_shares_per_order=7))[0].quantity, 7)

    def test_small_external_depth_does_not_limit_SIG_profit_sale(self):
        b, refs = books(price='.65', bids=1, asks=1)
        self.assertEqual(self.evaluate(book=b, refs=refs)[0].quantity, 76)

    def test_stale_reference_SIG_spread_and_disagreement_still_block(self):
        for failure in ('sig_stale','reference_stale','spread','disagreement'):
            b, refs = books(price='.65')
            if failure == 'sig_stale': b.source_at -= 100
            if failure == 'reference_stale': refs[0].source_at -= 100
            if failure == 'spread': refs[0] = Book.make([('.5',100)],[('.8',100)])
            if failure == 'disagreement': refs[0] = Book.make([('.4',100)],[('.42',100)])
            with self.subTest(failure=failure), self.assertRaises(ValueError):
                self.evaluate(book=b,refs=refs)

    def test_disabled_sells_and_flat_inventory_never_exit(self):
        self.assertIsNone(self.evaluate(execution=dict(ENABLED,sell_enabled=False))[0])
        self.assertIsNone(self.evaluate(held=0,cost=0)[0])

    def test_existing_overpriced_exit_keeps_priority(self):
        b, refs = books(price='.80', asks=100)
        self.assertEqual(self.evaluate(book=b,refs=refs)[0].reason,'overpriced_exit')

    def test_existing_convergence_exit_keeps_priority(self):
        b, refs = books(price='.745', asks=100)
        self.assertEqual(self.evaluate(book=b,refs=refs)[0].reason,'convergence_take_profit')

    def test_study_does_not_relabel_profit_target_as_legacy(self):
        b, refs = books(price='.65')
        signal, detail = self.evaluate(book=b,refs=refs)
        result = experiment(b,refs,SETTINGS,ENABLED,100,60,50,lambda side,q:D('.6')*q,signal,detail)
        self.assertEqual(signal.reason,'profit_target')
        self.assertTrue(all(r['candidate'] is None for r in result['decisions']))
        self.assertEqual(result['decisions'][0]['check']['reason'],'reference_ask_depth')
        self.assertEqual(detail['reason'],'profit_target')


class ProfitEngineTests(unittest.TestCase):
    def setUp(self):
        self.fixture = active.ActiveTests()
        self.fixture.setUp(); self.addCleanup(self.fixture.doCleanups)
        f = self.fixture
        self.config,self.sig,self.refs,self.engine,self.journal = f.config,f.sig,f.refs,f.engine,f.journal
        self.engine.execution.update(profit_target_enabled=True,profit_exit_mode="fixed",reentry_cooldown_seconds=1800)
        self.mapping = self.config['markets'][0]

    def buy(self):
        self.engine.cycle(); self.fixture.age_orders()
        self.assertEqual(self.sig.orders[1]['action'],'buy')

    def test_live_broker_profit_sell_frees_capital_blocks_rebuy_and_allows_more_sells(self):
        self.buy(); old_cost=self.engine.ledger.committed(); daily=self.journal.used(today=True)
        self.sig.prices['2']=('.65','.66')
        self.engine.cycle()
        self.assertEqual(self.sig.orders[2]['action'],'sell')
        self.assertLess(self.engine.ledger.committed(),old_cost)
        self.assertGreater(D(self.engine.ledger.summary()['realized_pnl_after_buffers']),0)
        self.assertEqual(self.journal.used(today=True),daily)
        self.fixture.age_orders()
        self.sig.prices['2']=('.55','.60')
        self.engine.cycle()
        self.assertEqual(len(self.sig.orders),2)  # Old entry opportunity is suppressed.
        self.sig.prices['2']=('.65','.66')
        self.engine.cycle()  # Remaining inventory can still exit during reentry wait.
        self.assertEqual(self.sig.orders[3]['action'],'sell')
        self.assertEqual(self.engine.ledger.held('2')[0],0)

    def test_preflight_profit_disappears_so_no_sale_is_sent(self):
        self.buy()
        self.sig.book=Mock(side_effect=[Book.make([('.65',200)],[('.66',200)]),
                                       Book.make([('.62',200)],[('.69',200)])])
        self.engine.cycle()
        self.assertEqual(len(self.sig.orders),1)
        self.assertFalse(self.journal.pending())

    def test_news_pause_blocks_profitable_sale(self):
        self.buy(); self.sig.prices['2']=('.65','.66')
        self.engine.news=Mock();self.engine.news.drain.return_value={};self.engine.news.block_reason.return_value='News pause'
        self.engine.cycle()
        self.assertEqual(len(self.sig.orders),1)

    def test_sale_and_reentry_wait_survive_restart(self):
        self.buy();self.sig.prices['2']=('.65','.66');self.engine.cycle();self.fixture.age_orders()
        again=ActiveEngine(self.config,self.sig,self.refs,self.journal,self.fixture.temp.name,live=True)
        self.assertGreater(again.reentry_wait('2'),1700)
        self.sig.prices['2']=('.55','.60');again.cycle()
        self.assertEqual(len(self.sig.orders),2)
        with patch('election_bot.active_engine.time.time',return_value=time.time()+1801):
            self.assertEqual(again.reentry_wait('2'),0)

    def test_atomic_fifo_recheck_rejects_changed_sale_basis(self):
        self.buy()
        p={'idempotencyKey':'profit-sale','exchangeId':'2','side':'yes','action':'sell','quantity':10,'price':'.65'}
        with patch.object(self.engine.ledger,'sale_basis',return_value=D('6.4')):
            with self.assertRaisesRegex(ReservationRejected,'FIFO profit'):
                self.engine.reserve_order(p,D('6.6'),self.sig.account(),self.sig.positions(),exit_reason='profit_target')
        self.assertFalse(self.journal.pending())

    def test_invalid_feature_settings_rejected(self):
        for change in ({'profit_target_enabled':'true'}, {'reentry_cooldown_seconds':True},
                       {'reentry_cooldown_seconds':-1}, {'reentry_cooldown_seconds':0},
                       {'reentry_cooldown_seconds':59}, {'reentry_cooldown_seconds':86401}):
            c=copy.deepcopy(self.config);c['execution'].update(change)
            with self.subTest(change=change),self.assertRaises(ValueError):validate_config(c)


class RaceReentryTests(unittest.TestCase):
    def setUp(self):
        self.f=races.RaceControlsTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.f.config['execution'].update(profit_target_enabled=True,profit_exit_mode="fixed",reentry_cooldown_seconds=1800)

    def test_disabled_sibling_sale_blocks_new_buys_under_atomic_guard(self):
        f=self.f;f.fill('buy','3',10);f.fill('sell','3',2,action='sell')
        self.assertGreater(f.engine.reentry_wait('2'),1700)
        with self.assertRaisesRegex(ReservationRejected,'post-sale'):
            f.engine.reserve_order(f.payload(),D('5.1'),f.sig.account(),f.sig.positions())
        self.assertFalse(f.journal.pending())

    def test_zero_fill_sale_does_not_start_wait(self):
        f=self.f;f.fill('zero','3',0,action='sell')
        self.assertEqual(f.engine.reentry_wait('2'),0)

    def test_persisted_binding_keeps_wait_when_sibling_removed(self):
        f=self.f;f.fill('buy','3',10);f.fill('sell','3',10,action='sell');f.config['markets'].pop()
        again=ActiveEngine(f.config,f.sig,f.refs,f.journal,f.temp.name,live=True)
        self.assertGreater(again.reentry_wait('2'),1700)
