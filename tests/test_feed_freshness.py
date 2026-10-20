import time
import unittest

from election_bot.clients import POLYMARKET_MAX_QUIET_SECONDS, polymarket_book


def data(changed):
    return {'timestamp': str(int(changed*1000)), 'bids': [{'price': '.40', 'size': '10'}],
            'asks': [{'price': '.42', 'size': '10'}]}


class PolymarketFreshnessTests(unittest.TestCase):
    def test_quiet_book_is_current_as_of_response(self):
        now = time.time()
        book = polymarket_book(data(now-50), {'received_at': now}, now)
        self.assertEqual(book.diagnostic(15, venue='Polymarket')['issues'], [])
        self.assertAlmostEqual(book.transport['book_quiet_seconds'], 50, delta=1)
        self.assertIn('unchanged', book.timestamp_basis)

    def test_dead_or_unserved_book_keeps_last_change_time(self):
        now = time.time()
        dead = polymarket_book(data(now-POLYMARKET_MAX_QUIET_SECONDS-60), {'received_at': now}, now)
        self.assertIn('source_stale', dead.diagnostic(15, venue='Polymarket')['issues'])
        unserved = polymarket_book(data(now-50), {'received_at': now}, None)
        self.assertIn('source_stale', unserved.diagnostic(15, venue='Polymarket')['issues'])
        self.assertEqual(unserved.timestamp_basis, 'exchange_book_timestamp')


class SigReadRetryTests(unittest.TestCase):
    def sig(self, errors):
        from unittest.mock import Mock, patch
        from election_bot.clients import Sig
        sig = Sig.__new__(Sig)
        sig.http = Mock()
        sig.http.request.side_effect = errors
        return sig

    def test_connection_failures_retry_then_succeed(self):
        from unittest.mock import patch
        from election_bot.clients import APIError
        sig = self.sig([APIError('GET /x failed (connection timed out)', method='GET', venue='SIG'),
                        APIError('GET /x failed (URLError)', method='GET', venue='SIG'), {'ok': True}])
        with patch('election_bot.clients.time.sleep') as sleep:
            self.assertEqual(sig.read('/x'), {'ok': True})
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [1, 2])

    def test_rate_limits_and_client_errors_are_not_retried(self):
        from unittest.mock import patch
        from election_bot.clients import APIError
        for status in (429, 401, 404):
            sig = self.sig([APIError('GET /x returned HTTP %d' % status, status=status, method='GET', venue='SIG')])
            with patch('election_bot.clients.time.sleep'), self.assertRaises(APIError):
                sig.read('/x')
            self.assertEqual(sig.http.request.call_count, 1)


class SharedRateLimitTests(unittest.TestCase):
    def test_budget_is_shared_through_the_file_and_waits_when_full(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        import election_bot.clients as clients
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        now = [1000.0]
        sleeps = []
        def sleep(seconds):
            sleeps.append(seconds)
            now[0] += seconds
        with patch.object(clients, 'SHARED_RATE_FILE', Path(temp.name)/'sig-rate.json'), \
             patch.object(clients, 'SHARED_LIMITS', {'read': 3, 'write': 1}):
            for _ in range(3):
                clients.shared_slot('read', sleep, lambda: now[0])
            self.assertEqual(sleeps, [])
            clients.shared_slot('write', sleep, lambda: now[0])     # separate budget
            now[0] += 30
            clients.shared_slot('read', sleep, lambda: now[0])      # full: waits until the oldest ages out
            self.assertGreaterEqual(now[0], 1060)
            self.assertTrue(sleeps)

    def test_no_file_means_no_limit(self):
        from unittest.mock import patch
        import election_bot.clients as clients
        with patch.object(clients, 'SHARED_RATE_FILE', None):
            clients.shared_slot('read', sleep=lambda s: self.fail('should not wait'))
