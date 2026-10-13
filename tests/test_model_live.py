import copy
import json
from pathlib import Path
import time
import tempfile
import unittest
from unittest.mock import patch, Mock

import test_active
from test_fair_value import evidence
from election_bot import model_live
from election_bot.active_engine import ReservationRejected, ActiveEngine
from election_bot.engine import validate_config
from election_bot.polling import PollStore
from election_bot.strategy import Book,D


class ModelLiveTests(unittest.TestCase):
    def setUp(self):
        from election_bot.demo import fixture
        from election_bot.state import Journal
        from election_bot.clients import fingerprint,contract_record
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.config,original,self.refs=fixture()
        self.config['execution']=dict(test_active.EXECUTION)
        self.sig=test_active.Broker(original)
        mapping=self.config['markets'][0]
        mapping.update(sig_market_id='2',race_key='2026:senate:NH',exposure_sign=1,office='senate')
        mapping['contract_fingerprint']=fingerprint(contract_record(self.sig.market('2'),mapping,self.refs.metadata(mapping)))
        self.journal=Journal(self.root/'journal.db','test');self.addCleanup(self.journal.close)
        self.engine=ActiveEngine(self.config,self.sig,self.refs,self.journal,self.root,live=True)
        self.config['model_trading']=dict(model_live.DEFAULTS,enabled=True,acknowledge_uncalibrated=True)
        self.config['research_polling']={'enabled':True}
        self.config['markets'][0]['office']='senate'
        self.sig.prices['2']=('.69','.70')  # No legacy external-bid entry gap.
        self.now=time.time()
        self.data=evidence(self.config,self.now)
        for p in self.data['polls']:p.update(dem_pct=65,rep_pct=33)
        self.store=PollStore(self.root/'fair-value.sqlite3');self.addCleanup(self.store.close)
        self.store.ingest(self.data,self.config,self.now-1)
        self.health={'status':'ok','last_success_at':self.now}
        (self.root/'poll-collector').mkdir()
        self.save_health()

    def save_health(self):
        (self.root/'poll-collector/status.json').write_text(json.dumps(self.health))

    def age(self):test_active.ActiveTests.age_orders(self)

    def tagged(self):return self.journal.db.execute('SELECT * FROM model_orders').fetchall()

    def test_opt_in_and_dependencies_validated(self):
        validate_config(self.config)
        for field,value in [('acknowledge_uncalibrated',False),('enabled','true'),('minimum_edge','.01'),('total','NaN')]:
            c=copy.deepcopy(self.config);c['model_trading'][field]=value
            with self.assertRaises(ValueError):validate_config(c)
        for section,field in [('research_polling','enabled'),('execution','sell_enabled')]:
            c=copy.deepcopy(self.config);c[section][field]=False
            with self.assertRaises(ValueError):validate_config(c)

    def test_live_route_to_fake_broker_and_durable_evidence(self):
        self.engine.cycle()
        self.assertEqual(len(self.sig.orders),1)
        order=self.sig.orders[1]
        self.assertEqual(order['action'],'buy');self.assertEqual(order['side'],'yes')
        self.assertLessEqual(D(order['quantity'])*(D(order['price'])+D('.01')),25)
        detail=json.loads(self.tagged()[0]['detail'])
        self.assertFalse(detail['calibrated'])
        self.assertEqual(detail['forecast']['pollster_count'],3)
        self.assertIn('contract_fingerprint',detail)
        cap,used=model_live.allowance(self.journal.db,model_live.settings(self.config),'2026:senate:NH',time.time())
        self.assertEqual(used['total'],D(order['quantity'])*D('.71'))
        # Restart retains attribution; does not reset model budget.
        ActiveEngine(self.config,self.sig,self.refs,self.journal,self.root,live=True)
        self.assertEqual(model_live.allowance(self.journal.db,model_live.settings(self.config),'2026:senate:NH',time.time())[1],used)

    def test_disabled_route_cannot_submit(self):
        self.config['model_trading']['enabled']=False
        self.engine.cycle();self.assertFalse(self.sig.orders);self.assertEqual(self.tagged(),[])

    def test_missing_stale_failed_or_future_collector_blocks(self):
        for value in [{'status':'error','last_success_at':self.now},
                      {'status':'ok','last_success_at':self.now-7201},
                      {'status':'ok','last_success_at':self.now+100}]:
            self.health=value;self.save_health();self.engine.cycle();self.assertFalse(self.sig.orders)
        (self.root/'poll-collector/status.json').unlink()
        self.engine.cycle();self.assertFalse(self.sig.orders)

    def test_sparse_withdrawn_and_changed_bindings_block(self):
        for p in self.data['polls'][1:]:p['withdrawn']=True
        self.store.ingest(self.data,self.config,time.time())
        self.engine.cycle();self.assertFalse(self.sig.orders)
        for p in self.data['polls']:p['withdrawn']=False
        self.store.ingest(self.data,self.config,time.time())
        self.config['markets'][0]['contract_fingerprint']='0'*64
        s,d=model_live.propose(self.root,self.config['markets'][0],self.config,self.journal.db,
            self.sig.book('2'),self.refs.books(None,None),self.config['strategy'],25,0)
        self.assertIsNone(s);self.assertEqual(d['reason'],'model_input_unavailable')

    def test_preflight_rechecks_and_rejects_changed_evidence_before_reservation(self):
        original=model_live.propose;calls=[]
        def changed(*args,**kwargs):
            calls.append(1)
            if len(calls)==3:
                self.health['status']='error';self.save_health()
            return original(*args,**kwargs)
        with patch('election_bot.model_live.propose',side_effect=changed):self.engine.cycle()
        self.assertEqual(len(calls),3)
        self.assertFalse(self.sig.orders);self.assertEqual(self.tagged(),[])
        self.assertEqual(self.journal.summary()['orders'],0)

    def test_partial_fill_and_sale_keep_gross_model_purchase_cost(self):
        self.sig.partial=4;self.engine.cycle()
        used=model_live.allowance(self.journal.db,model_live.settings(self.config),'2026:senate:NH',time.time())[1]
        self.assertEqual(used['total'],D('2.84'))
        self.age();self.sig.prices['2']=('.85','.86');self.engine.cycle()
        self.assertEqual(self.sig.orders[2]['action'],'sell')
        self.assertEqual(len(self.tagged()),1)
        self.assertEqual(model_live.allowance(self.journal.db,model_live.settings(self.config),'2026:senate:NH',time.time())[1]['total'],D('2.84'))

    def test_unknown_submission_keeps_model_reservation(self):
        self.sig.timeout=True
        with self.assertRaises(TimeoutError):self.engine.cycle()
        self.assertEqual(len(self.journal.pending()),1);self.assertEqual(len(self.tagged()),1)
        used=model_live.allowance(self.journal.db,model_live.settings(self.config),'2026:senate:NH',time.time())[1]
        self.assertEqual(used['total'],D('24.85'))
        with self.assertRaises(RuntimeError):self.engine.cycle()
        self.assertEqual(len(self.sig.orders),1)

    def test_model_race_budget_and_global_budget_both_apply(self):
        self.config['model_trading']['per_race']='25'
        self.engine.cycle();self.age();self.engine.cycle()
        self.assertEqual(len(self.sig.orders),1)
        self.config['limits']['total']='1'
        self.config['model_trading']['per_race']='250'
        self.engine.cycle();self.assertEqual(len(self.sig.orders),1)

    def test_daily_rollover_does_not_reset_total_or_race_purchases(self):
        self.config['model_trading']['daily']='25'
        self.engine.cycle()
        policy=model_live.settings(self.config)
        cap,used=model_live.allowance(self.journal.db,policy,'2026:senate:NH',self.now)
        self.assertEqual(cap,D('.15'))
        tomorrow,after=model_live.allowance(self.journal.db,policy,'2026:senate:NH',self.now+86400)
        self.assertEqual(tomorrow,D('25'))
        self.assertEqual(after['daily'],0)
        self.assertEqual(after['total'],used['total'])
        self.assertEqual(after['per_race'],used['per_race'])
        policy['total']=D('25')
        self.assertEqual(model_live.allowance(self.journal.db,policy,'other-race',self.now+86400)[0],D('.15'))

    def test_unfilled_order_releases_model_reservation(self):
        self.sig.partial=0
        self.engine.cycle()
        self.assertEqual(len(self.tagged()),1)
        cap,used=model_live.allowance(self.journal.db,model_live.settings(self.config),'2026:senate:NH',self.now)
        self.assertEqual(cap,D('25'));self.assertEqual(used['total'],0)

    def test_stale_quotes_block_model_at_reservation(self):
        mapping=self.config['markets'][0];book=self.sig.book('2');refs=self.refs.books(None,None)
        book.observed_at-=100
        payload=dict(exchangeId='2',side='yes',action='buy',quantity=1,price=.7,idempotencyKey='stale')
        with self.assertRaises(ReservationRejected):
            self.engine.reserve_order(payload,D('.71'),self.sig.account(),self.sig.positions(),
                exit_reason=model_live.REASON,model_context=(mapping,book,refs,self.config['strategy']))
        self.assertEqual(self.tagged(),[]);self.assertEqual(self.journal.summary()['orders'],0)

    def test_model_attribution_rolls_back_with_failed_reservation(self):
        mapping=self.config['markets'][0];book=self.sig.book('2');refs=self.refs.books(None,None)
        payload=dict(exchangeId='2',side='yes',action='buy',quantity=1,price=.7,idempotencyKey='rollback')
        with patch.object(self.engine,'loss_status',return_value={'realized_loss_stop_coins':1,'net_realized_loss':2}):
            with self.assertRaises(RuntimeError):
                self.engine.reserve_order(payload,D('.71'),self.sig.account(),self.sig.positions(),
                    exit_reason=model_live.REASON,model_context=(mapping,book,refs,self.config['strategy']))
        self.assertEqual(self.tagged(),[]);self.assertEqual(self.journal.summary()['orders'],0)

    def test_existing_market_entry_has_priority_and_not_tagged(self):
        self.sig.prices['2']=('.58','.60')
        self.engine.cycle();self.assertEqual(len(self.sig.orders),1);self.assertEqual(self.tagged(),[])

    def test_model_opposite_side_exposure_thin_depth_and_exit_conflict(self):
        p=model_live.settings(self.config);strategy=self.config['strategy'];refs=self.refs.books(None,None)
        f={'status':'estimated','version':'poll_margin_v1','yes_low':.90,'yes_probability':.95,'yes_high':.99}
        book=self.sig.book('2')
        self.assertIsNone(model_live.select(book,refs,strategy,self.config['execution'],f,p,25,-10))
        self.assertIsNone(model_live.select(book,refs,strategy,self.config['execution'],f,p,25,0,{'yes':0,'no':0}))
        thin=Book.make([('.69',3)],[('.70',3)])
        self.assertIsNone(model_live.select(thin,refs,strategy,self.config['execution'],f,p,25,0))
        conflict=Book.make([('.80',200)],[('.81',200)])
        self.assertIsNone(model_live.select(conflict,refs,strategy,self.config['execution'],f,p,25,0))
        no_f=dict(f,yes_low=.01,yes_probability=.05,yes_high=.10)
        no_refs=[r.complement() for r in refs]
        no=model_live.select(book.complement(),no_refs,strategy,self.config['execution'],no_f,p,25,0)
        self.assertEqual(no.side,'no');self.assertEqual(no.price,D('.70'))

    def test_news_and_stop_block_model_buys(self):
        self.engine.news=Mock();self.engine.news.drain.return_value={};self.engine.news.block_reason.return_value='news_pause'
        self.engine.cycle();self.assertFalse(self.sig.orders)
        self.engine.news=None;(self.root/'STOP').touch()
        self.engine.cycle();self.assertFalse(self.sig.orders)
