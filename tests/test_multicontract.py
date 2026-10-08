import copy
from datetime import datetime, timezone
import json
from pathlib import Path
import random
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from test_active import Broker, EXECUTION
from test_contract_review import example, republican
from election_bot.active_engine import ActiveEngine
from election_bot.clients import fingerprint
from election_bot.contract_review import assess
from election_bot.demo import fixture
from election_bot.engine import validate_config
from election_bot.regions import region_for_state
from election_bot.performance import report as performance_report
from election_bot.risk import Exposure
from election_bot.settlement import review_fields, verify_review, entry_settings
from election_bot.state import Journal
from election_bot.strategy import Book, D


def setup_contracts():
    config, original, refs = fixture()
    config['execution'] = dict(EXECUTION, multi_contract_races=True)
    config['limits'].update(per_order='50', per_market='100', total='500', daily='500',
        net_shares_total='200', net_shares_per_office='200', net_shares_per_region='200', realized_loss_stop_fraction='.1')
    d = example(); r = republican(d)
    for row, mid, ex, party in ((d,'1','2','Democratic Party'), (r,'3','4','Republican Party')):
        m, record = row['mapping'], row['contract']
        m.update(enabled=True, sig_market_id=mid, sig_exchange_id=ex, exposure_mode='gross')
        m['name'] = party.split()[0].lower(); m.pop('exposure_sign', None)
        year, office, place = m['race_key'].split(':')
        m.update(office=office,state_code=place.split('-')[0],region=region_for_state(place.split('-')[0]))
        record['sig'].update(id=mid,exchange=ex)
        m['contract_fingerprint'] = fingerprint(record)
        m['settlement_review'] = {
            'version':1, 'decision':'accept_basis_risk', 'party':party,
            'race_key':m['race_key'], 'contract_fingerprint':m['contract_fingerprint'],
            'reviewed_at':datetime.fromtimestamp(time.time()-60,timezone.utc).isoformat(),
            'full_rules_reviewed':True, 'rationale':'Synthetic test acceptance only; not a live approval.',
            'additional_entry_edge':'0', 'entry_edge_rationale':'Test fixture only.',
            'rule_sources':{v:'https://example.test/'+v for v in ('SIG','Kalshi','Polymarket')},
            'acknowledged_flags':[f['code'] for f in assess(record,m,party)['flags']]}
    config['markets'] = [d['mapping'],r['mapping']]
    return config, original, {x['mapping']['sig_market_id']:x for x in (d,r)}


class SettlementTests(unittest.TestCase):
    def setUp(self):
        self.config, _, self.rows = setup_contracts()
        self.mapping = self.config['markets'][0]
        self.record = self.rows['1']['contract']

    def test_explicit_mode_and_reviews_validate_without_claiming_equivalence(self):
        validate_config(self.config)
        result = verify_review(self.mapping,self.record,self.config['strategy'])
        self.assertFalse(result['settlement_equivalence_verified'])
        self.assertFalse(result['new_trading_authorized'])
        self.config['execution'].pop('multi_contract_races')
        with self.assertRaisesRegex(ValueError, 'only one contract per race'):validate_config(self.config)

    def test_missing_blocked_and_fingerprint_mismatched_reviews_fail_startup(self):
        for mutation in ('missing','blocked','fingerprint','race','party','full_rules','future','notes','sources','flags'):
            config=copy.deepcopy(self.config);m=config['markets'][0];r=m['settlement_review']
            if mutation=='missing':m.pop('settlement_review')
            elif mutation=='blocked':r['decision']='blocked'
            elif mutation=='fingerprint':r['contract_fingerprint']='f'*64
            elif mutation=='race':r['race_key']='2026:senate:ZZ'
            elif mutation=='party':r['party']='Independent Party'
            elif mutation=='full_rules':r['full_rules_reviewed']=False
            elif mutation=='future':r['reviewed_at']='2999-01-01T00:00:00Z'
            elif mutation=='notes':r['rationale']=' '
            elif mutation=='sources':r['rule_sources']['SIG']='https://secret:token@example.test/'
            elif mutation=='flags':r['acknowledged_flags']=['a','a']
            with self.subTest(mutation=mutation),self.assertRaises(ValueError):validate_config(config)

    def test_new_or_missing_acknowledgements_cannot_override_evidence(self):
        self.mapping['settlement_review']['acknowledged_flags']=[]
        with self.assertRaisesRegex(ValueError,'flags differ'):
            verify_review(self.mapping,self.record,self.config['strategy'])

    def test_changed_rules_and_wrong_party_fail_runtime(self):
        self.record['kalshi']['rules_secondary']+=' changed'
        with self.assertRaisesRegex(ValueError,'evidence changed'):
            verify_review(self.mapping,self.record,self.config['strategy'])
        # Re-pinning a wrong identity and accepting all flags still cannot override it.
        self.mapping['contract_fingerprint']=fingerprint(self.record)
        self.mapping['settlement_review'].update(contract_fingerprint=fingerprint(self.record),party='Republican Party')
        with self.assertRaisesRegex(ValueError,'structural identity'):
            verify_review(self.mapping,self.record,self.config['strategy'])

    def test_edge_adjustment_is_explicit_and_never_reduces_required_gap(self):
        self.mapping['settlement_review']['additional_entry_edge']='.02'
        settings=entry_settings(self.mapping,self.config['strategy'])
        self.assertEqual(D(settings['minimum_edge']),D('.07'))
        self.assertEqual(self.config['strategy']['minimum_edge'],'0.05')
        for value in (None,True,'NaN','Infinity','-.01','1'):
            self.mapping['settlement_review']['additional_entry_edge']=value
            with self.subTest(value=value),self.assertRaises(ValueError):review_fields(self.mapping,self.config['strategy'])

    def test_disabled_sibling_also_needs_gross_metadata(self):
        self.config['markets'][1]['enabled']=False
        self.config['markets'][1]['exposure_sign']=-1
        with self.assertRaisesRegex(ValueError,'gross exposure'):validate_config(self.config)

    def test_missing_caps_geography_party_duplicates_and_news_mismatch_rejected(self):
        for case in ('caps','geography','office','party','news','flag'):
            config=copy.deepcopy(self.config)
            if case=='caps':config['limits'].pop('realized_loss_stop_fraction')
            elif case=='geography':config['markets'][1].update(state_code='HI',region='west')
            elif case=='office':
                for m in config['markets']:m['office']='house'
            elif case=='party':config['markets'][1]['settlement_review']['party']='Democratic Party'
            elif case=='news':
                config['news']['enabled']=True
                for m in config['markets']:m['news_match']={'all':[[m['name']]]}
            elif case=='flag':config['execution']['multi_contract_races']='yes'
            with self.subTest(case=case),self.assertRaises(ValueError):validate_config(config)


class GrossExposureTests(unittest.TestCase):
    def setUp(self):
        self.config,_,_=setup_contracts()

    def exposure(self, d=0, r=0, pending=None):
        holdings={}
        for ex, qty in (('2',d),('4',r)):
            if qty:holdings[ex,'yes' if qty>0 else 'no']={'quantity':D(abs(qty))}
        ledger=SimpleNamespace(inventory=lambda:(holdings,{}),journal=SimpleNamespace(pending=lambda:pending or []))
        return Exposure(ledger,self.config)

    def pending(self,action,side,quantity):
        return {'payload':json.dumps(dict(exchangeId='4',action=action,side=side,quantity=quantity))}

    def test_opposite_party_or_side_holdings_never_net(self):
        for d,r in ((100,90),(100,-90),(-100,90),(-100,-90)):
            e=self.exposure(d,r)
            self.assertEqual(e.bounds['total'],[D(-190),D(190)])
            self.assertEqual(e.headroom('2','buy','yes'),10)
            self.assertEqual(e.headroom('4','buy','no'),10)
            self.assertEqual(e.summary()['unnetted_gross_shares']['total'],190)
            self.assertTrue(e.summary()['net_shares_excludes_gross_contracts'])

    def test_pending_buys_reserve_both_directions_and_sales_release_nothing(self):
        buy=self.exposure(100,50,[self.pending('buy','no',30)])
        sale=self.exposure(100,50,[self.pending('sell','yes',30)])
        self.assertEqual(buy.bounds['total'],[D(-180),D(180)])
        self.assertEqual(sale.bounds['total'],[D(-150),D(150)])
        self.assertEqual(buy.headroom('2','buy','yes'),20)

    def test_gross_sales_only_reduce_bounds_and_cannot_sell_sibling_inventory(self):
        e=self.exposure(250,-20)
        self.assertEqual(e.headroom('2','buy','yes'),0)
        self.assertEqual(e.headroom('2','sell','yes'),250)
        self.assertEqual(e.headroom('4','sell','yes'),0)
        self.assertEqual(e.headroom('4','sell','no'),20)

    def test_gross_and_existing_signed_positions_share_conservative_bounds(self):
        self.config['markets'][0].pop('exposure_mode');self.config['markets'][0]['exposure_sign']=1
        e=self.exposure(100,50)
        self.assertEqual(e.bounds['total'],[D(50),D(150)])
        self.assertEqual(e.headroom('4','buy','no'),50)
        self.assertEqual(e.headroom('2','buy','no'),250)

    def test_random_gross_buys_respect_all_groups_under_partial_fills(self):
        rng=random.Random(606)
        for _ in range(150):
            d,r,p=[rng.randrange(100) for _ in range(3)]
            e=self.exposure(d,-r,[self.pending('buy','no',p)])
            headroom=e.headroom('2','buy','yes')
            for frac in (D(0),D('.5'),D(1)):
                for executed in (D(0),headroom/2,headroom):
                    self.assertLessEqual(d+r+p*frac+executed,max(D(200),D(d+r+p*frac)))


class MultiContractIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.config,original,self.rows=setup_contracts()
        self.sig=Broker(original)
        def market(mid):
            row=self.rows[str(mid)];r=row['contract']['sig']
            return {'id':str(mid),'status':'open','title':r['title'],'settlementDate':r.get('settlementDate'),
                'resolution_tree':r['resolution_tree'],'exchanges':[{'id':r['exchange'],'option':r['option']}]}
        self.sig.market=market
        self.refs=Mock()
        self.refs.metadata.side_effect=lambda m:(self.rows[m['sig_market_id']]['contract']['kalshi'],
            self.rows[m['sig_market_id']]['contract']['polymarket'],self.rows[m['sig_market_id']]['contract']['poly_yes_token'])
        self.refs.books.side_effect=lambda m,meta:[Book.make([('.73',200)],[('.75',200)]),Book.make([('.74',200)],[('.76',200)])]
        self.journal=Journal(Path(self.temp.name)/'test.sqlite3','test');self.addCleanup(self.journal.close)
        self.engine=ActiveEngine(self.config,self.sig,self.refs,self.journal,self.temp.name,live=True)

    def age_orders(self):
        self.journal.db.execute('UPDATE orders SET created=?',(time.time()-1000,));self.journal.db.commit()

    def test_each_contract_can_trade_native_identity_under_one_race_budget(self):
        self.engine.cycle()
        self.assertEqual(len(self.sig.orders),1)  # Same-race cooldown prevents a second order.
        first=next(iter(self.sig.orders.values()))['exchangeId']
        self.age_orders();self.sig.prices[first]=('.70','.75')
        self.engine.cycle()
        self.assertEqual(len(self.sig.orders),2)
        self.assertEqual({o['exchangeId'] for o in self.sig.orders.values()},{'2','4'})
        self.assertEqual(self.engine.portfolio_summary()['open_races'],1)
        self.assertEqual(self.engine.portfolio_summary()['open_contracts'],2)
        performance=performance_report(Path(self.temp.name)/'test.sqlite3')
        self.assertEqual((performance['open_races'],performance['open_contracts']),(1,2))
        self.assertEqual(performance['race_count_basis'],'persisted_race_bindings')
        self.assertLessEqual(self.engine.races.committed(self.engine.ledger,'2'),100)
        self.age_orders();self.engine.cycle()
        self.assertLessEqual(self.engine.races.committed(self.engine.ledger,'2'),100)

    def test_changed_review_or_identity_never_submits(self):
        for m in self.config['markets']:m['settlement_review']['acknowledged_flags']=[]
        self.engine.cycle();self.assertFalse(self.sig.orders)

    def test_review_edge_changes_entry_decision(self):
        for m in self.config['markets']:m['settlement_review']['additional_entry_edge']='.15'
        self.engine.cycle();self.assertFalse(self.sig.orders)

    def test_dispute_on_disabled_sibling_blocks_other_contract(self):
        self.config['markets'][0]['enabled']=False
        news=Mock();news.block_reason.side_effect=lambda m:'dispute' if m['sig_exchange_id']=='2' else None
        news.drain.return_value={}
        engine=ActiveEngine(self.config,self.sig,self.refs,self.journal,self.temp.name,live=True,news=news)
        engine.cycle();self.assertFalse(self.sig.orders)
        self.assertEqual(engine.news_block(self.config['markets'][1]),'dispute')

    def test_gross_mode_cannot_be_downgraded_on_restart(self):
        self.config['execution']['multi_contract_races']=False
        self.config['markets'][1]['enabled']=False
        for m in self.config['markets']:
            m.pop('exposure_mode');m['exposure_sign']=1
        with self.assertRaisesRegex(ValueError,'Gross exposure binding'):
            ActiveEngine(self.config,self.sig,self.refs,self.journal,self.temp.name,live=True)

    def test_disabling_multi_mode_keeps_review_and_entry_edge(self):
        self.config['execution']['multi_contract_races']=False
        self.config['markets'][1]['enabled']=False
        self.config['markets'][0]['settlement_review']['additional_entry_edge']='.15'
        engine=ActiveEngine(self.config,self.sig,self.refs,self.journal,self.temp.name,live=True)
        engine.cycle();self.assertFalse(self.sig.orders)
        self.config['markets'][0].pop('settlement_review')
        with self.assertRaisesRegex(ValueError,'settlement review'):
            ActiveEngine(self.config,self.sig,self.refs,self.journal,self.temp.name,live=True)

    def test_removing_historical_gross_mapping_is_rejected(self):
        self.config['markets'].pop(1)
        with self.assertRaisesRegex(ValueError,'Keep historical gross mappings'):
            ActiveEngine(self.config,self.sig,self.refs,self.journal,self.temp.name,live=True)

    def test_paper_mode_routes_without_broker_order_submission(self):
        engine=ActiveEngine(self.config,self.sig,self.refs,self.journal,self.temp.name,live=False)
        engine.cycle();self.assertFalse(self.sig.orders)
        self.assertEqual(self.journal.db.execute('SELECT COUNT(*) FROM executions').fetchone()[0],1)

    def test_sibling_rule_change_blocks_only_that_contract_without_cross_routing(self):
        # Both claims retain their own rule verification and native exchange ID.
        self.rows['1']['contract']['kalshi']['rules_secondary']+=' changed'
        self.engine.cycle()
        self.assertEqual({o['exchangeId'] for o in self.sig.orders.values()},{'4'})

    def test_changed_rules_during_forced_preflight_block_order(self):
        count={}
        original=self.sig.market
        def changing(mid):
            count[mid]=count.get(mid,0)+1
            result=copy.deepcopy(original(mid))
            if count[mid]>1:result['title']+=' changed'
            return result
        self.sig.market=changing
        self.engine.cycle();self.assertFalse(self.sig.orders)

    def test_gross_share_headroom_enforced_at_final_reservation(self):
        self.engine.cycle();self.age_orders()
        held=sum(abs(q) for q in self.sig.inventory.values())
        self.config['limits'].update(net_shares_total=str(held+1),net_shares_per_office=str(held+1),net_shares_per_region=str(held+1))
        ex='4' if '2' in self.sig.inventory else '2'
        payload=dict(exchangeId=ex,side='yes',action='buy',quantity=2,price='.60',idempotencyKey='blocked')
        with self.assertRaisesRegex(ValueError,'Exposure changed'):
            self.engine.reserve_order(payload,D('1.22'),self.sig.account(),self.sig.positions())
        self.assertFalse(self.journal.pending())
