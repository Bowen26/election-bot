import json
import sqlite3
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch
from election_bot.performance_throttle import PerformanceThrottle,evaluate,report,enabled
from election_bot.strategy import D,Signal
import test_patient_entry as fixture

NOW=20*86400+5000
END=20*86400
class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.db=fixture.database();self.addCleanup(self.db.close)
        self.c={'performance_throttle':{'enabled':True}}
        self.t=PerformanceThrottle(self.db,self.c)
    def seed(self,net='-.04',bid='.49',count=20,measured=None,races=5,day=None,route='reference'):
        measured=count if measured is None else measured
        for day in ([END-2*86400,END-86400] if day is None else [day]):
            for i in range(count):
                key=f'{route}:{day}:{i}';at=day+(i//races)*3601+i%races
                self.db.execute("INSERT INTO executions VALUES (?,?,'buy','yes','10','.5','.01',?)",(key,str(i%races),at))
                self.db.execute('INSERT INTO route_entry_evidence VALUES (?,?,?,?,?)',(key,route,str(i%races),at,json.dumps({'bid':bid})))
                if i<measured:self.db.execute('INSERT INTO markouts VALUES (?,3600,?,?,NULL)',(key,at+3600,str(D(net)*10)))
        self.db.commit()
    def test_two_days_negative_and_falling_bids_halve(self):
        self.seed();r=self.t.refresh(NOW)['reference']
        self.assertEqual(r['suggested_multiplier'],'.5');self.assertTrue(all(d['ready'] for d in r['days']))
        with patch('election_bot.performance_throttle.time.time',return_value=NOW):
            self.assertEqual(self.t.scale(Signal('yes',D('.5'),11,D('.7'),D('.19'))).quantity,5)
    def test_cost_drag_without_bid_decline_does_not_throttle(self):
        # -.03 = unchanged .49 bid minus .50 entry and two .01 buffers.
        self.seed(net='-.03');self.assertEqual(self.t.refresh(NOW)['reference']['suggested_multiplier'],'1')
    def test_one_day_and_one_race_are_insufficient(self):
        self.seed(day=END-86400);self.assertEqual(self.t.refresh(NOW)['reference']['suggested_multiplier'],'1')
        self.db.execute('DELETE FROM markouts');self.db.execute('DELETE FROM executions');self.db.execute('DELETE FROM route_entry_evidence')
        self.seed(races=1);self.assertEqual(self.t.refresh(NOW)['reference']['suggested_multiplier'],'1')
    def test_missing_coverage_does_not_become_zero(self):
        self.seed(count=40,measured=20);d=self.t.refresh(NOW)['reference']
        self.assertEqual(d['suggested_multiplier'],'1');self.assertEqual(d['days'][0]['coverage'],'0.5')
    def test_spacing_uses_first_fill_not_best_outcome(self):
        self.seed();self.db.execute('UPDATE executions SET at=?',(END-86400+10,));self.db.commit()
        self.assertEqual(sum(d['eligible_spaced_fills'] for d in evaluate(self.db,NOW)['reference']['days']),5)
    def test_out_of_window_marks_rejected(self):
        self.seed();self.db.execute('UPDATE markouts SET at=at+901');self.db.commit()
        d=evaluate(self.db,NOW)['reference']['days'][0]
        self.assertEqual(d['measured'],0);self.assertEqual(d['invalid'],20)
    def test_no_automatic_restore_on_sparse_or_mixed_data(self):
        self.seed();self.t.refresh(NOW)
        r=self.t.refresh(NOW+4*86400)['reference']
        self.assertEqual(r['suggested_multiplier'],'.5')
        new=PerformanceThrottle(self.db,self.c)
        self.assertEqual(new.refresh(NOW+4*86400)['reference']['suggested_multiplier'],'.5')
    def test_restore_requires_two_ready_positive_days(self):
        self.seed();self.t.refresh(NOW)
        self.db.execute("UPDATE markouts SET pnl='.1'");self.db.commit()
        self.assertEqual(self.t.refresh(NOW)['reference']['suggested_multiplier'],'1')
    def test_routes_are_separate_and_sells_unchanged(self):
        self.seed();r=self.t.refresh(NOW);self.assertEqual(r['model']['suggested_multiplier'],'1')
        sell=Signal('yes',D('.6'),100,D('.7'),D('.09'),'sell','risk_reduction_exit')
        self.assertIs(self.t.scale(sell),sell)
    def test_report_only_does_not_reduce_and_stale_live_state_blocks_buys(self):
        self.seed();self.t.refresh(NOW);self.c['performance_throttle']['enabled']=False
        self.assertEqual(self.t.multiplier('entry_gap',NOW+99999),1)
        self.c['performance_throttle']['enabled']=True
        with self.assertRaisesRegex(ValueError,'stale'):self.t.multiplier('entry_gap',NOW+601)
    def test_unit_minimum_does_not_zero_out_small_orders(self):
        self.seed();self.t.refresh(NOW)
        with patch('election_bot.performance_throttle.time.time',return_value=NOW):
            self.assertEqual(self.t.scale(Signal('yes',D('.5'),1,D('.7'),D('.19'))).quantity,1)
    def test_disabled_config_and_missing_report_do_not_write(self):
        self.assertFalse(enabled({}))
        with self.assertRaises(ValueError):enabled({'performance_throttle':{'enabled':'true'}})
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'missing.db';self.assertFalse(report(path,{},NOW)['initialized']);self.assertFalse(path.exists())

class EngineIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.f=fixture.ThroughTheEngine();self.addCleanup(self.f.doCleanups)
        self.e=self.f.engine(enabled=False);self.e.config['performance_throttle']={'enabled':True}
    def test_reduced_live_fill_has_durable_benchmark(self):
        import time
        def refresh():
            now=time.time()
            for route in ('reference','patient','model'):
                self.f.journal.db.execute('INSERT OR REPLACE INTO route_throttle_state VALUES (?,?)',
                    (route,json.dumps({'suggested_multiplier':'.5','evaluated_at':now})))
            self.f.journal.db.commit();return {}
        with patch.object(self.e.performance_throttle,'refresh',side_effect=refresh):self.e.cycle()
        self.assertEqual(self.f.sig.orders[1]['quantity'],20)
        row=self.f.journal.db.execute('SELECT * FROM route_entry_evidence').fetchone()
        self.assertEqual(row['route'],'reference');self.assertEqual(json.loads(row['detail'])['bid'],'0.55')
    def test_changed_multiplier_rechecked_under_reservation_lock(self):
        from election_bot.active_engine import ReservationRejected
        original=self.e.reserve_order
        def reserve(*args,**kwargs):
            self.f.journal.db.execute("UPDATE route_throttle_state SET detail=json_set(detail,'$.suggested_multiplier','.5')")
            self.f.journal.db.commit()
            return original(*args,**kwargs)
        with patch.object(self.e,'reserve_order',side_effect=reserve):self.e.cycle()
        self.assertFalse(self.f.sig.orders)
        self.assertEqual(self.f.journal.db.execute('SELECT COUNT(*) FROM route_entry_evidence').fetchone()[0],0)

    def test_assessment_failure_blocks_buys_but_keeps_exits_available(self):
        with patch.object(self.e.performance_throttle,'refresh',side_effect=ValueError('bad evidence')):
            self.e.cycle()
        self.assertFalse(self.f.sig.orders)
        payload=dict(idempotencyKey='seed',exchangeId='2',action='buy',side='yes',quantity=10,price='.5')
        self.f.journal.reserve(payload,D('5.1'));self.e.ledger.record(payload,10,'.5','.01')
        self.f.journal.db.execute('UPDATE orders SET created=0');self.f.journal.db.commit()
        self.f.sig.inventory['2']=10;self.f.sig.prices['2']=('.80','.85')
        with patch.object(self.e.performance_throttle,'refresh',side_effect=ValueError('bad evidence')):
            self.e.cycle()
        self.assertEqual(self.f.sig.orders[1]['action'],'sell')

    def test_report_does_not_create_or_change_state(self):
        before=self.f.journal.db.execute('SELECT COUNT(*) FROM route_throttle_state').fetchone()[0]
        r=report(self.e.runtime/'j.db',self.e.config)
        self.assertTrue(r['initialized'])
        self.assertEqual(self.f.journal.db.execute('SELECT COUNT(*) FROM route_throttle_state').fetchone()[0],before)
