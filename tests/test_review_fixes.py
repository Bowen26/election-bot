"""Regression cases from the October review; all feeds and fills are offline."""
import copy
import gzip
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

import test_active
from test_clock_pool import timing, T, M
import test_profit_exit as profit
from election_bot.active_engine import ActiveEngine
from election_bot.clients import HTTP
from election_bot.clock_guard import check_timing, ClockCheckError, ClockSampleUnavailable
from election_bot.demo import fixture
from election_bot.engine import validate_config
from election_bot.exit_coverage import exit_coverage
from election_bot.maintenance import archive_events, retention_info, HOT_DAYS, KINDS
from election_bot.news import Article, NewsStore
from election_bot.profit_exit import profit_exit_mode
from election_bot.risk import bind_exposure_modes
from election_bot.state import Journal


class NewsDisabledRegressionTests(unittest.TestCase):
    def test_dispute_on_disabled_sibling_is_durable_without_rolling_back_feed(self):
        with tempfile.TemporaryDirectory() as root:
            config, _, _ = fixture()
            config['news']['enabled'] = True
            config['markets'].append(dict(config['markets'][0], name='disabled sibling',
                                           enabled=False, sig_exchange_id='other'))
            store = NewsStore(Path(root)/'news.db')
            self.addCleanup(store.close)
            source = config['news']['sources'][0]
            now = time.time()
            story = Article('New Hampshire Senate recount', 'https://www.npr.org/recount', now-1)
            self.assertEqual(store.ingest(story, source, config, now), 2)
            self.assertEqual(len(store.status()['unresolved_disputes']), 2)
            self.assertEqual(len(store.drain('live', 21600)), 2)
            with patch('election_bot.news.time.time', return_value=now+86400):
                self.assertIn('review required', store.block_reason('renamed mapping', config['news'], 'other'))
            later = Article('New Hampshire Senate poll', 'https://www.npr.org/poll', now-1)
            self.assertEqual(store.ingest(later, source, config, now), 2)
            self.assertEqual(store.status()['article_count'], 2)
            self.assertEqual(store.ingest(story, source, config, now), 0)

    def test_disabled_placeholder_without_exchange_keeps_dispute_by_name(self):
        with tempfile.TemporaryDirectory() as root:
            config, _, _ = fixture()
            mapping = config['markets'][0]
            mapping['enabled'] = False
            mapping.pop('sig_exchange_id')
            store = NewsStore(Path(root)/'news.db'); self.addCleanup(store.close)
            now = time.time()
            article = Article('New Hampshire Senate recount', 'https://www.npr.org/recount', now-1)
            self.assertEqual(store.ingest(article, config['news']['sources'][0], config, now), 1)
            self.assertIn('review required', store.block_reason(mapping['name'], config['news']))


class ClockEvidenceRegressionTests(unittest.TestCase):
    def test_absent_and_cached_evidence_retry_without_being_accepted(self):
        for sample in [None, {}, dict(timing(), http_date_at=None),
                       dict(timing(-100), cache_age_seconds=100)]:
            with self.subTest(sample=sample), self.assertRaises(ClockSampleUnavailable):
                check_timing(sample, T+.3, M+.3)

    def test_cached_or_missing_Date_cannot_mask_wall_clock_jump(self):
        for sample in [dict(timing(), cache_age_seconds=1), dict(timing(), http_date_at=None)]:
            with self.subTest(sample=sample), self.assertRaises(ClockCheckError) as raised:
                check_timing(sample, T+10, M+.3)
            self.assertNotIsInstance(raised.exception, ClockSampleUnavailable)

    def test_invalid_numeric_evidence_still_halts(self):
        for field, value in [('http_date_at', float('nan')), ('received_at', True),
                             ('cache_age_seconds', -1), ('received_monotonic', float('inf'))]:
            with self.subTest(field=field), self.assertRaises(ClockCheckError) as raised:
                check_timing(dict(timing(), **{field:value}), T+.3, M+.3)
            self.assertNotIsInstance(raised.exception, ClockSampleUnavailable)

    def test_failed_transport_and_JSON_preserve_sample_without_refreshing_age(self):
        client = HTTP('https://clob.polymarket.com')
        original = timing()
        client.last_timing = copy.deepcopy(original); client.server_at = T
        client.opener = Mock(); client.opener.open.side_effect = TimeoutError()
        with self.assertRaises(RuntimeError): client.request('/book')
        response = Mock(); response.read.return_value = b'invalid JSON'
        response.headers = {'Date':'Tue, 14 Nov 2023 22:13:20 GMT'}
        cm = Mock(); cm.__enter__ = Mock(return_value=response); cm.__exit__ = Mock(return_value=False)
        client.opener.open.side_effect = None; client.opener.open.return_value = cm
        with self.assertRaises(ValueError): client.request('/book')
        self.assertEqual(client.last_timing, original)
        self.assertEqual(client.server_at, T)
        with self.assertRaises(ClockSampleUnavailable): check_timing(client.last_timing,T+20,M+20)
        self.assertEqual(check_timing(client.last_timing,T+.3,M+.3)['status'], 'verified')


class StartupRegressionTests(unittest.TestCase):
    def setUp(self):
        self.f = test_active.ActiveTests(); self.f.setUp(); self.addCleanup(self.f.doCleanups)

    def new_engine(self):
        f = self.f
        return ActiveEngine(f.config, f.sig, f.refs, f.journal, f.temp.name, live=True)

    def test_profit_flag_alone_selects_trend_and_needs_history(self):
        f = self.f
        f.config['execution'].update(profit_target_enabled=True, reentry_cooldown_seconds=1800)
        f.config['execution'].pop('profit_exit_mode', None)
        validate_config(f.config)
        self.assertIsNotNone(self.new_engine().exit_trend)
        execution = dict(profit.ENABLED); execution.pop('profit_exit_mode')
        signal, detail = profit.ProfitTargetTests().evaluate(execution=execution)
        self.assertIsNone(signal)
        self.assertIn('trend', str(detail))
        self.assertEqual(profit_exit_mode(profit.ENABLED), 'fixed')
        self.assertEqual(profit_exit_mode({}), 'fixed')

    def test_missing_metadata_for_disabled_holding_fails_at_construction(self):
        f = self.f
        f.engine.cycle()
        f.config['markets'][0]['enabled'] = False
        f.config['limits'].update(net_shares_total='5000', net_shares_per_office='2500')
        f.config['markets'][0].pop('office', None)
        with self.assertRaisesRegex(RuntimeError, 'Unmapped portfolio exposure'): self.new_engine()

    def test_snapshot_starts_unset_and_coverage_logs_only_on_changes(self):
        f = self.f
        self.assertIsNone(f.engine.latest_snapshot_id)
        f.engine.cycle()  # report empty inventory, then buy
        f.engine.cycle()  # report newly owned inventory
        f.engine.cycle()  # unchanged inventory / cooldown
        reports = [json.loads(r[0]) for r in f.journal.db.execute("SELECT detail FROM events WHERE kind='exit_coverage'")]
        self.assertEqual([r['owned_positions'] for r in reports], [0,1])
        self.assertEqual(reports[-1]['without_automatic_exit'],0)

    def test_index_named_exposure_modes_is_not_a_table(self):
        f = self.f
        f.journal.db.execute('CREATE INDEX exposure_modes ON events(kind)')
        f.journal.db.commit()
        bind_exposure_modes(f.engine.ledger, f.config)

    def test_exit_coverage_identifies_disabled_unmapped_and_selling_off(self):
        f = self.f
        f.config['markets'].append(dict(f.config['markets'][0],sig_exchange_id='disabled',enabled=False))
        holdings = {(exchange,'yes'):{'quantity':3} for exchange in ('2','disabled','unknown')}
        result = exit_coverage(holdings, f.config)
        self.assertEqual(result['without_automatic_exit'],2)
        self.assertEqual(result['counts'],{'configured_for_exit':1,'mapping_disabled':1,'unmapped_contract':1})
        f.config['execution']['sell_enabled']=False
        self.assertEqual(exit_coverage(holdings,f.config)['without_automatic_exit'],3)


class ArchiveRegressionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.journal = Journal(self.root/'journal.db', 'test'); self.addCleanup(self.journal.close)
        self.db = self.journal.db
        self.now = time.time(); self.old = self.now-(HOT_DAYS+1)*86400

    def put(self, kind, at=None):
        self.db.execute('INSERT INTO events VALUES (?,?,?)',(self.old if at is None else at,kind,'{"nested": "exact text"}'))
        self.db.commit()

    def test_archive_is_lossless_bounded_and_keeps_orders_signals_and_recent_rows(self):
        for kind in KINDS: self.put(kind)
        self.put('signal'); self.put('order_closed'); self.put('quote_snapshot', self.now)
        original = [tuple(r) for r in self.db.execute('SELECT rowid,at,kind,detail FROM events ORDER BY rowid LIMIT 2')]
        result = archive_events(self.journal,self.root,self.now,limit=2)
        raw = gzip.decompress((self.root/result['file']).read_bytes())
        archived = [json.loads(line) for line in raw.splitlines()]
        self.assertEqual([tuple(row[k] for k in ('rowid','at','kind','detail')) for row in archived],original)
        self.assertEqual(hashlib.sha256(raw).hexdigest(),result['sha256'])
        self.assertEqual(retention_info(self.db)['archived_rows'],2)
        archive_events(self.journal,self.root,self.now)
        self.assertEqual([r[0] for r in self.db.execute('SELECT kind FROM events ORDER BY rowid')],
                         ['signal','order_closed','quote_snapshot'])
        self.assertIsNone(archive_events(self.journal,self.root,self.now))
        self.assertEqual(retention_info(self.db)['archived_rows'],len(KINDS))

    def test_failed_file_write_retains_all_originals(self):
        self.put('quote_snapshot')
        with patch('election_bot.maintenance.os.fsync',side_effect=OSError('disk failure')):
            with self.assertRaises(OSError):archive_events(self.journal,self.root,self.now)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM events').fetchone()[0],1)
        self.assertEqual(retention_info(self.db)['archived_rows'],0)

    def test_failed_database_delete_rolls_back_and_keeps_durable_archive(self):
        self.put('quote_snapshot'); self.put('decision')
        self.db.execute("CREATE TRIGGER deny_delete BEFORE DELETE ON events WHEN OLD.kind='decision' BEGIN SELECT RAISE(ABORT,'blocked'); END")
        self.db.commit()
        with self.assertRaises(sqlite3.IntegrityError):archive_events(self.journal,self.root,self.now)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM events').fetchone()[0],2)
        self.assertEqual(retention_info(self.db)['archived_rows'],0)
        self.assertEqual(len(list((self.root/'event-archive').glob('*.gz'))),1)
        self.db.execute('DROP TRIGGER deny_delete'); self.db.commit()
        self.assertEqual(archive_events(self.journal,self.root,self.now)['rows'],2)

    def test_no_archive_inside_active_transaction_or_invalid_batch_size(self):
        self.put('quote_snapshot')
        for limit in (0,2001,True):
            with self.assertRaises(ValueError):archive_events(self.journal,self.root,self.now,limit)
        self.db.execute('BEGIN')
        with self.assertRaises(RuntimeError):archive_events(self.journal,self.root,self.now)
        self.db.rollback()

    def test_maintenance_runs_between_cycles_and_is_rate_limited(self):
        f = test_active.ActiveTests(); f.setUp(); self.addCleanup(f.doCleanups)
        with patch('election_bot.maintenance.archive_events',return_value=None) as archive:
            with patch('election_bot.engine.time.monotonic',side_effect=[100,101,401]):
                f.engine.maintenance(); f.engine.maintenance(); f.engine.maintenance()
            self.assertEqual(archive.call_count,2)

    def test_verification_failure_keeps_original_rows(self):
        self.put('quote_snapshot')
        reader = Mock(); reader.read.return_value = b'corrupt archive'
        context = Mock(); context.__enter__ = Mock(return_value=reader); context.__exit__ = Mock(return_value=False)
        with patch('election_bot.maintenance.gzip.open',return_value=context):
            with self.assertRaisesRegex(RuntimeError,'verification failed'):
                archive_events(self.journal,self.root,self.now)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM events').fetchone()[0],1)
        self.assertEqual(retention_info(self.db)['archived_rows'],0)

    def test_long_lookback_reports_disclose_archived_measurements(self):
        from election_bot import leadership, exit_study
        self.put('quote_snapshot')
        archive_events(self.journal,self.root,self.now)
        for module in (leadership, exit_study):
            result = module.report(self.root/'journal.db',hours=720,now=self.now)
            self.assertEqual(result['event_retention']['archived_rows'],1)
            self.assertIn('archived',module.format_report(result))
