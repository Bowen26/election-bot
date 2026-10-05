import contextlib
import io
import json
from pathlib import Path
import tempfile
import time
import unittest
import urllib.error
from unittest.mock import Mock, patch

from election_bot import __main__ as cli
from election_bot.clients import APIError, HTTP
from election_bot.diagnostics import exit_summary, quote_summary
from election_bot.performance import report
from election_bot.state import Journal
from election_bot.strategy import Book, BookValidationError, D, choose_exit


class QuoteDiagnosticTests(unittest.TestCase):
    def test_source_age_and_local_age_are_distinct_and_guard_still_blocks(self):
        book = Book.make([('.5', 100)], [('.6', 100)], source_at=100,
                         venue='Polymarket', timestamp_basis='exchange_book_timestamp')
        book.observed_at = 199
        with patch('election_bot.strategy.time.time', return_value=200):
            detail = book.diagnostic(15)
            self.assertEqual(detail['issues'], ['source_stale'])
            self.assertEqual((detail['source_age_seconds'], detail['local_age_seconds']), (100, 1))
            with self.assertRaisesRegex(BookValidationError, 'Polymarket'):
                book.check(15)
        self.assertEqual(book.complement().timestamp_basis, 'exchange_book_timestamp')
        self.assertEqual(book.complement().venue, 'Polymarket')

    def test_future_boundary_and_nonfinite_timestamps(self):
        book = Book.make([('.5', 100)], [('.6', 100)])
        book.observed_at = 100
        book.source_at = 105
        with patch('election_bot.strategy.time.time', return_value=100):
            book.check(15)
            book.source_at = 105.01
            self.assertEqual(book.diagnostic(15)['issues'], ['source_future'])
            for stamp in (float('nan'), float('inf')):
                book.source_at = stamp
                with self.assertRaisesRegex(BookValidationError, 'Invalid book timestamp'):
                    book.check(15)
                json.dumps(book.diagnostic(15), allow_nan=False)

    def test_book_quality_reasons_are_explicit(self):
        for bids, asks, expected in [([], [('.6', 10)], 'two_sided_book_required'),
                                    ([('.6', 10)], [('.6', 10)], 'locked_or_crossed'),
                                    ([('.4', 10)], [('.6', 10)], 'reference_spread_too_wide')]:
            with self.subTest(expected=expected):
                book = Book.make(bids, asks, venue='Kalshi')
                self.assertIn(expected, book.diagnostic(15, '.08')['issues'])
                with self.assertRaises(BookValidationError):
                    book.check(15, '.08')

    def test_http_errors_keep_safe_venue_provenance(self):
        for base, venue in [('https://sig.thesuper.market/api/v1', 'SIG'),
                            ('https://external-api.kalshi.com/trade-api/v2', 'Kalshi'),
                            ('https://clob.polymarket.com', 'Polymarket')]:
            http = HTTP(base)
            http.opener = Mock()
            http.opener.open.side_effect = urllib.error.URLError('private upstream detail')
            with self.assertRaises(APIError) as caught:
                http.request('/book')
            self.assertEqual(caught.exception.venue, venue)
            self.assertEqual(caught.exception.method, 'GET')
            self.assertNotIn('private upstream detail', str(caught.exception))


class ExitDiagnosticTests(unittest.TestCase):
    settings = {'max_age_seconds': 15, 'max_reference_spread': '.08',
                'max_reference_disagreement': '.08', 'minimum_edge': '.05',
                'cost_buffer_per_share': '.01', 'min_reference_depth': '20', 'max_shares_per_order': 100}
    execution = {'exit_edge': '.02', 'take_profit_min': '.02'}

    def evaluate(self, bid, cost, depth=100, allowance='50', held=10):
        book = Book.make([(bid, 100)], [(D(bid)+D('.005'), 100)])
        refs = [Book.make([('.73', 100)], [('.75', depth)]),
                Book.make([('.74', 100)], [('.76', depth)])]
        detail = {}
        result = choose_exit(book, refs, self.settings, self.execution, D(held), D(cost), allowance, detail)
        return result, detail

    def test_price_convergence_and_profit_are_separate_requirements(self):
        signal, detail = self.evaluate('.60', '5')
        self.assertIsNone(signal)
        self.assertEqual(detail['reason'], 'price_not_converged')
        route = detail['routes']['convergence_take_profit']
        self.assertEqual(D(route['net_profit_per_share_at_average_cost']), D('.09'))
        self.assertFalse(route['price_converged'])
        signal, detail = self.evaluate('.745', '7.5')
        self.assertIsNone(signal)
        self.assertEqual(detail['reason'], 'profit_below_minimum')
        self.assertTrue(detail['routes']['convergence_take_profit']['price_converged'])

    def test_alternative_overpriced_route_can_exit_at_a_loss(self):
        signal, detail = self.evaluate('.80', '9')
        self.assertEqual(signal.reason, 'overpriced_exit')
        self.assertEqual(detail['status'], 'eligible')
        self.assertFalse(detail['routes']['convergence_take_profit']['passes_average_cost_checks'])

    def test_convergence_route_does_not_require_overpricing(self):
        signal, detail = self.evaluate('.745', '7')
        self.assertEqual(signal.reason, 'convergence_take_profit')
        self.assertFalse(detail['routes']['overpriced_exit']['passes'])
        self.assertTrue(detail['routes']['convergence_take_profit']['passes_average_cost_checks'])

    def test_reference_depth_and_subshare_limits_explained(self):
        signal, detail = self.evaluate('.8', '5', depth=19)
        self.assertIsNone(signal)
        self.assertEqual(detail['reason'], 'reference_ask_depth')
        self.assertEqual(detail['min_reference_ask_depth'], '19')
        signal, detail = self.evaluate('.8', '5', allowance='.1')
        self.assertIsNone(signal)
        self.assertEqual(detail['reason'], 'size_below_one_share')

    def test_no_holdings_is_not_a_blocked_sale(self):
        signal, detail = self.evaluate('.8', '0', held=0)
        self.assertIsNone(signal)
        self.assertEqual(detail, {'status': 'not_applicable', 'reason': 'no_position'})

    def test_no_side_exit_uses_complementary_bid_and_ask(self):
        book = Book.make([('.17', 100)], [('.20', 100)])
        refs = [Book.make([('.25', 100)], [('.27', 100)]),
                Book.make([('.24', 100)], [('.26', 100)])]
        detail = {}
        signal = choose_exit(book, refs, self.settings, self.execution, D(-10), D(5), '50', detail)
        self.assertEqual(signal.side, 'no')
        self.assertEqual(D(detail['sig_bid']), D('.8'))
        self.assertEqual(D(detail['routes']['overpriced_exit']['reference_ask']), D('.76'))


class DiagnosticReportingTests(unittest.TestCase):
    def test_healthier_venue_context_and_performance_reads_do_not_inflate_scan_failures(self):
        sample = {'phase': 'scan', 'books': [
            {'venue': 'SIG', 'issues': [], 'source_age_seconds': 1, 'local_age_seconds': 0},
            {'venue': 'Polymarket', 'issues': ['source_stale'], 'source_age_seconds': 30, 'local_age_seconds': 0}]}
        result = quote_summary([sample, dict(sample, phase='performance_observation')], [])
        self.assertEqual(len(result['by_venue_and_phase']), 2)
        self.assertTrue(all(r['venue'] == 'Polymarket' and r['failed_checks'] == 1 for r in result['by_venue_and_phase']))

    def test_eligible_and_blocked_exits_are_separate_from_fills_and_phases(self):
        events = [{'at': 1, 'exchange': '2', 'phase': 'scan', 'status': 'eligible', 'reason': 'overpriced_exit'},
                  {'at': 2, 'exchange': '2', 'phase': 'preflight', 'status': 'not_evaluated', 'reason': 'quote_validation'}]
        result = exit_summary(events)
        self.assertEqual(result['by_phase']['scan']['statuses'], {'eligible': 1})
        self.assertEqual(result['by_phase']['preflight']['statuses'], {'not_evaluated': 1})

    def test_read_only_report_and_cli_need_no_key_or_network(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            journal = Journal(root/'live.sqlite3', 'test')
            journal.event('exit_blocked', {'exchange': '2', 'phase': 'scan', 'held': '10',
                                          'status': 'not_evaluated', 'reason': 'cooldown'})
            journal.close()
            before = (root/'live.sqlite3').read_bytes()
            result = report(root/'live.sqlite3')
            self.assertEqual(result['exit_diagnostics_last_24h']['by_phase']['scan']['reasons'], {'cooldown': 1})
            with patch.object(cli, 'RUNTIME', root), patch('sys.argv', ['election_bot', 'diagnostics']), \
                 patch.object(cli, 'key', side_effect=AssertionError('No credentials needed')), \
                 patch.object(cli, 'Sig', side_effect=AssertionError('No network')), \
                 contextlib.redirect_stdout(io.StringIO()) as output:
                cli.main()
            self.assertIn('exit_diagnostics_last_24h', json.loads(output.getvalue()))
            self.assertEqual((root/'live.sqlite3').read_bytes(), before)
