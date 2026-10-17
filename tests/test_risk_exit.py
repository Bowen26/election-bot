import copy
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from election_bot.active_engine import ActiveEngine, ReservationRejected
from election_bot.clients import contract_record, fingerprint
from election_bot.demo import fixture
from election_bot.ledger import Ledger
from election_bot.risk_exit import RiskExit, DEFAULTS, REASON, settings
from election_bot.state import Journal
from election_bot.strategy import Book, D
import test_active


class RiskExitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.j = Journal(Path(self.temp.name)/'j.db', 'test'); self.addCleanup(self.j.close)
        self.l = Ledger(self.j)
        self.c, self.sig, self.refs = fixture()
        self.c['execution'] = dict(test_active.EXECUTION)
        self.c['risk_exit'] = dict(DEFAULTS, enabled=True)
        self.m = self.c['markets'][0]; self.n = 0
        self.g = RiskExit(self.l, self.c)
        self.g.bind(self.m, contract_record(self.sig.market('1'), self.m, self.refs.metadata(self.m)))
        self.base = time.time()-500
        self.fill()

    def fill(self, quantity=40, action='buy', side='yes', price='.60', tagged=False, at=None):
        self.n += 1
        p = dict(idempotencyKey=str(self.n), exchangeId='2', quantity=quantity, action=action, side=side, price=price)
        self.j.reserve(p, D(quantity)*(D(price)+D('.01')),
                       guard=(lambda: self.g.attach(p, {'test': True})) if tagged else None)
        self.l.record(p, quantity, price, '.01', self.base-1 if at is None else at)
        return p

    def sample(self, t, bid='.55', depth=200, **kwargs):
        book = Book.make([(bid, depth)], [(str(D(bid)+D('.01')), 200)])
        return self.g.propose(self.m, book, *self.l.held('2'), now=self.base+t, **kwargs)

    def confirm(self):
        self.sample(0); self.sample(60)
        return self.sample(120)

    def test_three_spaced_losses_trigger_partial_sale_and_buffered_audit(self):
        self.assertIsNone(self.sample(0)[0]); self.assertIsNone(self.sample(60)[0])
        s, d = self.sample(120)
        self.assertEqual((s.action, s.reason, s.side, s.quantity), ('sell', REASON, 'yes', 10))
        self.assertEqual(d['loss_per_share'], D('.07'))
        self.assertEqual(d['estimated_realized_pnl'], D('-.70'))

    def test_boundary_includes_both_buffers(self):
        self.sample(0, '.57'); self.sample(60, '.57')
        self.assertIsNotNone(self.sample(120, '.57')[0])
        self.assertIsNone(self.sample(180, '.575')[0])

    def test_bounce_breaks_loss_chain(self):
        self.sample(0); self.sample(60, '.65'); self.sample(120)
        self.assertIsNone(self.sample(180)[0])
        self.assertIsNotNone(self.sample(240)[0])

    def test_unscheduled_rebound_invalidates_history(self):
        self.confirm(); self.sample(121, '.65', phase='preflight')
        self.assertIsNone(self.sample(122)[0]); self.sample(182)
        self.assertIsNotNone(self.sample(242)[0])

    def test_stale_book_never_adds_confirmation(self):
        book = Book.make([('.55', 200)], [('.60', 200)], source_at=time.time()-100)
        with self.assertRaises(ValueError): self.g.propose(self.m, book, *self.l.held('2'))
        self.assertEqual(self.j.db.execute('SELECT COUNT(*) FROM risk_exit_samples').fetchone()[0], 0)

    def test_repeated_scans_and_preflight_cannot_supply_confirmations(self):
        self.sample(0)
        for t in (1, 20, 59): self.assertIsNone(self.sample(t)[0])
        self.sample(60)
        for t in (120, 180): self.assertIsNone(self.sample(t, phase='preflight', persist=False)[0])
        self.assertEqual(self.j.db.execute('SELECT COUNT(*) FROM risk_exit_samples').fetchone()[0], 2)

    def test_preflight_recovery_and_depth_recheck(self):
        self.confirm()
        self.assertIsNone(self.sample(121, '.65', phase='preflight', persist=False)[0])
        self.assertEqual(self.sample(121, depth=3, phase='preflight', persist=False)[0].quantity, 3)

    def test_gap_expiry_and_clock_reversal(self):
        self.confirm()
        self.assertEqual(self.sample(119)[1]['reason'], 'risk_exit_clock_reversal')
        self.assertIsNone(self.sample(721)[0])
        self.assertIsNone(self.sample(3000)[0])

    def test_restart_preserves_confirmations(self):
        self.confirm(); self.g = RiskExit(self.l, self.c)
        self.assertIsNotNone(self.sample(121, phase='preflight', persist=False)[0])

    def test_buy_or_sale_resets_confirmation_epoch(self):
        self.confirm(); self.fill(quantity=1)
        self.assertIsNone(self.sample(180)[0])
        self.sample(240); self.assertIsNotNone(self.sample(300)[0])
        self.fill(quantity=1, action='sell')
        self.assertIsNone(self.sample(360)[0])

    def test_policy_change_requires_new_observations(self):
        self.confirm(); self.c['risk_exit']['loss_per_share'] = '.06'
        self.g = RiskExit(self.l, self.c)
        self.assertIsNone(self.sample(180)[0])

    def test_no_side_uses_complement(self):
        self.fill(action='sell'); self.fill(side='no')
        for t in (0, 60, 120): s, d = self.sample(t, '.44')
        self.assertEqual((s.side, s.price, s.quantity), ('no', D('.55'), 10))

    def test_size_respects_each_cap(self):
        self.confirm()
        self.assertEqual(self.sample(121, quantity_cap=4, persist=False)[0].quantity, 4)
        self.assertEqual(self.sample(121, depth=3, persist=False)[0].quantity, 3)
        self.c['limits']['per_order'] = '2'
        self.assertEqual(self.sample(121, persist=False)[0].quantity, 3)
        self.c['strategy']['max_shares_per_order'] = 2
        self.assertEqual(self.sample(121, persist=False)[0].quantity, 2)

    def test_tiny_position_never_rounded_up_to_liquidation(self):
        self.fill(quantity=37, action='sell')
        self.assertEqual(self.confirm()[1]['reason'], 'risk_exit_size_below_one')

    def test_inventory_mismatch_rejected(self):
        with self.assertRaisesRegex(ValueError, 'inventory changed'):
            self.g.propose(self.m, self.sig.book('2'), 41, D('24.4'))

    def test_binding_rejects_rules_change_closed_or_unverified_contract(self):
        market = self.sig.market('1'); self.g.verify_market(self.m, market)
        market['title'] = 'Changed'
        with self.assertRaises(ValueError): self.g.verify_market(self.m, market)
        market = self.sig.market('1'); market['status'] = 'closed'
        with self.assertRaises(ValueError): self.g.verify_market(self.m, market)
        self.m['contract_fingerprint'] = '0'*64
        self.assertFalse(self.g.bound(self.m))
        with self.assertRaises(ValueError): self.g.verify_market(self.m, self.sig.market('1'))

    def test_confirmed_partial_fill_starts_cooldown_then_needs_new_samples(self):
        self.confirm(); self.fill(quantity=2, action='sell', tagged=True, at=self.base+120)
        self.assertEqual(self.sample(1919)[1]['reason'], 'risk_exit_cooldown')
        self.assertIsNone(self.sample(1920)[0]); self.sample(1980)
        self.assertEqual(self.sample(2040)[0].quantity, 9)

    def test_zero_fill_does_not_start_cooldown_or_reset_history(self):
        self.confirm(); self.fill(quantity=0, action='sell', tagged=True, at=self.base+120)
        self.assertIsNotNone(self.sample(121)[0])

    def test_average_trigger_retains_fifo_realized_audit(self):
        self.fill(action='sell'); self.fill(quantity=10, price='.20'); self.fill(quantity=30, price='.80')
        s, d = self.confirm()
        self.assertIsNotNone(s); self.assertGreater(d['estimated_realized_pnl'], 0)
        self.assertGreaterEqual(d['loss_per_share'], D('.05'))

    def test_attribution_rolls_back_with_failed_reservation(self):
        p = dict(idempotencyKey='reject', exchangeId='2')
        def guard():
            self.g.attach(p, {})
            raise ValueError('reject')
        with self.assertRaises(ValueError): self.j.reserve(p, 1, guard=guard)
        self.assertEqual(self.j.db.execute('SELECT COUNT(*) FROM risk_exit_orders').fetchone()[0], 0)

    def test_invalid_settings(self):
        for field, value in [('enabled', 1), ('partial_fraction', '.26'), ('loss_per_share', '0'),
                             ('sample_spacing_seconds', 0), ('cooldown_seconds', 1), ('extra', 1)]:
            c = copy.deepcopy(self.c); c['risk_exit'][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError): settings(c)
        self.c['execution']['sell_enabled'] = False
        with self.assertRaises(ValueError): settings(self.c)

    def test_disabled_does_not_create_tables(self):
        with tempfile.TemporaryDirectory() as root:
            j = Journal(Path(root)/'j.db', 'disabled')
            try:
                c = copy.deepcopy(self.c); c['risk_exit']['enabled'] = False
                RiskExit(Ledger(j), c)
                self.assertIsNone(j.db.execute("SELECT name FROM sqlite_master WHERE name='risk_exit_orders'").fetchone())
            finally: j.close()


class RiskExitEngineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.c, original, self.refs = fixture(); self.c['execution'] = dict(test_active.EXECUTION)
        self.c['risk_exit'] = dict(DEFAULTS, enabled=True)
        self.sig = test_active.Broker(original); self.m = self.c['markets'][0]
        self.m['sig_market_id'] = '2'
        self.m['contract_fingerprint'] = fingerprint(contract_record(self.sig.market('2'), self.m, self.refs.metadata(self.m)))
        self.j = Journal(Path(self.temp.name)/'j.db', 'test'); self.addCleanup(self.j.close)
        self.e = ActiveEngine(self.c, self.sig, self.refs, self.j, self.temp.name, live=True)
        self.e.cycle(); self.assertEqual(self.e.ledger.held('2')[0], 40)
        self.j.db.execute('UPDATE orders SET created=?', (time.time()-10000,)); self.j.db.commit()
        for t in (time.time()-180, time.time()-120, time.time()-60):
            self.e.risk_exit.propose(self.m, self.sig.book('2'), *self.e.ledger.held('2'), now=t)

    def test_independent_exit_during_external_feed_outage(self):
        self.refs.metadata = Mock(side_effect=AssertionError('external metadata must not be used'))
        self.refs.books = Mock(side_effect=AssertionError('external prices must not be used'))
        self.e.cycle()
        self.assertEqual((self.sig.orders[2]['action'], self.sig.orders[2]['quantity']), ('sell', 10))
        self.assertEqual(self.e.ledger.held('2')[0], 30)
        self.assertEqual(self.j.db.execute('SELECT COUNT(*) FROM risk_exit_orders').fetchone()[0], 1)
        self.assertEqual(self.j.db.execute('SELECT COUNT(*) FROM risk_exit_orders r JOIN executions e ON e.key=r.key').fetchone()[0], 1)

    def test_contract_change_blocks_sig_only_sale(self):
        old = self.sig.market
        self.sig.market = lambda mid: dict(old(mid), title='Changed rules')
        self.e.cycle(); self.assertEqual(len(self.sig.orders), 1)

    def test_preflight_bounce_never_falls_back_to_new_buy(self):
        original = self.e.risk_exit_decision
        def decision(*args, **kwargs):
            if kwargs.get('force'): self.sig.prices['2'] = ('.65', '.70')
            return original(*args, **kwargs)
        self.e.risk_exit_decision = decision
        self.e.cycle(); self.assertEqual(len(self.sig.orders), 1)

    def test_stop_and_news_block_exit(self):
        with patch.object(self.e, 'news_block', return_value='news pause'):
            self.e.cycle()
        self.assertEqual(len(self.sig.orders), 1)
        with patch.object(self.e, 'stopped', return_value=True): self.e.cycle()
        self.assertEqual(len(self.sig.orders), 1)

    def test_account_inventory_mismatch_blocks_exit(self):
        self.sig.inventory['2'] = 39
        with self.assertRaisesRegex(RuntimeError, 'portfolio differs'): self.e.cycle()
        self.assertEqual(len(self.sig.orders), 1)

    def test_unknown_submission_keeps_pending_reservation_and_tag(self):
        self.sig.timeout = True
        with self.assertRaises(TimeoutError): self.e.cycle()
        row = self.j.db.execute('SELECT o.* FROM orders o JOIN risk_exit_orders r ON o.key=r.key').fetchone()
        self.assertNotEqual(row['state'], 'closed')
        self.assertEqual(self.e.ledger.held('2')[0], 40)
        with self.assertRaises(RuntimeError): self.e.cycle()
        self.assertEqual(len(self.sig.orders), 2)

    def test_drawdown_buy_pause_does_not_block_risk_sale(self):
        with patch.object(self.e.drawdown, 'status', return_value={'buy_allowed': False, 'reason': 'drawdown_paused'}):
            self.e.cycle()
        self.assertEqual(self.sig.orders[2]['action'], 'sell')

    def test_realized_loss_halt_still_stops_all_trading(self):
        from election_bot.risk import LOSS_FIELD
        self.c['limits'][LOSS_FIELD] = '.1'
        with patch.object(self.e, 'loss_status', return_value={
                'realized_pnl_after_buffers': D('-1000'), 'net_realized_loss': D('1000'),
                'realized_loss_stop_coins': D('100'), 'includes_unrealized_losses': False}):
            self.assertFalse(self.e.cycle())
        self.assertEqual(len(self.sig.orders), 1)
        self.assertTrue((Path(self.temp.name)/'STOP').exists())

    def test_reservation_rechecks_preflight_context_and_loss(self):
        signal, book = self.e.risk_exit_decision(self.m, self.sig.positions(), phase='preflight', force=True)
        p = dict(idempotencyKey='candidate', exchangeId='2', action='sell', side='yes', quantity=signal.quantity, price=float(signal.price))
        reserve = signal.quantity*(signal.price+D('.01'))
        with self.assertRaises(ReservationRejected):
            self.e.reserve_order(p, reserve, self.sig.account(), self.sig.positions(), exit_reason=REASON)
        context = self.e.latest_risk_context
        with self.assertRaises(ReservationRejected):
            self.e.reserve_order(p, reserve, self.sig.account(), self.sig.positions(), exit_reason=REASON,
                                 risk_context=(self.m, book, time.time()-100))
        self.c['risk_exit']['loss_per_share'] = '.15'
        self.e.risk_exit = RiskExit(self.e.ledger, self.c)
        with self.assertRaises(ReservationRejected):
            self.e.reserve_order(p, reserve, self.sig.account(), self.sig.positions(), exit_reason=REASON, risk_context=context)
        self.assertEqual(self.j.db.execute('SELECT COUNT(*) FROM risk_exit_orders').fetchone()[0], 0)


if __name__ == '__main__': unittest.main()
