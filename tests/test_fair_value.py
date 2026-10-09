import copy
from datetime import datetime,timezone
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch, Mock

import test_active
from test_leadership import snapshot
from election_bot.polling import PollStore,validate_poll,validate_race
from election_bot.fair_value import estimate,compare,VERSION
from election_bot.fair_research import observe,report
from election_bot.fair_cli import demo,template
from election_bot.poll_sources import stage_public
from election_bot.demo import fixture
from election_bot.strategy import Book

T=1791400000.0

def iso(t):return datetime.fromtimestamp(t,timezone.utc).isoformat()


def evidence(config,now=T):
    m=config['markets'][0];m.update(race_key='2026:senate:NH',exposure_sign=1)
    race={'sig_exchange_id':m['sig_exchange_id'],'contract_fingerprint':m['contract_fingerprint'],
          'race_key':m['race_key'],'yes_party':'DEM','dem_candidate':'Test D','rep_candidate':'Test R',
          'contest_type':'general_plurality','election_at':iso(now+30*86400),'source_url':'https://example.test/race'}
    polls=[]
    for i in range(3):
        polls.append({'poll_id':'p'+str(i),'survey_id':'s'+str(i),'pollster':'Pollster '+str(i),
            'race_key':m['race_key'],'dem_candidate':'Test D','rep_candidate':'Test R',
            'stage':'general','population':'lv','partisan':False,'sample_size':1000,
            'field_start':iso(now-4*86400),'field_end':iso(now-3*86400),'published_at':iso(now-2*86400),
            'dem_pct':52,'rep_pct':43,'other_pct':2,'methodology':'online panel','source_url':'https://example.test/poll/'+str(i)})
    return {'schema_version':1,'races':[race],'polls':polls}


class InputTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.config,_,_=fixture();self.data=evidence(self.config)
        self.path=Path(self.tmp.name)/'fair-value.sqlite3'
        self.store=PollStore(self.path);self.addCleanup(self.store.close)

    def load(self):self.store.ingest(self.data,self.config,T-1)
    def predict(self,now=T):return estimate(self.store.latest('race',now)[0],self.store.latest('poll',now),now)

    def test_receipt_time_prevents_backdating_and_duplicates_do_not_refresh(self):
        self.load()
        self.assertEqual(self.store.latest('poll',T-2),[])
        result=self.store.ingest(self.data,self.config,T+10)
        self.assertEqual(result['inserted_versions'],0)
        self.assertTrue(all(r['_received_at']==T-1 for r in self.store.latest('poll',T+20)))

    def test_revision_withdrawal_and_reversion_preserve_asof_history(self):
        self.load();p=self.data['polls'][0]
        p['dem_pct']=40;p['withdrawn']=True
        self.store.ingest(self.data,self.config,T+10)
        self.assertEqual(next(p for p in self.store.latest('poll',T) if p['poll_id']=='p0')['dem_pct'],52)
        self.assertTrue(next(p for p in self.store.latest('poll',T+20) if p['poll_id']=='p0')['withdrawn'])
        p['dem_pct']=52;p.pop('withdrawn')
        self.store.ingest(self.data,self.config,T+30)
        self.assertEqual(next(p for p in self.store.latest('poll',T+40) if p['poll_id']=='p0')['dem_pct'],52)

    def test_invalid_batch_rolls_back_all_records(self):
        self.data['polls'][-1]['stage']='primary'
        with self.assertRaises(ValueError):self.store.ingest(self.data,self.config,T)
        self.assertEqual(self.store.latest('race',T),[])

    def test_poll_id_cannot_change_identity_and_transaction_rolls_back(self):
        self.load();self.data['polls'][0]['dem_pct']=51;self.data['polls'][1]['pollster']='different'
        with self.assertRaises(ValueError):self.store.ingest(self.data,self.config,T+5)
        self.assertEqual(next(p for p in self.store.latest('poll',T+6) if p['poll_id']=='p0')['dem_pct'],52)

    def test_wrong_contract_party_and_placeholder_are_rejected(self):
        for field,value in [('contract_fingerprint','wrong'),('race_key','wrong'),('yes_party','REP'),('dem_candidate','REPLACE_X')]:
            row=dict(self.data['races'][0],**{field:value})
            with self.subTest(field=field),self.assertRaises(ValueError):
                validate_race(row,{self.config['markets'][0]['sig_exchange_id']:self.config['markets'][0]})

    def test_invalid_poll_inputs_rejected(self):
        cases={'dem_pct':float('nan'),'rep_pct':True,'other_pct':99,'sample_size':0,
               'field_end':iso(T+100),'published_at':'2026-10-01','partisan':True,
               'population':'adults','source_url':'https://user:secret@example.test/poll'}
        for field,value in cases.items():
            with self.subTest(field=field),self.assertRaises(ValueError):validate_poll(dict(self.data['polls'][0],**{field:value}))

    def test_model_has_independent_probability_and_nonzero_common_error(self):
        self.load();r=self.predict()
        self.assertEqual(r['status'],'estimated');self.assertGreater(r['yes_probability'],.7)
        self.assertLess(r['yes_low'],r['yes_probability']);self.assertGreater(r['yes_high'],r['yes_probability'])
        self.assertGreater(r['predictive_sd_pp'],4);self.assertFalse(r['calibrated'])
        self.assertEqual(r,self.predict())

    def test_one_pollster_many_duplicates_cannot_satisfy_minimum(self):
        for p in self.data['polls']:p['pollster']='One firm'
        self.load();self.assertEqual(self.predict()['reason'],'insufficient_independent_pollsters')

    def test_future_publication_and_candidate_mismatch_excluded(self):
        self.data['polls'][0]['published_at']=iso(T+100)
        self.data['polls'][1]['dem_candidate']='Former nominee'
        self.load();r=self.predict()
        self.assertEqual(r['status'],'unavailable');self.assertEqual(r['pollster_count'],1)

    def test_old_and_third_party_polls_cannot_authorize_estimate(self):
        for case in ('old','other','election'):
            data=copy.deepcopy(self.data)
            if case=='old':
                for p in data['polls']:
                    p.update(field_start=iso(T-35*86400),field_end=iso(T-34*86400),published_at=iso(T-33*86400))
            elif case=='other':
                for p in data['polls']:p.update(dem_pct=46,rep_pct=43,other_pct=7)
            else:data['races'][0]['election_at']=iso(T-1)
            self.store.ingest(data,self.config,T-1)
            self.assertEqual(self.predict()['status'],'unavailable')

    def test_unknown_sponsorship_and_large_reported_error_reduce_information(self):
        self.load();original=self.predict()
        for poll in self.data['polls']:
            poll['partisan']=None;poll['reported_moe_pp']=8
        self.store.ingest(self.data,self.config,T)
        weaker=self.predict()
        self.assertLess(weaker['yes_probability'],original['yes_probability'])
        self.assertGreater(weaker['predictive_sd_pp'],original['predictive_sd_pp'])

    def test_republican_orientation_complements_democratic_probability(self):
        self.load();race=self.store.latest('race',T)[0];polls=self.store.latest('poll',T)
        dem=estimate(race,polls,T);rep=estimate(dict(race,yes_party='REP'),polls,T)
        self.assertAlmostEqual(dem['yes_probability']+rep['yes_probability'],1)
        self.assertAlmostEqual(rep['yes_low'],1-dem['yes_high'])

    def test_future_prior_is_not_used(self):
        self.data['races'][0]['prior']={'margin_pp':10,'sd_pp':12,'source_url':'https://example.test/prior',
                                     'published_at':iso(T+10),'method':'historical lean plus national environment'}
        self.load();self.assertEqual(self.predict()['reason'],'prior_not_published')

    def test_model_can_propose_when_references_disagree_or_have_no_gap(self):
        self.load();forecast=self.predict()
        with patch('election_bot.strategy.time.time',return_value=T):
            book=Book.make([('.48',100)],[('.50',100)])
            refs=[Book.make([('.49',100)],[('.51',100)]) for _ in range(2)]
            result=compare(forecast,book,refs,self.config['strategy'])
            self.assertIsNone(result['market']);self.assertTrue(result['model'][0]['eligible'])
            refs[0]=Book.make([('.20',100)],[('.22',100)])
            disagree=compare(forecast,book,refs,self.config['strategy'])
            self.assertTrue(disagree['model'][0]['eligible']);self.assertIn('disagree',disagree['market_reason'])
            refs[0].source_at-=100
            self.assertTrue(compare(forecast,book,refs,self.config['strategy'])['model'][0]['eligible'])
            book.source_at-=100
            with self.assertRaises(ValueError):compare(forecast,book,refs,self.config['strategy'])

    def test_research_error_never_returns_order_and_stale_binding_blocks(self):
        self.load();m=self.config['markets'][0]
        with patch('election_bot.strategy.time.time',return_value=T):
            book=Book.make([('.48',100)],[('.50',100)])
            refs=[book,book]
            wrong=dict(m,contract_fingerprint='changed')
            row=observe(self.tmp.name,wrong,book,refs,self.config['strategy'],'a',T)
            self.assertEqual(row['reason'],'contract_binding_changed')
            self.assertNotIn('order',row)
            self.store.db.execute("UPDATE evidence SET body='invalid'");self.store.db.commit()
            row=observe(self.tmp.name,m,book,refs,self.config['strategy'],'a',T)
            self.assertEqual(row['reason'],'research_input_or_quote_error')

    def test_demo_is_offline_and_templates_are_not_importable(self):
        self.assertTrue(demo()['synthetic'])
        self.config['markets'][0]['office']='senate'
        with self.assertRaises(ValueError):self.store.ingest(template(self.config),self.config,T)


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.f=test_active.ActiveTests();self.f.setUp();self.addCleanup(self.f.doCleanups)

    def test_research_records_but_cannot_change_the_live_decision(self):
        f=self.f;data=evidence(f.config,time.time())
        store=PollStore(Path(f.temp.name)/'fair-value.sqlite3')
        try:store.ingest(data,f.config)
        finally:store.close()
        f.engine.cycle()
        self.assertEqual(len(f.sig.orders),1)
        self.assertEqual(f.sig.orders[1]['action'],'buy')
        row=f.journal.db.execute("SELECT detail FROM events WHERE kind='fair_value_shadow'").fetchone()
        self.assertEqual(json.loads(row[0])['status'],'estimated')
        self.assertEqual(f.journal.db.execute("SELECT COUNT(*) FROM events WHERE kind='fair_value_shadow'").fetchone()[0],1)

    def test_model_only_candidate_never_causes_live_order(self):
        f=self.f;data=evidence(f.config,time.time())
        store=PollStore(Path(f.temp.name)/'fair-value.sqlite3')
        try:store.ingest(data,f.config)
        finally:store.close()
        f.sig.prices['2']=('.68','.70')
        f.engine.cycle()
        self.assertFalse(f.sig.orders)
        row=json.loads(f.journal.db.execute("SELECT detail FROM events WHERE kind='fair_value_shadow'").fetchone()[0])
        self.assertIsNone(row['comparison']['market'])
        self.assertTrue(any(r['eligible'] for r in row['comparison']['model']))

    def test_absent_input_store_leaves_trading_unchanged(self):
        self.f.engine.cycle()
        self.assertEqual(len(self.f.sig.orders),1)
        self.assertEqual(self.f.journal.db.execute("SELECT COUNT(*) FROM events WHERE kind='fair_value_shadow'").fetchone()[0],0)

    def test_public_feed_is_staged_without_creating_model_database(self):
        response=Mock();response.read.return_value=json.dumps({'polls':[{'date':'2026-06-08','results':{'primary':70}}]}).encode()
        cm=Mock();cm.__enter__=Mock(return_value=response);cm.__exit__=Mock(return_value=False)
        opener=Mock();opener.open.return_value=cm
        with patch('election_bot.poll_sources.urllib.request.build_opener',return_value=opener):
            result=stage_public(self.f.temp.name,T)
        self.assertEqual(result['model_inputs_imported'],0);self.assertGreater(result['newest_age_days'],90)
        self.assertFalse((Path(self.f.temp.name)/'fair-value.sqlite3').exists())
        self.assertTrue(Path(result['file']).exists())


class ReportTests(unittest.TestCase):
    def test_forward_markouts_use_future_bid_two_buffers_and_exclude_missing(self):
        with tempfile.TemporaryDirectory() as root:
            config,_,_=fixture();evidence(config)
            from election_bot.state import Journal
            journal=Journal(Path(root)/'live.sqlite3','test')
            try:
                first=snapshot(at=T,sig=('.48','.50'));future=snapshot(sid='b',at=T+305,sig=('.60','.62'))
                event={'version':1,'model_version':VERSION,'snapshot_id':'a','exchange':'2','captured_at':T,
                    'status':'estimated','cost_buffer_per_share':'.01',
                    'comparison':{'model':[{'eligible':True,'side':'yes','ask':'.50'}],'market':None}}
                for at,kind,row in [(T,'quote_snapshot',first),(T,'fair_value_shadow',event),(T+305,'quote_snapshot',future)]:
                    journal.db.execute('INSERT INTO events VALUES(?,?,?)',(at,kind,json.dumps(row)))
                journal.db.commit()
                result=report(root,config,now=T+2000)
                self.assertEqual(result['markouts']['model_300']['mean_net_change_per_share'],'0.08')
                self.assertEqual(result['markouts']['model_900']['statuses'],{'missing':1})
                self.assertEqual(result['markouts']['model_3600']['statuses'],{'pending':1})
                self.assertEqual(result['coverage']['unmapped_contracts'],1)
            finally:journal.close()


class SeedTests(unittest.TestCase):
    def test_dated_public_seed_can_be_imported_without_credentials_or_orders(self):
        from types import SimpleNamespace
        from election_bot.fair_cli import run
        config,_,_=fixture();m=config['markets'][0]
        m.update(name='nh-senate-democratic',race_key='2026:senate:NH',exposure_sign=1)
        args=SimpleNamespace(demo=False,seed_public=True)
        with tempfile.TemporaryDirectory() as root:
            first=run(args,root,config)
            self.assertEqual(first['inserted_versions'],3)
            self.assertFalse(first['live_model_orders'])
            self.assertEqual(run(args,root,config)['inserted_versions'],0)
            store=PollStore(Path(root)/'fair-value.sqlite3',readonly=True)
            try:
                rows=store.latest('poll',time.time())
                self.assertEqual(len(rows),2)
                self.assertTrue(all(p['publication_time_basis'].startswith('first_observed') for p in rows))
            finally:store.close()
