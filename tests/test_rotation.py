import copy
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from election_bot import __main__ as cli
from election_bot.demo import fixture
from election_bot.ledger import Ledger
from election_bot.rotation import RotationStudy, DEFAULTS, report, settings, simulate_sale
from election_bot.state import Journal
from election_bot.strategy import Book, D
import test_active


class RotationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)/'j.db'
        self.now = time.time()
        mock = patch('time.time', side_effect=lambda: self.now); mock.start(); self.addCleanup(mock.stop)
        self.j = Journal(self.path, 'test'); self.addCleanup(self.j.close); self.l = Ledger(self.j)
        self.c, _, _ = fixture(); self.c['execution'] = dict(test_active.EXECUTION)
        self.c['rotation_study'] = dict(DEFAULTS, enabled=True)
        self.a = self.c['markets'][0]; self.a['sig_market_id'] = '1'
        self.b = dict(self.a, name='second', sig_exchange_id='3', sig_market_id='3')
        self.c['markets'].append(self.b)
        self.g = RotationStudy(self.l, self.c); self.fill_number = 0
        self.fill('2', 100)

    def fill(self, exchange, q, side='yes', price='.60'):
        self.fill_number += 1
        p = dict(exchangeId=exchange, idempotencyKey=str(self.fill_number), quantity=q, side=side, action='buy', price=price)
        self.j.reserve(p, D(q)*(D(price)+D('.01'))); self.l.record(p, q, price, '.01')

    def book(self, bid, ask, depth=200):
        return Book.make([(bid, depth)], [(ask, depth)])

    def observe(self, mapping, bid, ask, ref, **kwargs):
        defaults = dict(buy_allowed=True, available=25, cash=1000)
        defaults.update(kwargs)
        self.g.observe(mapping, self.book(bid, ask), [self.book(ref, str(D(ref)+D('.01')))]*2,
                       mapping['name']+'-snapshot', **defaults)

    def propose(self):
        self.observe(self.a, '.55', '.56', '.56')
        self.observe(self.b, '.39', '.40', '.70')
        rows = self.j.db.execute('SELECT * FROM rotation_proposals').fetchall()
        return json.loads(rows[-1]['detail']) if rows else None

    def test_proposal_accounts_for_sell_buy_buffers_and_partial_size(self):
        d = self.propose()
        self.assertEqual(d['source']['quantity'], 25)
        self.assertEqual(d['target']['quantity'], 32)
        self.assertEqual(D(d['sale_proceeds']), D('13.50'))
        self.assertEqual(D(d['purchase_cost']), D('13.12'))
        self.assertEqual(D(d['residual_cash']), D('.38'))
        self.assertEqual(D(d['proxy_improvement_coins']), D('8.71'))
        self.assertTrue(d['currently_fundable_without_sale'])
        self.assertTrue(d['simulation_only'])
        self.assertEqual(self.l.held('2')[0], 100)
        self.assertEqual(self.l.held('3')[0], 0)
        self.assertEqual(self.j.db.execute('SELECT COUNT(*) FROM orders').fetchone()[0], 1)
        self.assertFalse(self.j.pending())

    def test_weak_destination_is_rejected(self):
        self.observe(self.a, '.55', '.56', '.56')
        self.observe(self.b, '.39', '.40', '.42')
        self.assertEqual(report(self.path)['proposals'], 0)

    def test_opportunity_cost_prevents_selling_better_holding(self):
        self.observe(self.a, '.55', '.56', '.95')
        self.observe(self.b, '.39', '.40', '.60')
        self.assertEqual(report(self.path)['proposals'], 0)

    def test_quote_age_reference_disagreement_and_future_cache_excluded(self):
        self.observe(self.a, '.55', '.56', '.56'); self.now += 16
        self.observe(self.b, '.39', '.40', '.70')
        self.assertEqual(report(self.path)['proposals'], 0)
        self.g.cache.clear()
        self.g.observe(self.a, self.book('.55', '.56'), [self.book('.56', '.57'), self.book('.90', '.91')], 'bad', True, 25, 1000)
        self.observe(self.b, '.39', '.40', '.70')
        self.assertEqual(report(self.path)['proposals'], 0)

    def test_live_fill_invalidates_cached_other_contract(self):
        self.observe(self.a, '.55', '.56', '.56'); self.fill('2', 1)
        self.observe(self.b, '.39', '.40', '.70')
        self.assertEqual(report(self.path)['proposals'], 0)

    def test_same_race_and_owned_destination_excluded(self):
        self.b['sig_market_id'] = '1'
        self.assertIsNone(self.propose())
        self.b['sig_market_id'] = '3'; self.fill('3', 1)
        self.assertIsNone(self.propose())

    def test_buy_pause_news_and_pending_order_block_proposal(self):
        self.observe(self.a, '.55', '.56', '.56')
        self.observe(self.b, '.39', '.40', '.70', buy_allowed=False)
        self.assertEqual(report(self.path)['proposals'], 0)
        self.observe(self.b, '.39', '.40', '.70', blocked=lambda m: m == self.a)
        self.assertEqual(report(self.path)['proposals'], 0)
        self.j.reserve(dict(exchangeId='2', idempotencyKey='pending'), 1)
        self.assertIsNone(self.propose())

    def test_gross_contracts_excluded(self):
        self.b['exposure_mode'] = 'gross'
        self.assertIsNone(self.propose())

    def test_daily_budget_does_not_recycle_on_simulated_sale(self):
        self.c['limits']['daily'] = '61'
        self.assertIsNone(self.propose())

    def test_realized_loss_halt_blocks_rotated_purchase(self):
        self.c['limits']['total'] = '100'
        self.c['limits']['realized_loss_stop_fraction'] = '.01'
        self.assertIsNone(self.propose())

    def test_post_sale_total_cap_can_release_capital(self):
        self.c['limits']['total'] = '61'
        self.assertIsNotNone(self.propose())
        self.assertEqual(self.l.committed(), D('61'))

    def test_target_race_cap_limits_buy_size(self):
        self.c['limits']['per_market'] = '10'
        d = self.propose(); self.assertLessEqual(D(d['purchase_cost']), 10)

    def test_share_exposure_checked_after_hypothetical_sale(self):
        self.c['limits'].update(net_shares_total=100, net_shares_per_office=100)
        for m in (self.a, self.b): m.update(office='senate', exposure_sign=1)
        d = self.propose()
        self.assertEqual(d['target']['quantity'], 25)
        self.assertEqual(self.l.held('2')[0], 100)

    def test_no_side_source_and_target(self):
        # Separate exchange so the fixture YES holding does not oppose the NO lot.
        self.fill('4', 100, side='no')
        m = dict(self.a, sig_exchange_id='4', sig_market_id='4', name='no-source')
        self.c['markets'].append(m)
        self.observe(m, '.44', '.45', '.43')
        self.observe(self.b, '.60', '.61', '.29')
        d = json.loads(self.j.db.execute('SELECT detail FROM rotation_proposals').fetchone()[0])
        self.assertEqual(d['source']['side'], 'no'); self.assertEqual(d['target']['side'], 'no')

    def test_small_holdings_not_rounded_up(self):
        self.fill('4', 3)
        m = dict(self.a, sig_exchange_id='4', sig_market_id='4', name='small')
        self.c['markets'].append(m)
        self.observe(m, '.55', '.56', '.56'); self.observe(self.b, '.39', '.40', '.70')
        self.assertEqual(report(self.path)['proposals'], 0)

    def test_proposal_cooldown_survives_restart_and_clock_reversal(self):
        self.propose(); self.g = RotationStudy(self.l, self.c); self.propose()
        self.assertEqual(report(self.path)['proposals'], 1)
        self.now -= 60; self.propose()
        self.assertEqual(self.j.db.execute('SELECT COUNT(*) FROM rotation_proposals').fetchone()[0], 1)
        self.now += 1861; self.propose()
        self.assertEqual(report(self.path)['proposals'], 2)

    def test_paired_forward_outcome_compares_with_retaining_original_shares(self):
        self.propose(); self.now += 900
        self.g.observe_outcomes('2', self.book('.50', '.51'))
        self.now += 10; self.g.observe_outcomes('3', self.book('.60', '.61'))
        r = report(self.path)
        self.assertEqual(r['outcomes']['900']['measured'], 1)
        self.assertEqual(D(r['outcomes']['900']['mean_incremental_coins']), D('7.01'))
        self.assertEqual(r['outcomes']['3600']['not_due'], 1)

    def test_insufficient_depth_cannot_be_replaced_by_later_favorable_quote(self):
        self.propose(); self.now += 900
        self.g.observe_outcomes('2', self.book('.50', '.51', depth=1))
        self.g.observe_outcomes('2', self.book('.90', '.91'))
        self.g.observe_outcomes('3', self.book('.60', '.61'))
        r = report(self.path)['outcomes']['900']
        self.assertEqual(r['insufficient_depth'], 1); self.assertIsNone(r['mean_incremental_coins'])

    def test_stale_future_observation_not_used(self):
        self.propose(); b = self.book('.50', '.51'); self.now += 900
        with self.assertRaises(ValueError): self.g.observe_outcomes('2', b)
        self.assertEqual(report(self.path)['outcomes']['900']['pending'], 1)

    def test_missing_and_unaligned_outcomes_not_counted_as_zero(self):
        self.propose(); self.now += 900
        self.g.observe_outcomes('2', self.book('.50', '.51')); self.now += 150
        self.g.observe_outcomes('3', self.book('.60', '.61'))
        self.assertEqual(report(self.path)['outcomes']['900']['unaligned'], 1)
        self.now += 4000
        self.assertEqual(report(self.path)['outcomes']['3600']['missing'], 1)

    def test_fifo_simulation_does_not_change_original_ledger(self):
        self.fill('2', 100, price='.80')
        before = copy.deepcopy(self.l.inventory())
        sim, basis, pnl = simulate_sale(self.l, '2', 'yes', 125, D('.70'), D('.01'))
        self.assertEqual(basis, D('81.25'))
        self.assertEqual(sim.inventory()[0][('2', 'yes')]['quantity'], 75)
        self.assertEqual(before, self.l.inventory())

    def test_report_is_read_only_and_missing_database_not_created(self):
        self.propose(); before = hashlib.sha256(self.path.read_bytes()).digest()
        report(self.path)
        self.assertEqual(before, hashlib.sha256(self.path.read_bytes()).digest())
        p = Path(self.temp.name)/'absent.db'; self.assertFalse(report(p)['initialized']); self.assertFalse(p.exists())
        for hours in (0, -1, 169, float('nan')):
            with self.assertRaises(ValueError): report(self.path, hours)

    def test_settings_validate_and_disabled_creates_no_tables(self):
        for k, value in [('enabled', 1), ('partial_fraction', '.5'), ('depth_fraction', '.5'), ('cooldown_seconds', 1)]:
            c = copy.deepcopy(self.c); c['rotation_study'][k] = value
            with self.assertRaises(ValueError): settings(c)
        self.c['rotation_study']['enabled'] = False
        with tempfile.TemporaryDirectory() as root:
            j = Journal(Path(root)/'j.db', 'disabled')
            try:
                RotationStudy(Ledger(j), self.c)
                self.assertIsNone(j.db.execute("SELECT name FROM sqlite_master WHERE name='rotation_proposals'").fetchone())
            finally: j.close()

    def test_cli_report_never_loads_credentials_or_client(self):
        with patch.object(sys, 'argv', ['election_bot', 'rotation-study']), \
             patch.object(cli, 'RUNTIME', Path(self.temp.name)), patch.object(cli, 'key') as key, \
             patch.object(cli, 'Sig') as sig, patch('builtins.print'):
            cli.main()
        key.assert_not_called(); sig.assert_not_called()


class RotationEngineTests(unittest.TestCase):
    def test_study_does_not_change_live_order_and_only_scans_propose(self):
        f = test_active.ActiveTests(); f.setUp(); self.addCleanup(f.doCleanups)
        f.config['rotation_study'] = dict(DEFAULTS, enabled=True)
        f.engine.rotation_study = RotationStudy(f.engine.ledger, f.config)
        with patch.object(f.engine.rotation_study, 'observe', wraps=f.engine.rotation_study.observe) as collect, \
             patch.object(f.engine.rotation_study, 'observe_outcomes', wraps=f.engine.rotation_study.observe_outcomes) as mark:
            f.engine.cycle()
        self.assertEqual(collect.call_count, 1)
        self.assertEqual(mark.call_count, 2)
        self.assertEqual(len(f.sig.orders), 1)
        self.assertEqual(f.sig.orders[1]['quantity'], 40)
        self.assertEqual(f.sig.orders[1]['action'], 'buy')
