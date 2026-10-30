from datetime import datetime, timezone
import io
import json
import time
import unittest
import urllib.error
from unittest.mock import Mock, patch

from election_bot.clients import APIError, HTTP, Sig, RecoveryHistoryUnavailable
from election_bot.manual_release import ReleaseRefused
from election_bot.strategy import D
from election_bot.supervisor import HALT, failure_code
from tests import test_active


class ExpiredRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.f = test_active.ActiveTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.e, self.j, self.s = self.f.engine, self.f.journal, self.f.sig
        self.payload = {'idempotencyKey': 'uncertain', 'exchangeId': '2', 'side': 'yes',
            'action': 'buy', 'quantity': 10, 'price': .6, 'tournamentId': self.s.tid,
            'expirationDate': datetime.fromtimestamp(time.time()-120, timezone.utc).isoformat()}
        self.j.reserve(self.payload, '6.10')
        self.j.db.execute('UPDATE orders SET created=?', (time.time()-135,))
        self.j.db.commit()
        self.s.recovery_history = Mock(return_value={'orders': [], 'sequence': 100})
        self.error = APIError('expired', status=400, method='POST', venue='SIG', rejection='expired_order')
        self.s.place = Mock(side_effect=self.error)
        self.sleep = patch('election_bot.expired_recovery.time.sleep')
        self.sleep.start()
        self.addCleanup(self.sleep.stop)

    def test_explicit_recovery_releases_only_after_two_verified_checks(self):
        self.e.reconcile(replay_unknown=True)
        self.s.place.assert_called_once_with(self.payload)
        self.assertEqual(self.s.recovery_history.call_count, 2)
        self.assertFalse(self.j.pending())
        self.assertEqual(self.j.db.execute('SELECT quantity FROM executions').fetchone()[0], '0')
        event = self.j.db.execute("SELECT detail FROM events WHERE kind='expired_request_verified_unplaced'").fetchone()
        self.assertEqual(len(json.loads(event[0])['checks']), 2)
        self.e.reconcile(replay_unknown=True)
        self.assertEqual(self.s.place.call_count, 1)

    def test_full_recovery_allows_coverage_to_advance_between_pages(self):
        self.s.read = Mock(side_effect=[
            {'data': [], 'coverage': {'complete': True, 'projectedThroughSequence': n},
             'pagination': {'hasMore': n % 2 == 0, 'nextCursor': 'cursor' if n % 2 == 0 else None}}
            for n in (100, 101, 102, 103)])
        self.s.check_clock = Mock()
        self.s.recovery_history = lambda exchange: Sig.recovery_history(self.s, exchange)
        self.e.reconcile(replay_unknown=True)
        self.assertFalse(self.j.pending())
        self.assertEqual(self.j.db.execute('SELECT quantity FROM executions').fetchone()[0], '0')
        event = self.j.db.execute("SELECT detail FROM events WHERE kind='expired_request_verified_unplaced'").fetchone()
        self.assertEqual([r['sequence'] for r in json.loads(event[0])['checks']], [101, 103])

    def test_normal_restart_never_replays_or_releases(self):
        with self.assertRaisesRegex(RuntimeError, 'Unknown order'):
            self.e.reconcile()
        self.s.place.assert_not_called()
        self.s.recovery_history.assert_not_called()
        self.assertTrue(self.j.pending())

    def test_unverified_history_retries_without_releasing_then_recovers(self):
        self.s.recovery_history.side_effect = RecoveryHistoryUnavailable('coverage unavailable')
        with self.assertRaises(RecoveryHistoryUnavailable):
            self.e.reconcile(replay_unknown=True)
        self.assertEqual(self.j.pending()[0]['key'], self.payload['idempotencyKey'])
        self.assertEqual(self.j.db.execute('SELECT COUNT(*) FROM executions').fetchone()[0], 0)
        self.s.recovery_history.side_effect = None
        self.e.reconcile(replay_unknown=True)
        self.assertFalse(self.j.pending())
        self.assertEqual(self.j.db.execute('SELECT quantity FROM executions').fetchone()[0], '0')
        self.assertEqual(self.s.place.call_count, 2)
        for call in self.s.place.call_args_list:
            self.assertEqual(call.args[0], self.payload)

    def test_other_400_never_enters_fallback(self):
        self.s.place.side_effect = APIError('other', status=400, method='POST', venue='SIG')
        with self.assertRaises(APIError):
            self.e.reconcile(replay_unknown=True)
        self.s.recovery_history.assert_not_called()
        self.assertTrue(self.j.pending())

    def test_recent_request_retains_reservation(self):
        self.j.db.execute('UPDATE orders SET created=?', (time.time()-30,))
        self.j.db.commit()
        with self.assertRaisesRegex(RuntimeError, '90 seconds'):
            self.e.reconcile(replay_unknown=True)
        self.s.recovery_history.assert_not_called()
        self.assertTrue(self.j.pending())

    def test_unknown_remote_order_retains_reservation_even_if_inventory_matches(self):
        self.s.recovery_history.return_value = {'orders': [{'id': 123}], 'sequence': 100}
        with self.assertRaisesRegex(RuntimeError, 'Unrecognized'):
            self.e.reconcile(replay_unknown=True)
        self.assertTrue(self.j.pending())

    def test_portfolio_mismatch_retains_reservation(self):
        self.s.inventory['2'] = 10
        with self.assertRaisesRegex(RuntimeError, 'portfolio differs'):
            self.e.reconcile(replay_unknown=True)
        self.assertTrue(self.j.pending())

    def test_second_check_failure_retains_reservation_without_zero_execution(self):
        self.s.recovery_history.side_effect = [{'orders': [], 'sequence': 100},
                                              {'orders': [], 'sequence': 99}]
        with self.assertRaisesRegex(RuntimeError, 'regressed'):
            self.e.reconcile(replay_unknown=True)
        self.assertTrue(self.j.pending())
        self.assertEqual(self.j.db.execute('SELECT COUNT(*) FROM executions').fetchone()[0], 0)

    def test_new_order_on_second_read_retains_reservation(self):
        self.s.recovery_history.side_effect = [{'orders': [], 'sequence': 100},
                                              {'orders': [{'id': 9}], 'sequence': 101}]
        with self.assertRaisesRegex(RuntimeError, 'Unrecognized'):
            self.e.reconcile(replay_unknown=True)
        self.assertTrue(self.j.pending())

    def test_failed_accounting_rolls_back_evidence_and_keeps_reservation(self):
        with patch.object(self.e.ledger, 'record', side_effect=RuntimeError('disk error')):
            with self.assertRaisesRegex(RuntimeError, 'disk error'):
                self.e.reconcile(replay_unknown=True)
        self.assertTrue(self.j.pending())
        self.assertEqual(self.j.db.execute("SELECT COUNT(*) FROM events WHERE kind='expired_request_verified_unplaced'").fetchone()[0], 0)

    def test_accounted_history_and_missing_order(self):
        original = dict(self.payload, idempotencyKey='prior', quantity=3)
        # Set up an already-confirmed order without submitting another order.
        self.j.db.execute('INSERT INTO orders VALUES (?,?,?,?,?,?,?,?)',
            ('prior', '2', '1.83', '2026-10-08', time.time()-200, 'closed', json.dumps(original),
             json.dumps({'orderId': 7})))
        self.j.db.commit()
        self.e.ledger.record(original, 3, '.6', '.01')
        self.s.inventory['2'] = 3
        order = dict(original, id=7, open=False, priceLimit=.6, quantityFilled=3)
        self.s.recovery_history.return_value = {'orders': [], 'sequence': 100}
        with self.assertRaisesRegex(RuntimeError, 'missing'):
            self.e.reconcile(replay_unknown=True)
        self.assertTrue(self.j.pending())
        self.s.recovery_history.return_value = {'orders': [order], 'sequence': 101}
        order['quantityFilled'] = 4
        with self.assertRaisesRegex(RuntimeError, 'differs'):
            self.e.reconcile(replay_unknown=True)
        self.assertTrue(self.j.pending())
        order['quantityFilled'] = 3
        self.e.reconcile(replay_unknown=True)
        self.assertFalse(self.j.pending())


class RecoveryClientTests(unittest.TestCase):
    def test_narrow_expiration_error_classification_and_no_body_leak(self):
        for message, expected in [('expirationDate must be in the future.', 'expired_order'),
                                  ('secret private error', None)]:
            http = HTTP('https://sig.thesuper.market/api/v1')
            body = json.dumps({'error': {'code': 'VALIDATION_ERROR', 'message': message}}).encode()
            http.opener = Mock()
            http.opener.open.side_effect = urllib.error.HTTPError('https://sig.thesuper.market/api/v1/orders',
                400, 'Bad request', {}, io.BytesIO(body))
            with self.assertRaises(APIError) as caught:
                http.request('/orders', 'POST', payload={})
            self.assertEqual(caught.exception.rejection, expected)
            self.assertNotIn(message, str(caught.exception))

    def client(self, pages):
        s = Sig.__new__(Sig)
        s.tid = 'tournament'
        s.read = Mock(side_effect=pages)
        s.check_clock = Mock()
        return s

    def page(self, sequence=100, complete=True, more=False, rows=None):
        return {'data': rows or [], 'coverage': {'complete': complete, 'projectedThroughSequence': sequence},
                'pagination': {'hasMore': more, 'nextCursor': 'next' if more else None}}

    def test_paginated_history_rejects_regressing_checkpoint(self):
        s = self.client([self.page(more=True), self.page(sequence=99)])
        with self.assertRaisesRegex(RuntimeError, 'checkpoint'):
            s.recovery_history('2')
        s.check_clock.assert_not_called()

    def test_incomplete_and_wrong_scope_history_fail(self):
        for page in [self.page(complete=False), self.page(sequence=None),
                     self.page(rows=[{'id': 7, 'exchangeId': '3', 'tournamentId': 'tournament'}])]:
            with self.subTest(page=page), self.assertRaises(RuntimeError):
                self.client([page]).recovery_history('2')

    def test_only_coverage_checkpoint_failures_get_recovery_retry_type(self):
        for page in [self.page(complete=False), self.page(sequence=None), self.page(sequence=True)]:
            with self.subTest(page=page), self.assertRaises(RecoveryHistoryUnavailable):
                self.client([page]).recovery_history('2')
        with self.assertRaises(RecoveryHistoryUnavailable):
            self.client([self.page(more=True), self.page(sequence=99)]).recovery_history('2')
        wrong = self.page(rows=[{'id': 7, 'exchangeId': '3', 'tournamentId': 'tournament'}])
        with self.assertRaises(RuntimeError) as error:
            self.client([wrong]).recovery_history('2')
        self.assertNotIsInstance(error.exception, RecoveryHistoryUnavailable)

    def test_complete_history_preserves_pagination_and_checks_clock(self):
        s = self.client([self.page(more=True), self.page()])
        self.assertEqual(s.recovery_history('2'), {'orders': [], 'sequence': 100})
        self.assertEqual(s.read.call_args_list[1].kwargs['params']['cursor'], 'next')
        s.check_clock.assert_called_once()


class AdvancingRecoveryCheckpointTests(unittest.TestCase):
    client = RecoveryClientTests.client
    page = RecoveryClientTests.page
    def test_advancing_checkpoint_with_empty_terminal_page(self):
        rows = [{'id': i, 'exchangeId': '2', 'tournamentId': 'tournament'} for i in range(1, 101)]
        s = self.client([self.page(sequence=100, more=True, rows=rows), self.page(sequence=105)])
        self.assertEqual(s.recovery_history('2'), {'orders': rows, 'sequence': 105})
        self.assertEqual(s.read.call_args_list[1].kwargs['params']['cursor'], 'next')
        s.check_clock.assert_called_once()

    def test_advancing_checkpoint_preserves_all_rows(self):
        a = {'id': 1, 'exchangeId': '2', 'tournamentId': 'tournament'}
        b = dict(a, id=2)
        s = self.client([self.page(more=True, rows=[a]), self.page(sequence=101, rows=[b])])
        self.assertEqual(s.recovery_history('2')['orders'], [a, b])

    def test_advancing_checkpoint_does_not_allow_duplicate_orders(self):
        row = {'id': 1, 'exchangeId': '2', 'tournamentId': 'tournament'}
        s = self.client([self.page(more=True, rows=[row]), self.page(sequence=101, rows=[row])])
        with self.assertRaisesRegex(RuntimeError, 'identity mismatch'): s.recovery_history('2')

    def test_advancing_checkpoint_does_not_allow_repeated_cursor(self):
        s = self.client([self.page(more=True), self.page(sequence=101, more=True)])
        with self.assertRaisesRegex(RuntimeError, 'pagination'): s.recovery_history('2')

    def test_invalid_coverage_on_later_page_still_blocks(self):
        for sequence, complete in [(None, True), (True, True), (-1, True), (101, False)]:
            with self.subTest(sequence=sequence, complete=complete):
                s = self.client([self.page(more=True), self.page(sequence=sequence, complete=complete)])
                with self.assertRaisesRegex(RuntimeError, 'coverage/checkpoint'): s.recovery_history('2')
                s.check_clock.assert_not_called()

    def test_complete_history_still_requires_valid_clock(self):
        s = self.client([self.page(more=True), self.page(sequence=101)])
        s.check_clock.side_effect = RuntimeError('invalid clock')
        with self.assertRaisesRegex(RuntimeError, 'invalid clock'): s.recovery_history('2')


class FakeClock:
    """Wall and monotonic time that advance only when the release wait sleeps."""
    def __init__(self, now, on_sleep=None):
        self.now, self.slept, self.on_sleep = now, 0, on_sleep

    def time(self):
        return self.now

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds
        self.slept += seconds
        if self.on_sleep:
            self.on_sleep()


class AutoReleaseTests(unittest.TestCase):
    """SIG history stays incomplete: with release_unplaced, recovery runs the operator release checks."""
    def setUp(self):
        self.f = test_active.ActiveTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.e, self.j, self.s = self.f.engine, self.f.journal, self.f.sig
        self.e.config['auto_recovery'] = {'enabled': True, 'release_unplaced': True}
        self.s.recovery_history = Mock(side_effect=RecoveryHistoryUnavailable('coverage unavailable'))
        self.s.read = Mock(return_value={'data': [], 'coverage': {'complete': False, 'projectedThroughSequence': 5},
                                         'pagination': {'hasMore': False, 'nextCursor': None}})
        self.s.place = Mock(side_effect=APIError('expired', status=400, method='POST', venue='SIG',
                                                 rejection='expired_order'))
        patcher = patch('election_bot.expired_recovery.time.sleep')
        patcher.start()
        self.addCleanup(patcher.stop)

    def reserve(self, expired_seconds_ago):
        expired = time.time()-expired_seconds_ago
        self.payload = {'idempotencyKey': 'uncertain', 'exchangeId': '2', 'side': 'yes', 'action': 'buy',
                        'quantity': 10, 'price': .6, 'tournamentId': self.s.tid,
                        'expirationDate': datetime.fromtimestamp(expired, timezone.utc).isoformat()}
        self.j.reserve(self.payload, '6.10')
        self.j.db.execute('UPDATE orders SET created=?', (expired-15,))
        self.j.db.commit()

    def kinds(self):
        return [kind for (kind,) in self.j.db.execute('SELECT kind FROM events ORDER BY rowid')]

    def detail(self, kind):
        return json.loads(self.j.db.execute('SELECT detail FROM events WHERE kind=?', (kind,)).fetchone()[0])

    def assert_retained(self):
        self.assertEqual([r['key'] for r in self.j.pending()], ['uncertain'])
        self.assertEqual(self.j.db.execute('SELECT COUNT(*) FROM executions').fetchone()[0], 0)

    def test_releases_once_operator_checks_pass(self):
        self.reserve(700)
        self.e.reconcile(replay_unknown=True)
        self.s.place.assert_called_once_with(self.payload)
        self.assertFalse(self.j.pending())
        self.assertEqual(D(self.j.db.execute("SELECT quantity FROM executions WHERE key='uncertain'").fetchone()[0]), 0)
        evidence = self.detail('auto_released_unplaced')
        self.assertEqual(evidence['coverage'][0]['complete'], False)
        self.assertIsNone(evidence['request_listed_on_sig'])
        self.assertEqual(self.detail('recovery_complete')['outcome'], 'auto_released_unplaced')
        self.assertNotIn('auto_release_wait', self.kinds())
        self.assertNotIn('operator_released_unplaced', self.kinds())

    def test_incomplete_second_read_also_falls_back(self):
        self.reserve(700)
        self.s.recovery_history.side_effect = [{'orders': [], 'sequence': 100},
                                              RecoveryHistoryUnavailable('coverage unavailable')]
        self.e.reconcile(replay_unknown=True)
        self.assertFalse(self.j.pending())
        self.assertIn('auto_released_unplaced', self.kinds())
        self.assertNotIn('expired_request_verified_unplaced', self.kinds())

    def test_waits_until_ten_minutes_after_expiration(self):
        self.reserve(200)
        clock = FakeClock(time.time())
        with patch('election_bot.manual_release.time', clock):
            self.e.reconcile(replay_unknown=True)
        self.assertFalse(self.j.pending())
        self.assertAlmostEqual(self.detail('auto_release_wait')['wait_seconds'], 400, delta=5)
        self.assertGreaterEqual(clock.slept, 395)

    def test_stop_ends_supervised_wait_with_reservation_retained(self):
        self.reserve(200)
        self.e.recovery_stop_guard = True
        clock = FakeClock(time.time(), on_sleep=lambda: (self.e.runtime/'STOP').touch())
        with patch('election_bot.manual_release.time', clock), self.assertRaises(KeyboardInterrupt):
            self.e.reconcile(replay_unknown=True)
        self.assert_retained()
        self.s.read.assert_not_called()

    def test_refusal_halts_with_reservation_retained(self):
        self.reserve(700)
        self.s.cash += D(6)  # Cash moved as if the request had filled.
        with self.assertRaisesRegex(ReleaseRefused, 'Automatic release refused: SIG cash') as caught:
            self.e.reconcile(replay_unknown=True)
        self.assertEqual(failure_code(caught.exception), HALT)
        self.assert_retained()
        self.assertNotIn('auto_released_unplaced', self.kinds())

    def test_other_recovery_failures_never_fall_back(self):
        self.reserve(700)
        self.s.recovery_history.side_effect = None
        self.s.recovery_history.return_value = {'orders': [{'id': 123}], 'sequence': 100}
        with self.assertRaisesRegex(RuntimeError, 'Unrecognized'):
            self.e.reconcile(replay_unknown=True)
        self.s.read.assert_not_called()
        self.assert_retained()

    def test_off_unless_configured(self):
        self.e.config['auto_recovery'] = {'enabled': True}
        self.reserve(700)
        with self.assertRaises(RecoveryHistoryUnavailable):
            self.e.reconcile(replay_unknown=True)
        self.s.read.assert_not_called()
        self.assert_retained()
