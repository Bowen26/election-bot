from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from election_bot.ledger import Ledger
from election_bot.measurement import WINDOWS
from election_bot.performance import report
from election_bot.state import Journal
from election_bot.strategy import Book, D


class MeasurementTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'journal.db'
        self.journal = Journal(self.path, 'measurement-test')
        self.addCleanup(self.journal.close)
        self.ledger = Ledger(self.journal)
        self.fill_time = self.ledger.short_horizons_started_at + 1

    def fill(self, key='buy', exchange='2', quantity=10, price='.5', side='yes', at=None, action='buy'):
        payload = dict(idempotencyKey=key, exchangeId=exchange, quantity=quantity,
                       price=price, side=side, action=action)
        self.journal.reserve(payload, D(quantity)*(D(price)+D('.01')))
        self.ledger.record(payload, quantity, price, '.01', self.fill_time if at is None else at)

    def observe(self, at, bids=None, asks=None, source_at=None):
        with patch('election_bot.strategy.time.time', return_value=at):
            book = Book.make(bids or [('.6', 100)], asks or [('.7', 100)], source_at=source_at)
            self.ledger.observe('2', book, 15, at)

    def marks(self):
        return {r['horizon']: dict(r) for r in self.journal.db.execute('SELECT * FROM markouts')}

    def test_each_horizon_is_measured_once_and_restart_preserves_it(self):
        self.fill()
        for horizon in WINDOWS:
            self.observe(self.fill_time+horizon+1)
        self.assertEqual(set(self.marks()), set(WINDOWS))
        self.assertTrue(all(D(r['pnl']) == D('.8') for r in self.marks().values()))
        original = self.marks()
        started = self.ledger.short_horizons_started_at
        self.ledger = Ledger(self.journal)
        self.assertEqual(self.ledger.short_horizons_started_at, started)
        self.observe(self.fill_time+86402, bids=[('.65', 100)])
        self.assertEqual(self.marks(), original)

    def test_no_early_sample_even_if_fetch_happens_after_target(self):
        self.fill()
        self.observe(self.fill_time+59)
        self.assertFalse(self.marks())
        self.observe(self.fill_time+61, source_at=self.fill_time+59)
        self.assertFalse(self.marks())
        self.observe(self.fill_time+62)
        self.assertEqual(set(self.marks()), {60})

    def test_stale_quote_never_counts(self):
        self.fill()
        with self.assertRaisesRegex(ValueError, 'Stale'):
            self.observe(self.fill_time+80, source_at=self.fill_time+60)
        self.assertFalse(self.marks())

    def test_full_depth_weighted_value_and_no_outcome(self):
        self.fill(side='no')
        self.observe(self.fill_time+61, bids=[('.3', 100)], asks=[('.35', 3), ('.4', 7)])
        # NO bids .65 x 3 and .60 x 7; subtract .50 entry and two .01 buffers.
        self.assertEqual(D(self.marks()[60]['pnl']), D('.95'))

    def test_thin_depth_retries_but_late_liquidity_cannot_backfill(self):
        self.fill()
        self.observe(self.fill_time+61, bids=[('.6', 3)])
        self.assertFalse(self.marks())
        self.assertEqual(self.ledger.due_exchanges(self.fill_time+119), ['2'])
        self.observe(self.fill_time+121)
        self.assertIsNone(self.marks()[60]['pnl'])
        self.assertEqual(self.marks()[60]['reason'], 'Observation window missed')

    def test_offline_windows_expire_without_any_quote(self):
        self.fill()
        self.assertEqual(self.ledger.due_exchanges(self.fill_time+200), [])
        self.assertIsNone(self.marks()[60]['pnl'])

    def test_observation_queue_is_bounded_deduplicated_and_earliest_first(self):
        for i in range(6):
            self.fill(key=str(i), exchange=str(i), at=self.fill_time+i)
        self.fill(key='same-market', exchange='0', at=self.fill_time+1)
        self.assertEqual(self.ledger.due_exchanges(self.fill_time+70), ['0', '1', '2', '3'])

    def test_sold_positions_still_get_hypothetical_buy_measurements(self):
        self.fill()
        self.fill(key='sale', action='sell', price='.7', at=self.fill_time+10)
        self.observe(self.fill_time+61)
        self.assertEqual(D(self.marks()[60]['pnl']), D('.8'))
        self.assertEqual(self.ledger.held('2')[0], 0)
        self.assertEqual(self.journal.db.execute('SELECT COUNT(*) FROM valuations').fetchone()[0], 0)

    def test_upgrade_preserves_legacy_measurements_and_excludes_old_short_horizons(self):
        old_at = self.fill_time-4000
        self.fill(at=old_at)
        with self.journal.db:
            self.journal.db.execute('INSERT INTO markouts VALUES (?,?,?,?,?)',
                                    ('buy', 3600, old_at+3601, '.8', None))
            self.journal.db.execute('DROP TABLE performance_settings')
        before = [tuple(r) for r in self.journal.db.execute('SELECT * FROM orders')]
        self.ledger = Ledger(self.journal)
        self.ledger.expire_observations(self.fill_time+5000)
        self.assertEqual(set(self.marks()), {3600})
        self.assertEqual([tuple(r) for r in self.journal.db.execute('SELECT * FROM orders')], before)
        with patch('election_bot.performance.time.time', return_value=self.fill_time+5000):
            result = report(self.path)
        self.assertEqual(result['buy_markouts_seconds']['60']['eligible_buys'], 0)
        self.assertEqual(result['buy_markouts_seconds']['3600']['observations'], 1)

    def test_report_exposes_missing_pending_delay_and_normalized_returns(self):
        self.fill()
        self.observe(self.fill_time+61)
        self.fill(key='missed', exchange='3', at=self.fill_time)
        self.fill(key='pending', exchange='4', at=self.fill_time+100)
        self.fill(key='future', exchange='5', at=self.fill_time+190)
        before = self.path.read_bytes()
        with patch('election_bot.performance.time.time', return_value=self.fill_time+200):
            result = report(self.path)
        self.assertEqual(self.path.read_bytes(), before)
        row = result['buy_markouts_seconds']['60']
        self.assertEqual((row['observations'], row['missed'], row['awaiting_quote'], row['not_due']), (1, 1, 1, 1))
        self.assertEqual(row['coverage_percent'], 33.33)
        self.assertEqual(D(row['pnl_per_share_after_buffers']), D('.08'))
        self.assertEqual(row['average_delay_seconds'], 1)
        self.assertEqual(row['positive_percent'], 100)
        self.assertEqual(len(result['buy_markouts_by_market']), 4)

    def test_legacy_report_never_creates_schema(self):
        self.fill()
        with self.journal.db:
            self.journal.db.execute('DROP TABLE performance_settings')
        before = self.path.read_bytes()
        result = report(self.path)
        self.assertIsNone(result['short_horizons_started_at'])
        self.assertEqual(result['buy_markouts_seconds']['60']['eligible_buys'], 0)
        self.assertEqual(self.path.read_bytes(), before)
