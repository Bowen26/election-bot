from datetime import datetime, timezone
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

import test_active
from test_clock_pool import timing, T, M
from election_bot.clock_guard import check_timing, ClockCheckError, ClockSampleUnavailable, SubmissionClockUnavailable
from election_bot.clients import APIError, Sig
from election_bot.demo import fixture
from election_bot.engine import Engine
from election_bot.runner import run_loop
from election_bot.state import Journal
from election_bot.supervisor import failure_code, RETRY, HALT


class TimingClassificationTests(unittest.TestCase):
    def test_slow_and_expired_samples_are_retryable_with_diagnostics(self):
        sample=dict(timing(),received_at=T+6,received_monotonic=M+6)
        with self.assertRaises(ClockSampleUnavailable) as e:check_timing(sample,T+6.1,M+6.1)
        self.assertEqual(e.exception.detail['request_seconds'],6)
        with self.assertRaises(ClockSampleUnavailable) as e:check_timing(timing(),T+20,M+20)
        self.assertGreater(e.exception.detail['sample_age_seconds'],15)

    def test_slow_sample_detects_discontinuity_before_using_its_offset(self):
        s=dict(timing(),received_at=T+10,received_monotonic=M+6)
        with self.assertRaises(ClockSampleUnavailable) as e:
            check_timing(s,s['received_at']+.1,M+6.1)
        self.assertEqual(e.exception.detail['reason_code'],'clock_discontinuity')
        self.assertEqual(e.exception.detail['request_clock_difference_seconds'],4)

    def test_slow_stable_sample_never_masks_definite_skew(self):
        s=dict(timing(30),received_at=T+6,received_monotonic=M+6)
        with self.assertRaises(ClockCheckError) as e:check_timing(s,T+6.1,M+6.1)
        self.assertNotIsInstance(e.exception,ClockSampleUnavailable)

    def test_clock_adjustment_requires_new_sample_then_rechecks_actual_skew(self):
        original=timing()
        for jump in (-300,300):
            with self.subTest(jump=jump):
                for _ in range(2):
                    with self.assertRaises(ClockSampleUnavailable) as caught:
                        check_timing(original,T+.3+jump,M+.3)
                    self.assertEqual(caught.exception.detail['reason_code'],'clock_discontinuity')
                fresh=dict(timing(),request_started_at=T+jump,received_at=T+jump+.2,
                           http_date_at=T+jump)
                self.assertEqual(check_timing(fresh,T+jump+.3,M+.3)['status'],'verified')
                # A fresh stable clock still 30 seconds off the server stays halted.
                fresh['http_date_at'] += 30
                with self.assertRaises(ClockCheckError) as caught:
                    check_timing(fresh,T+jump+.3,M+.3)
                self.assertNotIsInstance(caught.exception,ClockSampleUnavailable)
        self.assertEqual(original,timing())

    def test_discontinuity_blocks_POST_until_fresh_timing_replaces_old_sample(self):
        sig=Sig.__new__(Sig);sig.tid='test';sig.http=Mock()
        sig.http.last_timing=timing()
        payload={'tournamentId':'test'}
        with patch('election_bot.clock_guard.time.time',return_value=T+300.3), \
             patch('election_bot.clock_guard.time.monotonic',return_value=M+.3):
            with self.assertRaises(SubmissionClockUnavailable):sig.place(payload)
            sig.http.request.assert_not_called()
            sig.http.last_timing=dict(timing(),request_started_at=T+300,received_at=T+300.2,http_date_at=T+300)
            sig.place(payload)
            sig.http.request.assert_called_once_with('/orders','POST',payload=payload)

    def test_negative_timing_and_boundary_remain_hard_halts(self):
        for s in [dict(timing(),cache_age_seconds=-1),dict(timing(),received_monotonic=M-1),timing(5)]:
            with self.subTest(sample=s),self.assertRaises(ClockCheckError) as e:check_timing(s,T+.3,M+.3)
            self.assertNotIsInstance(e.exception,ClockSampleUnavailable)

    def test_SIG_marks_only_clock_failure_before_POST_as_not_submitted(self):
        sig=Sig.__new__(Sig);sig.tid='test';sig.http=Mock()
        sig.check_clock=Mock(side_effect=ClockSampleUnavailable('slow'))
        with self.assertRaises(SubmissionClockUnavailable):sig.place({'tournamentId':'test'})
        sig.http.request.assert_not_called()
        sig.check_clock.side_effect=None
        sig.http.request.side_effect=APIError('POST uncertain',method='POST')
        with self.assertRaises(APIError):sig.place({'tournamentId':'test'})
        sig.http.request.assert_called_once()

    def test_supervisor_distinguishes_temporary_evidence_from_clock_problem(self):
        self.assertEqual(failure_code(ClockSampleUnavailable('slow')),RETRY)
        self.assertEqual(failure_code(ClockCheckError('skew')),HALT)


class ClockLoopTests(unittest.TestCase):
    def engine(self,effects):
        e=Mock();e.stopped.return_value=False;e.cycle.side_effect=effects;e.journal.pending.return_value=[]
        e.config={'execution':{'enabled':True,'batch_pause_seconds':1}}
        return e

    def test_waits_before_fresh_cycle_and_reports_recovery(self):
        e=self.engine([ClockSampleUnavailable('slow'),True,False])
        with patch('election_bot.runner.wait',return_value=True) as wait:run_loop(e)
        self.assertEqual(e.cycle.call_count,3)
        self.assertEqual(wait.call_args_list[0].args,(e,5))
        self.assertIn('clock_recovered',[c.args[0] for c in e.report.call_args_list])

    def test_repeated_bad_samples_back_off_and_STOP_interrupts(self):
        e=self.engine([ClockSampleUnavailable('slow')]*6)
        with patch('election_bot.runner.wait',side_effect=[True]*5+[False]) as wait:run_loop(e)
        self.assertEqual([c.args[1] for c in wait.call_args_list],[5,10,20,40,60,60])
        self.assertNotIn('clock_recovered',[c.args[0] for c in e.report.call_args_list])

    def test_discontinuity_pauses_then_fresh_definite_skew_halts(self):
        try:check_timing(timing(),T+300.3,M+.3)
        except ClockSampleUnavailable as error:jump=error
        e=self.engine([jump,ClockCheckError('Local clock differs from SIG')])
        with patch('election_bot.runner.wait',return_value=True) as wait:
            with self.assertRaisesRegex(ClockCheckError,'differs from SIG'):run_loop(e)
        wait.assert_called_once_with(e,5)
        self.assertNotIn('clock_recovered',[c.args[0] for c in e.report.call_args_list])

    def test_once_and_hard_clock_errors_still_raise(self):
        for error,once in [(ClockSampleUnavailable('slow'),True),(ClockCheckError('skew'),False)]:
            e=self.engine([error])
            with self.subTest(once=once),patch('election_bot.runner.wait') as wait,self.assertRaises(ClockCheckError):run_loop(e,once=once)
            wait.assert_not_called()


class ClockReservationTests(unittest.TestCase):
    def setUp(self):
        self.f=test_active.ActiveTests();self.f.setUp();self.addCleanup(self.f.doCleanups)

    def test_unsubmitted_new_order_releases_only_its_unused_reservation(self):
        f=self.f;f.sig.place=Mock(side_effect=SubmissionClockUnavailable('slow'))
        with self.assertRaises(SubmissionClockUnavailable):f.engine.cycle()
        self.assertFalse(f.journal.pending());self.assertEqual(f.engine.ledger.committed(),0)
        self.assertEqual(f.journal.db.execute('SELECT quantity FROM executions').fetchone()[0],'0')
        self.assertFalse(f.sig.orders)

    def test_unclassified_failure_after_submission_retains_pending_order(self):
        f=self.f;f.sig.place=Mock(side_effect=APIError('unknown outcome',method='POST'))
        with self.assertRaises(APIError):f.engine.cycle()
        self.assertEqual(len(f.journal.pending()),1)
        self.assertGreater(f.engine.ledger.committed(),0)

    def test_explicit_recovery_keeps_old_unknown_reservation_if_clock_blocks_replay(self):
        f=self.f;p={'idempotencyKey':'unknown','exchangeId':'2','tournamentId':f.sig.tid,
                   'action':'buy','side':'yes','quantity':10,'price':.6,
                   'expirationDate':datetime.fromtimestamp(time.time()-60,timezone.utc).isoformat()}
        f.journal.reserve(p,6.1);f.sig.place=Mock(side_effect=SubmissionClockUnavailable('slow'))
        with self.assertRaises(SubmissionClockUnavailable):f.engine.reconcile(replay_unknown=True)
        self.assertEqual(len(f.journal.pending()),1)
        self.assertEqual(f.journal.db.execute('SELECT COUNT(*) FROM executions').fetchone()[0],0)

    def test_account_clock_pause_occurs_before_any_reservation(self):
        f=self.f;f.sig.check_clock=Mock(side_effect=ClockSampleUnavailable('slow',{'request_seconds':6}))
        with self.assertRaises(ClockSampleUnavailable):f.engine.cycle()
        self.assertFalse(f.journal.pending());self.assertFalse(f.sig.orders)
        self.assertEqual(f.journal.db.execute("SELECT COUNT(*) FROM events WHERE kind='clock_sample_unavailable'").fetchone()[0],1)
        self.assertEqual(f.journal.db.execute("SELECT COUNT(*) FROM events WHERE kind='clock_halt'").fetchone()[0],0)

    def test_legacy_engine_also_releases_only_proven_unsent_orders(self):
        c,sig,refs=fixture()
        with tempfile.TemporaryDirectory() as d:
            j=Journal(Path(d)/'journal.db','test')
            try:
                engine=Engine(c,sig,refs,j,d,live=True)
                sig.place=Mock(side_effect=SubmissionClockUnavailable('slow'))
                with self.assertRaises(SubmissionClockUnavailable):engine.cycle()
                self.assertFalse(j.pending());self.assertEqual(j.used(),0)
            finally:j.close()
