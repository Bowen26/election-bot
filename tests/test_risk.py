import copy
import json
import random
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import test_active
from election_bot.active_engine import ActiveEngine
from election_bot.clients import iso_time
from election_bot.demo import fixture
from election_bot.engine import Engine, validate_config
from election_bot.risk import Exposure, verify_account_inventory
from election_bot.state import Journal
from election_bot.strategy import Book, D, choose


def risk_config():
    config, _, _ = fixture()
    config['execution'] = dict(test_active.EXECUTION)
    config['limits'].update(net_shares_total='100', net_shares_per_office='100',
                            realized_loss_stop_fraction='.10')
    base = config['markets'][0]
    base.update(office='senate', exposure_sign=1)
    config['markets'].append(dict(base, name='other', sig_exchange_id='3',
                                  sig_market_id='3', office='house'))
    return config


def fake_ledger(yes=0, no=0, pending=None):
    holdings = {}
    if yes:
        holdings['2', 'yes'] = {'quantity': D(yes)}
    if no:
        holdings['3', 'no'] = {'quantity': D(no)}
    return SimpleNamespace(inventory=lambda: (holdings, {}),
                           journal=SimpleNamespace(pending=lambda: pending or []))


def pending(exchange, action, side, quantity):
    return {'payload': json.dumps(dict(exchangeId=exchange, action=action, side=side, quantity=quantity))}


class ExposureTests(unittest.TestCase):
    def test_cap_allows_offsets_and_shrinks_directional_additions(self):
        exposure = Exposure(fake_ledger(100, 0), risk_config())
        self.assertEqual(exposure.headroom('2', 'buy', 'yes'), 0)
        self.assertEqual(exposure.headroom('2', 'buy', 'no'), 200)
        self.assertEqual(exposure.headroom('2', 'sell', 'yes'), 200)
        exposure = Exposure(fake_ledger(97), risk_config())
        self.assertEqual(exposure.headroom('2', 'buy', 'yes'), 3)

    def test_office_can_bind_before_total(self):
        config = risk_config()
        config['limits'].update(net_shares_total='200', net_shares_per_office='100')
        exposure = Exposure(fake_ledger(98, 50), config)
        self.assertEqual(exposure.net['total'], 48)
        self.assertEqual(exposure.headroom('2', 'buy', 'yes'), 2)

    def test_selling_offset_can_increase_net_exposure(self):
        config = risk_config()
        config['limits'].update(net_shares_total='1000', net_shares_per_office='1000')
        exposure = Exposure(fake_ledger(1000, 900), config)
        self.assertEqual(exposure.net['total'], 100)
        self.assertEqual(exposure.headroom('3', 'sell', 'no'), 900)
        exposure = Exposure(fake_ledger(1000, 0), config)
        self.assertEqual(exposure.headroom('3', 'sell', 'no'), 0)

    def test_breach_allows_reducing_but_never_worsening_order(self):
        exposure = Exposure(fake_ledger(120), risk_config())
        self.assertEqual(exposure.headroom('2', 'buy', 'yes'), 0)
        self.assertEqual(exposure.headroom('2', 'sell', 'yes'), 220)
        self.assertEqual(exposure.headroom('2', 'buy', 'no'), 220)

    def test_pending_buy_does_not_assume_offset_will_fill(self):
        exposure = Exposure(fake_ledger(100, pending=[pending('2', 'buy', 'no', 40)]), risk_config())
        self.assertEqual(exposure.bounds['total'], [D(60), D(100)])
        self.assertEqual(exposure.headroom('2', 'buy', 'yes'), 0)
        self.assertEqual(exposure.headroom('2', 'buy', 'no'), 160)

    def test_pending_sell_reserves_exposure_increase(self):
        exposure = Exposure(fake_ledger(90, 20, [pending('3', 'sell', 'no', 15)]), risk_config())
        self.assertEqual(exposure.bounds['total'], [D(70), D(85)])
        self.assertEqual(exposure.headroom('3', 'sell', 'no'), 15)

    def test_unknown_pending_outcome_fails_closed(self):
        with self.assertRaisesRegex(RuntimeError, 'Unknown action/side'):
            Exposure(fake_ledger(pending=[pending('2', 'buy', 'maybe', 10)]), risk_config())

    def test_explicit_inverse_orientation_is_respected(self):
        config = risk_config()
        config['markets'][0]['exposure_sign'] = -1
        exposure = Exposure(fake_ledger(80), config)
        self.assertEqual(exposure.net['total'], -80)
        self.assertEqual(exposure.headroom('2', 'buy', 'yes'), 20)
        self.assertEqual(exposure.headroom('2', 'sell', 'yes'), 180)

    def test_disabled_owned_race_still_counts_and_missing_mapping_halts(self):
        config = risk_config()
        config['markets'][0]['enabled'] = False
        self.assertEqual(Exposure(fake_ledger(80), config).net['total'], 80)
        config['markets'].pop(0)
        with self.assertRaisesRegex(RuntimeError, 'Unmapped portfolio'):
            Exposure(fake_ledger(80), config)

    def test_account_inventory_checks_all_races_including_manual_positions(self):
        verify_account_inventory(fake_ledger(10), [{'exchangeId': '2', 'quantity': 10}])
        with self.assertRaisesRegex(RuntimeError, 'Account portfolio differs'):
            verify_account_inventory(fake_ledger(10), [{'exchangeId': '2', 'quantity': 10},
                                                       {'exchangeId': '99', 'quantity': -5}])
        with self.assertRaisesRegex(RuntimeError, 'Settled account'):
            verify_account_inventory(fake_ledger(10), [{'exchangeId': '2', 'quantity': 10, 'settled': True}])

    def test_random_partial_fills_never_worsen_breached_direction_or_cross_cap(self):
        rng = random.Random(402)
        config = risk_config()
        for _ in range(400):
            yes, no, q = rng.randrange(151), rng.randrange(151), rng.randrange(51)
            pending_action, pending_side = rng.choice(['buy', 'sell']), rng.choice(['yes', 'no'])
            exposure = Exposure(fake_ledger(yes, no, [pending('2', pending_action, pending_side, q)]), config)
            for action in ('buy', 'sell'):
                for side in ('yes', 'no'):
                    size = exposure.headroom('3', action, side)
                    direction = Exposure.direction(action, side)
                    for pending_fraction in (D(0), D('.5'), D(1)):
                        old_total = D(yes-no) + q*pending_fraction*Exposure.direction(pending_action, pending_side)
                        for fill_fraction in (D(0), D('.5'), D(1)):
                            after = old_total + size*fill_fraction*direction
                            self.assertLessEqual(abs(after), max(D(100), abs(old_total)))
                            after_office = -D(no) + size*fill_fraction*direction
                            self.assertLessEqual(abs(after_office), max(D(100), D(no)))


class StartupValidationTests(unittest.TestCase):
    def test_explicit_timezones_and_equivalent_offsets(self):
        self.assertEqual(iso_time('2026-10-05T16:00:00Z'), iso_time('2026-10-05T12:00:00-04:00'))
        for value in ('2026-10-05T16:00:00', '2026-10-05', None, 123):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'timezone'):
                iso_time(value)

    def test_enabled_mapping_requires_all_reference_fields(self):
        for field in ('name', 'sig_market_id', 'sig_exchange_id', 'kalshi_ticker',
                      'polymarket_event', 'polymarket_market', 'contract_fingerprint',
                      'kalshi_yes_matches_sig_yes', 'polymarket_yes_matches_sig_yes'):
            config = risk_config()
            del config['markets'][0][field]
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, field):
                validate_config(config)

    def test_invalid_orientation_and_duplicate_names(self):
        config = risk_config()
        config['markets'][0]['kalshi_yes_matches_sig_yes'] = 'true'
        with self.assertRaisesRegex(ValueError, 'boolean'):
            validate_config(config)
        config = risk_config()
        config['markets'][1]['name'] = config['markets'][0]['name']
        with self.assertRaisesRegex(ValueError, 'names must be unique'):
            validate_config(config)

    def test_risk_fields_require_explicit_office_and_factor_sign(self):
        for field in ('office', 'exposure_sign'):
            config = risk_config()
            del config['markets'][0][field]
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, field):
                validate_config(config)

    def test_fraction_and_share_caps_are_validated(self):
        for field, value in [('net_shares_total', 0), ('net_shares_total', '1.5'),
                             ('net_shares_per_office', 101), ('net_shares_total', True),
                             ('realized_loss_stop_fraction', 0), ('realized_loss_stop_fraction', 'NaN'),
                             ('realized_loss_stop_fraction', 1.1)]:
            config = risk_config()
            config['limits'][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                validate_config(config)
        config = risk_config()
        del config['limits']['net_shares_per_office']
        with self.assertRaisesRegex(ValueError, 'both'):
            validate_config(config)

    def test_legacy_engine_cannot_silently_ignore_risk_controls(self):
        config = risk_config()
        config['execution']['enabled'] = False
        with self.assertRaisesRegex(ValueError, 'ActiveEngine'):
            validate_config(config)

    def test_base_paper_recovery_releases_unfilled_reservation(self):
        config, sig, refs = fixture()
        with tempfile.TemporaryDirectory() as folder:
            journal = Journal(Path(folder)/'paper.db', 'test')
            try:
                journal.reserve({'idempotencyKey': 'crash', 'exchangeId': '2'}, 25)
                Engine(config, sig, refs, journal, folder).reconcile()
                self.assertEqual(journal.used(), 0)
                self.assertFalse(journal.pending())
            finally:
                journal.close()


class RiskIntegrationTests(unittest.TestCase):
    def setUp(self):
        test_active.ActiveTests.setUp(self)
        self.config['limits'].update(net_shares_total='100', net_shares_per_office='100',
                                      realized_loss_stop_fraction='.10')
        self.config['markets'][0].update(office='senate', exposure_sign=1)
        validate_config(self.config)

    def seed(self, key, quantity, price, exchange='2', side='yes', action='buy'):
        payload = dict(idempotencyKey=key, exchangeId=exchange, action=action, side=side,
                       quantity=quantity, price=price)
        self.journal.reserve(payload, D(quantity)*(D(price)+D('.01')))
        self.engine.ledger.record(payload, quantity, price, '.01', time.time()-1000)
        self.sig.inventory[exchange] = self.sig.inventory.get(exchange, D(0)) + (
            quantity*Exposure.direction(action, side))
        self.journal.db.execute('UPDATE orders SET created=?', (time.time()-1000,))
        self.journal.db.commit()

    def test_buy_shrinks_at_cap_then_stops_adding(self):
        self.seed('seed', 97, '.2')
        self.engine.cycle()
        self.assertEqual(self.sig.orders[1]['quantity'], 3)
        self.journal.db.execute('UPDATE orders SET created=?', (time.time()-1000,))
        self.journal.db.commit()
        self.engine.cycle()
        self.assertEqual(len(self.sig.orders), 1)
        self.assertEqual(self.engine.ledger.held('2')[0], 100)

    def test_preflight_rechecks_cap_after_scan(self):
        original = self.engine.decide
        def decide(*args, **kwargs):
            signal = original(*args, **kwargs)
            if kwargs.get('phase', 'scan') == 'scan':
                self.config['limits'].update(net_shares_total='2', net_shares_per_office='2')
            return signal
        self.engine.decide = decide
        self.engine.cycle()
        self.assertEqual(self.sig.orders[1]['quantity'], 2)

    def test_pending_submission_still_blocks_new_orders(self):
        self.journal.reserve(dict(idempotencyKey='pending', exchangeId='2', action='buy', side='yes',
                                  quantity=99, price=.6), 60)
        with self.assertRaisesRegex(RuntimeError, 'Unknown order status'):
            self.engine.cycle()
        self.assertFalse(self.sig.orders)

    def test_exit_selling_no_is_shrunk_when_it_removes_offset(self):
        base = self.config['markets'][0]
        self.config['markets'].append(dict(base, name='offset', sig_exchange_id='3', sig_market_id='3'))
        # Race assignments are captured at startup, as in a config reload.
        self.engine = ActiveEngine(self.config, self.sig, self.refs, self.journal, self.temp.name, live=True)
        self.seed('long', 100, '.2', exchange='3')
        self.seed('short', 20, '.2', side='no')
        # Net is 80; only 20 NO can be sold under cap 100.
        refs = [Book.make([('.2', 200)], [('.22', 200)]), Book.make([('.2', 200)], [('.22', 200)])]
        book = Book.make([('.14', 200)], [('.16', 200)])
        signal = self.engine.decide(base, self.sig.account(), self.sig.positions(), book, refs)
        self.assertEqual((signal.action, signal.side, signal.quantity), ('sell', 'no', 20))
        self.config['limits'].update(net_shares_total='85', net_shares_per_office='85')
        signal = self.engine.decide(base, self.sig.account(), self.sig.positions(), book, refs)
        self.assertEqual((signal.action, signal.side, signal.quantity), ('sell', 'no', 5))

    def test_existing_over_cap_position_can_sell_toward_limit(self):
        self.seed('seed', 120, '.2')
        self.sig.prices['2'] = ('.80', '.85')
        self.engine.cycle()
        self.assertEqual(self.sig.orders[1]['action'], 'sell')
        self.assertLess(self.engine.ledger.held('2')[0], 120)

    def test_reduced_exit_quantity_still_uses_actual_fifo_cost(self):
        base = self.config['markets'][0]
        self.config['markets'].append(dict(base, name='offset', sig_exchange_id='3', sig_market_id='3'))
        # Race assignments are captured at startup, as in a config reload.
        self.engine = ActiveEngine(self.config, self.sig, self.refs, self.journal, self.temp.name, live=True)
        self.seed('expensive', 10, '.8')
        self.seed('cheap', 10, '.2')
        self.seed('offset', 118, '.2', exchange='3', side='no')
        # Net -98 permits selling only 2 YES. The average is profitable, the first FIFO lot is not.
        signal = self.engine.decide(base, self.sig.account(), self.sig.positions(),
            Book.make([('.75', 200)], [('.80', 200)]), self.refs.books({}, None))
        self.assertIsNone(signal)
        decision = json.loads(self.journal.db.execute("SELECT detail FROM events WHERE kind='decision'").fetchone()[0])
        self.assertEqual(decision['exit_check']['exposure_headroom'], '2')
        self.assertEqual(decision['exit_check']['reason'], 'fifo_profit_below_minimum')

    def test_naive_tournament_close_is_rejected_before_order(self):
        account = self.sig.account()
        account['endDate'] = '2099-01-01T00:00:00'
        self.sig.account = Mock(return_value=account)
        with self.assertRaisesRegex(ValueError, 'timezone'):
            self.engine.cycle()
        self.assertFalse(self.sig.orders)

    def test_realized_gains_offset_losses_in_named_net_loss_rule(self):
        self.seed('buy1', 10, '.5')
        self.seed('sell1', 10, '.4', action='sell')  # -1.2
        self.seed('buy2', 10, '.2')
        self.seed('sell2', 10, '.5', action='sell')  # +2.8
        self.assertEqual(self.engine.loss_status()['realized_pnl_after_buffers'], D('1.6'))
        self.assertEqual(self.engine.loss_status()['net_realized_loss'], 0)
        self.assertFalse(self.engine.check_loss_stop())

    def test_global_account_mismatch_blocks_submission(self):
        self.sig.inventory['999'] = 5
        with self.assertRaisesRegex(RuntimeError, 'Account portfolio differs'):
            self.engine.cycle()
        self.assertFalse(self.sig.orders)

    def test_realized_stop_fires_at_threshold_and_survives_resume(self):
        self.config['limits']['realized_loss_stop_fraction'] = '.024'  # 12 of 500
        self.seed('buy', 100, '.5')
        self.seed('sell', 100, '.4', action='sell')
        self.sig.account = Mock(side_effect=AssertionError('Stop must precede any scan'))
        self.assertFalse(self.engine.cycle())
        self.assertTrue((Path(self.temp.name)/'STOP').exists())
        (Path(self.temp.name)/'STOP').unlink()  # Ordinary resume must not bypass the loss test.
        again = ActiveEngine(self.config, self.sig, self.refs, self.journal, self.temp.name, live=True)
        self.assertFalse(again.cycle())
        self.assertFalse(self.sig.orders)

    def test_unsold_losses_do_not_claim_to_trigger_realized_stop(self):
        self.seed('buy', 100, '.9')
        self.engine.ledger.observe('2', Book.make([('.1', 200)], [('.2', 200)]), 15)
        self.assertFalse(self.engine.check_loss_stop())
        self.assertFalse(self.engine.loss_status()['includes_unrealized_losses'])
        self.assertEqual(self.engine.loss_status()['net_realized_loss'], 0)

    def test_loss_stop_halts_batch_immediately_after_confirmed_sale(self):
        self.config['limits']['realized_loss_stop_fraction'] = '.002'  # 1 of 500
        self.seed('buy', 10, '.9')
        self.sig.prices['2'] = ('.80', '.85')
        self.assertFalse(self.engine.cycle())
        self.assertEqual(len(self.sig.orders), 1)
        self.assertEqual(self.sig.orders[1]['action'], 'sell')
        self.assertTrue(self.engine.stopped())
        self.assertFalse(self.journal.pending())
        self.assertEqual(self.engine.loss_status()['net_realized_loss'], D('1.20'))

    def test_quote_snapshots_preserve_all_venues_and_link_execution(self):
        self.engine.cycle()
        snapshots = [json.loads(r['detail']) for r in self.journal.db.execute(
            "SELECT detail FROM events WHERE kind='quote_snapshot'")]
        self.assertEqual([s['phase'] for s in snapshots], ['scan', 'preflight'])
        self.assertEqual([q['venue'] for q in snapshots[0]['quotes']], ['SIG', 'Kalshi', 'Polymarket'])
        self.assertEqual([D(q['midpoint']) for q in snapshots[0]['quotes']], [D('.575'), D('.74'), D('.75')])
        for quote in snapshots[0]['quotes']:
            self.assertIn('source_at', quote)
            self.assertIn('timestamp_basis', quote)
        signal = json.loads(self.journal.db.execute("SELECT detail FROM events WHERE kind='signal'").fetchone()[0])
        self.assertEqual(signal['snapshot_id'], snapshots[1]['snapshot_id'])
        self.assertEqual(signal['order_key'], self.sig.orders[1]['idempotencyKey'])

    def test_shadow_logs_once_without_changing_order_or_network_calls(self):
        original = self.refs.books
        with patch.object(self.refs, 'books', wraps=original) as books:
            self.engine.cycle()
            self.assertEqual(books.call_count, 2)  # scan + existing preflight only
        shadows = [json.loads(r['detail']) for r in self.journal.db.execute(
            "SELECT detail FROM events WHERE kind='shadow_decision'")]
        self.assertEqual(len(shadows), 1)
        self.assertEqual(len(self.sig.orders), 1)
        baseline = shadows[0]['decisions'][0]['candidate']
        self.assertEqual(baseline['quantity'], self.sig.orders[1]['quantity'])
        self.assertEqual(D(baseline['price']), D(self.sig.orders[1]['price']))
        self.assertEqual(baseline['side'], self.sig.orders[1]['side'])
        self.assertFalse(self.journal.pending())
        self.assertEqual(self.journal.db.execute('SELECT COUNT(*) FROM orders').fetchone()[0], 1)

    def test_shadow_quote_aging_does_not_change_live_choice(self):
        with patch('election_bot.active_engine.shadow_decisions', side_effect=ValueError('quote aged out')):
            self.engine.cycle()
        self.assertEqual(len(self.sig.orders), 1)
        shadow = json.loads(self.journal.db.execute(
            "SELECT detail FROM events WHERE kind='shadow_decision'").fetchone()[0])
        self.assertEqual(shadow['status'], 'unavailable')
        self.assertFalse(self.journal.pending())

    def test_exit_experiment_cannot_submit_alternative_sales(self):
        self.seed('held', 30, '.60')
        self.sig.prices['2'] = ('.745', '.750')
        original = self.refs.books
        def thin(*args):
            refs = original(*args)
            for book in refs:
                book.asks = [(price, D(5)) for price, _ in book.asks]
            return refs
        with patch.object(self.refs, 'books', side_effect=thin) as reads:
            self.engine.cycle()
            self.assertEqual(reads.call_count, 1)  # No shadow preflight or extra feed fetch.
        self.assertFalse(self.sig.orders)
        study = json.loads(self.journal.db.execute(
            "SELECT detail FROM events WHERE kind='exit_shadow'").fetchone()[0])
        self.assertIsNone(study['decisions'][0]['candidate'])
        self.assertEqual(study['decisions'][1]['candidate']['action'], 'sell')
        self.assertEqual(study['decisions'][1]['candidate']['side'], 'yes')
        self.assertFalse(self.journal.pending())
        self.assertEqual(self.engine.ledger.held('2')[0], 30)

    def test_exit_experiment_failure_cannot_block_existing_live_exit(self):
        self.seed('held', 30, '.60')
        self.sig.prices['2'] = ('.80', '.85')
        with patch('election_bot.active_engine.exit_experiment', side_effect=ValueError('measurement aged out')):
            self.engine.cycle()
        self.assertEqual(len(self.sig.orders), 1)
        self.assertEqual(self.sig.orders[1]['action'], 'sell')
        studies = [json.loads(r[0]) for r in self.journal.db.execute(
            "SELECT detail FROM events WHERE kind='exit_shadow'")]
        self.assertEqual(len(studies), 1)  # Scan only, no preflight duplication.
        self.assertEqual(studies[0]['status'], 'unavailable')
        self.assertFalse(self.journal.pending())

    def test_invalid_quote_is_logged_as_invalid_not_refreshed(self):
        book, refs = self.sig.book('2'), self.refs.books({}, None)
        refs[1].source_at = time.time()-90
        with self.assertRaisesRegex(ValueError, 'Stale'):
            self.engine.decide(self.config['markets'][0], self.sig.account(), [], book, refs)
        snapshot = json.loads(self.journal.db.execute("SELECT detail FROM events WHERE kind='quote_snapshot'").fetchone()[0])
        self.assertIn('source_stale', snapshot['quotes'][2]['quality']['issues'])
        self.assertFalse(self.sig.orders)

    def test_strategy_rejects_candidate_with_zero_directional_headroom(self):
        # Exposure rejection remains distinct from a failed price comparison.
        config, sig, refs = fixture()
        checks = []
        self.assertIsNone(choose(sig.book('2'), refs.books({}, None), config['strategy'], 25,
                                 checks, {'yes': D(0), 'no': D(100)}))
        self.assertEqual(checks[0]['reason'], 'exposure_limit')
