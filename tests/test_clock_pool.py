from concurrent.futures import ThreadPoolExecutor
import threading
import time
import tempfile
import json
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

import test_active
from election_bot import __main__ as cli
from election_bot.demo import fixture
from election_bot.clock_guard import check_timing, ClockCheckError
from election_bot.clients import APIError, HTTP, References, Sig

T = 1791320000.0
M = 1000.0


def timing(offset=0):
    return {'request_started_at':T,'received_at':T+.2,'http_date_at':T+offset,
            'cache_age_seconds':0,'request_started_monotonic':M,'received_monotonic':M+.2}


class ClockTests(unittest.TestCase):
    def check(self, sample, now=T+.3, mono=M+.3):
        return check_timing(sample, now, mono)

    def test_valid_date_uses_interval_including_header_precision(self):
        result=self.check(timing())
        self.assertEqual(result['status'],'verified')
        self.assertAlmostEqual(result['server_minus_local_seconds'][0],-.2)
        self.assertEqual(result['server_minus_local_seconds'][1],1)
        self.assertEqual(result['max_skew_seconds'],5)

    def test_large_skew_both_directions_is_rejected(self):
        for offset in (-20,20):
            with self.subTest(offset=offset),self.assertRaisesRegex(ClockCheckError,'differs from SIG'):
                self.check(timing(offset))

    def test_ambiguous_boundary_is_not_reported_as_healthy_or_definite_skew(self):
        for offset in (-5,5):
            with self.subTest(offset=offset),self.assertRaisesRegex(ClockCheckError,'inconclusive'):
                self.check(timing(offset))

    def test_missing_cached_invalid_and_expired_evidence_cannot_pass(self):
        cases=[None,{},dict(timing(),http_date_at=None),dict(timing(),cache_age_seconds=1),
               dict(timing(),cache_age_seconds=-1),dict(timing(),received_at=float('nan')),
               dict(timing(),received_monotonic=M-1),dict(timing(),received_at=True)]
        for sample in cases:
            with self.subTest(sample=sample),self.assertRaises(ClockCheckError):self.check(sample)
        with self.assertRaises(ClockCheckError):self.check(timing(),T+20,M+20)

    def test_local_clock_jump_during_request_and_after_receipt_rejects_sample(self):
        with self.assertRaisesRegex(ClockCheckError,'Local clock changed'):
            self.check(dict(timing(),received_at=T+3),T+3.1,M+.3)
        with self.assertRaisesRegex(ClockCheckError,'Local clock changed'):
            self.check(timing(),T+3,M+.3)

    def test_slow_roundtrip_does_not_widen_interval_to_hide_skew(self):
        sample=dict(timing(),received_at=T+6,received_monotonic=M+6)
        with self.assertRaisesRegex(ClockCheckError,'too slow'):
            self.check(sample,T+6.1,M+6.1)

    def test_sig_post_is_blocked_but_reads_and_cancel_remain_available(self):
        sig=Sig.__new__(Sig);sig.tid='test';sig.http=Mock();sig.http.last_timing=None
        with self.assertRaises(ClockCheckError):sig.place({'tournamentId':'test'})
        sig.http.request.assert_not_called()
        sig.order(1);sig.cancel(1)
        self.assertEqual([c.args for c in sig.http.request.call_args_list],[('/orders/1',),('/orders/1','DELETE')])

    def test_successful_sig_post_still_has_single_submission(self):
        sig=Sig.__new__(Sig);sig.tid='test';sig.http=Mock();sig.http.last_timing=timing()
        payload={'tournamentId':'test'}
        with patch('election_bot.clock_guard.time.time',return_value=T+.3),patch('election_bot.clock_guard.time.monotonic',return_value=M+.3):
            sig.place(payload)
        sig.http.request.assert_called_once_with('/orders','POST',payload=payload)

    def test_clock_cli_is_read_only_and_closes_reference_client(self):
        config, _, _ = fixture()
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);path=root/'config.json';path.write_text(json.dumps(config))
            before=path.read_bytes();client=Mock();client.check_clock.return_value={'status':'verified'}
            refs=References()
            with patch.object(cli,'RUNTIME',root/'runtime'),patch.object(cli,'key',return_value='synthetic-test-key'), \
                 patch.object(cli,'Sig',return_value=client),patch.object(cli,'References',return_value=refs), \
                 patch.object(cli,'Journal') as journal,patch.object(cli,'output') as output, \
                 patch('sys.argv',['bot','--config',str(path),'clock-check']):
                cli.main()
            client.check_clock.assert_called_once();client.place.assert_not_called();client.cancel.assert_not_called()
            journal.assert_not_called();output.assert_called_once_with({'status':'verified'})
            self.assertTrue(refs._closed);self.assertEqual(before,path.read_bytes())
            self.assertFalse(list(root.rglob('*.sqlite3')))

    def test_http_records_monotonic_timing_without_sensitive_headers(self):
        http=HTTP('https://sig.thesuper.market/api/v1')
        response=Mock();response.read.return_value=b'{}'
        response.headers={'Date':'Tue, 06 Oct 2026 20:00:00 GMT','Age':'0','Authorization':'secret'}
        http.opener=Mock();http.opener.open.return_value.__enter__=Mock(return_value=response)
        http.opener.open.return_value.__exit__=Mock(return_value=False)
        with patch('election_bot.clients.time.time',side_effect=[T,T+.2]),patch('election_bot.clients.time.monotonic',side_effect=[M,M+.2]):
            http.request('/tournaments/test')
        self.assertEqual(http.last_timing['request_started_monotonic'],M)
        self.assertEqual(http.last_timing['received_monotonic'],M+.2)
        self.assertNotIn('secret',str(http.last_timing))


class ClockIntegrationTests(unittest.TestCase):
    def setUp(self):
        # Reuse the established fully offline broker, not the inherited test suite.
        self.case=test_active.ActiveTests();self.case.setUp();self.addCleanup(self.case.doCleanups)

    def test_clock_failure_stops_before_quotes_reservations_or_orders(self):
        self.case.sig.check_clock=Mock(side_effect=ClockCheckError('clock skew'))
        with self.assertRaises(ClockCheckError):self.case.engine.cycle()
        self.assertFalse(self.case.sig.orders);self.assertFalse(self.case.journal.pending())
        self.assertEqual(self.case.journal.db.execute("SELECT COUNT(*) FROM events WHERE kind='clock_halt'").fetchone()[0],1)

    def test_clock_is_rechecked_after_preflight_before_reserving(self):
        self.case.sig.check_clock=Mock(side_effect=[{'status':'verified'}, {'status':'verified'}, ClockCheckError('clock changed')])
        with self.assertRaises(ClockCheckError):self.case.engine.cycle()
        self.assertFalse(self.case.sig.orders);self.assertFalse(self.case.journal.pending())

    def test_healthy_clock_is_checked_repeatedly_but_success_logged_once(self):
        self.case.sig.check_clock=Mock(return_value={'status':'verified'})
        self.case.engine.cycle()
        self.assertGreaterEqual(self.case.sig.check_clock.call_count,3)
        self.assertEqual(self.case.journal.db.execute("SELECT COUNT(*) FROM events WHERE kind='clock_check'").fetchone()[0],1)
        self.assertEqual(len(self.case.sig.orders),1)


class PoolTests(unittest.TestCase):
    def setUp(self):
        self.refs=References();self.addCleanup(self.refs.close)
        self.mapping={'kalshi_ticker':'X','kalshi_yes_matches_sig_yes':True,'polymarket_yes_matches_sig_yes':True}
        self.metadata=({'status':'active'},{'active':True,'closed':False,'acceptingOrders':True,'conditionId':'condition'},'token')
        self.kdata={'orderbook_fp':{'yes_dollars':[['.5',100]],'no_dollars':[['.4',100]]}}
        self.pdata={'asset_id':'token','market':'condition','timestamp':str(int(T*1000)),
                    'bids':[{'price':'.5','size':100}],'asks':[{'price':'.6','size':100}]}
        self.refs.kalshi.request=Mock(return_value=self.kdata)
        self.refs.clob.request=Mock(return_value=self.pdata)
        self.refs.kalshi.server_at=T

    def test_pool_is_lazy_reused_and_fixed_at_two_workers(self):
        self.assertIsNone(self.refs._pool)
        with patch('election_bot.clients.ThreadPoolExecutor',wraps=ThreadPoolExecutor) as factory:
            self.refs.books(self.mapping,self.metadata);pool=self.refs._pool
            self.refs.books(self.mapping,self.metadata)
            self.assertIs(self.refs._pool,pool);factory.assert_called_once_with(max_workers=2,thread_name_prefix='reference-feed')
        self.assertEqual(self.refs.kalshi.request.call_count,2);self.assertEqual(self.refs.clob.request.call_count,2)

    def test_workers_are_concurrent_and_response_timestamps_remain_separate(self):
        barrier=threading.Barrier(2)
        def k(*a,**kw):
            barrier.wait(timeout=2);self.refs.kalshi.last_timing={'received_at':T+1};return self.kdata
        def p(*a,**kw):
            barrier.wait(timeout=2);self.refs.clob.last_timing={'received_at':T+2};return self.pdata
        self.refs.kalshi.request.side_effect=k;self.refs.clob.request.side_effect=p
        a,b=self.refs.books(self.mapping,self.metadata)
        self.assertEqual((a.observed_at,b.observed_at),(T+1,T+2))
        self.assertEqual(b.source_at,T)

    def test_failure_drains_partner_before_return_and_pool_remains_usable(self):
        entered=threading.Event();release=threading.Event();done=threading.Event();errors=[]
        def slow(*a,**kw):
            entered.set()
            if not release.wait(2):raise TimeoutError('test release missing')
            return self.pdata
        self.refs.clob.request.side_effect=slow
        self.refs.kalshi.request.side_effect=APIError('read failed',method='GET')
        def call():
            try:self.refs.books(self.mapping,self.metadata)
            except Exception as e:errors.append(e)
            finally:done.set()
        worker=threading.Thread(target=call);worker.start()
        try:
            self.assertTrue(entered.wait(2));self.assertFalse(done.is_set())
        finally:
            release.set();worker.join(3)
        self.assertTrue(done.is_set());self.assertIsInstance(errors[0],APIError)
        pool=self.refs._pool
        self.refs.kalshi.request.side_effect=None;self.refs.clob.request.side_effect=None
        self.refs.books(self.mapping,self.metadata);self.assertIs(self.refs._pool,pool)

    def test_second_submission_failure_still_drains_first_request(self):
        entered=threading.Event();release=threading.Event();done=threading.Event();errors=[]
        underlying=ThreadPoolExecutor(max_workers=2)
        self.addCleanup(underlying.shutdown,wait=True)
        pool=Mock();pool.submit.side_effect=[underlying.submit(lambda: (entered.set(),release.wait(2))),RuntimeError('cannot start worker')]
        pool.shutdown.side_effect=underlying.shutdown
        self.refs._pool=pool
        def call():
            try:self.refs.books(self.mapping,self.metadata)
            except RuntimeError as e:errors.append(str(e))
            finally:done.set()
        worker=threading.Thread(target=call);worker.start()
        try:
            self.assertTrue(entered.wait(2));self.assertFalse(done.is_set())
        finally:
            release.set();worker.join(3)
        self.assertTrue(done.is_set());self.assertEqual(errors,['cannot start worker'])

    def test_two_callers_cannot_overlap_shared_client_state(self):
        entered=threading.Event();release=threading.Event()
        def slow(*a,**kw):
            entered.set()
            if not release.wait(2):raise TimeoutError('test release missing')
            return self.kdata
        self.refs.kalshi.request.side_effect=slow
        with ThreadPoolExecutor(max_workers=2) as callers:
            first=callers.submit(self.refs.books,self.mapping,self.metadata)
            self.assertTrue(entered.wait(2))
            second=callers.submit(self.refs.books,self.mapping,self.metadata)
            try:
                self.assertEqual(self.refs.kalshi.request.call_count,1)
            finally:release.set()
            first.result(timeout=3);second.result(timeout=3)
        self.assertEqual(self.refs.kalshi.request.call_count,2)

    def test_context_manager_closes_on_exception_and_rejects_reuse(self):
        with self.assertRaisesRegex(ValueError,'test'):
            with self.refs:
                self.refs.books(self.mapping,self.metadata)
                raise ValueError('test')
        self.refs.close()
        with self.assertRaisesRegex(RuntimeError,'closed'):self.refs.books(self.mapping,self.metadata)
        with self.assertRaisesRegex(RuntimeError,'closed'):self.refs.metadata(self.mapping)
        with self.assertRaises(RuntimeError):self.refs._pool.submit(lambda:None)

    def test_metadata_only_client_closes_without_creating_workers(self):
        self.refs.close();self.assertIsNone(self.refs._pool)
