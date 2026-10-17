import copy
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from election_bot.fair_pairs import analyze
from election_bot.fair_research import report, format_report
from election_bot.fair_value import VERSION
from election_bot.leadership import load_events
from election_bot.demo import fixture
from tests.test_leadership import T, snapshot


def event(sid='a', at=T, actions=('yes','no'), sig=('.48','.50')):
    variants={}
    for name,action in zip(('neutral_prior','historical_lean'),actions):
        variants[name]={'forecast':{'version':VERSION,'experiment':'historical_lean_v1',
            'status':'estimated','prior_variant':name,'base_race_hash':'base','as_of':at,
            'race_key':'2026:senate:NH','policy':{'test':True},'yes_low':.3,'yes_probability':.5,'yes_high':.7,
            'weights':[{'poll_id':'poll','hash':'hash','received_at':T-100}]},
            'comparison':{'model':[{'side':side,'ask':ask,'depth':'100','eligible':side==action}
                for side,ask in [('yes',sig[1]),('no',str(1-float(sig[0])))]]}}
    return {'version':1,'model_version':VERSION,'snapshot_id':sid,'exchange':'2','captured_at':at,
            'status':'estimated','cost_buffer_per_share':'.01','prior_variants':variants}


class PairTests(unittest.TestCase):
    def setUp(self):
        self.db=sqlite3.connect(':memory:')
        self.addCleanup(self.db.close)
        self.db.execute('CREATE TABLE events(at REAL,kind TEXT,detail TEXT)')

    def insert(self,at,kind,body):
        self.db.execute('INSERT INTO events VALUES(?,?,?)',(at,kind,json.dumps(body)))

    def start(self,actions=('yes','no'),sid='a',at=T):
        self.insert(at,'quote_snapshot',snapshot(sid=sid,at=at,sig=('.48','.50')))
        e=event(sid,at,actions)
        self.insert(at,'fair_value_shadow',e)
        return e

    def future(self,at=T+305,depth=100,sig=('.60','.62')):
        self.insert(at,'quote_snapshot',snapshot(sid=str(at),at=at,sig=sig,depth=depth))

    def result(self,now=T+2000):
        quotes,_=load_events(self.db,T-1,now)
        return analyze(self.db,quotes,T-1,now)

    def test_opposite_sides_same_quote_two_buffers(self):
        self.start();self.future()
        r=self.result();h=r['horizons']['300']
        self.assertEqual(h['mean_neutral_net_coins'],'0.08')
        self.assertEqual(h['mean_historical_net_coins'],'-0.16')
        self.assertEqual(h['mean_difference_net_coins'],'-0.24')
        self.assertEqual(h['neutral_better'],1)
        self.assertEqual(r['decisions'],{'opposite_sides':1})

    def test_cash_is_zero_and_both_cash_is_not_measured(self):
        self.start((None,'yes'));self.future()
        h=self.result()['horizons']['300']
        self.assertEqual(h['mean_neutral_net_coins'],'0')
        self.assertEqual(h['mean_difference_net_coins'],'0.08')
        self.start((None,None),'cash',T+500)
        h=self.result()['horizons']['300']
        self.assertEqual(h['measured_pairs'],1)
        self.assertEqual(h['statuses']['both_cash'],1)

    def test_same_side_ties(self):
        self.start(('no','no'));self.future()
        h=self.result()['horizons']['300']
        self.assertEqual(h['mean_difference_net_coins'],'0.00')
        self.assertEqual(h['ties'],1)

    def test_missing_pending_and_thin_are_not_zero(self):
        self.start();self.future(depth='.5');self.future(T+310)
        r=self.result()
        self.assertEqual(r['horizons']['300']['statuses'],{'insufficient_exit_depth':1})
        self.assertIsNone(r['horizons']['300']['mean_difference_net_coins'])
        self.assertEqual(r['horizons']['900']['statuses'],{'missing':1})
        self.assertEqual(r['horizons']['3600']['statuses'],{'pending':1})

    def test_thinning_is_outcome_independent_and_by_race(self):
        self.start(sid='a');self.start(sid='b',at=T+100)
        self.start(sid='c',at=T+421);self.future(T+726)
        r=self.result()
        self.assertEqual(r['horizons']['300']['statuses'],{'missing':1,'thinned':1,'measured':1})
        self.assertEqual(r['horizons']['900']['statuses'],{'missing':1,'thinned':2})
        self.assertIn('2026:senate:NH',r['horizons']['300']['by_race'])

    def test_conflicting_duplicates_exclude_entire_pair(self):
        e=self.start();self.insert(T,'fair_value_shadow',copy.deepcopy(e));self.future()
        self.assertEqual(self.result()['horizons']['300']['measured_pairs'],1)
        e['cost_buffer_per_share']='.02';self.insert(T,'fair_value_shadow',e)
        r=self.result()
        self.assertEqual(r['coverage']['conflicting_snapshot_ids'],1)
        self.assertEqual(r['horizons'],{})

    def test_invalid_identity_evidence_time_quote_and_probabilities(self):
        mutations=[
            lambda e:e.update(exchange='wrong'),
            lambda e:e.update(captured_at=T+100),
            lambda e:e.update(cost_buffer_per_share='NaN'),
            lambda e:e.update(cost_buffer_per_share='-.01'),
            lambda e:e['prior_variants']['historical_lean']['forecast'].update(base_race_hash='different'),
            lambda e:e['prior_variants']['historical_lean']['forecast'].update(weights=[]),
            lambda e:e['prior_variants']['historical_lean']['forecast']['weights'][0].update(hash='different'),
            lambda e:e['prior_variants']['historical_lean']['forecast']['weights'][0].update(received_at=T+1),
            lambda e:e['prior_variants']['historical_lean']['forecast'].update(yes_probability=float('nan')),
            lambda e:e['prior_variants']['historical_lean']['forecast'].update(as_of=T+1),
            lambda e:e['prior_variants']['historical_lean']['comparison']['model'][0].update(ask='.51'),
            lambda e:e['prior_variants']['historical_lean']['comparison']['model'][0].update(eligible=True),
        ]
        for change in mutations:
            with self.subTest(change=change):
                self.db.execute('DELETE FROM events')
                self.insert(T,'quote_snapshot',snapshot(sig=('.48','.50')))
                e=event();change(e);self.insert(T,'fair_value_shadow',e)
                self.assertEqual(self.result()['coverage']['invalid_pair'],1)
                self.assertEqual(self.result()['horizons'],{})

    def test_absent_or_unavailable_variants_not_reconstructed(self):
        e=self.start();self.db.execute("DELETE FROM events WHERE kind='fair_value_shadow'")
        e.pop('prior_variants');self.insert(T,'fair_value_shadow',e)
        self.assertEqual(self.result()['coverage']['missing_pair'],1)
        self.db.execute("DELETE FROM events WHERE kind='fair_value_shadow'")
        e=event();e['prior_variants']['historical_lean']['forecast']['status']='unavailable'
        self.insert(T,'fair_value_shadow',e)
        self.assertEqual(self.result()['coverage']['unavailable_pair'],1)

    def test_conflicting_or_stale_snapshot_not_joined(self):
        self.start()
        q=snapshot(sig=('.48','.51'));self.insert(T,'quote_snapshot',q)
        self.assertEqual(self.result()['coverage']['unmatched_snapshot'],1)

    def test_report_integration_read_only_and_no_missing_database_creation(self):
        c,_,_=fixture()
        with tempfile.TemporaryDirectory() as root:
            r=report(root,c,now=T+2000)
            self.assertEqual(list(Path(root).iterdir()),[])
            self.start();self.future()
            out=sqlite3.connect(Path(root)/'live.sqlite3')
            self.db.commit();self.db.backup(out);out.close()
            before=(Path(root)/'live.sqlite3').read_bytes()
            r=report(root,c,now=T+2000)
            self.assertEqual(r['paired_priors']['horizons']['300']['measured_pairs'],1)
            self.assertIn('Paired prior decisions:',format_report(r))
            self.assertEqual(before,(Path(root)/'live.sqlite3').read_bytes())
            json.dumps(r)

    def test_outcome_after_report_time_is_excluded(self):
        self.start();self.future()
        h=self.result(T+304)['horizons']['300']
        self.assertEqual(h['statuses'],{'pending':1})

    def test_real_observer_output_is_pairable(self):
        from unittest.mock import patch
        from test_fundamentals import configured, build_prior, iso, evidence, T as FT
        from election_bot.polling import PollStore
        from election_bot.fair_research import observe
        from election_bot.strategy import Book
        c=configured();data=evidence(c)
        data['races'][0]['fundamentals_prior']=build_prior('NH',iso(FT-1))
        with tempfile.TemporaryDirectory() as root:
            store=PollStore(Path(root)/'fair-value.sqlite3')
            store.ingest(data,c,FT-1);store.close()
            with patch('election_bot.strategy.time.time',return_value=FT):
                book=Book.make([('.48',100)],[('.50',100)])
                refs=[Book.make([('.49',100)],[('.51',100)]) for _ in range(2)]
                e=observe(root,c['markets'][0],book,refs,c['strategy'],'real',FT)
            self.insert(FT,'quote_snapshot',snapshot(sid='real',at=FT,sig=('.48','.50')))
            self.insert(FT,'fair_value_shadow',e)
            quotes,_=load_events(self.db,FT-1,FT+2000)
            r=analyze(self.db,quotes,FT-1,FT+2000)
            self.assertEqual(r['coverage']['valid_pairs'],1)
            self.assertNotIn('invalid_pair',r['coverage'])

    def test_malformed_event_and_variant_are_counted(self):
        self.insert(T,'fair_value_shadow',[])
        e=event();e['prior_variants']['historical_lean']=None
        self.insert(T,'fair_value_shadow',e)
        r=self.result()
        self.assertEqual(r['coverage']['malformed_events'],1)
        self.assertEqual(r['coverage']['invalid_pair'],1)
