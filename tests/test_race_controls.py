from pathlib import Path
import tempfile
import time
import unittest

from test_active import Broker, EXECUTION
from election_bot.active_engine import ActiveEngine, ReservationRejected
from election_bot.clients import contract_record, fingerprint
from election_bot.contract_review import race_budgets
from election_bot.demo import fixture
from election_bot.engine import validate_config
from election_bot.ledger import Ledger
from election_bot.race_controls import RaceGroups
from election_bot.state import Journal
from election_bot.strategy import D


class RaceControlsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.config, original, self.refs = fixture()
        self.config['execution'] = dict(EXECUTION)
        self.config['limits'].update(per_market='100', per_order='50', total='500', daily='200')
        self.sig = Broker(original)
        base = self.config['markets'][0]
        base.update(sig_market_id='2', race_key='2026:senate:NH')
        base['contract_fingerprint'] = fingerprint(contract_record(self.sig.market('2'), base, self.refs.metadata(base)))
        self.config['markets'].append(dict(base, name='sibling', enabled=False, sig_exchange_id='3', sig_market_id='3'))
        self.path = Path(self.temp.name)/'test.sqlite3'
        self.journal = Journal(self.path, 'test'); self.addCleanup(self.journal.close)
        self.engine = ActiveEngine(self.config, self.sig, self.refs, self.journal, self.temp.name, live=True)

    def payload(self, key='next', exchange='2', action='buy', side='yes', quantity=10, price='.5'):
        return dict(idempotencyKey=key, exchangeId=exchange, action=action, side=side, quantity=quantity, price=price)

    def fill(self, key, exchange, quantity, price='.5', action='buy', side='yes'):
        p = self.payload(key, exchange, action, side, quantity, price)
        self.journal.reserve(p, D(quantity)*(D(price)+D('.01')))
        self.engine.ledger.record(p, quantity, price, '.01')
        self.journal.db.execute('UPDATE orders SET created=? WHERE key=?', (time.time()-1000, key))
        self.journal.db.commit()
        self.sig.inventory[exchange] = self.engine.ledger.held(exchange)[0]

    def test_disabled_sibling_consumes_shared_allowance_without_netting_claims(self):
        self.fill('d', '2', 100)
        self.fill('r', '3', 90, side='no')
        self.assertEqual(self.engine.races.committed(self.engine.ledger, '2'), D('96.9'))
        self.assertEqual(self.engine.available('2', self.sig.account()), D('3.1'))
        summary = self.engine.portfolio_summary()
        self.assertEqual((summary['open_races'], summary['open_contracts']), (1, 2))

    def test_realized_losses_and_pending_sibling_stay_in_same_pool(self):
        self.fill('d', '2', 100)
        self.fill('sell', '2', 100, '.4', action='sell')
        self.fill('r', '3', 100)
        self.journal.reserve(self.payload('pending', exchange='3'), D('5.1'))
        self.assertEqual(self.engine.races.committed(self.engine.ledger, '2'), D('68.1'))
        self.assertEqual(self.engine.available('2', self.sig.account()), D('31.9'))
        with self.assertRaisesRegex(RuntimeError, 'Unresolved order'):
            self.engine.reserve_order(self.payload(), D('5.1'), self.sig.account(), self.sig.positions())

    def test_restart_preserves_bindings_and_disabling_does_not_reset_budget(self):
        self.fill('d', '2', 100)
        self.config['markets'][0]['enabled'] = False
        again = ActiveEngine(self.config, self.sig, self.refs, self.journal, self.temp.name, live=True)
        self.assertEqual(again.available('3', self.sig.account()), D(49))
        self.config['markets'].pop(0)
        again = ActiveEngine(self.config, self.sig, self.refs, self.journal, self.temp.name, live=True)
        self.assertEqual(again.available('3', self.sig.account()), D(49))

    def test_offline_report_and_execution_share_persisted_accounting(self):
        self.fill('d', '2', 100)
        self.config['markets'].pop(0)
        before = self.path.read_bytes()
        result = race_budgets(self.config, self.path)
        self.assertEqual(result['status'], 'journal_snapshot_only')
        self.assertEqual(D(result['races']['2026:senate:NH']['committed']), D(51))
        self.assertEqual(before, self.path.read_bytes())

    def test_sibling_gains_offset_race_losses_but_never_expand_cap(self):
        self.fill('d', '2', 100)
        self.fill('d-sale', '2', 100, '.4', action='sell')
        self.fill('r', '3', 100)
        self.fill('r-sale', '3', 100, '.7', action='sell')
        self.assertEqual(self.engine.races.committed(self.engine.ledger, '2'), D(0))
        self.config['limits'].update(per_order='1000', daily='1000')
        self.assertEqual(self.engine.available('2', self.sig.account()), D(100))

    def test_persisted_race_cannot_be_renamed_to_reset_allowance(self):
        self.config['markets'][0]['race_key'] = '2026:senate:NE'
        with self.assertRaisesRegex(ValueError, 'Persisted race assignment changed'):
            ActiveEngine(self.config, self.sig, self.refs, self.journal, self.temp.name, live=True)

    def test_unknown_inventory_fails_closed_even_without_directional_caps(self):
        self.fill('unknown', '99', 1)
        with self.assertRaisesRegex(RuntimeError, 'Unmapped race accounting'):
            self.engine.available('2', self.sig.account())

    def test_cooldown_and_scheduler_include_disabled_siblings(self):
        self.fill('r', '3', 1)
        self.journal.db.execute('UPDATE orders SET created=?', (time.time(),)); self.journal.db.commit()
        name = self.config['markets'][0]['name']
        self.assertGreater(self.engine.scan_hints()[name]['not_before'], time.time())
        with self.assertRaisesRegex(ReservationRejected, 'Race cooldown'):
            self.engine.reserve_order(self.payload(), D('5.1'), self.sig.account(), self.sig.positions())
        self.assertFalse(self.journal.pending())
        self.assertFalse(self.journal.db.in_transaction)

    def test_atomic_guard_rejects_second_sibling_after_first_uses_allowance(self):
        self.fill('sibling', '3', 190)
        self.engine.ledger.inventory()  # Also exercise cached inventory invalidation.
        with self.assertRaisesRegex(ReservationRejected, 'Coin limits'):
            self.engine.reserve_order(self.payload(quantity=10), D('5.1'), self.sig.account(), self.sig.positions())
        self.assertFalse(self.journal.pending())
        self.engine.reserve_order(self.payload(quantity=6), D('3.06'), self.sig.account(), self.sig.positions())
        self.assertEqual(self.engine.races.committed(self.engine.ledger, '2'), D('99.96'))

    def test_guard_rereads_external_execution_changes_before_reserving(self):
        self.engine.ledger.inventory()
        other = Journal(self.path, 'test')
        try:
            ledger = Ledger(other)
            payload = self.payload('sibling', '3', quantity=190)
            other.reserve(payload, D('96.9')); ledger.record(payload, 190, '.5', '.01')
            other.db.execute('UPDATE orders SET created=?', (time.time()-1000,)); other.db.commit()
        finally:
            other.close()
        with self.assertRaisesRegex(ReservationRejected, 'Coin limits'):
            self.engine.reserve_order(self.payload(), D('5.1'), self.sig.account(), self.sig.positions())
        self.assertFalse(self.journal.pending())

    def test_failed_guard_rolls_back_its_writes_and_releases_sqlite_lock(self):
        def rejected():
            self.journal.db.execute("INSERT INTO race_bindings VALUES ('88','test')")
            raise ValueError('rejected')
        with self.assertRaisesRegex(ValueError, 'rejected'):
            self.journal.reserve(self.payload(), D('5.1'), guard=rejected)
        self.assertIsNone(self.journal.db.execute("SELECT * FROM race_bindings WHERE exchange='88'").fetchone())
        self.assertFalse(self.journal.db.in_transaction)
        self.journal.reserve(self.payload(), D('5.1'))
        self.assertEqual(len(self.journal.pending()), 1)

    def test_daily_total_cash_and_per_order_caps_still_apply(self):
        for field, value in (('daily', '3'), ('total', '3'), ('per_order', '3')):
            old = self.config['limits'][field]; self.config['limits'][field] = value
            with self.subTest(field=field), self.assertRaisesRegex(ReservationRejected, 'Coin limits'):
                self.engine.reserve_order(self.payload(), D('5.1'), self.sig.account(), self.sig.positions())
            self.config['limits'][field] = old
        with self.assertRaisesRegex(ReservationRejected, 'Coin limits'):
            self.engine.reserve_order(self.payload(), D('5.1'), dict(self.sig.account(), myBalance='3'), [])
        self.assertFalse(self.journal.pending())

    def test_sale_releases_only_confirmed_cost_and_uses_native_inventory(self):
        self.fill('d', '2', 190)
        self.engine.reserve_order(self.payload(action='sell'), D('5.1'), self.sig.account(), self.sig.positions())
        self.assertGreater(self.engine.races.committed(self.engine.ledger, '3'), D('96.9'))
        self.engine.ledger.record(self.payload(action='sell'), 10, '.5', '.01')
        self.assertEqual(self.engine.races.committed(self.engine.ledger, '3'), D('92.0'))
        with self.assertRaisesRegex(RuntimeError, 'exceeds bot-owned'):
            self.engine.ledger.sale_basis('3', 'yes', 1)

    def test_multi_contract_activation_still_requires_settlement_and_exposure_work(self):
        self.config['markets'][1]['enabled'] = True
        with self.assertRaisesRegex(ValueError, 'only one contract per race'):
            validate_config(self.config)

    def test_disabled_duplicate_exchange_and_empty_race_are_rejected(self):
        self.config['markets'][1]['sig_exchange_id'] = '2'
        with self.assertRaisesRegex(ValueError, 'unique exchange'):
            validate_config(self.config)
        self.config['markets'][1]['sig_exchange_id'] = '3'
        self.config['markets'][1]['race_key'] = ''
        with self.assertRaisesRegex(ValueError, 'nonempty race_key'):
            validate_config(self.config)

    def test_existing_single_contract_available_amount_is_unchanged(self):
        self.fill('d', '2', 150)
        before = min(D(50), D(100)-self.engine.ledger.committed('2'),
                     D(500)-self.engine.ledger.committed(), D(200)-self.journal.used(today=True))
        self.assertEqual(self.engine.available('2', self.sig.account()), before)
