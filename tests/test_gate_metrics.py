import json
import unittest
from unittest.mock import patch

from election_bot.gate_metrics import GateMetrics
from election_bot.active_engine import ReservationRejected
from election_bot.strategy import D
import test_active


class GateMetricsTests(unittest.TestCase):
    def test_dedup_is_per_contract_gate_and_phase(self):
        m=GateMetrics()
        for _ in range(3):m.blocked('drawdown','scan','2')
        m.blocked('drawdown','preflight','2');m.blocked('drawdown','scan','3')
        m.blocked('news','scan','2')
        self.assertEqual(m.summary()['gate_counts'],{'drawdown':3,'news':1})
        self.assertEqual(m.summary()['gate_counts_by_phase']['scan']['drawdown'],2)


class CycleGateTests(unittest.TestCase):
    def setUp(self):
        self.f=test_active.ActiveTests();self.f.setUp();self.addCleanup(self.f.doCleanups)

    def last(self):
        row=self.f.journal.db.execute("SELECT detail FROM events WHERE kind='scan_complete' ORDER BY rowid DESC LIMIT 1").fetchone()
        return json.loads(row[0])

    def test_zero_edge_is_visible_and_cycle_counts_reset(self):
        self.f.sig.prices['2']=('.69','.70')
        self.f.engine.cycle();r=self.last()
        self.assertEqual(r['gate_counts']['reference_price_gap'],1)
        self.assertEqual(r['gate_counts']['no_eligible_signal'],1)
        self.assertEqual(r['orders'],0)
        self.f.engine.cycle()
        self.assertEqual(self.last()['gate_counts']['reference_price_gap'],1)

    def test_drawdown_and_guard_are_distinct_overlapping_blocks(self):
        with patch.object(self.f.engine.drawdown,'status',return_value={'buy_allowed':False,'reason':'drawdown_paused'}), patch.object(self.f.engine.position_guard,'check',return_value={'buy_allowed':False,'reason':'price_decline'}):
            self.f.engine.cycle()
        r=self.last()
        self.assertEqual(r['gate_counts']['portfolio_drawdown'],1)
        self.assertEqual(r['gate_counts']['position_entry_guard'],1)
        self.assertFalse(self.f.sig.orders)

    def test_news_cooldown_and_open_orders_early_returns_have_summary(self):
        from unittest.mock import Mock
        self.f.engine.news=Mock();self.f.engine.news.drain.return_value={};self.f.engine.news.block_reason.return_value='news_pause'
        self.f.engine.cycle()
        self.assertEqual(self.last()['gate_counts']['news_pause'],1)
        self.f.engine.news=None
        with patch.object(self.f.engine.races,'last_order',return_value=__import__('time').time()):
            self.f.engine.cycle()
        self.assertEqual(self.last()['gate_counts']['race_cooldown'],1)
        with patch.object(self.f.sig,'open_orders',return_value=[{'id':'existing'}]):
            self.f.engine.cycle()
        self.assertEqual(self.last()['status'],'waiting')
        self.assertEqual(self.last()['gate_counts'],{'open_orders':1})

    def test_halt_summary_preserves_original_exception(self):
        error=RuntimeError('unresolved')
        with patch.object(self.f.engine,'reconcile',side_effect=error):
            with self.assertRaises(RuntimeError) as caught:self.f.engine.cycle()
        self.assertIs(caught.exception,error)
        self.assertEqual(self.last()['status'],'halted')
        self.assertEqual(self.last()['gate_counts']['order_recovery'],1)
        with patch.object(self.f.engine,'reconcile',side_effect=error),patch.object(self.f.engine,'report',side_effect=ValueError('write failed')):
            with self.assertRaises(RuntimeError) as caught:self.f.engine.cycle()
        self.assertIs(caught.exception,error)

    def test_successful_order_keeps_existing_summary_fields(self):
        self.f.engine.cycle();r=self.last()
        self.assertEqual(r['orders'],1)
        self.assertEqual(r['status'],'complete')
        self.assertIn('risk_committed',r)
        self.assertEqual(len(self.f.sig.orders),1)

    def test_reservation_gate_is_typed_and_not_message_matched(self):
        with patch.object(self.f.engine,'available',return_value=D(0)):
            payload={'exchangeId':'2','action':'buy','side':'yes','quantity':1,'price':.6,'idempotencyKey':'test'}
            with self.assertRaises(ReservationRejected) as caught:
                self.f.engine.reserve_order(payload,D('.61'),self.f.sig.account(),self.f.sig.positions())
        self.assertEqual(caught.exception.gate,'coin_limits')
        self.assertEqual(self.f.engine.gate_metrics.summary()['gate_counts_by_phase']['reservation'],{'coin_limits':1})
        self.assertFalse(self.f.journal.pending())

    def test_preflight_failure_separate_from_scan(self):
        original=self.f.engine.decide
        def changed(*args,**kwargs):
            if kwargs.get('phase')=='preflight':return None
            return original(*args,**kwargs)
        with patch.object(self.f.engine,'decide',side_effect=changed):self.f.engine.cycle()
        self.assertEqual(self.last()['gate_counts_by_phase']['preflight'],{'preflight_recheck':1})
        self.assertFalse(self.f.sig.orders)


class DrawdownRecoveryVisibilityTests(unittest.TestCase):
    def test_recovery_target_preserves_strict_resume_rule(self):
        from test_drawdown import DrawdownTests
        f=DrawdownTests();f.setUp();self.addCleanup(f.doCleanups)
        f.fill();f.mark();f.g.status()
        f.mark('.50','.51');s=f.g.status()
        self.assertTrue(s['paused'])
        self.assertEqual(s['resume_requires_portfolio_pnl_above'],D('3'))
        self.assertEqual(s['recovery_gap_coins'],D('5'))
        self.assertEqual(s['peak_policy'],'persistent_high_water_mark')
        self.assertFalse(s['automatic_timeout'])
        f.mark('.55','.56');s=f.g.status()
        self.assertEqual(s['recovery_gap_coins'],D('0'))
        self.assertTrue(s['paused']) # equality does not resume
