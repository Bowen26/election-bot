from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from election_bot.clients import APIError, HTTP, Sig
from election_bot.demo import fixture
from election_bot.engine import Engine
from election_bot.state import Journal, exclusive_lock
from election_bot.strategy import Book, D, choose


class StatusRetryTests(unittest.TestCase):
    def setUp(self):
        self.sig = Sig.__new__(Sig)
        self.sig.tid = 'test'
        self.sig.http = Mock()

    @patch('election_bot.clients.time.sleep')
    def test_status_read_recovers_from_temporary_503(self, sleep):
        result = {'id': 123, 'open': False, 'quantityFilled': 31}
        self.sig.http.request.side_effect = [APIError('unavailable', status=503), result]
        self.assertEqual(self.sig.order(123), result)
        self.assertEqual(self.sig.http.request.call_count, 2)
        for call in self.sig.http.request.call_args_list:
            self.assertEqual(call.args, ('/orders/123',))
        sleep.assert_called_once_with(1)

    @patch('election_bot.clients.time.sleep')
    def test_persistent_503_stops_after_three_reads(self, sleep):
        self.sig.http.request.side_effect = APIError('unavailable', status=503)
        with self.assertRaises(APIError):
            self.sig.order(123)
        self.assertEqual(self.sig.http.request.call_count, 3)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [1, 2])

    @patch('election_bot.clients.time.sleep')
    def test_authorization_failure_is_not_retried(self, sleep):
        self.sig.http.request.side_effect = APIError('forbidden', status=403)
        with self.assertRaises(APIError):
            self.sig.order(123)
        self.sig.http.request.assert_called_once()
        sleep.assert_not_called()

    @patch('election_bot.clients.time.sleep')
    def test_long_retry_after_halts_instead_of_retrying_early(self, sleep):
        self.sig.http.request.side_effect = APIError('unavailable', status=503, retry_after=60)
        with self.assertRaises(APIError):
            self.sig.order(123)
        self.sig.http.request.assert_called_once()
        sleep.assert_not_called()

    def test_submission_503_is_never_automatically_retried(self):
        self.sig.http.request.side_effect = APIError('uncertain submission', status=503)
        payload = {'tournamentId': 'test'}
        with self.assertRaises(APIError):
            self.sig.place(payload)
        self.sig.http.request.assert_called_once_with('/orders', 'POST', payload=payload)

    @patch('election_bot.clients.time.sleep')
    def test_market_resolution_tree_503_retries_only_the_failed_get(self, sleep):
        self.sig.slug = 'test'
        self.sig.http.request.side_effect = [
            {'id': '381', 'status': 'open'}, APIError('unavailable', status=503),
            {'market_id': '381', 'root': {'type': 'contract'}}]
        market = self.sig.market('381')
        self.assertEqual(market['resolution_tree'], {'type': 'contract'})
        calls = self.sig.http.request.call_args_list
        self.assertEqual([c.args[0] for c in calls],
                         ['/tournaments/test/markets/381', '/markets/381/nodes', '/markets/381/nodes'])
        self.assertEqual(calls[-1].kwargs['params'], {'tournamentId': 'test'})
        sleep.assert_called_once_with(1)


class StrategyTests(unittest.TestCase):
    def setUp(self):
        self.config, self.sig, self.refs = fixture()
        self.settings = self.config['strategy']
        self.books = self.refs.books({}, None)

    def test_yes_buy_respects_cash_depth_and_fee_reservation(self):
        signal = choose(self.sig.book('2'), self.books, self.settings, D(25))
        self.assertEqual((signal.side, signal.price, signal.quantity), ('yes', D('.60'), 40))
        self.assertEqual(signal.edge, D('.12'))
        self.assertLessEqual(signal.quantity * (signal.price + D('.01')), 25)

    def test_no_buy_uses_complement_of_yes_bid(self):
        book = Book.make([('.85', 50)], [('.90', 50)])
        signal = choose(book, self.books, self.settings, D(25))
        self.assertEqual((signal.side, signal.price, signal.quantity), ('no', D('.15'), 50))

    def test_no_trade_when_budget_exhausted(self):
        self.assertIsNone(choose(self.sig.book('2'), self.books, self.settings, 0))

    def test_stale_reference_rejected(self):
        self.books[0].source_at = time.time()-100
        with self.assertRaisesRegex(ValueError, 'Stale'):
            choose(self.sig.book('2'), self.books, self.settings, 25)

    def test_future_timestamp_rejected(self):
        self.books[0].source_at = time.time()+100
        with self.assertRaisesRegex(ValueError, 'future'):
            choose(self.sig.book('2'), self.books, self.settings, 25)

    def test_crossed_reference_rejected(self):
        self.books[0] = Book.make([('.8', 100)], [('.7', 100)])
        with self.assertRaisesRegex(ValueError, 'crossed'):
            choose(self.sig.book('2'), self.books, self.settings, 25)

    def test_reference_disagreement_rejected(self):
        self.books[0] = Book.make([('.9', 100)], [('.92', 100)])
        with self.assertRaisesRegex(ValueError, 'disagree'):
            choose(self.sig.book('2'), self.books, self.settings, 25)

    def test_thin_reference_blocks_trade(self):
        self.books[0] = Book.make([('.73', 1)], [('.75', 1)])
        self.assertIsNone(choose(self.sig.book('2'), self.books, self.settings, 25))

    def test_off_tick_sig_price_rejected(self):
        book = Book.make([('.55', 100)], [('.601', 100)])
        with self.assertRaisesRegex(ValueError, 'tick'):
            choose(book, self.books, self.settings, 25)

    def test_invalid_numbers_rejected(self):
        for value in ('NaN', 'Infinity', '-Infinity'):
            with self.assertRaises(ValueError):
                Book.make([(value, 10)], [('.8', 10)])

    def test_sort_and_aggregate_book_levels(self):
        book = Book.make([('.1', 1), ('.3', 2), ('.3', 3)], [('.8', 2), ('.7', 3)])
        self.assertEqual(book.bids[0], (D('.3'), D(5)))
        self.assertEqual(book.asks[0], (D('.7'), D(3)))


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.config, self.sig, self.refs = fixture()
        self.journal = Journal(self.directory / 'state.db', 'test')
        self.engine = Engine(self.config, self.sig, self.refs, self.journal, self.directory, live=True)

    def tearDown(self):
        self.journal.close()
        self.temp.cleanup()

    def test_partial_fill_cancels_remainder_and_persists_budget(self):
        self.engine.cycle()
        self.assertEqual(self.sig.cancelled, [123])
        self.assertEqual(self.journal.used(), D('6.10'))
        self.assertEqual(len(self.journal.pending()), 0)
        self.assertEqual(self.sig.placed[0]['tournamentId'], self.sig.tid)
        self.journal.close()
        self.journal = Journal(self.directory / 'state.db', 'test')
        self.assertEqual(self.journal.used(), D('6.10'))

    @patch('election_bot.clients.time.sleep')
    def test_status_503_after_fill_does_not_duplicate_submission(self, sleep):
        reader = Sig.__new__(Sig)
        reader.http = Mock()
        reader.http.request.side_effect = [APIError('unavailable', status=503),
            {'exchangeId': '2', 'tournamentId': self.sig.tid, 'open': False, 'quantityFilled': 10}]
        self.sig.order = reader.order
        self.engine.cycle()
        self.assertEqual(len(self.sig.placed), 1)
        self.assertFalse(self.journal.pending())
        self.assertEqual(self.journal.used(), D('6.10'))

    def test_reservation_is_committed_before_network_submission(self):
        original = self.sig.place
        def place(payload):
            other = Journal(self.directory / 'state.db', 'test')
            try:
                self.assertEqual(len(other.pending()), 1)
                self.assertEqual(json.loads(other.pending()[0]['payload']), payload)
            finally:
                other.close()
            return original(payload)
        self.sig.place = place
        self.engine.cycle()

    def test_unknown_submission_blocks_retry_and_survives_restart(self):
        def timeout(payload):
            self.sig.placed.append(payload)
            raise TimeoutError('Response lost after acceptance')
        self.sig.place = timeout
        with self.assertRaises(TimeoutError):
            self.engine.cycle()
        self.journal.close()
        self.journal = Journal(self.directory / 'state.db', 'test')
        self.engine.journal = self.journal
        with self.assertRaisesRegex(RuntimeError, 'Unknown order'):
            self.engine.cycle()
        self.assertEqual(len(self.sig.placed), 1)
        self.assertEqual(self.journal.used(), D('24.40'))

    def test_recovery_reuses_exact_payload_and_key_after_expiration(self):
        payload = {'idempotencyKey': 'same-key', 'exchangeId': '2', 'side': 'yes',
                   'action': 'buy', 'quantity': 40, 'price': .6, 'tournamentId': self.sig.tid,
                   'expirationDate': datetime.fromtimestamp(time.time()-10, timezone.utc).isoformat()}
        self.journal.reserve(payload, D('24.40'))
        self.engine.reconcile(replay_unknown=True)
        self.assertEqual(self.sig.placed, [payload])
        self.assertFalse(self.journal.pending())

    def test_recovery_cannot_replay_unexpired_unknown_order(self):
        payload = {'idempotencyKey': 'same-key', 'exchangeId': '2',
                   'expirationDate': datetime.fromtimestamp(time.time()+30, timezone.utc).isoformat()}
        self.journal.reserve(payload, 25)
        with self.assertRaisesRegex(RuntimeError, 'Wait until'):
            self.engine.reconcile(replay_unknown=True)
        self.assertFalse(self.sig.placed)

    def test_disabling_all_markets_does_not_prevent_recovery(self):
        payload = {'idempotencyKey': 'old-key', 'exchangeId': '2', 'side': 'yes',
                   'action': 'buy', 'quantity': 40, 'price': .6, 'tournamentId': self.sig.tid,
                   'expirationDate': datetime.fromtimestamp(time.time()-10, timezone.utc).isoformat()}
        self.journal.reserve(payload, D('24.40'))
        self.journal.response('old-key', self.sig.place(payload))
        self.config['markets'][0]['enabled'] = False
        engine = Engine(self.config, self.sig, self.refs, self.journal, self.directory, live=True)
        engine.reconcile()
        self.assertFalse(self.journal.pending())
        self.assertEqual(self.sig.cancelled, [123])

    def test_closed_noop_without_order_id_releases_reservation(self):
        self.sig.place = lambda payload: {'orderId': None, 'open': False,
                                         'quantityTraded': 0, 'totalCost': 0}
        self.engine.cycle()
        self.assertEqual(self.journal.used(), 0)
        self.assertFalse(self.journal.pending())

    def test_invalid_acknowledgement_retains_reservation(self):
        self.sig.place = lambda payload: {'unexpected': 'response'}
        with self.assertRaisesRegex(RuntimeError, 'Malformed'):
            self.engine.cycle()
        self.assertEqual(self.journal.used(), D('24.40'))
        self.assertEqual(len(self.journal.pending()), 1)

    def test_unconfirmed_cancel_blocks_new_orders(self):
        self.sig.cancel = lambda oid: None
        with patch('election_bot.engine.time.sleep'), self.assertRaisesRegex(RuntimeError, 'Cancellation'):
            self.engine.cycle()
        self.assertEqual(len(self.journal.pending()), 1)
        self.assertEqual(self.journal.used(), D('24.40'))

    def test_wrong_order_identity_cannot_cancel_unrelated_order(self):
        self.sig.order = lambda oid: {'exchangeId': '999', 'tournamentId': 'other', 'open': True}
        with self.assertRaisesRegex(RuntimeError, 'identity mismatch'):
            self.engine.cycle()
        self.assertFalse(self.sig.cancelled)
        self.assertEqual(len(self.journal.pending()), 1)

    def test_manual_orders_block_new_trades_without_cancellation(self):
        self.sig.open_orders = lambda: [{'id': 99}]
        self.engine.cycle()
        self.assertFalse(self.sig.placed)
        self.assertFalse(self.sig.cancelled)

    def test_stop_blocks_submissions(self):
        (self.directory / 'STOP').touch()
        self.assertFalse(self.engine.cycle())
        self.assertFalse(self.sig.placed)

    def test_stop_during_feed_request_blocks_submission(self):
        original = self.sig.book
        def book(exchange):
            (self.directory / 'STOP').touch()
            return original(exchange)
        self.sig.book = book
        self.assertFalse(self.engine.cycle())
        self.assertFalse(self.sig.placed)

    def test_cash_limit_includes_cost_buffer(self):
        original = self.sig.account
        self.sig.account = lambda: {**original(), 'myBalance': 3}
        # Use paper to avoid the demo broker's intentionally fixed ten-share fill.
        self.engine.live = False
        self.engine.cycle()
        self.assertEqual(self.journal.used(), D('2.44'))
        self.assertFalse(self.sig.placed)

    def test_daily_limit_blocks_new_trade(self):
        self.journal.reserve({'idempotencyKey': 'previous', 'exchangeId': 'different'}, 200)
        self.journal.complete('previous', 200)
        self.engine.cycle()
        self.assertFalse(self.sig.placed)

    def test_opposite_inventory_is_not_netted(self):
        self.sig.positions = lambda: [{'exchangeId': '2', 'quantity': -10}]
        self.engine.cycle()
        self.assertFalse(self.sig.placed)

    def test_contract_change_blocks_trade(self):
        original = self.sig.market
        self.sig.market = lambda mid: {**original(mid), 'resolution_tree': {'type': 'changed'}}
        self.engine.cycle()
        self.assertFalse(self.sig.placed)

    def test_wrong_tournament_blocks_trade(self):
        self.config['tournament_id'] = 'other'
        with self.assertRaisesRegex(ValueError, 'tournament ID'):
            self.engine.cycle()
        self.assertFalse(self.sig.placed)

    def test_cooldown_blocks_repeat_entry(self):
        self.engine.cycle()
        self.engine.cycle()
        self.assertEqual(len(self.sig.placed), 1)

    def test_missing_final_fill_count_retains_full_budget(self):
        original = self.sig.order
        self.sig.order = lambda oid: {**original(oid), 'quantityFilled': None}
        self.engine.cycle()
        self.assertEqual(self.journal.used(), D('24.40'))

    def test_duplicate_process_lock_rejected(self):
        with exclusive_lock(self.directory):
            with self.assertRaisesRegex(RuntimeError, 'Another bot'):
                with exclusive_lock(self.directory):
                    self.fail('second lock acquired')

    def test_external_clients_cannot_submit_orders(self):
        http = HTTP('https://clob.polymarket.com')
        with self.assertRaisesRegex(ValueError, 'read-only'):
            http.request('/order', 'POST', payload={})


if __name__ == '__main__':
    unittest.main()
