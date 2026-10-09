from datetime import datetime, timezone
import io
import json
import time
import unittest
import urllib.error
from unittest.mock import Mock, patch

from election_bot.clients import APIError, HTTP, Sig
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

    def test_normal_restart_never_replays_or_releases(self):
        with self.assertRaisesRegex(RuntimeError, 'Unknown order'):
            self.e.reconcile()
        self.s.place.assert_not_called()
        self.s.recovery_history.assert_not_called()
        self.assertTrue(self.j.pending())

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

    def test_paginated_history_requires_same_checkpoint(self):
        s = self.client([self.page(more=True), self.page(sequence=101)])
        with self.assertRaisesRegex(RuntimeError, 'checkpoint'):
            s.recovery_history('2')
        s.check_clock.assert_not_called()

    def test_incomplete_and_wrong_scope_history_fail(self):
        for page in [self.page(complete=False), self.page(sequence=None),
                     self.page(rows=[{'id': 7, 'exchangeId': '3', 'tournamentId': 'tournament'}])]:
            with self.subTest(page=page), self.assertRaises(RuntimeError):
                self.client([page]).recovery_history('2')

    def test_complete_history_preserves_pagination_and_checks_clock(self):
        s = self.client([self.page(more=True), self.page()])
        self.assertEqual(s.recovery_history('2'), {'orders': [], 'sequence': 100})
        self.assertEqual(s.read.call_args_list[1].kwargs['params']['cursor'], 'next')
        s.check_clock.assert_called_once()
