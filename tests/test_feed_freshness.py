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


class JournalConcurrencyTests(unittest.TestCase):
    def test_long_reader_does_not_block_writer(self):
        import sqlite3, tempfile
        from pathlib import Path
        from election_bot.state import Journal
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        path = Path(temp.name)/'live.sqlite3'
        journal = Journal(path, 'test')
        self.addCleanup(journal.close)
        self.assertEqual(journal.db.execute('PRAGMA journal_mode').fetchone()[0], 'wal')
        journal.event('x', {'n': 1})
        reader = sqlite3.connect(path.resolve().as_uri()+'?mode=ro', uri=True)
        self.addCleanup(reader.close)
        reader.execute('BEGIN')
        reader.execute('SELECT COUNT(*) FROM events').fetchone()       # long read transaction open
        journal.event('x', {'n': 2})                                     # writer is not blocked
        self.assertEqual(journal.db.execute('SELECT COUNT(*) FROM events').fetchone()[0], 2)


class TruncatedResponseTests(unittest.TestCase):
    def test_cut_off_json_is_a_retryable_transport_error(self):
        import io
        from unittest.mock import MagicMock, patch
        from election_bot.clients import APIError, HTTP, Sig
        http = HTTP('https://sig.thesuper.market/api/v1', 'key')
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = b'{"data": [{"id": "1", "title": "cut of'
        response.headers = {'Date': 'Tue, 20 Oct 2026 13:00:00 GMT'}
        http.opener = MagicMock()
        http.opener.open.return_value = response
        with patch('election_bot.clients.shared_slot'), patch('election_bot.clients.time.sleep'), \
             self.assertRaises(APIError) as caught:
            http.request('/x')
        self.assertIsNone(caught.exception.status)          # status None: Sig.read retries it
        sig = Sig.__new__(Sig)
        sig.http = MagicMock()
        sig.http.request.side_effect = [caught.exception, {'ok': True}]
        with patch('election_bot.clients.time.sleep'):
            self.assertEqual(sig.read('/x'), {'ok': True})
