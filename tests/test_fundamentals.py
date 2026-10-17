import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from election_bot.demo import fixture
from election_bot.fundamentals import build_prior, validate_prior, seed, variants, margin, POLICY
from election_bot.polling import PollStore, digest, validate_race, read_input
from election_bot.fair_value import estimate
from election_bot.fair_research import observe, report, format_report
from election_bot.poll_collector import collect
from election_bot.strategy import Book
from test_fair_value import T, iso, evidence
from test_leadership import snapshot


def configured():
    c,_,_=fixture()
    c['markets'][0].update(office='senate',race_key='2026:senate:NH',exposure_sign=1)
    return c


class FundamentalTests(unittest.TestCase):
    def test_official_counts_formula_and_sign(self):
        for state, direction in [('NH',1),('IA',-1),('NC',-1)]:
            p=build_prior(state,iso(T))
            self.assertGreater(direction*p['margin_pp'],0)
            self.assertAlmostEqual(p['margin_pp'],.25*sum(
                r['state_two_party_margin_pp']-r['national_two_party_margin_pp'] for r in p['components']))
            self.assertEqual(p['sd_pp'],12)
            self.assertFalse(p['policy']['calibrated'])
            validate_prior(p,'2026:senate:'+state)
        p=build_prior('NH',iso(T))
        self.assertEqual(p['components'][0]['state_votes'],{'dem':424937,'rep':365660})
        self.assertEqual(p['components'][1]['national_votes'],{'dem':75017613,'rep':77302580})

    def test_malformed_votes_unsupported_state_and_altered_prior_rejected(self):
        for value in (True,0,-1,3.5,float('nan'),'123'):
            with self.assertRaises(ValueError):margin({'dem':value,'rep':100})
        with self.assertRaises(ValueError):build_prior('CA',iso(T))
        with self.assertRaises(ValueError):build_prior('NH','2026-10-11')
        for field,value in [('margin_pp',30),('sd_pp',1),('dataset_hash','wrong')]:
            p=build_prior('NH',iso(T));p[field]=value
            with self.assertRaises(ValueError):validate_prior(p,'2026:senate:NH')
        with self.assertRaises(ValueError):validate_prior(build_prior('NH',iso(T)),'2026:house:NH-01')
        with self.assertRaises(ValueError):validate_prior(build_prior('NH',iso(T)),'2026:senate:IA')

    def test_transaction_rejects_tampered_historical_components(self):
        c=configured();data=evidence(c)
        data['races'][0]['fundamentals_prior']=build_prior('NH',iso(T))
        data['races'][0]['fundamentals_prior']['components'][0]['state_votes']['dem']=123
        with tempfile.TemporaryDirectory() as root:
            s=PollStore(Path(root)/'fair-value.sqlite3')
            try:
                with self.assertRaises(ValueError):s.ingest(data,c,T)
                self.assertEqual(s.latest('race',T),[])
            finally:s.close()

    def test_prior_policy_is_not_a_mutable_reference_to_model_constants(self):
        p=build_prior('NH',iso(T))
        p['policy']['cycle_weights'][0]=1
        self.assertEqual(POLICY['cycle_weights'],[.5,.5])
        with self.assertRaises(ValueError):validate_prior(p,'2026:senate:NH')

    def test_counterfactuals_share_polls_leave_configured_custom_prior_unchanged(self):
        c=configured();data=evidence(c)
        r=data['races'][0]
        r['prior']=dict(margin_pp=-5,sd_pp=15,published_at=iso(T-1),
                        source_url='https://example.test/custom',method='custom research prior')
        r['fundamentals_prior']=build_prior('NH',iso(T-1))
        with tempfile.TemporaryDirectory() as root:
            s=PollStore(Path(root)/'fair-value.sqlite3')
            try:
                s.ingest(data,c,T-1);race=s.latest('race',T)[0];polls=s.latest('poll',T)
                original=copy.deepcopy(race);baseline=estimate(race,polls,T)
                alt=variants(race,polls,T)
                self.assertEqual(race,original);self.assertEqual(estimate(race,polls,T),baseline)
                self.assertEqual(alt['neutral_prior']['poll_ids'],alt['historical_lean']['poll_ids'])
                self.assertGreater(alt['historical_lean']['yes_probability'],alt['neutral_prior']['yes_probability'])
                self.assertNotEqual(alt['historical_lean']['estimate_id'],alt['neutral_prior']['estimate_id'])
                rep=variants(dict(race,yes_party='REP'),polls,T)
                self.assertAlmostEqual(rep['historical_lean']['yes_probability']+alt['historical_lean']['yes_probability'],1)
                self.assertEqual(variants(dict(race,_received_at=T+1),polls,T),{})
                future=copy.deepcopy(race);future['fundamentals_prior']['published_at']=iso(T+1)
                self.assertEqual(variants(future,polls,T),{})
                sparse=variants(race,polls[:1],T)
                self.assertTrue(all(f['status']=='unavailable' for f in sparse.values()))
            finally:s.close()

    def test_seed_idempotent_no_backdating_and_collector_preserves_it(self):
        from election_bot.votehub import bindings
        c=configured()
        with tempfile.TemporaryDirectory() as root:
            s=PollStore(Path(root)/'fair-value.sqlite3')
            try:
                r=bindings(c)[0];r['prior']=dict(margin_pp=1,sd_pp=12,published_at=iso(T-2),source_url='https://example.test/prior',method='custom')
                s.ingest(dict(schema_version=1,races=[r],polls=[]),c,T-1)
            finally:s.close()
            result=seed(root,c,T)
            self.assertEqual(result['inserted_versions'],1)
            self.assertFalse(result['live_model_orders'])
            self.assertEqual(seed(root,c,T+1)['inserted_versions'],0)
            with self.assertRaises(ValueError):seed(root,c,T-2)
            collect(root,c,force=True,now=T+2,fetcher=lambda *args:('https://api.votehub.com/polls',[]))
            s=PollStore(Path(root)/'fair-value.sqlite3')
            try:
                old=s.latest('race',T-1)[0];new=s.latest('race',T+2)[0]
                self.assertNotIn('fundamentals_prior',old)
                self.assertEqual(new['_received_at'],T)
                self.assertEqual(new['prior'],r['prior'])
                self.assertEqual(new['fundamentals_prior']['published_at'],iso(T))
            finally:s.close()

    def test_changed_binding_is_not_silently_rebound_by_seed(self):
        c=configured()
        with tempfile.TemporaryDirectory() as root:
            seed(root,c,T)
            c['markets'][0]['contract_fingerprint']='changed'
            r=seed(root,c,T+1)
            self.assertEqual(r['bound_contracts'],0)
            self.assertEqual(r['skipped'][0]['reason'],'binding_changed_requires_review')

    def test_observation_records_both_variants_but_never_a_live_order(self):
        c=configured();data=evidence(c);data['races'][0]['fundamentals_prior']=build_prior('NH',iso(T-1))
        with tempfile.TemporaryDirectory() as root:
            s=PollStore(Path(root)/'fair-value.sqlite3')
            try:s.ingest(data,c,T-1)
            finally:s.close()
            with patch('election_bot.strategy.time.time',return_value=T):
                book=Book.make([('.48',100)],[('.50',100)])
                refs=[Book.make([('.49',100)],[('.51',100)]) for _ in range(2)]
                result=observe(root,c['markets'][0],book,refs,c['strategy'],'test',T)
            self.assertEqual(set(result['prior_variants']),{'neutral_prior','historical_lean'})
            self.assertTrue(result['research_only'])
            self.assertTrue(all(v['comparison']['research_only'] for v in result['prior_variants'].values()))
            self.assertFalse((Path(root)/'live.sqlite3').exists())
            formatted=format_report(report(root,c,now=T))
            self.assertIn('historical_lean: YES',formatted)
            self.assertIn('neutral_prior: YES',formatted)

    def test_forward_reports_keep_variant_outcomes_separate(self):
        from election_bot.state import Journal
        c=configured()
        with tempfile.TemporaryDirectory() as root:
            journal=Journal(Path(root)/'live.sqlite3','test')
            try:
                first=snapshot(at=T,sig=('.48','.50'));future=snapshot(sid='b',at=T+305,sig=('.60','.62'))
                proposal={'eligible':True,'side':'yes','ask':'.50'}
                event={'version':1,'model_version':'poll_margin_v1','snapshot_id':'a','exchange':'2','captured_at':T,
                    'status':'estimated','cost_buffer_per_share':'.01','comparison':{'model':[],'market':None},
                    'prior_variants':{name:{'forecast':{'experiment':'historical_lean_v1'},'comparison':{'model':[proposal]}}
                                      for name in ('neutral_prior','historical_lean')}}
                for at,kind,row in [(T,'quote_snapshot',first),(T,'fair_value_shadow',event),(T+305,'quote_snapshot',future)]:
                    journal.db.execute('INSERT INTO events VALUES(?,?,?)',(at,kind,json.dumps(row)))
                journal.db.commit();result=report(root,c,now=T+2000)
                for name in ('neutral_prior','historical_lean'):
                    self.assertEqual(result['markouts'][name+'_300']['mean_net_change_per_share'],'0.08')
                    self.assertEqual(result['markouts'][name+'_900']['statuses'],{'missing':1})
                self.assertNotIn('model_300',result['markouts'])
            finally:journal.close()


class ExpandedCatalogPriorTests(unittest.TestCase):
    def test_expanded_state_imported_without_refreshing_existing_priors(self):
        from election_bot.votehub import bindings
        c=configured()
        other=copy.deepcopy(c['markets'][0]);other.update(sig_exchange_id='other',race_key='2026:senate:TX')
        c['markets'].append(other)
        with tempfile.TemporaryDirectory() as root:
            store=PollStore(Path(root)/'fair-value.sqlite3')
            try:
                texas=[r for r in bindings(c) if r['race_key']=='2026:senate:TX'][0]
                store.ingest(dict(schema_version=1,races=[texas],polls=[]),c,T-1)
                before=store.latest('race',T)[0]
            finally:store.close()
            result=seed(root,c,T)
            self.assertEqual(result['bound_contracts'],2)
            self.assertEqual(result['skipped'],[])
            self.assertEqual(seed(root,c,T+1)['inserted_versions'],0)
            store=PollStore(Path(root)/'fair-value.sqlite3')
            try:
                races={r['sig_exchange_id']:r for r in store.latest('race',T+1)}
                self.assertEqual(races['other']['prior'],before['prior'])
                self.assertEqual(races['other']['fundamentals_prior']['state'],'TX')
                self.assertEqual(races['other']['fundamentals_prior']['published_at'],iso(T))
                self.assertIn('fundamentals_prior',races[c['markets'][0]['sig_exchange_id']])
            finally:store.close()


class ExpandedFundamentalsTests(unittest.TestCase):
    def test_new_state_counts_and_formula(self):
        expected = {
            'MI': [(2804040,2649852),(2736533,2816636)],
            'OH': [(2679165,3154834),(2533699,3180116)],
            'TX': [(5259126,5890347),(4835250,6393597)],
        }
        for state, counts in expected.items():
            p = build_prior(state, iso(T))
            validate_prior(p, '2026:senate:'+state)
            self.assertEqual([(r['state_votes']['dem'],r['state_votes']['rep']) for r in p['components']], counts)
            self.assertEqual([(r['dem_page'],r['rep_page']) for r in p['components']], [(2,8),(3,5)])
            self.assertAlmostEqual(p['margin_pp'], .25*sum(r['relative_lean_pp'] for r in p['components']))
            self.assertEqual(p['sd_pp'], 12)
            self.assertFalse(p['policy']['calibrated'])

    def test_legacy_prior_hash_and_values_unchanged(self):
        from election_bot.fundamentals import DATA_PATH
        legacy = read_input(DATA_PATH)
        for state in ('NH','IA','NC'):
            old = build_prior(state, iso(T), legacy)
            self.assertEqual(build_prior(state, iso(T)), old)
            validate_prior(old, '2026:senate:'+state)
        self.assertNotEqual(build_prior('TX',iso(T))['dataset_hash'], digest(legacy))

    def test_new_state_hash_and_counts_tampering_rejected(self):
        for state in ('MI','OH','TX'):
            p = build_prior(state, iso(T)); p['components'][1]['state_votes']['dem'] += 1
            with self.assertRaises(ValueError): validate_prior(p, '2026:senate:'+state)
            p = build_prior(state, iso(T)); p['dataset_hash'] = build_prior('NH',iso(T))['dataset_hash']
            with self.assertRaises(ValueError): validate_prior(p, '2026:senate:'+state)

    def test_all_six_states_both_orientations_seed_idempotently(self):
        c = configured(); template = c['markets'][0]; c['markets'] = []
        for i,state in enumerate(('NH','IA','NC','MI','OH','TX')):
            for sign in (1,-1):
                c['markets'].append(dict(template,name=state+str(sign),sig_exchange_id=str(100+i*2+(sign==1)),
                                         race_key='2026:senate:'+state,exposure_sign=sign))
        with tempfile.TemporaryDirectory() as root:
            self.assertEqual(seed(root,c,T)['inserted_versions'],12)
            self.assertEqual(seed(root,c,T+1)['inserted_versions'],0)
            collect(root,c,force=True,now=T+2,fetcher=lambda *args:('https://api.votehub.com/polls',[]))
            store=PollStore(Path(root)/'fair-value.sqlite3')
            try:
                self.assertEqual(store.latest('race',T-1),[])
                races=store.latest('race',T+1)
                self.assertEqual(len(races),12)
                self.assertTrue(all(r['prior'] is None for r in races))
                self.assertTrue(all(r['_received_at']==T and r['fundamentals_prior']['published_at']==iso(T) for r in races))
                for r in races: validate_prior(r['fundamentals_prior'], r['race_key'])
            finally:store.close()
            self.assertFalse((Path(root)/'live.sqlite3').exists())

    def test_unreviewed_state_still_skips_without_breaking_known_states(self):
        from election_bot.votehub import bindings
        c=configured(); other=dict(c['markets'][0],sig_exchange_id='99',race_key='2026:senate:CA')
        c['markets'].append(other); bound=bindings(c)[0]
        fake=dict(bound,sig_exchange_id='99',race_key='2026:senate:CA')
        with tempfile.TemporaryDirectory() as root, patch('election_bot.votehub.bindings',return_value=[bound,fake]):
            result=seed(root,c,T)
            self.assertEqual(result['bound_contracts'],1)
            self.assertEqual(result['skipped'],[dict(exchange='99',reason='historical_state_not_reviewed')])

    def test_expanded_prior_keeps_sparse_poll_filter_and_primary_forecast(self):
        c=configured();data=evidence(c);r=data['races'][0]
        r['race_key']='2026:senate:MI';c['markets'][0]['race_key']=r['race_key']
        for poll in data['polls']:poll['race_key']=r['race_key']
        with tempfile.TemporaryDirectory() as root:
            store=PollStore(Path(root)/'fair-value.sqlite3')
            try:
                store.ingest(data,c,T-2)
                polls=store.latest('poll',T);original=store.latest('race',T)[0]
                baseline=estimate(original,polls,T)
                r['fundamentals_prior']=build_prior('MI',iso(T-1));store.ingest(data,c,T-1)
                race=store.latest('race',T)[0]
                updated=estimate(race,polls,T)
                for key in ('yes_probability','yes_low','yes_high','poll_ids','policy'):
                    self.assertEqual(updated[key],baseline[key])
                self.assertEqual(set(variants(race,polls,T)),{'neutral_prior','historical_lean'})
                self.assertEqual(variants(race,polls,T-2),{})
                sparse=variants(race,polls[:1],T)
                self.assertTrue(all(f['status']=='unavailable' for f in sparse.values()))
                rep=variants(dict(race,yes_party='REP'),polls,T)
                dem=variants(race,polls,T)
                self.assertAlmostEqual(rep['historical_lean']['yes_probability']+dem['historical_lean']['yes_probability'],1)
            finally:store.close()
