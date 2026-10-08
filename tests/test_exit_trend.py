import copy
import json
import sqlite3
import time
import unittest
from unittest.mock import Mock, patch

import test_active as active
from test_exit_study import SETTINGS, EXECUTION
from election_bot.active_engine import ActiveEngine, ReservationRejected
from election_bot.engine import validate_config
from election_bot.exit_study import checked_exit, experiment
from election_bot.exit_trend import ExitTrend, assess, settings
from election_bot.strategy import Book, D

MODE = dict(EXECUTION, profit_target_enabled=True, profit_exit_mode='trend', reentry_cooldown_seconds=1800)


def sample(at, bid, refs=('.73','.74'), depth=200, ref_depth=200):
    return {'at': at, 'bid': str(bid), 'depth': str(depth),
            'reference_bids': list(refs), 'reference_depths': [str(ref_depth)]*2}


def context(rows, current=None, **options):
    return {'samples':rows, 'current':current or rows[-1], 'settings':settings(dict(MODE,exit_trend=options)),
            'epoch':1,'min_reference_depth':'20'}


def quote(bid='.65', refs=('.73','.74'), depth=200):
    return Book.make([(bid,depth)],[(D(bid)+D('.005'),200)]), [
        Book.make([(p,200)],[(D(p)+D('.02'),200)]) for p in refs]


class TrendRuleTests(unittest.TestCase):
    def test_rising_bid_with_remaining_upside_holds(self):
        result=assess(context([sample(0,'.63'),sample(60,'.64'),sample(120,'.65')]),25)
        self.assertFalse(result['allowed']);self.assertEqual(result['reason'],'hold_supported_rise')

    def test_flat_wide_gap_also_holds(self):
        result=assess(context([sample(t,'.65') for t in (0,60,120)]),25)
        self.assertEqual(result['reason'],'hold_no_confirmed_exit')

    def test_two_spaced_pullbacks_confirm_trailing_exit(self):
        result=assess(context([sample(0,'.69'),sample(60,'.67'),sample(120,'.665')]),25)
        self.assertTrue(result['allowed']);self.assertEqual(result['reason'],'trailing_pullback')

    def test_single_dip_does_not_confirm(self):
        result=assess(context([sample(0,'.69'),sample(60,'.69'),sample(120,'.66')]),25)
        self.assertFalse(result['allowed'])

    def test_current_rebound_invalidates_historical_trigger(self):
        rows=[sample(0,'.69'),sample(60,'.67'),sample(120,'.66')]
        result=assess(context(rows,current=sample(125,'.685')),25)
        self.assertFalse(result['allowed'])

    def test_reference_weakening_requires_both_venues_twice(self):
        rows=[sample(0,'.65',('.80','.81')),sample(60,'.65',('.77','.78')),sample(120,'.65',('.76','.77'))]
        self.assertEqual(assess(context(rows),25)['reason'],'references_weakened')
        rows[1]=sample(60,'.65',('.77','.81'))
        self.assertFalse(assess(context(rows),25)['allowed'])

    def test_narrowing_requires_both_gaps_small_twice(self):
        rows=[sample(0,'.70'),sample(60,'.72'),sample(120,'.725')]
        self.assertEqual(assess(context(rows),25)['reason'],'gap_narrowed')
        rows[1]=sample(60,'.71')
        self.assertFalse(assess(context(rows),25)['allowed'])

    def test_history_span_count_and_outage_guard(self):
        for rows in ([sample(0,'.69'),sample(60,'.66')],
                     [sample(0,'.69'),sample(30,'.66'),sample(60,'.66')],
                     [sample(0,'.69'),sample(601,'.66'),sample(661,'.66')]):
            with self.subTest(rows=rows):
                self.assertEqual(assess(context(rows),25)['reason'],'trend_history_insufficient')
        rows=[sample(0,'.69'),sample(60,'.66'),sample(120,'.66')]
        self.assertEqual(assess(context(rows,current=sample(721,'.66')),25)['reason'],'trend_history_insufficient')

    def test_thin_peak_cannot_support_larger_trailing_sale(self):
        rows=[sample(0,'.69',depth=1),sample(60,'.66'),sample(120,'.66')]
        self.assertFalse(assess(context(rows),25)['allowed'])
        self.assertTrue(assess(context(rows),1)['allowed'])

    def test_bad_reference_depth_breaks_chain_and_current_depth_blocks(self):
        rows=[sample(0,'.69'),sample(60,'.66',ref_depth=1),sample(120,'.66')]
        self.assertEqual(assess(context(rows),25)['reason'],'trend_history_insufficient')
        self.assertEqual(assess(context(rows,current=sample(121,'.66',depth=2)),25)['reason'],'trend_depth_insufficient')

    def test_future_history_is_not_evidence(self):
        self.assertEqual(assess(context([sample(125,'.69')],current=sample(120,'.66')),25)['reason'],'trend_clock_reversal')


class TrendHistoryTests(unittest.TestCase):
    def setUp(self):
        self.db=sqlite3.connect(':memory:');self.addCleanup(self.db.close)
        self.db.execute('CREATE TABLE executions(exchange TEXT,quantity TEXT)')
        self.history=ExitTrend(self.db)
        self.mapping={'sig_exchange_id':'2','contract_fingerprint':'a'*64}

    def ctx(self,t,phase='scan',held=100,execution=None,mapping=None):
        b,r=quote()
        return self.history.context(mapping or self.mapping,b,r,held,SETTINGS,execution or MODE,phase,now=t)

    def test_only_spaced_scans_accumulate_not_preflight_or_repeats(self):
        self.assertEqual(len(self.ctx(1000)['samples']),1)
        self.assertEqual(len(self.ctx(1001)['samples']),1)
        self.assertEqual(len(self.ctx(1060,'preflight')['samples']),1)
        self.assertEqual(len(self.ctx(1060)['samples']),2)
        self.assertEqual(len(self.ctx(1120)['samples']),3)

    def test_restart_preserves_matching_epoch_and_policy(self):
        self.ctx(1000);self.ctx(1060);self.history=ExitTrend(self.db)
        self.assertEqual(len(self.ctx(1120)['samples']),3)

    def test_fills_and_side_changes_require_new_history_zero_fills_do_not(self):
        self.ctx(1000);self.ctx(1060)
        self.db.execute("INSERT INTO executions VALUES ('2','0')");self.db.commit()
        self.assertEqual(len(self.ctx(1120)['samples']),3)
        self.db.execute("INSERT INTO executions VALUES ('2','1')");self.db.commit()
        self.assertEqual(len(self.ctx(1180)['samples']),1)
        self.assertEqual(len(self.ctx(1240,held=-100)['samples']),1)

    def test_contract_or_policy_change_discards_incompatible_evidence(self):
        self.ctx(1000);self.ctx(1060)
        self.assertEqual(len(self.ctx(1120,mapping=dict(self.mapping,contract_fingerprint='b'*64))['samples']),1)
        self.assertEqual(len(self.ctx(1180,execution=dict(MODE,exit_trend={'trailing_drop':'.03'}))['samples']),1)

    def test_old_samples_pruned(self):
        self.ctx(1000);self.ctx(1060)
        self.assertEqual(len(self.ctx(3000)['samples']),1)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM exit_trend_samples').fetchone()[0],1)

    def test_no_side_uses_executable_complement_prices(self):
        c=self.ctx(1000,held=-100)
        self.assertEqual(D(c['current']['bid']),D('.345'))
        self.assertEqual([D(v) for v in c['current']['reference_bids']],[D('.25'),D('.24')])


class TrendIntegrationTests(unittest.TestCase):
    def evaluate(self,rows,price='.66',basis='.60',held=100,cost=60,cap=None,refs=('.73','.74')):
        b,r=quote(price,refs)
        detail={}
        signal=checked_exit(b,r,SETTINGS,MODE,D(held),D(cost),50,
            lambda side,q:D(basis)*q,detail,cap,trend_check=lambda q:assess(context(rows),q))
        return signal,detail

    def test_partial_exit_requires_trend_and_profits(self):
        rows=[sample(0,'.69'),sample(60,'.66'),sample(120,'.66')]
        signal,detail=self.evaluate(rows)
        self.assertEqual((signal.reason,signal.quantity),('trend_profit_target',25))
        self.assertEqual(detail['trend_trigger'],'trailing_pullback')
        self.assertIsNone(self.evaluate(rows,basis='.65')[0])
        self.assertIsNone(self.evaluate(rows,cost=65)[0])
        self.assertEqual(self.evaluate(rows,cap=3)[0].quantity,3)

    def test_no_side_trend_sells_native_no_and_respects_partial_cap(self):
        b,r=quote('.66');detail={}
        rows=[sample(0,'.69'),sample(60,'.66'),sample(120,'.66')]
        signal=checked_exit(b.complement(),[q.complement() for q in r],SETTINGS,MODE,-100,60,50,
            lambda side,q:D('.60')*q,detail,trend_check=lambda q:assess(context(rows),q))
        self.assertEqual((signal.side,signal.action,signal.reason,signal.quantity),
                         ('no','sell','trend_profit_target',25))

    def test_convergence_also_requires_trend_confirmation_in_trend_mode(self):
        b,r=quote('.745');detail={}
        signal=checked_exit(b,r,SETTINGS,MODE,100,60,50,lambda side,q:D('.60')*q,detail)
        self.assertIsNone(signal);self.assertEqual(detail['reason'],'trend_history_unavailable')

    def test_overpricing_remains_independent_risk_exit(self):
        b,r=quote('.80');detail={}
        signal=checked_exit(b,r,SETTINGS,MODE,100,90,50,lambda side,q:D('.90')*q,detail)
        self.assertEqual(signal.reason,'overpriced_exit')

    def test_depth_study_keeps_original_policies(self):
        b,r=quote('.745');detail={}
        signal=checked_exit(b,r,SETTINGS,MODE,100,60,50,lambda side,q:D('.60')*q,detail)
        study=experiment(b,r,SETTINGS,MODE,100,60,50,lambda side,q:D('.60')*q,signal,detail)
        self.assertTrue(all(d['candidate']['reason']=='convergence_take_profit' for d in study['decisions']))

    def test_fraction_floor_and_dust_close(self):
        for held,cost,expected in [(3,'1.8',1),(5,'3',1),(9,'5.4',2)]:
            rows=[sample(0,'.69'),sample(60,'.66'),sample(120,'.66')]
            self.assertEqual(self.evaluate(rows,held=held,cost=cost)[0].quantity,expected)


class TrendEngineTests(unittest.TestCase):
    def setUp(self):
        self.f=active.ActiveTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.f.config['execution'].update(MODE)
        self.f.engine=ActiveEngine(self.f.config,self.f.sig,self.f.refs,self.f.journal,self.f.temp.name,live=True)
        self.e=self.f.engine;self.m=self.f.config['markets'][0]

    def prepare(self):
        f=self.f;self.e.cycle();f.age_orders()
        self.assertEqual(len(f.sig.orders),1)
        self.now=time.time()
        for seconds,bid in [(-180,'.69'),(-120,'.66'),(-60,'.66')]:
            b,r=quote(bid)
            self.e.exit_trend.context(self.m,b,r,40,f.config['strategy'],f.config['execution'],'scan',now=self.now+seconds)

    def test_live_partial_sale_then_new_epoch_prevents_immediate_second_sale(self):
        self.prepare();f=self.f;f.sig.prices['2']=('.66','.665');self.e.cycle()
        self.assertEqual((f.sig.orders[2]['action'],f.sig.orders[2]['quantity']),('sell',10))
        f.age_orders();self.e.cycle()
        self.assertEqual(len(f.sig.orders),2)
        decisions=[json.loads(r[0]) for r in f.journal.db.execute("SELECT detail FROM events WHERE kind='decision'")]
        self.assertEqual(decisions[-1]['exit_check']['reason'],'trend_history_insufficient')
        self.assertEqual(self.e.ledger.held('2')[0],30)

    def test_preflight_rebound_cancels_proposed_trailing_sale(self):
        self.prepare();f=self.f
        f.sig.book=Mock(side_effect=[Book.make([('.66',200)],[('.69',200)]),
                                    Book.make([('.685',200)],[('.69',200)])])
        self.e.cycle()
        self.assertEqual(len(f.sig.orders),1);self.assertFalse(f.journal.pending())

    def test_atomic_guard_rejects_history_from_previous_inventory(self):
        self.prepare();f=self.f
        p={'idempotencyKey':'trend','exchangeId':'2','action':'sell','side':'yes','quantity':10,'price':'.66'}
        with self.assertRaisesRegex(ReservationRejected,'Position changed'):
            self.e.reserve_order(p,D('6.7'),f.sig.account(),f.sig.positions(),exit_reason='trend_profit_target',exit_epoch=-1)
        self.assertFalse(f.journal.pending())

    def test_atomic_guard_still_rechecks_FIFO_for_trend_sale(self):
        self.prepare();f=self.f
        p={'idempotencyKey':'trend','exchangeId':'2','action':'sell','side':'yes','quantity':10,'price':'.66'}
        with patch.object(self.e.ledger,'sale_basis',return_value=D('6.5')):
            with self.assertRaisesRegex(ReservationRejected,'FIFO profit'):
                self.e.reserve_order(p,D('6.7'),f.sig.account(),f.sig.positions(),
                    exit_reason='trend_profit_target',exit_epoch=self.e.exit_trend.epoch('2'))
        self.assertFalse(f.journal.pending())

    def test_stale_reference_does_not_add_samples_or_sell(self):
        self.prepare();f=self.f;f.sig.prices['2']=('.66','.665')
        before=f.journal.db.execute('SELECT COUNT(*) FROM exit_trend_samples').fetchone()[0]
        original=f.refs.books
        def stale(*args):
            r=original(*args);r[0].source_at-=100;return r
        f.refs.books=stale;self.e.cycle()
        self.assertEqual(len(f.sig.orders),1)
        self.assertEqual(f.journal.db.execute('SELECT COUNT(*) FROM exit_trend_samples').fetchone()[0],before)

    def test_invalid_config_rejected(self):
        for patch_value in [{'minimum_samples':2},{'confirmation_samples':1},{'confirmation_samples':3},
                            {'sample_spacing_seconds':True},{'max_sample_gap_seconds':30},
                            {'partial_fraction':'0'},{'partial_fraction':'.6'},{'remaining_gap':'NaN'},
                            {'trailing_drop':'0'},{'unknown':1},{'minimum_span_seconds':3000}]:
            c=copy.deepcopy(self.f.config);c['execution']['exit_trend']=patch_value
            with self.subTest(patch_value=patch_value),self.assertRaises(ValueError):validate_config(c)
        for extra in [{'profit_exit_mode':'typo'},{'profit_target_enabled':False}]:
            c=copy.deepcopy(self.f.config);c['execution'].update(extra)
            with self.subTest(extra=extra),self.assertRaises(ValueError):validate_config(c)
