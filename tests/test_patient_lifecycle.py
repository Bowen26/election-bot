import json
import unittest
from unittest.mock import patch,Mock
from datetime import datetime,timezone
from election_bot.patient_wait import observe
from election_bot.patient_entry import sized_control,settings,arm
from election_bot.patient_report import report
from election_bot.strategy import D
import test_patient_entry as fixture


class WaitTests(unittest.TestCase):
    def setUp(self):
        self.now=100.;self.reads=0
        self.payload={'exchangeId':'2','tournamentId':'t','action':'buy','side':'yes','price':'.5',
                      'expirationDate':datetime.fromtimestamp(115,timezone.utc).isoformat()}
        self.order={'id':1,'exchangeId':'2','tournamentId':'t','action':'buy','side':'yes','priceLimit':'.5','open':True}
    def run_wait(self,**kwargs):
        def sleep(n):self.now+=n
        def refresh(oid):self.reads+=1;return dict(self.order)
        defaults=dict(refresh=refresh,eligible=lambda:True,stopped=lambda:False,
                      wall=lambda:self.now,monotonic=lambda:self.now,sleep=sleep)
        defaults.update(kwargs)
        return observe(self.payload,self.order,5,**defaults)
    def test_wait_is_bounded_and_does_not_resubmit(self):
        _,reason=self.run_wait()
        self.assertEqual(reason,'patience_expired');self.assertEqual(self.now,105)
        self.assertEqual(self.reads,4)
    def test_signal_and_stop_cancel_without_sleep(self):
        self.assertEqual(self.run_wait(eligible=lambda:False)[1],'signal_invalidated')
        self.assertEqual(self.run_wait(stopped=lambda:True)[1],'stop')
        self.assertEqual(self.now,100)
    def test_exchange_expiry_shortens_patience(self):
        self.now=112;self.assertEqual(self.run_wait()[1],'patience_expired');self.assertEqual(self.now,113)
    def test_network_delay_does_not_extend_deadline(self):
        def eligible():self.now+=8;return True
        self.assertEqual(self.run_wait(eligible=eligible)[1],'patience_expired');self.assertEqual(self.reads,0)
    def test_closed_fill_finishes_without_cancel_request(self):
        self.assertEqual(self.run_wait(refresh=lambda _:dict(self.order,open=False))[1],'closed')
    def test_changed_identity_is_not_accepted(self):
        with self.assertRaises(RuntimeError):self.run_wait(refresh=lambda _:dict(self.order,exchangeId='evil'))


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.f=fixture.ThroughTheEngine();self.addCleanup(self.f.doCleanups)
        self.engine=self.f.engine();self.sig=self.f.sig
    def open_order(self):
        original=self.sig.place
        def place(payload):
            response=original(payload);self.sig.orders[response['orderId']]['open']=True
            return dict(response,open=True)
        self.sig.place=place
    def test_partial_fill_cancel_and_account_once(self):
        self.sig.partial=7;self.open_order()
        with patch('election_bot.patient_wait.observe',side_effect=lambda p,o,*args:(o,'patience_expired')) as wait:
            self.engine.cycle()
        self.assertEqual(wait.call_count,1);self.assertEqual(self.sig.cancelled,[1])
        self.assertFalse(self.f.journal.pending())
        self.assertEqual(self.engine.ledger.held('2')[0],D(7))
        self.engine.reconcile();self.assertEqual(self.engine.ledger.held('2')[0],D(7))
    def test_interrupt_cancels_and_records_before_propagating(self):
        self.sig.partial=3;self.open_order()
        with patch('election_bot.patient_wait.observe',side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):self.engine.cycle()
        self.assertEqual(self.sig.cancelled,[1]);self.assertFalse(self.f.journal.pending())
        self.assertEqual(self.engine.ledger.held('2')[0],D(3))
    def test_monitor_error_cancels_and_records_before_propagating(self):
        self.open_order()
        with patch('election_bot.patient_wait.observe',side_effect=ValueError('stale')):
            with self.assertRaisesRegex(ValueError,'stale'):self.engine.cycle()
        self.assertEqual(self.sig.cancelled,[1]);self.assertFalse(self.f.journal.pending())
    def test_cancel_failure_retains_reservation_and_recovery_never_waits(self):
        self.open_order();cancel=self.sig.cancel;self.sig.cancel=Mock(side_effect=TimeoutError('cancel'))
        with patch('election_bot.patient_wait.observe',side_effect=lambda p,o,*args:(o,'patience_expired')):
            with self.assertRaises(TimeoutError):self.engine.cycle()
        self.assertTrue(self.f.journal.pending());self.sig.cancel=cancel
        with patch('election_bot.patient_wait.observe',side_effect=AssertionError('must not wait')):
            self.engine.reconcile()
        self.assertFalse(self.f.journal.pending());self.assertEqual(self.sig.cancelled,[1])
    def test_unknown_submission_never_enters_wait(self):
        self.sig.timeout=True
        with patch('election_bot.patient_wait.observe') as wait:
            with self.assertRaises(TimeoutError):self.engine.cycle()
        wait.assert_not_called();self.assertTrue(self.f.journal.pending())
    def test_paper_never_assumes_resting_fill(self):
        self.engine.live=False;self.engine.cycle()
        self.assertEqual(self.engine.ledger.held('2')[0],0);self.assertFalse(self.sig.orders)
    def test_patient_size_caps_both_arms(self):
        signal=fixture.Improvement().entry('.5');signal.quantity=200
        capped=sized_control(signal,fixture.config())
        self.assertLessEqual(capped.quantity*(capped.price+D('.01')),25)
        self.assertLessEqual(capped.quantity,50)
    def test_pending_report_is_not_counted_as_failed_fill(self):
        db=fixture.database()
        db.execute("INSERT INTO patient_orders VALUES ('p','2','patient','improved',1,'{}')")
        g=report(db)['by_group']['patient_improved']
        self.assertEqual(g['pending_attempts'],1);self.assertIsNone(g['fill_rate'])
    def test_nonfill_advances_assignment_seed(self):
        db=fixture.database();p=settings(fixture.config(control_fraction='.5'))
        with patch('election_bot.patient_entry.hashlib.sha256') as hash:
            hash.return_value.hexdigest.return_value='00000000'
            arm(db,'2',p);self.assertEqual(hash.call_args.args[0],b'2:0')
            fixture.fill(db,'zero','2',quantity='0',amount='0')
            arm(db,'2',p);self.assertEqual(hash.call_args.args[0],b'2:1')

class SamplingAndReportTests(unittest.TestCase):
    def test_outside_sample_keeps_original_size(self):
        from election_bot.patient_entry import propose,attach_control
        db=fixture.database();c=fixture.config(pilot_fraction='0');signal=fixture.Improvement().entry('.5');signal.quantity=200
        self.assertEqual(sized_control(signal,c,db,'2').quantity,200)
        self.assertIsNone(propose(db,{'sig_exchange_id':'2'},c,fixture.book('.46','.50'),signal)[0])
        db.execute('BEGIN')
        attach_control(db,{'idempotencyKey':'x','exchangeId':'2','side':'yes','action':'buy','price':'.5','quantity':200},c,None)
        self.assertEqual(db.execute('SELECT COUNT(*) FROM patient_orders').fetchone()[0],0)
    def test_controls_are_capped_at_same_size(self):
        db=fixture.database();c=fixture.config(control_fraction='1');signal=fixture.Improvement().entry('.5');signal.quantity=200
        self.assertEqual(sized_control(signal,c,db,'2').quantity,49)
    def test_absolute_prices_do_not_create_fake_improvement(self):
        db=fixture.database()
        fixture.fill(db,'a','2',price='.20');fixture.fill(db,'b','3',price='.80',arm_name='control',priced='control')
        r=report(db)
        self.assertNotIn('price_improvement_per_share',r['comparison'])
        self.assertIsNone(r['by_group']['patient_improved']['mean_improvement_vs_own_quoted_ask'])
    def test_failed_patient_fallbacks_are_not_controls(self):
        db=fixture.database();fixture.fill(db,'a','2',arm_name='patient',priced='control')
        self.assertIn('patient_fallback_control',report(db)['by_group'])
        self.assertNotIn('control_improvable',report(db)['by_group'])
