import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from election_bot.model_rotation import ModelRotationStudy,report,values,allocation_attribution,direct_buy_attribution
from election_bot.rotation import report as combined_report,format_report
from election_bot.strategy import D
import test_rotation


class ModelRotationTests(unittest.TestCase):
    def setUp(self):
        self.f=test_rotation.RotationTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        for m,state in ((self.f.a,'NH'),(self.f.b,'TX')):
            m.update(race_key='2026:senate:'+state,office='senate',exposure_sign=1)
        self.forecasts={'2':('.54','.55','.56'),'3':('.70','.75','.80')}
        self.poll_hash='poll'
        self.calls=0
        self.g=ModelRotationStudy(self.f.l,self.f.c,self.f.temp.name,self.forecast)
        self.seq=0

    def forecast(self,m,now):
        self.calls+=1
        low,mean,high=self.forecasts[m['sig_exchange_id']]
        return {'status':'estimated','version':'poll_margin_v1','race_key':m['race_key'],
                'as_of':now,'yes_low':low,'yes_probability':mean,'yes_high':high,
                'estimate_id':str(now)+m['sig_exchange_id'],'race_hash':'race'+m['sig_exchange_id'],
                'weights':[{'hash':self.poll_hash,'weight':now}], 'policy':{'unchanged':True}}

    def observe(self,m,bid,ask,**kwargs):
        self.seq+=1
        defaults={'buy_allowed':True,'available':25,'cash':1000,'now':self.f.now};defaults.update(kwargs)
        self.g.observe(m,self.f.book(bid,ask),str(self.seq),**defaults)

    def pair(self):
        self.observe(self.f.a,'.55','.56');self.observe(self.f.b,'.39','.40')

    def trials(self):
        return [json.loads(r[0]) for r in self.f.j.db.execute('SELECT detail FROM model_rotation_trials ORDER BY at')]

    def confirm(self):
        self.pair();self.f.now+=60;self.pair();self.f.now+=60;self.pair()
        return self.trials()[-1] if self.trials() else None

    def purchase(self):
        self.f.now+=1;self.observe(self.f.b,'.39','.40')
        return self.trials()[-1]

    def test_three_confirmations_then_separate_purchase(self):
        baseline=self.f.l.inventory();orders=self.f.j.summary()['orders']
        self.pair();self.assertEqual(self.trials(),[])
        self.f.now+=60;self.pair();self.assertEqual(self.trials(),[])
        self.f.now+=60;self.pair();d=self.trials()[0]
        self.assertEqual(d['confirmation']['count'],3)
        self.assertEqual(d['status'],'awaiting_purchase')
        self.assertEqual(D(d['simulated_cash']),D('13.50'))
        self.assertIsNone(d['purchase'])
        d=self.purchase()
        self.assertEqual(d['status'],'purchased')
        self.assertEqual(d['purchase']['quantity'],32)
        self.assertEqual(D(d['simulated_cash']),D('.38'))
        self.assertEqual(D(d['purchase']['cost'])+D(d['simulated_cash']),D(d['sale_proceeds']))
        self.assertEqual(self.f.l.inventory(),baseline)
        self.assertEqual(self.f.j.summary()['orders'],orders)
        self.assertFalse(self.f.j.pending())

    def test_no_side_model_values_are_oriented(self):
        f=self.forecast(self.f.a,self.f.now)
        self.assertEqual(values(f,'no'),(D('.44'),D('.45'),D('.46')))
        self.forecasts['3']=('.20','.25','.30')
        self.confirm();d=self.trials()[0]
        self.assertEqual(d['target']['side'],'no')

    def test_one_leg_repeated_or_too_fast_does_not_confirm(self):
        self.pair()
        for _ in range(5):self.f.now+=1;self.observe(self.f.b,'.39','.40')
        self.assertEqual(self.trials(),[])
        rows=self.f.j.db.execute('SELECT detail FROM model_rotation_confirmations').fetchall()
        self.assertTrue(all(json.loads(r[0])['count']==1 for r in rows))

    def test_confirmation_survives_restart_without_quote_cache(self):
        self.pair();self.f.now+=60;self.pair()
        self.g=ModelRotationStudy(self.f.l,self.f.c,self.f.temp.name,self.forecast)
        self.f.now+=60;self.pair()
        self.assertEqual(len(self.trials()),1)

    def test_evidence_revision_and_live_fills_reset_confirmation(self):
        self.pair();self.f.now+=60;self.pair();self.poll_hash='revised'
        self.f.now+=60;self.pair();self.assertFalse(self.trials())
        self.f.fill('2',1);self.f.now+=60;self.pair();self.assertFalse(self.trials())
        counts=[json.loads(r[0])['count'] for r in self.f.j.db.execute('SELECT detail FROM model_rotation_confirmations')]
        self.assertTrue(all(c==1 for c in counts))

    def test_reversal_gap_and_buy_pause_reset(self):
        self.pair();self.f.now+=60;self.pair()
        self.forecasts['3']=('.30','.35','.40');self.f.now+=60;self.pair()
        self.assertFalse(self.f.j.db.execute('SELECT 1 FROM model_rotation_confirmations').fetchone())
        self.forecasts['3']=('.70','.75','.80');self.f.now+=1;self.pair()
        self.f.now+=181;self.pair();self.assertFalse(self.trials())
        self.observe(self.f.b,'.39','.40',buy_allowed=False)
        self.assertFalse(self.f.j.db.execute('SELECT 1 FROM model_rotation_confirmations').fetchone())

    def test_missing_or_stale_model_never_falls_back_to_external_prices(self):
        with patch.object(self.g,'forecast_provider',side_effect=ValueError('unavailable')):
            self.confirm()
        self.assertEqual(self.trials(),[])
        original=self.forecast
        def stale(m,now):return dict(original(m,now),as_of=now-16)
        self.g.forecast_provider=stale;self.confirm();self.assertEqual(self.trials(),[])

    def test_fresh_target_quote_rechecks_edge_and_keeps_cash(self):
        self.confirm();self.f.now+=1;self.observe(self.f.b,'.68','.69')
        d=self.trials()[0]
        self.assertEqual(d['status'],'cash_only')
        self.assertEqual(d['purchase_skip_reason'],'destination_edge_disappeared')
        self.assertEqual(D(d['simulated_cash']),D(d['sale_proceeds']))

    def test_source_model_improvement_can_cancel_purchase(self):
        self.confirm();self.forecasts['2']=('.94','.95','.99');d=self.purchase()
        self.assertEqual(d['status'],'cash_only')
        self.assertEqual(d['purchase_skip_reason'],'rotation_improvement_disappeared')

    def test_pending_buy_does_not_use_old_source_timestamp(self):
        self.confirm();start=self.f.now;self.f.now+=1
        with patch('time.time',return_value=start):book=self.f.book('.39','.40')
        self.g.observe(self.f.b,book,'new-id-old-book',True,25,1000,now=self.f.now)
        self.assertEqual(self.trials()[0]['status'],'awaiting_purchase')
        self.f.now+=61;self.observe(self.f.a,'.55','.56')
        self.assertEqual(self.trials()[0]['status'],'cash_only')

    def test_pending_buy_rechecks_inventory_and_news(self):
        self.confirm();self.f.fill('2',1);d=self.purchase()
        self.assertEqual(d['purchase_skip_reason'],'live_inventory_changed')

    def test_pending_buy_rechecks_cash_daily_and_depth(self):
        self.confirm();self.f.c['limits']['daily']='61' # original source acquisition already used 61
        d=self.purchase()
        self.assertEqual(d['status'],'cash_only')
        self.assertEqual(d['purchase_skip_reason'],'purchase_cash_depth_or_exposure')

    def test_active_trial_races_cannot_be_reused(self):
        self.confirm();self.purchase()
        for _ in range(3):self.f.now+=60;self.pair()
        self.assertEqual(len(self.trials()),1)

    def test_outcomes_use_actual_refreshed_purchase_and_two_buffers(self):
        first=self.confirm();self.f.now+=1;self.observe(self.f.b,'.395','.405');d=self.trials()[0]
        self.assertEqual(d['purchase']['ask'],'0.405')
        self.f.now=first['at']+905
        self.g.observe_outcomes(self.f.a,self.f.book('.56','.57'))
        self.g.observe_outcomes(self.f.b,self.f.book('.60','.61'))
        r=report(self.f.path,now=self.f.now)
        expected=D(d['simulated_cash'])+D(d['purchase']['quantity'])*D('.59')-D(d['source']['quantity'])*D('.55')
        self.assertEqual(D(r['outcomes']['900']['mean_incremental_coins']),expected)
        self.assertEqual(r['outcomes']['3600']['not_due'],1)
        attribution=r['outcomes']['900']['allocation_attribution']['all']
        self.assertEqual(attribution['measured'],1)
        self.assertEqual(D(attribution['mean_exit_vs_hold_coins'])+D(attribution['mean_reinvestment_vs_cash_coins']),expected)
        self.assertEqual(r['outcomes']['900']['allocation_attribution']['purchased']['measured'],1)

    def test_cash_only_compares_sale_proceeds_against_holding(self):
        first=self.confirm();self.f.now+=61;self.observe(self.f.a,'.55','.56')
        self.f.now=first['at']+905;self.g.observe_outcomes(self.f.a,self.f.book('.50','.51'))
        r=report(self.f.path,now=self.f.now)
        self.assertEqual(D(r['outcomes']['900']['mean_incremental_coins']),D('1.25'))

    def test_first_thin_outcome_cannot_be_replaced(self):
        first=self.confirm();self.purchase();self.f.now=first['at']+905
        self.g.observe_outcomes(self.f.a,self.f.book('.56','.57',depth=10))
        self.g.observe_outcomes(self.f.a,self.f.book('.56','.57',depth=1000))
        self.g.observe_outcomes(self.f.b,self.f.book('.60','.61',depth=1000))
        r=report(self.f.path,now=self.f.now)
        self.assertEqual(r['outcomes']['900']['insufficient_depth_or_tick'],1)
        self.assertIsNone(r['outcomes']['900']['mean_incremental_coins'])
        self.assertEqual(r['outcomes']['900']['allocation_attribution']['all']['measured'],0)

    def test_no_outcomes_remain_missing_not_zero(self):
        first=self.confirm();r=report(self.f.path,now=first['at']+5000)
        self.assertEqual(r['outcomes']['900']['missing'],1)
        self.assertIsNone(r['outcomes']['900']['mean_incremental_coins'])
        self.assertEqual(r['outcomes']['900']['allocation_attribution']['all']['measured'],0)
        self.assertEqual(r['purchase_statuses']['cash_only_expired'],1)

    def test_report_readonly_and_existing_external_results_unchanged(self):
        self.confirm();self.purchase()
        before=hashlib.sha256(self.f.path.read_bytes()).hexdigest()
        r=combined_report(self.f.path,now=self.f.now)
        self.assertEqual(r['proposals'],0)
        self.assertEqual(r['independent_model']['trials'],1)
        self.assertIn('Independent-model rotation',format_report(r))
        self.assertEqual(hashlib.sha256(self.f.path.read_bytes()).hexdigest(),before)
        missing=Path(self.f.temp.name)/'missing.sqlite3'
        self.assertFalse(report(missing)['initialized']);self.assertFalse(missing.exists())

    def test_ranking_contains_all_fresh_contracts_and_buy_return_on_cost(self):
        self.pair();r=report(self.f.path)['latest_ranking']
        self.assertEqual(r['fresh_contracts'],2)
        self.assertEqual(len(r['owned']),1)
        self.assertEqual(len(r['buys']),2)
        self.assertEqual(r['buys'][0]['side'],'yes')
        self.assertGreater(D(r['buys'][0]['edge_per_purchase_coin']),0)
        self.assertEqual(r['switches'][0]['source']['exchange'],'2')

    def test_better_source_hold_value_prevents_switch_despite_profitable_position(self):
        self.forecasts['2']=('.94','.95','.99')
        self.forecasts['3']=('.55','.60','.65')
        self.confirm();self.assertFalse(self.trials())

    def test_first_fresh_purchase_quote_news_block_is_final(self):
        self.confirm();self.f.now+=1
        self.observe(self.f.b,'.39','.40',blocked=lambda m:True)
        d=self.trials()[0]
        self.assertEqual(d['status'],'cash_only')
        self.assertEqual(d['purchase_skip_reason'],'race_gate')
        self.f.now+=1;self.observe(self.f.b,'.39','.40')
        self.assertIsNone(self.trials()[0]['purchase'])

    def test_binding_change_prevents_purchase(self):
        self.confirm();self.f.b['contract_fingerprint']='changed';d=self.purchase()
        self.assertEqual(d['status'],'cash_only')
        self.assertEqual(d['purchase_skip_reason'],'binding_changed')

    def test_unrelated_new_quote_keeps_stronger_old_pair_in_global_ranking(self):
        self.pair()
        c=dict(self.f.b,sig_exchange_id='4',sig_market_id='4',name='third',race_key='2026:senate:MI')
        self.f.c['markets'].append(c);self.forecasts['4']=('.45','.50','.55')
        self.observe(c,'.39','.40')
        ranking=report(self.f.path)['latest_ranking']
        self.assertEqual(ranking['fresh_contracts'],3)
        self.assertEqual(ranking['switches'][0]['target']['exchange'],'3')

    def test_live_buy_pause_cancels_pending_purchase_but_never_releases_sale_cash(self):
        self.confirm();self.f.now+=1;self.observe(self.f.b,'.39','.40',buy_allowed=False)
        d=self.trials()[0]
        self.assertEqual(d['status'],'cash_only')
        self.assertEqual(D(d['simulated_cash']),D(d['sale_proceeds']))

    def test_original_external_study_still_uses_reference_provider(self):
        self.f.g.observe(self.f.a,self.f.book('.55','.56'),[self.f.book('.56','.57')]*2,'a',True,25,1000)
        self.f.g.observe(self.f.b,self.f.book('.39','.40'),[self.f.book('.70','.71')]*2,'b',True,25,1000)
        r=combined_report(self.f.path)
        self.assertEqual(r['proposals'],1)
        self.assertEqual(r['independent_model']['trials'],0)

    def test_simulated_daily_reservations_and_profit_reinvestment_cap(self):
        d=self.confirm()
        self.assertEqual(self.g.simulated_daily_used(self.f.now),D(d['purchase_cost']))
        self.purchase();self.assertEqual(self.g.simulated_daily_used(self.f.now),D(self.trials()[0]['purchase']['cost']))
        fake=copy.deepcopy(d);fake['fifo_basis']='2';fake['target_available']='25'
        q=self.g.constrain_trial(fake,self.f.now)
        if q:self.assertLessEqual(D(q['purchase_cost']),D(2))

    def test_no_broker_calls_are_added_to_active_engine_hook(self):
        import test_active
        f=test_active.ActiveTests();f.setUp();self.addCleanup(f.doCleanups)
        with patch.object(f.engine.model_rotation_study,'enabled',True),patch.object(f.engine.model_rotation_study,'observe') as observer:
            f.engine.cycle()
        self.assertEqual(observer.call_count,1)
        self.assertEqual(len(f.sig.orders),1) # unchanged existing reference entry

    def test_known_cached_news_pause_invalidates_saved_confirmation(self):
        self.pair();self.f.now+=1
        self.observe(self.f.b,'.39','.40',blocked=lambda m:m['sig_exchange_id']=='2')
        self.assertFalse(self.f.j.db.execute('SELECT 1 FROM model_rotation_confirmations').fetchone())


    def diagnostics(self):
        return report(self.f.path)['opportunity_diagnostics']

    def test_diagnostics_separates_model_failure_from_book_failure(self):
        with patch.object(self.g,'forecast_provider',side_effect=ValueError('model_insufficient_independent_pollsters')):
            self.observe(self.f.a,'.55','.56')
        self.observe(self.f.b,'.39','.50')
        c=self.diagnostics()['counts']
        self.assertEqual(c['observed'],2)
        self.assertEqual(c['model_insufficient_independent_pollsters'],1)
        self.assertEqual(c['sig_spread'],1)
        self.assertNotIn('model_and_quote_ready',c)

    def test_diagnostics_first_gate_preserves_no_forecast_during_pause(self):
        with patch.object(self.g,'forecast_provider') as forecast:
            self.observe(self.f.a,'.55','.56',buy_allowed=False)
            forecast.assert_not_called()
        self.assertEqual(self.diagnostics()['counts'],{'buy_pause':1,'observed':1})

    def test_diagnostics_pending_is_distinct_from_buy_pause(self):
        with patch.object(self.f.j,'pending',return_value=[{'pending':True}]):
            self.observe(self.f.a,'.55','.56',buy_allowed=False)
        self.assertEqual(self.diagnostics()['counts'],{'pending_order':1,'observed':1})

    def test_diagnostics_private_exception_text_not_saved(self):
        with patch.object(self.g,'forecast_provider',side_effect=ValueError('private secret path')):
            self.observe(self.f.a,'.55','.56')
        self.assertEqual(self.diagnostics()['counts']['model_validation_failed'],1)
        self.assertNotIn('private',json.dumps(self.diagnostics()))

    def test_diagnostics_ready_and_missing_source_destination(self):
        self.observe(self.f.b,'.39','.40')
        self.f.now+=16
        self.observe(self.f.a,'.55','.56')
        c=self.diagnostics()['counts']
        self.assertEqual(c['model_and_quote_ready'],2)
        self.assertEqual(c['no_fresh_owned_source'],1)
        self.assertEqual(c['no_fresh_unowned_destination'],1)
        self.assertEqual(len(self.diagnostics()['by_race']),2)

    def test_diagnostics_window_future_exclusion_and_retention(self):
        self.observe(self.f.a,'.55','.56')
        self.f.now+=3601;self.observe(self.f.b,'.39','.40')
        r=report(self.f.path,hours=1)['opportunity_diagnostics']
        self.assertEqual(r['observations'],1)
        self.assertEqual(r['by_race'][0]['exchange'],'3')
        self.g.diagnose(self.f.a,'observed',self.f.now+100)
        self.assertEqual(self.diagnostics()['observations'],2)
        self.f.now+=8*86400;self.observe(self.f.a,'.55','.56')
        self.assertEqual(self.diagnostics()['observations'],1)
        self.assertEqual(self.f.j.db.execute("SELECT COUNT(*) FROM model_rotation_diagnostics WHERE reason='observed'").fetchone()[0],1)

    def test_diagnostics_readonly_legacy_report_does_not_create_table(self):
        with self.f.j.db:self.f.j.db.execute('DROP TABLE model_rotation_diagnostics')
        self.assertFalse(self.diagnostics()['initialized'])
        self.assertFalse(self.f.j.db.execute("SELECT 1 FROM sqlite_master WHERE name='model_rotation_diagnostics'").fetchone())

    def test_diagnostics_trial_creation_and_restart(self):
        self.confirm()
        self.assertEqual(self.diagnostics()['counts']['trial_created'],1)
        before=self.diagnostics()
        self.g=ModelRotationStudy(self.f.l,self.f.c,self.f.temp.name,self.forecast)
        self.assertEqual(self.diagnostics(),before)

    def test_direct_buy_uses_unchanged_inventory_and_same_purchase(self):
        self.confirm();before=self.f.l.inventory();d=self.purchase()
        b=d['direct_buy_baseline']
        self.assertTrue(b['eligible']);self.assertEqual(b['quantity'],d['purchase']['quantity'])
        self.assertEqual(b['cost'],d['purchase']['cost']);self.assertEqual(b['initial_cash'],'1000')
        self.assertEqual(self.f.l.inventory(),before)

    def test_direct_buy_cannot_borrow_rotation_sale_proceeds(self):
        self.confirm();self.f.now+=1;self.observe(self.f.b,'.39','.40',cash=0)
        d=self.trials()[0]
        self.assertEqual(d['status'],'purchased')
        self.assertFalse(d['direct_buy_baseline']['eligible'])
        self.assertIn('spare_cash',d['direct_buy_baseline']['reason'])

    def test_direct_buy_does_not_release_source_capital(self):
        self.confirm();self.f.c['limits']['total']='65';d=self.purchase()
        self.assertEqual(d['status'],'purchased')
        self.assertIn('total_capital',d['direct_buy_baseline']['reason'])
        self.assertEqual(D(d['direct_buy_baseline']['budget_checks']['total_capital']),D(4))

    def test_direct_buy_does_not_release_source_exposure(self):
        self.confirm();self.f.c['limits'].update(net_shares_total=110,net_shares_per_office=110)
        d=self.purchase();self.assertEqual(d['status'],'purchased')
        self.assertIn('directional_exposure',d['direct_buy_baseline']['reason'])

    def test_direct_buy_initial_cash_cannot_be_replaced_by_later_deposit(self):
        d=self.confirm();d['direct_buy_baseline']['initial_cash']='0'
        with self.f.j.db:self.f.j.db.execute('UPDATE model_rotation_trials SET detail=?',(json.dumps(d),))
        d=self.purchase();self.assertIn('spare_cash',d['direct_buy_baseline']['reason'])

    def test_direct_buy_validation_failure_preserves_rotation_result(self):
        self.confirm()
        with patch.object(self.g,'direct_buy_baseline',side_effect=ValueError('bad input')):d=self.purchase()
        self.assertEqual(d['status'],'purchased')
        self.assertFalse(d['direct_buy_baseline']['eligible'])
        self.assertEqual(d['direct_buy_baseline']['reason'],'validation_unavailable')

    def test_direct_buy_baseline_survives_restart(self):
        self.confirm();self.g=ModelRotationStudy(self.f.l,self.f.c,self.f.temp.name,self.forecast)
        d=self.purchase();self.assertTrue(d['direct_buy_baseline']['eligible'])

    def test_direct_buy_legacy_trial_not_backfilled(self):
        d=self.confirm();d.pop('direct_buy_baseline')
        with self.f.j.db:self.f.j.db.execute('UPDATE model_rotation_trials SET detail=?',(json.dumps(d),))
        d=self.purchase();self.assertNotIn('direct_buy_baseline',d)
        self.assertEqual(report(self.f.path)['direct_buy_eligibility'],{'not_collected_or_pending':1})

    def test_direct_buy_cash_only_and_missing_outcomes_not_scored(self):
        self.confirm();self.f.now+=1;self.observe(self.f.b,'.68','.69')
        self.assertEqual(self.trials()[0]['direct_buy_baseline']['reason'],'no_rotation_purchase')
        r=report(self.f.path)
        self.assertEqual(r['outcomes']['900']['direct_buy_attribution']['measured'],0)

    def test_direct_buy_report_requires_both_aligned_complete_legs(self):
        d=self.confirm();self.purchase();self.f.now=d['at']+900
        self.g.observe_outcomes(self.f.a,self.f.book('.60','.61'))
        self.assertEqual(report(self.f.path)['outcomes']['900']['direct_buy_attribution']['measured'],0)
        self.g.observe_outcomes(self.f.b,self.f.book('.45','.46'))
        r=report(self.f.path)['outcomes']['900']['direct_buy_attribution']
        self.assertEqual(r['measured'],1)
        self.assertEqual(D(r['mean_direct_buy_vs_rotation_coins']),D('1.25'))
        self.assertEqual(D(r['mean_direct_buy_vs_hold_and_cash_coins']),D('.96'))

class AllocationAttributionTests(unittest.TestCase):
    def test_profitable_exit_can_hide_bad_replacement(self):
        result=allocation_attribution([{'hold':'8','cash':'10','rotate':'9','purchased':True,'fundable':True}])
        all_rows=result['all']
        self.assertEqual(all_rows['mean_exit_vs_hold_coins'],'2')
        self.assertEqual(all_rows['mean_reinvestment_vs_cash_coins'],'-1')
        self.assertEqual(all_rows['mean_rotation_vs_hold_coins'],'1')
        self.assertEqual(all_rows['best_terminal_value_counts'],{'cash':1})
        self.assertEqual(result['purchased']['measured'],1)
        self.assertEqual(result['not_cash_budget_fundable']['measured'],0)

    def test_cash_only_tie_is_not_a_replacement_win(self):
        r=allocation_attribution([{'hold':'8','cash':'10','rotate':'10','purchased':False,'fundable':False}])
        self.assertEqual(r['cash_only']['mean_reinvestment_vs_cash_coins'],'0')
        self.assertEqual(r['all']['best_terminal_value_counts'],{'cash+rotation_policy':1})
        self.assertIsNone(r['purchased']['mean_reinvestment_vs_cash_coins'])

    def test_empty_and_overlapping_cohorts(self):
        self.assertIsNone(allocation_attribution([])['all']['mean_exit_vs_hold_coins'])
        r=allocation_attribution([
            {'hold':'10','cash':'8','rotate':'12','purchased':True,'fundable':False},
            {'hold':'12','cash':'8','rotate':'8','purchased':False,'fundable':True}])
        self.assertEqual(r['all']['measured'],2)
        self.assertEqual(r['all']['mean_rotation_vs_hold_coins'],'-1')
        self.assertEqual(r['all']['best_terminal_value_counts'],{'rotation_policy':1,'hold':1})


class DirectBuyArithmeticTests(unittest.TestCase):
    def test_equal_starting_wealth_and_no_double_counted_purchase(self):
        sample={'hold':'12','cash':'10','rotate':'11','purchased':True,
                'direct_buy_baseline':{'eligible':True,'initial_cash':'20','cost':'8'}}
        r=direct_buy_attribution([sample])
        self.assertEqual(r['mean_direct_buy_vs_hold_and_cash_coins'],'1')
        self.assertEqual(r['mean_direct_buy_vs_rotation_coins'],'2')
        self.assertEqual(r['best_terminal_value_counts'],{'hold_and_direct_buy':1})
        sample['direct_buy_baseline']['eligible']=False
        self.assertIsNone(direct_buy_attribution([sample])['mean_direct_buy_vs_rotation_coins'])

    def test_bad_target_does_not_get_called_profitable_due_to_good_source(self):
        sample={'hold':'15','cash':'10','rotate':'9','purchased':True,
                'direct_buy_baseline':{'eligible':True,'initial_cash':'20','cost':'8'}}
        r=direct_buy_attribution([sample])
        self.assertEqual(r['mean_direct_buy_vs_hold_and_cash_coins'],'-1')
        self.assertEqual(r['mean_direct_buy_vs_rotation_coins'],'5')
        self.assertEqual(r['best_terminal_value_counts'],{'hold_and_cash':1})
