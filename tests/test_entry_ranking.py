import unittest
from unittest.mock import patch
from election_bot.entry_ranking import candidate,ordered,settings
from election_bot.strategy import D,Signal,Book
from election_bot.active_engine import ActiveEngine
from election_bot.clients import contract_record,fingerprint
import test_patient_entry as fixture

class RankingTests(unittest.TestCase):
    def test_sort_and_stale_filter(self):
        rows=[{'mapping':{'sig_exchange_id':e},'surplus':D(v),'edge_per_coin':D('.1'),'expires':t}
              for e,v,t in [('2','1',100),('3','2',100),('4','100',90)]]
        self.assertEqual([d['mapping']['sig_exchange_id'] for d in ordered(rows,99)],['3','2'])
    def test_patient_discount_does_not_boost_ranking(self):
        b=Book.make([('.55',100)],[('.60',100)])
        c={'strategy':{'cost_buffer_per_share':'.01','max_age_seconds':15}}
        a=candidate({'sig_exchange_id':'2'},Signal('yes',D('.6'),10,D('.73'),D('.12')),b,[b,b],c)
        p=candidate({'sig_exchange_id':'2'},Signal('yes',D('.595'),10,D('.73'),D('.125'),'buy','patient_entry'),b,[b,b],c)
        self.assertEqual(a['surplus'],p['surplus'])
    def test_configuration_is_explicit(self):
        self.assertFalse(settings({}))
        with self.assertRaises(ValueError):settings({'entry_ranking':{'enabled':'true'}})

class RankingIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.f=fixture.ThroughTheEngine();self.addCleanup(self.f.doCleanups)
        old=self.f.engine(enabled=False);c=old.config
        c['entry_ranking']={'enabled':True};c['scan_batch_size']=2
        c['execution']['max_orders_per_cycle']=1
        a=c['markets'][0];b=dict(a,name='second',sig_exchange_id='3',sig_market_id='3')
        b['contract_fingerprint']=fingerprint(contract_record(self.f.sig.market('3'),b,old.references.metadata(b)))
        c['markets'].append(b)
        self.e=ActiveEngine(c,self.f.sig,old.references,self.f.journal,old.runtime,live=True)
        self.f.sig.prices['2']=('.55','.60');self.f.sig.prices['3']=('.45','.50')
        # Deterministic scan order so the higher surplus candidate is seen second.
        self.e.scanner.batch=lambda *args:iter([a,b])
    def test_stronger_second_candidate_gets_first_order(self):
        self.e.cycle()
        self.assertEqual(self.f.sig.orders[1]['exchangeId'],'3')
        self.assertEqual(self.e._scan_metrics['quotes_checked'],2)
    def test_preflight_invalidated_candidate_does_not_trade(self):
        original=self.f.sig.book;calls={}
        def book(ex):
            calls[ex]=calls.get(ex,0)+1
            if ex=='3' and calls[ex]>=2:return Book.make([('.80',200)],[('.85',200)])
            return original(ex)
        self.f.sig.book=book;self.e.cycle()
        self.assertEqual(self.f.sig.orders[1]['exchangeId'],'2')
    def test_expired_candidates_are_not_sent(self):
        with patch('election_bot.entry_ranking.ordered',return_value=[]):self.e.cycle()
        self.assertFalse(self.f.sig.orders)
    def test_exit_preempts_queued_buys(self):
        payload={'exchangeId':'3','side':'yes','action':'buy','quantity':10,'price':'.5','idempotencyKey':'seed'}
        self.f.journal.reserve(payload,D('5.1'));self.e.ledger.record(payload,10,'.5','.01')
        self.f.journal.db.execute('UPDATE orders SET created=0');self.f.journal.db.commit()
        self.f.sig.inventory['3']=10;self.f.sig.prices['3']=('.80','.85')
        self.e.cycle()
        self.assertEqual(self.f.sig.orders[1]['exchangeId'],'3')
        self.assertEqual(self.f.sig.orders[1]['action'],'sell')

    def test_ranked_patient_keeps_cap_and_attribution(self):
        self.e.config['patient_entry'].update(enabled=True,control_fraction='0',pilot_fraction='1')
        self.e.cycle();order=self.f.sig.orders[1]
        self.assertEqual(order['exchangeId'],'3');self.assertEqual(D(order['price']),D('.495'))
        self.assertLessEqual(D(order['price'])*order['quantity'],25)
        tag=self.f.journal.db.execute('SELECT priced FROM patient_orders').fetchone()
        self.assertEqual(tag['priced'],'improved')
