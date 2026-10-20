import unittest
from election_bot import model_live
from election_bot.strategy import D,Book
from election_bot.demo import fixture
from test_active import EXECUTION


class BoundTests(unittest.TestCase):
    def forecast(self,low=1,mean=1,high=1):
        return dict(status='estimated',version='poll_margin_v1',yes_low=low,yes_probability=mean,yes_high=high)

    def test_collapsed_tail_gets_symmetric_absolute_haircut(self):
        p={'minimum_probability_haircut':'.05'}
        yes=model_live.execution_bounds(self.forecast(),p)
        no=model_live.execution_bounds(self.forecast(0,0,0),p)
        self.assertEqual(yes['yes']['execution_bound'],D('.95'))
        self.assertEqual(no['no']['execution_bound'],D('.95'))
        self.assertEqual(yes['no']['execution_bound'],0)
        self.assertTrue(yes['yes']['floor_binding'])

    def test_preserves_more_conservative_existing_bound(self):
        p={'minimum_probability_haircut':'.05'}
        r=model_live.execution_bounds(self.forecast(.5,.7,.9),p)
        self.assertEqual(r['yes']['execution_bound'],D('.5'))
        self.assertEqual(r['no']['execution_bound'],D('.1'))
        self.assertFalse(r['yes']['floor_binding'])

    def test_zero_default_preserves_existing_execution(self):
        for prediction in (self.forecast(),self.forecast(.7,.9,.98)):
            r=model_live.execution_bounds(prediction,{})
            self.assertEqual(r['yes']['execution_bound'],D(prediction['yes_low']))
            self.assertEqual(r['no']['execution_bound'],1-D(prediction['yes_high']))

    def test_haircut_blocks_previously_eligible_tail_trade_without_changing_prediction(self):
        config,_,refs=fixture();strategy=config['strategy']
        books=[Book.make([('.87',200)],[('.89',200)]) for _ in range(2)]
        book=Book.make([('.89',200)],[('.90',200)])
        p=model_live.settings(config);f=self.forecast()
        signal=model_live.select(book,books,strategy,EXECUTION,f,p,25,0)
        self.assertIsNotNone(signal)
        p['minimum_probability_haircut']=D('.05')
        self.assertIsNone(model_live.select(book,books,strategy,EXECUTION,f,p,25,0))
        self.assertEqual(f,self.forecast())

    def test_invalid_haircuts_rejected(self):
        c,_,_=fixture()
        for value in ('-.01','.26','NaN'):
            c['model_trading']['minimum_probability_haircut']=value
            with self.assertRaises(ValueError):model_live.settings(c)

    def test_bound_diagnostics_are_persisted_with_model_reservation(self):
        from test_model_live import ModelLiveTests
        f=ModelLiveTests();f.setUp();self.addCleanup(f.doCleanups)
        f.config['model_trading']['minimum_probability_haircut']='.05'
        f.engine.cycle()
        self.assertEqual(len(f.sig.orders),1)
        import json
        detail=json.loads(f.tagged()[0]['detail'])
        self.assertEqual(D(detail['execution_bounds']['yes']['execution_haircut']),D('.05'))
        self.assertTrue(detail['execution_bounds']['yes']['floor_binding'])
        self.assertEqual(detail['settings']['minimum_probability_haircut'],'0.05')

    def test_reservation_rechecks_changed_haircut(self):
        from test_model_live import ModelLiveTests
        from unittest.mock import patch
        f=ModelLiveTests();f.setUp();self.addCleanup(f.doCleanups)
        # At .895 a near-saturated forecast passes .08 but not a .05 haircut.
        f.sig.prices['2']=('.885','.895')
        refs=[Book.make([('.88',200)],[('.90',200)]) for _ in range(2)]
        original=model_live.propose;calls=[]
        def changed(*args,**kwargs):
            calls.append(1)
            if len(calls)==3:f.config['model_trading']['minimum_probability_haircut']='.05'
            return original(*args,**kwargs)
        with patch.object(f.refs,'books',return_value=refs),patch('election_bot.model_live.propose',side_effect=changed):
            f.engine.cycle()
        self.assertEqual(len(calls),3)
        self.assertFalse(f.sig.orders)
        self.assertEqual(f.tagged(),[])
