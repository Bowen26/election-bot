import copy
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from election_bot.active_engine import ActiveEngine
from election_bot.clients import APIError, contract_record, fingerprint
from election_bot.runner import run_loop
from election_bot.demo import fixture
from election_bot.ledger import Ledger
from election_bot.performance import report
from election_bot.state import Journal
from election_bot.strategy import Book, D, choose_exit

EXECUTION = {'enabled': True, 'sell_enabled': True, 'max_orders_per_cycle': 4,
             'metadata_cache_seconds': 900, 'batch_pause_seconds': 1,
             'exit_edge': '0.02', 'take_profit_min': '0.02'}


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'journal.db'
        self.journal = Journal(self.path, 'test')
        self.addCleanup(self.journal.close)
        self.ledger = Ledger(self.journal)

    def fill(self, key, action, quantity, price, side='yes', at=None):
        payload = {'idempotencyKey': key, 'exchangeId': '2', 'action': action, 'side': side,
                   'quantity': quantity, 'price': price}
        self.journal.reserve(payload, D(quantity) * (D(price) + D('.01')))
        self.ledger.record(payload, quantity, price, '.01', at)
        return payload

    def test_profit_releases_cost_without_expanding_capital_pool(self):
        self.fill('buy', 'buy', 100, '.5')
        self.assertEqual(self.ledger.committed(), 51)
        self.fill('sell', 'sell', 100, '.7')
        self.assertEqual(self.ledger.committed(), 0)
        self.assertEqual(self.ledger.summary()['realized_pnl_after_buffers'], '18.00')
        self.assertEqual(self.journal.used(today=True), 51)  # Daily buys do not recycle.

    def test_loss_still_consumes_risk_pool_after_flattening(self):
        self.fill('buy', 'buy', 100, '.5')
        self.fill('sell', 'sell', 100, '.4')
        self.assertEqual(self.ledger.committed(), 12)

    def test_fifo_partial_sell_and_no_side(self):
        self.fill('a', 'buy', 10, '.3', 'no')
        self.fill('b', 'buy', 10, '.5', 'no')
        self.fill('c', 'sell', 15, '.6', 'no')
        self.assertEqual(self.ledger.held('2'), (D(-5), D('2.55')))
        self.assertEqual(self.ledger.summary()['realized_pnl_after_buffers'], '3.20')

    def test_restart_and_duplicate_completion_do_not_release_twice(self):
        self.fill('buy', 'buy', 10, '.5')
        payload = self.fill('sell', 'sell', 5, '.7')
        again = Ledger(self.journal)
        again.record(payload, 5, '.7', '.01')
        self.assertEqual(again.held('2'), (D(5), D('2.55')))
        self.assertEqual(again.summary()['realized_pnl_after_buffers'], '0.90')

    def test_unknown_pending_keeps_reservation(self):
        self.fill('buy', 'buy', 10, '.5')
        self.journal.reserve({'idempotencyKey': 'pending', 'exchangeId': '2'}, 20)
        self.assertEqual(self.ledger.committed(), D('25.1'))

    def test_markout_uses_full_depth_and_buffers_once(self):
        now = time.time()
        self.fill('buy', 'buy', 10, '.5', at=now-3601)
        book = Book.make([('.7', 3), ('.6', 7)], [('.8', 10)])
        self.ledger.observe('2', book, 15, now)
        self.ledger.observe('2', book, 15, now+1)
        rows = self.journal.db.execute('SELECT * FROM markouts').fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(D(rows[0]['pnl']), D('1.1'))

    def test_thin_book_never_values_unavailable_exit(self):
        now = time.time()
        self.fill('buy', 'buy', 10, '.5', at=now-3601)
        book = Book.make([('.7', 3)], [('.8', 10)])
        self.ledger.observe('2', book, 15, now)
        valuation = self.journal.db.execute('SELECT * FROM valuations').fetchone()
        self.assertIsNone(valuation['pnl'])
        self.assertEqual(self.journal.db.execute('SELECT COUNT(*) FROM markouts').fetchone()[0], 0)
        self.ledger.observe('2', book, 15, now+1000)
        row = self.journal.db.execute('SELECT * FROM markouts').fetchone()
        self.assertEqual(row['reason'], 'Observation window missed')

    def test_performance_report_is_read_only_and_labels_estimates(self):
        self.fill('buy', 'buy', 10, '.5')
        result = report(self.path)
        self.assertEqual(result['open_races'], 1)
        self.assertIn('estimates', result['cost_note'])


class Broker:
    tid = 'offline-demo-tournament'

    def __init__(self, original):
        self.original = original
        self.orders = {}
        self.inventory = {}
        self.prices = {}
        self.cash = D(100000)
        self.partial = None
        self.convert = False
        self.timeout = False
        self.cancelled = []

    def account(self):
        return dict(self.original.account(), myBalance=self.cash)

    def open_orders(self):
        return []

    def positions(self):
        return [{'exchangeId': e, 'quantity': q, 'settled': False} for e, q in self.inventory.items()]

    def market(self, mid):
        return dict(self.original.market(mid), id=str(mid), exchanges=[{'id': str(mid), 'option': 'Yes'}])

    def book(self, exchange):
        bid, ask = self.prices.get(exchange, ('.55', '.60'))
        return Book.make([(bid, 200)], [(ask, 200)])

    def place(self, payload):
        oid = len(self.orders)+1
        filled = min(payload['quantity'], self.partial if self.partial is not None else payload['quantity'])
        action, side = payload['action'], payload['side']
        if self.convert:
            action, side = 'buy', 'no' if side == 'yes' else 'yes'
        order = dict(payload, id=oid, priceLimit=payload['price'], quantityFilled=filled,
                     action=action, side=side, open=False,
                     createdAt=datetime.now(timezone.utc).isoformat())
        self.orders[oid] = order
        direction = 1 if side == 'yes' else -1
        self.inventory[payload['exchangeId']] = self.inventory.get(payload['exchangeId'], D(0)) + (
            filled * direction * (1 if action == 'buy' else -1))
        self.cash += filled * D(payload['price']) * (-1 if action == 'buy' else 1)
        if self.timeout:
            raise TimeoutError('Acknowledgement lost')
        return {'orderId': oid, 'open': False, 'quantityTraded': filled,
                'action': action, 'side': side, 'totalCost': filled*payload['price']}

    def order(self, oid):
        return self.orders[oid]

    def cancel(self, oid):
        self.cancelled.append(oid)
        self.orders[oid]['open'] = False

    def fills(self, oid):
        o = self.orders[oid]
        return {'orderId': oid, 'exchangeId': o['exchangeId'], 'tournamentId': self.tid,
                'coverage': {'complete': True}, 'totalQuantityFilled': o['quantityFilled']*(1 if o['side']=='yes' else -1),
                'avgFillPrice': o['priceLimit'], 'data': [{'filledAt': o['createdAt']}]}


class ActiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config, original, self.refs = fixture()
        self.config['execution'] = dict(EXECUTION)
        self.sig = Broker(original)
        mapping = self.config['markets'][0]
        mapping['sig_market_id'] = '2'
        mapping['contract_fingerprint'] = fingerprint(contract_record(self.sig.market('2'), mapping,
                                                                      self.refs.metadata(mapping)))
        self.journal = Journal(Path(self.temp.name) / 'journal.db', 'test')
        self.addCleanup(self.journal.close)
        self.engine = ActiveEngine(self.config, self.sig, self.refs, self.journal, self.temp.name, live=True)

    def age_orders(self):
        self.journal.db.execute('UPDATE orders SET created=?', (time.time()-1000,))
        self.journal.db.commit()

    def test_buy_sell_and_capital_reuse_with_realized_pnl(self):
        self.engine.cycle()
        self.assertEqual(len(self.sig.orders), 1)
        self.age_orders()
        self.sig.prices['2'] = ('.80', '.85')
        self.engine.cycle()
        self.assertEqual(self.sig.orders[2]['action'], 'sell')
        self.assertLess(self.engine.ledger.committed(), D('24.4'))
        self.assertGreater(D(self.engine.ledger.summary()['realized_pnl_after_buffers']), 0)

    def test_near_entry_priority_does_not_relax_trade_threshold(self):
        self.sig.prices['2'] = ('.67', '.69')
        self.engine.cycle()
        name = self.config['markets'][0]['name']
        self.assertIn('near_entry', self.engine.scan_hints()[name]['reasons'])
        self.assertFalse(self.sig.orders)

    def test_owned_positions_and_order_cooldown_feed_scheduler(self):
        self.engine.cycle()
        name = self.config['markets'][0]['name']
        hint = self.engine.scan_hints()[name]
        self.assertIn('owned_position', hint['reasons'])
        self.assertGreater(hint['not_before'], time.time())
        self.engine.execution['priority_scanning'] = False
        self.assertIsNone(self.engine.scan_hints())

    def test_cooldown_retains_news_and_does_not_count_as_quote(self):
        self.engine.cycle()
        name = self.config['markets'][0]['name']
        self.engine.pending_news[name] = [{'event_id': 1}]
        before = self.journal.db.execute("SELECT COUNT(*) FROM events WHERE kind='scan_quote'").fetchone()[0]
        self.engine.cycle()
        self.assertIn(name, self.engine.pending_news)
        self.assertEqual(self.journal.db.execute("SELECT COUNT(*) FROM events WHERE kind='scan_quote'").fetchone()[0], before)

    def test_final_preflight_is_not_double_counted_as_quote_visit(self):
        self.engine.cycle()
        self.assertEqual(self.journal.db.execute("SELECT COUNT(*) FROM events WHERE kind='decision'").fetchone()[0], 2)
        self.assertEqual(self.journal.db.execute("SELECT COUNT(*) FROM events WHERE kind='scan_quote'").fetchone()[0], 1)
        stats = report(Path(self.temp.name)/'journal.db')['scan_performance_last_24h']['policies']['priority_v1']
        self.assertEqual(stats['successful_quote_checks'], 1)
        self.assertEqual(stats['quote_intervals']['samples'], 0)

    def test_reference_failure_invalidates_interest_and_is_not_successful_quote(self):
        self.sig.prices['2'] = ('.67', '.69')
        self.engine.cycle()
        name = self.config['markets'][0]['name']
        self.refs.books = Mock(side_effect=APIError('Temporary failure', status=503, method='GET'))
        self.engine.cycle()
        self.assertEqual(self.engine.scan_hints()[name]['score'], 0)
        self.assertEqual(self.journal.db.execute("SELECT COUNT(*) FROM events WHERE kind='scan_visit'").fetchone()[0], 2)
        self.assertEqual(self.journal.db.execute("SELECT COUNT(*) FROM events WHERE kind='scan_quote'").fetchone()[0], 1)

    def test_priority_toggle_requires_boolean(self):
        from election_bot.engine import validate_config
        self.config['execution']['priority_scanning'] = 'false'
        with self.assertRaisesRegex(ValueError, 'priority_scanning'):
            validate_config(self.config)

    def test_stale_reference_identifies_venue_and_blocks_owned_exit(self):
        self.engine.cycle()
        self.age_orders()
        original = self.refs.books
        def stale(mapping, metadata):
            books = original(mapping, metadata)
            books[1].source_at = time.time()-60
            return books
        self.refs.books = stale
        self.engine.cycle()
        detail = json.loads(self.journal.db.execute("SELECT detail FROM events WHERE kind='quote_diagnostics' ORDER BY at DESC LIMIT 1").fetchone()[0])
        self.assertEqual(detail['phase'], 'scan')
        failed = [b for b in detail['books'] if b['issues']]
        self.assertEqual([b['venue'] for b in failed], ['Polymarket'])
        self.assertIn('source_stale', failed[0]['issues'])
        blocker = json.loads(self.journal.db.execute("SELECT detail FROM events WHERE kind='exit_blocked' ORDER BY at DESC LIMIT 1").fetchone()[0])
        self.assertEqual(blocker['reason'], 'quote_validation')
        self.assertEqual(len(self.sig.orders), 1)

    def test_exit_diagnostics_cover_disabled_and_inventory_mismatch(self):
        self.engine.cycle()
        self.age_orders()
        self.engine.execution['sell_enabled'] = False
        self.sig.prices['2'] = ('.80', '.85')
        self.engine.cycle()
        detail = json.loads(self.journal.db.execute("SELECT detail FROM events WHERE kind='decision' ORDER BY at DESC LIMIT 1").fetchone()[0])
        self.assertEqual(detail['exit_check']['reason'], 'selling_disabled')
        self.engine.execution['sell_enabled'] = True
        self.sig.inventory['2'] -= 1
        self.engine.cycle()
        blocker = json.loads(self.journal.db.execute("SELECT detail FROM events WHERE kind='exit_blocked' ORDER BY at DESC LIMIT 1").fetchone()[0])
        self.assertEqual(blocker['reason'], 'inventory_mismatch')
        self.assertEqual(len(self.sig.orders), 1)

    def test_sell_scan_and_preflight_diagnostics_are_separate(self):
        self.engine.cycle()
        self.age_orders()
        self.sig.prices['2'] = ('.80', '.85')
        self.engine.cycle()
        summary = report(Path(self.temp.name)/'journal.db')['exit_diagnostics_last_24h']['by_phase']
        self.assertEqual(summary['scan']['statuses'], {'eligible': 1})
        self.assertEqual(summary['preflight']['statuses'], {'eligible': 1})
        self.assertEqual(self.sig.orders[2]['action'], 'sell')

    def test_open_orders_and_news_are_not_evaluated_exit_guards(self):
        self.engine.cycle()
        self.age_orders()
        self.sig.open_orders = Mock(return_value=[{'id': 'manual'}])
        self.engine.cycle()
        self.sig.open_orders.return_value = []
        news = Mock()
        news.drain.return_value = {}
        news.block_reason.return_value = 'Risk headline'
        self.engine.news = news
        self.engine.cycle()
        summary = report(Path(self.temp.name)/'journal.db')['exit_diagnostics_last_24h']['by_phase']['scan']
        self.assertEqual(summary['statuses'], {'not_evaluated': 2})
        self.assertEqual(summary['reasons'], {'open_orders': 1, 'news_pause': 1})
        self.assertEqual(len(self.sig.orders), 1)

    def test_due_measurement_works_without_reference_feeds_or_new_orders(self):
        self.engine.cycle()
        self.journal.db.execute('UPDATE executions SET at=?', (time.time()-3601,))
        self.journal.db.commit()
        self.sig.place = Mock(wraps=self.sig.place)
        self.sig.cancel = Mock(wraps=self.sig.cancel)
        self.refs.books = Mock(side_effect=APIError('External feed down', status=503, method='GET'))
        self.engine.cycle()
        row = self.journal.db.execute('SELECT * FROM markouts WHERE horizon=3600').fetchone()
        self.assertIsNotNone(row['pnl'])
        self.sig.place.assert_not_called()
        self.sig.cancel.assert_not_called()

    def test_observation_failure_is_retried_without_trading_and_stop_is_respected(self):
        self.engine.cycle()
        self.journal.db.execute('UPDATE executions SET at=?', (time.time()-3601,))
        self.journal.db.commit()
        original = self.sig.book
        self.sig.book = Mock(side_effect=APIError('503', status=503, method='GET'))
        self.engine.observe_due()
        self.assertIsNone(self.journal.db.execute('SELECT * FROM markouts WHERE horizon=3600').fetchone())
        self.sig.book = Mock(wraps=original)
        (Path(self.temp.name)/'STOP').touch()
        self.engine.observe_due()
        self.sig.book.assert_not_called()
        (Path(self.temp.name)/'STOP').unlink()
        self.engine.observe_due()
        self.assertIsNotNone(self.journal.db.execute('SELECT pnl FROM markouts WHERE horizon=3600').fetchone()[0])
        self.assertEqual(len(self.sig.orders), 1)

    def test_observation_rate_limit_propagates_to_recovery_loop(self):
        self.engine.cycle()
        self.journal.db.execute('UPDATE executions SET at=?', (time.time()-3601,))
        self.journal.db.commit()
        self.sig.book = Mock(side_effect=APIError('429', status=429, method='GET', retry_after=30))
        with self.assertRaises(APIError):
            self.engine.observe_due()

    def test_no_side_sale_closes_owned_no(self):
        self.sig.prices['2'] = ('.85', '.90')
        self.engine.cycle()
        self.assertEqual(self.sig.orders[1]['side'], 'no')
        self.age_orders()
        self.sig.prices['2'] = ('.65', '.68')  # NO bid .32 above external NO ask .27.
        self.engine.cycle()
        self.assertEqual((self.sig.orders[2]['action'], self.sig.orders[2]['side']), ('sell', 'no'))

    def test_partial_exit_releases_only_confirmed_quantity(self):
        self.engine.cycle()
        before = self.engine.ledger.held('2')[0]
        self.age_orders()
        self.sig.prices['2'] = ('.8', '.85')
        self.sig.partial = 3
        self.engine.cycle()
        self.assertEqual(self.engine.ledger.held('2')[0], before-3)

    def test_manual_inventory_change_blocks_sale(self):
        self.engine.cycle()
        self.age_orders()
        self.sig.inventory['2'] -= 5
        self.sig.prices['2'] = ('.8', '.85')
        self.engine.cycle()
        self.assertEqual(len(self.sig.orders), 1)

    def test_canonicalized_sell_halts_and_keeps_pending(self):
        self.engine.cycle()
        self.age_orders()
        self.sig.prices['2'] = ('.8', '.85')
        self.sig.convert = True
        with self.assertRaisesRegex(RuntimeError, 'canonicalized'):
            self.engine.cycle()
        self.assertEqual(len(self.journal.pending()), 1)

    def test_unknown_submission_is_not_repeated(self):
        self.sig.timeout = True
        with self.assertRaises(TimeoutError):
            self.engine.cycle()
        with self.assertRaisesRegex(RuntimeError, 'Unknown order'):
            self.engine.cycle()
        self.assertEqual(len(self.sig.orders), 1)

    def test_read_outages_reconcile_before_resuming_without_duplicate_order(self):
        # Status and fill reads fail on successive cycles after one accepted buy.
        original_order, original_fills = self.sig.order, self.sig.fills
        counts = {'order': 0, 'fills': 0}
        def flaky(name, original, oid):
            counts[name] += 1
            if counts[name] == 1:
                raise APIError('Temporarily unavailable', status=503, method='GET')
            return original(oid)
        self.sig.order = lambda oid: flaky('order', original_order, oid)
        self.sig.fills = lambda oid: flaky('fills', original_fills, oid)
        pauses = []
        def pause(engine, seconds, news=None):
            pauses.append(seconds)
            self.assertEqual(len(self.sig.orders), 1)
            if seconds > 1:
                self.assertEqual(len(self.journal.pending()), 1)
                self.assertEqual(self.journal.db.execute('SELECT COUNT(*) FROM executions').fetchone()[0], 0)
                self.assertGreater(self.engine.ledger.committed(), 0)
                return True
            return False
        with patch('election_bot.runner.wait', side_effect=pause):
            run_loop(self.engine)
        self.assertEqual(pauses, [5, 10, 1])
        self.assertFalse(self.journal.pending())
        self.assertEqual(self.journal.db.execute('SELECT COUNT(*) FROM executions').fetchone()[0], 1)
        self.assertEqual(self.engine.ledger.held('2')[0], self.sig.inventory['2'])

    def test_stale_fill_projection_does_not_release_budget(self):
        original = self.sig.fills
        self.sig.fills = lambda oid: dict(original(oid), totalQuantityFilled=0)
        with self.assertRaisesRegex(RuntimeError, 'totals disagree'):
            self.engine.cycle()
        self.assertTrue(self.journal.pending())

    def test_multiple_sequential_orders_refresh_cash_and_inventory(self):
        base = self.config['markets'][0]
        self.config['markets'] = []
        for i in range(2, 7):
            m = dict(base, name=str(i), sig_market_id=str(i), sig_exchange_id=str(i))
            m['contract_fingerprint'] = fingerprint(contract_record(self.sig.market(str(i)), m, self.refs.metadata(m)))
            self.config['markets'].append(m)
        self.engine = ActiveEngine(self.config, self.sig, self.refs, self.journal, self.temp.name, live=True)
        self.engine.cycle()
        self.assertEqual(len(self.sig.orders), 4)
        self.assertFalse(self.journal.pending())
        self.assertLessEqual(self.journal.used(), D(self.config['limits']['daily']))

    def test_rule_change_on_final_check_blocks_cached_signal(self):
        m = self.config['markets'][0]
        self.engine.metadata(m)
        original = self.sig.market
        self.sig.market = lambda mid: dict(original(mid), title='Changed')
        self.engine.cycle()
        self.assertFalse(self.sig.orders)

    def test_metadata_cache_reduces_reads_when_no_trade(self):
        self.sig.prices['2'] = ('.72', '.74')
        self.sig.market = Mock(wraps=self.sig.market)
        self.engine.cycle()
        self.engine.cycle()
        self.assertEqual(self.sig.market.call_count, 1)

    def test_paper_buy_sell_never_uses_broker_write_methods(self):
        self.engine.live = False
        self.engine.cycle()
        self.age_orders()
        self.sig.prices['2'] = ('.80', '.85')
        self.engine.cycle()
        self.assertFalse(self.sig.orders)
        self.assertGreater(D(self.engine.ledger.summary()['realized_pnl_after_buffers']), 0)

    def test_legacy_buy_import_is_idempotent(self):
        payload = {'idempotencyKey': 'legacy', 'exchangeId': '2', 'action': 'buy', 'side': 'yes',
                   'quantity': 10, 'price': .6, 'tournamentId': self.sig.tid}
        self.journal.reserve(payload, D('6.1'))
        self.journal.response('legacy', self.sig.place(payload))
        self.journal.complete('legacy', D('6.1'))
        self.engine.sync_history()
        self.engine.sync_history()
        self.assertEqual(self.engine.ledger.held('2'), (D(10), D('6.1')))
        self.assertEqual(len(self.sig.orders), 1)

    def test_daily_cap_does_not_block_reducing_inventory(self):
        self.engine.cycle()
        self.config['limits']['daily'] = str(self.journal.used(today=True))
        self.age_orders()
        self.sig.prices['2'] = ('.8', '.85')
        self.engine.cycle()
        self.assertEqual(self.sig.orders[2]['action'], 'sell')

    def test_fresh_position_change_prevents_over_sell(self):
        self.engine.cycle()
        self.age_orders()
        self.sig.prices['2'] = ('.8', '.85')
        old_positions = self.sig.positions()
        self.sig.positions = Mock(side_effect=[old_positions, []])
        self.engine.cycle()
        self.assertEqual(len(self.sig.orders), 1)

    def test_stop_during_quote_fetch_prevents_submission(self):
        original = self.sig.book
        def stopping_book(exchange):
            (Path(self.temp.name) / 'STOP').touch()
            return original(exchange)
        self.sig.book = stopping_book
        self.engine.cycle()
        self.assertFalse(self.sig.orders)

    def test_pending_sale_does_not_free_capital_until_fill_verified(self):
        self.engine.cycle()
        self.age_orders()
        before = self.engine.ledger.committed()
        self.sig.prices['2'] = ('.8', '.85')
        self.sig.fills = Mock(side_effect=RuntimeError('Fill reporting unavailable'))
        with self.assertRaises(RuntimeError):
            self.engine.cycle()
        self.assertTrue(self.journal.pending())
        self.assertGreaterEqual(self.engine.ledger.committed(), before)

    def test_news_flag_appearing_during_preflight_prevents_submission(self):
        news = Mock()
        news.drain.return_value = {}
        news.block_reason.side_effect = [None, 'candidate withdrawn']
        self.engine.news = news
        self.engine.cycle()
        self.assertFalse(self.sig.orders)

    def test_take_profit_checks_fifo_shares_not_only_average_cost(self):
        for key, qty, price in [('expensive', 30, '.9'), ('cheap', 70, '.3')]:
            payload = {'idempotencyKey': key, 'exchangeId': '2', 'action': 'buy',
                       'side': 'yes', 'quantity': qty, 'price': price}
            self.journal.reserve(payload, D(qty)*(D(price)+D('.01')))
            self.engine.ledger.record(payload, qty, price, '.01')
        self.sig.inventory['2'] = D(100)
        self.age_orders()
        self.sig.prices['2'] = ('.745', '.75')
        self.engine.cycle()
        self.assertFalse(self.sig.orders)
        detail = json.loads(self.journal.db.execute("SELECT detail FROM events WHERE kind='decision' ORDER BY at DESC LIMIT 1").fetchone()[0])
        self.assertEqual(detail['exit_check']['reason'], 'fifo_profit_below_minimum')
        self.assertFalse(detail['exit_check']['routes']['convergence_take_profit']['fifo_profit_passed'])
