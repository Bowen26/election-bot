import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from election_bot.clients import APIError
from election_bot.demo import fixture
from election_bot.engine import Engine, validate_config
from election_bot.scanner import Scanner
from election_bot.state import Journal


class ScannerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.markets = [{'name': str(i)} for i in range(17)]

    def test_full_coverage_across_bounded_batches(self):
        scan = Scanner(self.markets, self.directory.name)
        names = [m['name'] for _ in range(3) for m in scan.batch({}, 8)]
        self.assertEqual(names[:17], [str(i) for i in range(17)])

    def test_continuous_news_and_early_orders_do_not_starve_other_races(self):
        scan = Scanner(self.markets, self.directory.name)
        visited = []
        for _ in range(34):
            # Early return after the first yielded mapping, like a filled order.
            visited.append(next(scan.batch({'0': ['news']}, 8))['name'])
        self.assertEqual(set(visited), {str(i) for i in range(17)})

    def test_restart_keeps_scan_position(self):
        scan = Scanner(self.markets, self.directory.name)
        list(scan.batch({}, 8))
        scan = Scanner(self.markets, self.directory.name)
        self.assertEqual(next(scan.batch({}, 8))['name'], '8')

    def test_no_duplicates_in_batch(self):
        scan = Scanner(self.markets, self.directory.name)
        names = [m['name'] for m in scan.batch({'0': [], '1': [], '16': []}, 17)]
        self.assertEqual(len(set(names)), 17)


class ExpandedEngineTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.config, self.sig, self.refs = fixture()
        self.journal = Journal(Path(self.directory.name) / 'test.sqlite3', 'test')
        self.addCleanup(self.journal.close)

    def engine(self, news=None):
        return Engine(self.config, self.sig, self.refs, self.journal, self.directory.name, news=news)

    def test_more_than_five_supported_and_duplicate_race_rejected(self):
        base = self.config['markets'][0]
        self.config['markets'] = [dict(base, name=str(i), sig_market_id=str(i),
                                      sig_exchange_id=str(i), race_key=str(i)) for i in range(120)]
        validate_config(self.config)
        self.config['markets'][-1]['race_key'] = '0'
        with self.assertRaisesRegex(ValueError, 'one contract per race'):
            validate_config(self.config)

    def test_invalid_batch_size_rejected(self):
        for size in (0, 21, 2.5, True):
            self.config['scan_batch_size'] = size
            with self.assertRaises(ValueError):
                validate_config(self.config)

    def test_read_failure_isolated_but_rate_limit_halts(self):
        self.sig.market = Mock(side_effect=APIError('temporary', status=503))
        self.assertTrue(self.engine().cycle())
        self.assertEqual(self.sig.placed, [])
        self.sig.market.side_effect = APIError('rate limited', status=429)
        with self.assertRaises(APIError):
            self.engine().cycle()

    def test_cash_rechecked_before_order(self):
        original = self.sig.account()
        self.sig.account = Mock(side_effect=[original, dict(original, myBalance=0)])
        self.engine().cycle()
        self.assertEqual(self.journal.summary()['orders'], 0)

    def test_news_for_unscanned_race_retained_next_cycle(self):
        base = self.config['markets'][0]
        self.config['markets'] = [dict(base, name=str(i), sig_market_id=str(i + 10),
                                      sig_exchange_id=str(i + 20)) for i in range(2)]
        self.config['scan_batch_size'] = 1
        news = Mock()
        news.drain.side_effect = [{'0': [{'event_id': 1}], '1': [{'event_id': 2}]}, {}]
        news.block_reason.return_value = None
        # Missing mappings are intentionally skipped, without clearing other news.
        engine = self.engine(news)
        engine.cycle()
        self.assertIn('1', engine.pending_news)
        engine.cycle()
        # Ordinary coverage may scan race 0 again; keep scanning until race 1 gets a turn.
        news.drain.side_effect = None
        news.drain.return_value = {}
        engine.cycle()
        self.assertNotIn('1', engine.pending_news)

    def test_raised_limits_do_not_reset_cumulative_spend(self):
        self.config['limits'].update(per_order='50', per_market='250', total='10000', daily='2000')
        payload = {'idempotencyKey': 'old', 'exchangeId': '2'}
        self.journal.reserve(payload, 249)
        self.journal.complete('old', 249)
        self.engine().cycle()
        self.assertGreaterEqual(self.journal.used(market='2'), 249)
        self.assertLessEqual(self.journal.used(market='2'), 250)
