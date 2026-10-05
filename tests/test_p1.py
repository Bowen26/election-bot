import copy
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

import test_active
from election_bot.clients import APIError, Sig, iso_time
from election_bot.demo import fixture
from election_bot.ledger import inventory_from_executions
from election_bot.news import Article, NewsGate, NewsStore, position_dispute_report
from election_bot.performance import report
from election_bot.strategy import D


def fill_page(rows, more=False, cursor=None, total=40, average=.6):
    return {'orderId': 1, 'exchangeId': '2', 'tournamentId': 'offline-demo-tournament',
            'coverage': {'complete': True, 'projectedThroughSequence': 100},
            'totalQuantityFilled': total, 'avgFillPrice': average, 'data': rows,
            'pagination': {'limit': 100, 'hasMore': more, 'nextCursor': cursor}}


def fill_row(identity, quantity, at='2026-10-05T12:00:00Z'):
    return {'id': identity, 'quantity': quantity, 'price': .6,
            'side': 'yes' if quantity > 0 else 'no', 'filledAt': at}


class FillPaginationTests(unittest.TestCase):
    def setUp(self):
        self.sig = Sig.__new__(Sig)
        self.sig.tid = 'offline-demo-tournament'
        self.sig.read = Mock()

    def pages(self):
        return [fill_page([fill_row(11, 10)], True, 'cursor-1'),
                fill_page([fill_row(12, 30, '2026-10-05T12:00:15Z')])]

    def test_all_pages_collected_and_last_timestamp_retained(self):
        self.sig.read.side_effect = self.pages()
        result = self.sig.fills(1)
        self.assertEqual(result['pages_read'], 2)
        self.assertEqual(len(result['data']), 2)
        self.assertEqual(result['totalQuantityFilled'], 40)
        self.assertEqual(max(iso_time(r['filledAt']) for r in result['data']), iso_time('2026-10-05T12:00:15Z'))
        self.assertEqual(self.sig.read.call_args_list[0].kwargs['params'], {'limit': 100})
        self.assertEqual(self.sig.read.call_args_list[1].kwargs['params'], {'limit': 100, 'cursor': 'cursor-1'})

    def test_no_side_signed_quantities_and_zero_fills(self):
        self.sig.read.side_effect = [fill_page([fill_row(1, -10)], True, 'c', total=-40),
                                     fill_page([fill_row(2, -30)], total=-40)]
        self.assertEqual(self.sig.fills(1)['totalQuantityFilled'], -40)
        self.sig.read.side_effect = [fill_page([], total=0, average=None)]
        self.assertEqual(self.sig.fills(1)['data'], [])

    def test_projection_sequence_can_advance_without_order_changing(self):
        pages = self.pages()
        pages[1]['coverage']['projectedThroughSequence'] = 999
        self.sig.read.side_effect = pages
        self.assertEqual(self.sig.fills(1)['pages_read'], 2)

    def test_wrong_identity_incomplete_projection_and_changed_totals_rejected(self):
        for field, value in [('orderId', 99), ('exchangeId', '3'), ('tournamentId', 'wrong'),
                             ('coverage', {'complete': False}), ('totalQuantityFilled', 41),
                             ('avgFillPrice', .7)]:
            pages = self.pages()
            pages[1][field] = value
            self.sig.read.side_effect = pages
            with self.subTest(field=field), self.assertRaisesRegex(RuntimeError, 'reservation retained'):
                self.sig.fills(1)

    def test_rows_must_cover_total_without_duplicates(self):
        for bad in (fill_row(11, 30), fill_row(12, 29), fill_row(12, -30)):
            pages = self.pages(); pages[1]['data'] = [bad]
            self.sig.read.side_effect = pages
            with self.subTest(row=bad), self.assertRaises(RuntimeError):
                self.sig.fills(1)

    def test_missing_cursor_repeated_cursor_and_empty_intermediate_page_rejected(self):
        for page in (fill_page([fill_row(11, 10)], True, None), fill_page([], True, 'c')):
            self.sig.read.side_effect = [page]
            with self.assertRaisesRegex(RuntimeError, 'Incomplete fill pagination'):
                self.sig.fills(1)
        self.sig.read.side_effect = [fill_page([fill_row(11, 10)], True, 'c'),
                                     fill_page([fill_row(12, 10)], True, 'c')]
        with self.assertRaisesRegex(RuntimeError, 'Incomplete fill pagination'):
            self.sig.fills(1)

    def test_page_limit_bounds_requests(self):
        self.sig.read.side_effect = [fill_page([fill_row(i+1, 1)], True, str(i+1), total=101) for i in range(100)]
        with self.assertRaisesRegex(RuntimeError, 'pagination limit exceeded'):
            self.sig.fills(1)
        self.assertEqual(self.sig.read.call_count, 100)

    def test_naive_timestamp_on_later_page_rejected(self):
        pages = self.pages(); pages[1]['data'][0]['filledAt'] = '2026-10-05T12:00:15'
        self.sig.read.side_effect = pages
        with self.assertRaisesRegex(ValueError, 'timezone'):
            self.sig.fills(1)

    def test_later_page_failure_does_not_return_partial_history(self):
        self.sig.read.side_effect = [self.pages()[0], APIError('unavailable', status=503, method='GET')]
        with self.assertRaises(APIError):
            self.sig.fills(1)


class FIFOTests(unittest.TestCase):
    def test_pure_fifo_preserves_input_and_matches_partial_no_inventory(self):
        rows = [dict(exchange='2', side='no', action=a, quantity=q, price=p, buffer='.01')
                for a,q,p in [('buy',10,'.3'), ('buy',10,'.5'), ('sell',15,'.6')]]
        original = copy.deepcopy(rows)
        holdings, realized = inventory_from_executions(rows)
        self.assertEqual(rows, original)
        self.assertEqual(holdings['2','no']['quantity'], 5)
        self.assertEqual(holdings['2','no']['cost'], D('2.55'))
        self.assertEqual(realized['2'], D('3.20'))
        self.assertEqual(inventory_from_executions(rows), (holdings, realized))

    def test_oversell_still_fails_closed(self):
        with self.assertRaisesRegex(RuntimeError, 'Sell exceeds'):
            inventory_from_executions([dict(exchange='2', side='yes', action='sell', quantity=1, price='.5', buffer='.01')])


class DisputeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)/'news.sqlite3'
        self.config, _, _ = fixture()
        self.config['news']['enabled'] = True
        self.mapping = self.config['markets'][0]
        self.source = self.config['news']['sources'][0]
        self.store = NewsStore(self.path)
        self.addCleanup(lambda: self.store.close())
        self.store.health(self.source, time.time())

    def dispute(self, suffix=''):
        article = Article('New Hampshire Senate election disputed results '+suffix,
                          'https://www.npr.org/story'+suffix, time.time()-10)
        self.store.ingest(article, self.source, self.config)
        return article

    def test_flag_survives_pause_expiry_restart_and_mapping_rename(self):
        self.dispute()
        self.store.db.execute('UPDATE pauses SET until=0');self.store.db.commit()
        self.store.close();self.store = NewsStore(self.path)
        self.assertIn('review required', self.store.block_reason(self.mapping['name'], self.config['news']))
        self.assertIn('review required', self.store.block_reason('renamed', self.config['news'], '2'))
        self.assertIsNone(self.store.block_reason('other', self.config['news'], '99'))

    def test_specific_review_preserves_other_flags_and_newer_pause(self):
        self.dispute('one');self.dispute('two')
        flags = self.store.status()['unresolved_disputes']
        self.store.clear_dispute(flags[0]['article'], self.mapping['name'], 'Reviewed original source; report corrected')
        self.assertEqual(len(self.store.status()['unresolved_disputes']), 1)
        self.assertEqual(len(self.store.status()['active_pauses']), 1)
        self.assertIn('review required', self.store.block_reason(self.mapping['name'], self.config['news']))
        self.store.clear_dispute(flags[1]['article'], self.mapping['name'], 'Second report also reviewed')
        self.assertIsNone(self.store.block_reason(self.mapping['name'], self.config['news']))
        rows = self.store.db.execute('SELECT cleared_at,review_note FROM disputes').fetchall()
        self.assertTrue(all(r['cleared_at'] and r['review_note'] for r in rows))

    def test_duplicate_does_not_rearm_cleared_flag_but_new_article_does(self):
        article = self.dispute()
        aid = self.store.status()['unresolved_disputes'][0]['article']
        self.store.clear_dispute(aid, self.mapping['name'], 'Reviewed')
        self.store.ingest(article, self.source, self.config)
        self.assertFalse(self.store.status()['unresolved_disputes'])
        self.dispute('new report')
        self.assertTrue(self.store.status()['unresolved_disputes'])

    def test_review_requires_exact_flag_and_nonempty_note(self):
        self.dispute();aid = self.store.status()['unresolved_disputes'][0]['article']
        for identity, market, note in [(aid, 'wrong', 'Reviewed'), (aid, self.mapping['name'], ' '),
                                       (999, self.mapping['name'], 'Reviewed')]:
            with self.assertRaises(ValueError):
                self.store.clear_dispute(identity, market, note)
        self.assertTrue(self.store.status()['unresolved_disputes'])

    def test_alert_only_and_stale_stories_never_create_persistent_flag(self):
        article = Article('New Hampshire Senate election recount', 'https://www.npr.org/a', time.time()-10)
        self.store.ingest(article, {**self.source, 'pause_on_risk': False}, self.config)
        stale = Article('New Hampshire Senate election contested results', 'https://www.npr.org/b', time.time()-90000)
        self.store.ingest(stale, self.source, self.config)
        self.assertFalse(self.store.status()['unresolved_disputes'])
        self.store.ingest(article, self.source, self.config)
        self.assertEqual(len(self.store.status()['unresolved_disputes']), 1)

    def test_migrate_only_active_legacy_disputes(self):
        self.dispute('active');self.dispute('expired')
        flags=self.store.status()['unresolved_disputes']
        self.store.db.execute('DELETE FROM disputes')
        # The single current pause refers to the expired story; it must not be revived.
        self.store.db.execute('UPDATE pauses SET until=0');self.store.db.commit()
        self.store.migrate_active_disputes(self.config)
        self.assertFalse(self.store.status()['unresolved_disputes'])
        self.store.db.execute('UPDATE pauses SET until=?,article=?', (time.time()+60, flags[0]['article']))
        self.store.db.commit();self.store.migrate_active_disputes(self.config)
        self.assertEqual(len(self.store.status()['unresolved_disputes']), 1)
        self.store.migrate_active_disputes(self.config)
        self.assertEqual(len(self.store.status()['unresolved_disputes']), 1)

    def test_disabled_placeholder_mapping_does_not_break_news_migration(self):
        self.config['markets'].insert(0, {'enabled': False})
        self.dispute()
        self.store.migrate_active_disputes(self.config)
        self.assertEqual(len(self.store.status()['unresolved_disputes']), 1)

    def test_read_only_position_report_keeps_flag_when_race_disabled(self):
        self.dispute();self.mapping['enabled'] = False
        holdings = {('2','no'): {'quantity': D(10), 'cost': D('3.5')}}
        result=position_dispute_report(self.path, holdings)
        self.assertTrue(result['available'])
        self.assertEqual(result['positions'][0]['quantity'], '10')
        self.assertEqual(result['positions'][0]['status'], 'disputed_result_review_required')
        self.assertEqual(result['positions'][0]['side'], 'no')
        missing = Path(self.temp.name)/'missing.db'
        self.assertFalse(position_dispute_report(missing, holdings)['available'])
        self.assertFalse(missing.exists())

    def test_read_only_report_does_not_migrate_legacy_news_database(self):
        path = Path(self.temp.name)/'legacy.sqlite3'
        db=sqlite3.connect(path)
        db.execute('CREATE TABLE articles(id INTEGER PRIMARY KEY)');db.commit();db.close()
        before=path.read_bytes()
        result=position_dispute_report(path,{})
        self.assertFalse(result['available'])
        self.assertIn('Restart',result['reason'])
        self.assertEqual(path.read_bytes(),before)

    def test_cli_review_is_local_and_does_not_read_credentials(self):
        from election_bot import __main__ as cli
        self.dispute();aid=self.store.status()['unresolved_disputes'][0]['article']
        config_path=Path(self.temp.name)/'config.json';config_path.write_text(json.dumps(self.config))
        with patch.object(cli,'RUNTIME',Path(self.temp.name)), patch.object(cli,'key') as key, \
             patch.object(cli,'output') as output, patch('sys.argv', ['bot','--config',str(config_path),
                'news','--clear-dispute',str(aid),'--mapping',self.mapping['name'],'--note','Source reviewed']):
            cli.main()
            key.assert_not_called()
            self.assertTrue(output.call_args_list[0].args[0]['cleared'])
        self.assertFalse(self.store.status()['unresolved_disputes'])


class LifecycleIntegrationTests(unittest.TestCase):
    def setUp(self):
        test_active.ActiveTests.setUp(self)

    def test_execution_time_uses_last_fill_from_all_pages(self):
        client=Sig.__new__(Sig);client.tid=self.sig.tid
        pages=[fill_page([fill_row(1,10)],True,'next'),fill_page([fill_row(2,30,'2026-10-05T12:00:15Z')])]
        client.read=Mock(side_effect=pages);self.sig.fills=client.fills
        self.engine.cycle()
        row=self.journal.db.execute('SELECT * FROM executions').fetchone()
        self.assertEqual(row['at'],iso_time('2026-10-05T12:00:15Z'))
        self.assertEqual(D(row['quantity']),40)

    def test_failed_later_fill_page_keeps_order_reserved(self):
        client=Sig.__new__(Sig);client.tid=self.sig.tid
        client.read=Mock(side_effect=[fill_page([fill_row(1,10)],True,'next'),
                                      APIError('503',status=503,method='GET')])
        self.sig.fills=client.fills
        with self.assertRaises(APIError):self.engine.cycle()
        self.assertEqual(len(self.journal.pending()),1)
        self.assertEqual(self.journal.db.execute('SELECT COUNT(*) FROM executions').fetchone()[0],0)
        self.assertEqual(len(self.sig.orders),1)

    def test_nonzero_fill_without_rows_retains_reservation(self):
        original=self.sig.fills
        self.sig.fills=lambda oid: dict(original(oid),data=[])
        with self.assertRaisesRegex(RuntimeError,'no timestamped rows'):
            self.engine.cycle()
        self.assertEqual(len(self.journal.pending()),1)
        self.assertEqual(self.journal.db.execute('SELECT COUNT(*) FROM executions').fetchone()[0],0)

    def test_disputed_owned_position_remains_flagged_and_unsold_after_timer(self):
        self.engine.cycle()
        self.journal.db.execute('UPDATE orders SET created=?',(time.time()-1000,));self.journal.db.commit()
        self.config['news']['enabled']=True
        gate=NewsGate(self.config,self.temp.name,'test');self.addCleanup(gate.close)
        self.engine.news=gate
        source=self.config['news']['sources'][0];gate.store.health(source,time.time())
        gate.store.ingest(Article('New Hampshire Senate election disputed results',
            'https://www.npr.org/dispute',time.time()-10),source,self.config)
        gate.store.db.execute('UPDATE pauses SET until=0');gate.store.db.commit()
        self.sig.prices['2']=('.80','.85')  # Normally eligible for an exit.
        self.engine.cycle()
        self.assertEqual(len(self.sig.orders),1)
        flags=report(Path(self.temp.name)/'journal.db')['position_news_risk']
        self.assertEqual(flags['positions'][0]['quantity'],'40')
        self.assertTrue(flags['available'])
