import copy
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from election_bot.near_miss import POLICY, record, report, snapshot, path_for, format_report
from election_bot.strategy import Book,D
from election_bot.fair_value import VERSION
from election_bot.fair_research import report as full_report
from test_leadership import T


class NearMissTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.mapping={'sig_exchange_id':'2','race_key':'2026:senate:NH','contract_fingerprint':'pin'}
        self.counter=0

    def book(self,at=T,bid='.48',ask='.50',depth=100):
        return Book([(D(bid),D(depth))],[(D(ask),D(depth))],at,at,'test','fetch_time')

    def research(self,at=T,prob=.60,low=.555,high=.65):
        self.counter+=1
        return {'snapshot_id':str(self.counter),'prior_variants':{
            name:{'forecast':{'version':VERSION,'status':'estimated','as_of':at,
                'race_key':self.mapping['race_key'],'yes_low':low,'yes_high':high,
                'yes_probability':prob,'estimate_id':'test','pollster_count':2,'excluded':{}}}
            for name in POLICY['models']}}

    def step(self,at=T,book=None,research=None,paper=False):
        record(self.root,self.mapping,book or self.book(at),research or self.research(at),'.01',at,paper)

    def rows(self):
        db=sqlite3.connect(path_for(self.root));db.row_factory=sqlite3.Row
        try:return [dict(r) for r in db.execute('SELECT * FROM positions')]
        finally:db.close()

    def portfolio(self,pid='neutral_prior:.03:300',now=T+400):
        return next(p for p in report(self.root,now=now)['portfolios'] if p['id']==pid)

    def test_thresholds_subtract_both_buffers_and_report_price_vs_uncertainty(self):
        d=snapshot(self.mapping,self.book(),self.research(),'.01',T)
        info=d['models']['neutral_prior']
        self.assertEqual(info['decisions']['.03'],{'side':'yes','ask':'0.50'})
        self.assertIsNone(info['decisions']['.04'])
        self.assertEqual(info['sides'][0]['thresholds']['.04']['rejections'],['uncertainty'])
        self.assertEqual(info['sides'][0]['thresholds']['.04']['edge_shortfall'],'0.005')
        self.assertEqual(info['sides'][1]['thresholds']['.03']['rejections'],['price_gap'])

    def test_exact_threshold_and_no_orientation(self):
        for side,low,high in [('yes',.55,.65),('no',.30,.43)]:
            # NO ask .52; 1-.43-.52-.02=.03.
            d=snapshot(self.mapping,self.book(),self.research(low=low,high=high,prob=(low+high)/2),'.01',T)
            self.assertEqual(d['models']['neutral_prior']['decisions']['.03']['side'],side)

    def test_cash_conservation_exit_fee_and_timely_profit(self):
        self.step();self.step(T+305,self.book(T+305,bid='.60',ask='.62'))
        r=self.portfolio()
        self.assertEqual(r['lifetime_entries'],1)
        self.assertEqual(r['open_positions'],0)
        self.assertEqual(D(r['simulated_cash']),D('100.08'))
        self.assertEqual(r['lifetime_realized_pnl'],'0.08')
        self.assertEqual(r['mean_timely_net_coins'],'0.08')
        h=self.portfolio('neutral_prior:.04:300')
        self.assertEqual(h['lifetime_entries'],0)
        self.assertEqual(h['simulated_cash'],'100')

    def test_cash_cap_and_isolation_from_live_ledger(self):
        self.step()
        db=sqlite3.connect(path_for(self.root));db.execute("UPDATE portfolios SET cash='0.10' WHERE id='neutral_prior:.03:300'");db.commit();db.close()
        self.mapping=dict(self.mapping,race_key='2026:senate:TX',sig_exchange_id='3')
        self.step(T+1)
        p=self.portfolio()
        self.assertEqual(p['decisions_in_window']['cash_limit'],1)
        self.assertEqual(p['lifetime_entries'],1)
        self.assertFalse((self.root/'live.sqlite3').exists())
        self.assertEqual(self.portfolio('historical_lean:.03:300')['lifetime_entries'],2)

    def test_no_cash_release_on_missing_stale_thin_or_early_quotes(self):
        self.step()
        self.step(T+301,self.book(T-20))
        self.step(T+305,self.book(T+305,depth=3))
        self.step(T+306,self.book(T+299)) # source before due cannot supply exit
        p=self.portfolio(now=T+1000)
        self.assertEqual(p['simulated_cash'],'99.49')
        self.assertEqual(p['overdue_open_positions'],1)
        self.assertIsNone(p['mean_timely_net_coins'])
        self.step(T+1001,self.book(T+1001,bid='.60',ask='.62'),self.research(T+1001,prob=.5,low=.4,high=.6))
        p=self.portfolio(now=T+1001)
        self.assertEqual(p['lifetime_closed'],1)
        self.assertEqual(p['timely_exits_in_window'],0)
        self.assertEqual(p['simulated_cash'],'100.08')

    def test_first_thin_then_within_window_exit_is_not_clean_timely_outcome(self):
        self.step();self.step(T+305,self.book(T+305,depth=3))
        self.step(T+310,self.book(T+310,bid='.60',ask='.62'))
        p=self.portfolio()
        self.assertEqual(p['closed_in_window'],1)
        self.assertEqual(p['timely_exits_in_window'],0)

    def test_restart_duplicate_and_clock_regression(self):
        e=self.research();self.step(research=e);self.step(research=e)
        self.assertEqual(self.portfolio()['lifetime_entries'],1)
        with self.assertRaisesRegex(ValueError,'clock regressed'):self.step(T-1)
        self.step(T+10)
        self.assertEqual(self.portfolio()['decisions_in_window']['position_open'],1)
        self.assertEqual(report(self.root,now=T+10)['observations_in_window'],2)

    def test_binding_change_cannot_release_cash(self):
        self.step();self.mapping=dict(self.mapping,contract_fingerprint='changed')
        self.step(T+305,self.book(T+305,bid='.60',ask='.62'))
        self.assertEqual(self.portfolio()['lifetime_closed'],0)
        self.assertEqual(self.rows()[0]['first_exit_issue'],'binding_changed')

    def test_malformed_future_stale_depth_spread_and_tick_diagnostics(self):
        for book,reason in [(self.book(T+1),'Stale or future quote'),(self.book(T-16),'Stale or future quote')]:
            d=snapshot(self.mapping,book,self.research(),'.01',T)
            self.assertEqual(d['quote_rejections'],[reason])
            self.assertEqual(d['models']['neutral_prior']['decisions'],{})
        for book,reason in [(self.book(depth=3),'entry_depth'),(self.book(bid='.40'),'spread'),(self.book(ask='.501'),'tick')]:
            d=snapshot(self.mapping,book,self.research(),'.01',T)
            self.assertIn(reason,d['models']['neutral_prior']['sides'][0]['thresholds']['.03']['rejections'])
        e=self.research();e['prior_variants']['neutral_prior']['forecast']['as_of']=T-1
        d=snapshot(self.mapping,self.book(),e,'.01',T)
        self.assertEqual(d['models']['neutral_prior']['reason'],'invalid_forecast')

    def test_unavailable_forecast_does_not_block_exit(self):
        self.step()
        e={'snapshot_id':'missing','reason':'insufficient_independent_pollsters'}
        self.step(T+305,self.book(T+305,bid='.60',ask='.62'),e)
        self.assertEqual(self.portfolio()['lifetime_closed'],1)
        self.assertTrue(any('polling:' in k for v in report(self.root,now=T+400)['diagnostics'].values() for k in v['counts']))

    def test_readonly_report_no_creation_and_paper_separate(self):
        self.assertFalse(report(self.root,now=T)['initialized'])
        self.assertEqual(list(self.root.iterdir()),[])
        self.step(paper=True)
        self.assertFalse(report(self.root,now=T)['initialized'])
        self.assertTrue(report(self.root,paper=True,now=T)['initialized'])
        before=path_for(self.root,True).read_bytes()
        text=format_report(report(self.root,paper=True,now=T))
        self.assertIn('simulated funds only',text)
        self.assertEqual(before,path_for(self.root,True).read_bytes())

    def test_policy_changes_refused_not_reset(self):
        self.step()
        with patch.dict(POLICY,initial_cash='200'):
            with self.assertRaisesRegex(ValueError,'policy changed'):self.step(T+1)
        self.assertEqual(self.portfolio()['simulated_cash'],'99.49')

    def test_report_window_does_not_reset_capital_or_positions(self):
        self.step();self.step(T+305,self.book(T+305,bid='.60',ask='.62'))
        p=self.portfolio(now=T+100000)
        self.assertEqual(p['closed_in_window'],0)
        self.assertEqual(p['lifetime_closed'],1)
        self.assertEqual(p['simulated_cash'],'100.08')

    def test_transaction_rolls_back_mid_step_error(self):
        self.step()
        original=json.dumps
        def serialize(value,**kwargs):
            if 'portfolio_decisions' in value:raise ValueError('failure after simulated exit')
            return original(value,**kwargs)
        with patch('election_bot.near_miss.json.dumps',side_effect=serialize):
            with self.assertRaisesRegex(ValueError,'failure after simulated exit'):
                self.step(T+305,self.book(T+305,bid='.60',ask='.62'))
        p=self.portfolio()
        self.assertEqual(p['simulated_cash'],'99.49')
        self.assertEqual(p['lifetime_closed'],0)
        self.assertEqual(report(self.root,now=T+400)['observations_in_window'],1)

    def test_real_worker_observer_records_without_altering_orders(self):
        import test_active
        from test_fundamentals import evidence,build_prior,iso,T as FT
        from election_bot.polling import PollStore
        f=test_active.ActiveTests();f.setUp();self.addCleanup(f.doCleanups)
        data=evidence(f.config)
        data['races'][0]['fundamentals_prior']=build_prior('NH',iso(FT-1))
        store=PollStore(Path(f.temp.name)/'fair-value.sqlite3')
        store.ingest(data,f.config,FT-1);store.close()
        with patch('election_bot.active_engine.time.time',return_value=FT):
            f.engine.record_quote_snapshot(f.config['markets'][0],self.book(FT),[], 'scan')
        # Fixture is live but only this research hook was called; no orders placed.
        self.assertEqual(len(f.sig.orders),0)
        observed=report(f.temp.name,paper=not f.engine.live,now=FT)
        self.assertTrue(observed['initialized'])
        self.assertEqual(observed['observations_in_window'],1)
        with patch('election_bot.active_engine.time.time',return_value=FT+1):
            f.engine.record_quote_snapshot(f.config['markets'][0],self.book(FT+1),[], 'preflight')
        self.assertEqual(report(f.temp.name,paper=not f.engine.live,now=FT+1)['observations_in_window'],1)

    def test_research_disk_failure_cannot_change_decision_or_place_orders(self):
        import test_active
        f=test_active.ActiveTests();f.setUp();self.addCleanup(f.doCleanups)
        e={'snapshot_id':'fake','reason':'no_reviewed_polling_mapping'}
        with patch('election_bot.fair_research.observe',return_value=e), patch('election_bot.near_miss.record',side_effect=sqlite3.OperationalError('busy')):
            f.engine.record_quote_snapshot(f.config['markets'][0],self.book(),[], 'scan')
        self.assertEqual(len(f.sig.orders),0)
        self.assertIsNotNone(f.journal.db.execute("SELECT 1 FROM events WHERE kind='near_miss_skip'").fetchone())

    def test_report_integration_does_not_create_research_files(self):
        from election_bot.demo import fixture
        config,_,_=fixture()
        r=full_report(self.root,config,now=T)
        self.assertFalse(r['near_miss']['initialized'])
        self.assertEqual(list(self.root.iterdir()),[])
        self.step()
        r=full_report(self.root,config,now=T)
        self.assertTrue(r['near_miss']['initialized'])
        self.assertEqual(len(r['near_miss']['portfolios']),18)

    def test_invalid_exit_tick_keeps_cash_reserved(self):
        self.step();self.step(T+305,self.book(T+305,bid='.601',ask='.62'))
        self.assertEqual(self.portfolio()['simulated_cash'],'99.49')
        self.assertEqual(self.portfolio()['lifetime_closed'],0)

    def test_diagnostics_retention_never_drops_open_positions(self):
        self.step()
        later=T+8*86400
        self.step(later,self.book(later-100),{'snapshot_id':'late','reason':'unavailable'})
        p=self.portfolio(now=later)
        self.assertEqual(p['lifetime_entries'],1)
        self.assertEqual(p['overdue_open_positions'],1)
        self.assertEqual(p['simulated_cash'],'99.49')
        self.assertEqual(report(self.root,now=later)['observations_in_window'],1)
