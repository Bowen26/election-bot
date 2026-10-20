import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from election_bot.patient_entry import (DEFAULTS, REASON, TICK, allowance, arm, attach,
                                        attach_control, counterfactual, improve, initialize,
                                        prior_buys, propose, settings)
from election_bot.patient_report import report
from election_bot.strategy import Book, D, Signal, choose


def database():
    db = sqlite3.connect(':memory:')
    db.row_factory = sqlite3.Row
    db.executescript('''
      CREATE TABLE orders (key TEXT PRIMARY KEY, market TEXT, amount TEXT,
        day TEXT, created REAL, state TEXT, payload TEXT, response TEXT);
      CREATE TABLE executions (key TEXT PRIMARY KEY, exchange TEXT, action TEXT,
        side TEXT, quantity TEXT, price TEXT, buffer TEXT, at REAL);
      CREATE TABLE markouts (key TEXT, horizon INTEGER, at REAL, pnl TEXT, reason TEXT,
        PRIMARY KEY(key,horizon));''')
    initialize(db)
    return db


def config(**patient):
    return {'strategy': {'cost_buffer_per_share': '0.01', 'minimum_edge': '0.05',
                         'max_shares_per_order': 200, 'max_age_seconds': 15,
                         'min_reference_depth': '20', 'max_reference_spread': '0.08',
                         'max_reference_disagreement': '0.08'},
            'execution': {'enabled': True},
            'patient_entry': {**DEFAULTS, 'enabled': True, 'control_fraction': '0', 'pilot_fraction':'1', **patient}}


def book(bid, ask, size=500):
    return Book.make([(bid, size)], [(ask, size)], venue='SIG')


def fill(db, key, exchange, quantity='10', price='0.40', amount='4.10', arm_name='patient',
         priced='improved', improvable=1, markouts=()):
    db.execute('INSERT INTO orders VALUES (?,?,?,?,?,?,?,NULL)',
               (key, exchange, amount, '2026-10-17', 0.0, 'closed', '{}'))
    db.execute("INSERT INTO executions VALUES (?,?,'buy','yes',?,?,'0.01',0.0)",
               (key, exchange, quantity, price))
    db.execute('INSERT INTO patient_orders VALUES (?,?,?,?,?,?)',
               (key, exchange, arm_name, priced, improvable, '{}'))
    for horizon, pnl in markouts:
        db.execute('INSERT INTO markouts VALUES (?,?,0.0,?,NULL)', (key, horizon, pnl))
    db.commit()


class Settings(unittest.TestCase):
    def test_disabled_by_default_and_requires_execution(self):
        self.assertFalse(settings({'strategy': {}, 'execution': {'enabled': True}})['enabled'])
        with self.assertRaises(ValueError):
            settings({'strategy': {}, 'execution': {'enabled': False},
                      'patient_entry': {'enabled': True}})

    def test_rejects_unknown_keys_and_bad_ranges(self):
        for bad in ({'unknown': 1}, {'improvement_ticks': 0}, {'improvement_ticks': 11},
                    {'improvement_ticks': True}, {'allocation': '0'}, {'control_fraction': '1.5'},
                    {'min_sig_spread': '0.001'}, {'enabled': 'yes'}, {'allocation': True}):
            with self.assertRaises(ValueError, msg=bad):
                settings({'strategy': {}, 'execution': {'enabled': True}, 'patient_entry': bad})


class Assignment(unittest.TestCase):
    def test_deterministic_and_rederivable(self):
        db, p = database(), settings(config(control_fraction='.5'))
        first = arm(db, '101', p)
        self.assertEqual(first, arm(db, '101', p))      # stable while committed state is unchanged
        self.assertIn(first, ('patient', 'control'))

    def test_assignment_moves_with_committed_buys(self):
        db, p = database(), settings(config(control_fraction='.5'))
        seen = {arm(db, '101', p)}
        for n in range(1, 12):
            fill(db, 'k%d' % n, '101')
            self.assertEqual(prior_buys(db, '101'), n)
            seen.add(arm(db, '101', p))
        self.assertEqual(seen, {'patient', 'control'}, 'both arms must be reachable')

    def test_extreme_fractions_are_pure(self):
        db = database()
        self.assertEqual(arm(db, '101', settings(config(control_fraction='1'))), 'control')
        self.assertEqual(arm(db, '101', settings(config(control_fraction='0'))), 'patient')


class Improvement(unittest.TestCase):
    def entry(self, ask, side='yes', reference='0.60'):
        return Signal(side, D(ask), 20, D(reference), D(reference)-D(ask)-D('0.01'))

    def test_improves_price_keeps_size_and_widens_edge(self):
        p = settings(config())
        control = self.entry('0.50')
        improved, why = improve(book('0.46', '0.50'), control, p, D('0.01'))
        self.assertEqual(why, 'improved')
        self.assertEqual(improved.price, D('0.495'))
        self.assertEqual(improved.quantity, control.quantity, 'size must not change between arms')
        self.assertGreater(improved.edge, control.edge)
        self.assertEqual(improved.reason, REASON)
        self.assertEqual(improved.action, 'buy')
        self.assertEqual(improved.price % TICK, 0)

    def test_never_prices_below_the_bid(self):
        p = settings(config(improvement_ticks=10))
        improved, _ = improve(book('0.48', '0.50'), self.entry('0.50'), p, D('0.01'))
        self.assertEqual(improved.price, D('0.48'), 'clamps to the bid rather than resting below it')

    def test_spread_floor_is_inclusive_at_the_boundary(self):
        p = settings(config())                       # min_sig_spread 0.01
        self.assertEqual(improve(book('0.495', '0.50'), self.entry('0.50'), p, D('0.01'))[1],
                         'spread_too_narrow_to_improve')
        improved, why = improve(book('0.49', '0.50'), self.entry('0.50'), p, D('0.01'))
        self.assertEqual(why, 'improved', 'a spread of exactly the floor is eligible')
        self.assertEqual(improved.price, D('0.495'))

    def test_one_sided_book_is_refused(self):
        p = settings(config())
        empty = Book.make([], [('0.50', 500)], venue='SIG')
        self.assertEqual(improve(empty, self.entry('0.50'), p, D('0.01'))[1],
                         'two_sided_book_required')

    def test_refuses_when_the_ask_moved_or_route_is_wrong(self):
        p = settings(config())
        self.assertEqual(improve(book('0.46', '0.505'), self.entry('0.50'), p, D('0.01'))[1],
                         'ask_moved_since_selection')
        sell = Signal('yes', D('0.50'), 20, D('0.60'), D('0.09'), 'sell', 'overpriced_exit')
        self.assertEqual(improve(book('0.46', '0.50'), sell, p, D('0.01'))[1], 'not_a_plain_entry')

    def test_no_side_uses_the_complement_book(self):
        p = settings(config())
        # The NO book is the YES book reflected: a YES bid of 0.50 is a NO ask of 0.50,
        # and a YES ask of 0.54 is a NO bid of 0.46.
        improved, why = improve(book('0.50', '0.54'), self.entry('0.50', side='no'), p, D('0.01'))
        self.assertEqual(why, 'improved')
        self.assertEqual(improved.price, D('0.495'))


class Allowance(unittest.TestCase):
    def test_unfilled_orders_consume_nothing(self):
        db, p = database(), settings(config())
        fill(db, 'unfilled', '101', quantity='0', amount='0')
        remaining, spent = allowance(db, p)
        self.assertEqual(spent, D(0))
        self.assertEqual(remaining, p['allocation'])

    def test_only_improved_prices_are_charged(self):
        db, p = database(), settings(config())
        fill(db, 'a', '101', amount='100', arm_name='patient', priced='improved')
        fill(db, 'b', '102', amount='100', arm_name='control', priced='control')
        # Assigned to the patient arm but the route declined, so it paid the control price.
        fill(db, 'c', '103', amount='100', arm_name='patient', priced='control')
        _, spent = allowance(db, p)
        self.assertEqual(spent, D(100), 'only the price actually sent spends the allocation')

    def test_exhausted_allocation_blocks_the_route(self):
        db = database()
        fill(db, 'a', '101', amount='500')
        signal, detail = propose(db, {'sig_exchange_id': '101'}, config(),
                                 book('0.46', '0.50'), Improvement().entry('0.50'))
        self.assertIsNone(signal)
        self.assertEqual(detail['reason'], 'patient_allocation_exhausted')

    def test_order_larger_than_the_remaining_allocation_is_refused(self):
        db = database()
        fill(db, 'a', '101', amount='499')
        signal, detail = propose(db, {'sig_exchange_id': '101'}, config(),
                                 book('0.46', '0.50'), Improvement().entry('0.50'))
        self.assertIsNone(signal)
        self.assertEqual(detail['reason'], 'patient_allocation_too_small')


class Propose(unittest.TestCase):
    def test_control_arm_keeps_the_original_entry(self):
        db = database()
        signal, detail = propose(db, {'sig_exchange_id': '101'}, config(control_fraction='1'),
                                 book('0.46', '0.50'), Improvement().entry('0.50'))
        self.assertIsNone(signal, 'control arm must leave the control signal untouched')
        self.assertEqual(detail['status'], 'control')

    def test_disabled_and_absent_entry_are_inert(self):
        db = database()
        off = config(); off['patient_entry']['enabled'] = False
        self.assertEqual(propose(db, {'sig_exchange_id': '101'}, off,
                                 book('0.46', '0.50'), Improvement().entry('0.50'))[1]['reason'],
                         'disabled')
        self.assertEqual(propose(db, {'sig_exchange_id': '101'}, config(),
                                 book('0.46', '0.50'), None)[1]['reason'], 'no_control_entry')

    def test_eligible_proposal_records_the_improvement(self):
        db = database()
        signal, detail = propose(db, {'sig_exchange_id': '101'}, config(),
                                 book('0.46', '0.50'), Improvement().entry('0.50'))
        self.assertEqual(signal.price, D('0.495'))
        self.assertEqual(detail['status'], 'eligible')
        self.assertEqual(detail['improvement'], '0.005')
        self.assertEqual(detail['arm'], 'patient')
        self.assertEqual(detail['control_price'], '0.50')

    def test_patient_price_never_exceeds_the_control_price(self):
        db = database()
        for bid, ask in (('0.10', '0.20'), ('0.46', '0.50'), ('0.80', '0.90'), ('0.01', '0.99')):
            control = Improvement().entry(ask, reference='0.995')
            signal, _ = propose(db, {'sig_exchange_id': '101'}, config(), book(bid, ask), control)
            if signal is not None:
                self.assertLess(signal.price, control.price, (bid, ask))
                self.assertEqual(signal.quantity, control.quantity)


class Attribution(unittest.TestCase):
    def test_attach_requires_the_reservation_transaction(self):
        db = database()
        with self.assertRaises(RuntimeError):
            attach(db, {'idempotencyKey': 'k', 'exchangeId': '101'}, {'arm': 'patient'})

    def test_attach_inside_a_transaction_records_the_arm(self):
        db = database()
        db.execute('BEGIN')
        attach(db, {'idempotencyKey': 'k', 'exchangeId': '101'}, {'arm': 'patient', 'x': 1})
        db.commit()
        row = db.execute('SELECT * FROM patient_orders WHERE key=?', ('k',)).fetchone()
        self.assertEqual(row['arm'], 'patient')
        self.assertEqual(json.loads(row['detail'])['x'], 1)


class AgainstRealChoose(unittest.TestCase):
    """The improvement must compose with the real entry selector, not a hand-built Signal."""
    def test_choose_output_improves_cleanly(self):
        settings_ = config()['strategy']
        sig = Book.make([(D('0.46'), 500)], [(D('0.50'), 500)], venue='SIG')
        refs = [Book.make([(D('0.60'), 500)], [(D('0.62'), 500)], venue=v)
                for v in ('Kalshi', 'Polymarket')]
        control = choose(sig, refs, settings_, D('1000'))
        self.assertIsNotNone(control)
        self.assertEqual(control.price, D('0.50'))
        improved, why = improve(sig, control, settings(config()), D('0.01'))
        self.assertEqual(why, 'improved')
        self.assertLess(improved.price, control.price)
        self.assertGreater(improved.edge, control.edge)


class Counterfactual(unittest.TestCase):
    def test_marks_wide_and_narrow_spreads(self):
        p = settings(config())
        wide = counterfactual(book('0.46', '0.50'), 'yes', '0.50', p)
        self.assertTrue(wide['improvable'])
        self.assertEqual(wide['would_be_price'], '0.495')
        narrow = counterfactual(book('0.495', '0.50'), 'yes', '0.50', p)
        self.assertFalse(narrow['improvable'])
        self.assertEqual(narrow['reason'], 'spread_too_narrow_to_improve')

    def test_never_raises_on_a_broken_book(self):
        class Broken:
            bids = asks = None
            def complement(self):
                raise RuntimeError('unusable book')
        for bad in (Broken(), None, 'not a book'):
            result = counterfactual(bad, 'no', '0.50', settings(config()))
            self.assertIsNone(result['improvable'])
            self.assertIn('counterfactual_failed', result['reason'])


class ControlAttribution(unittest.TestCase):
    def payload(self):
        return dict(idempotencyKey='c1', exchangeId='101', action='buy', side='yes',
                    quantity=10, price=D('0.50'))

    def test_records_the_control_arm_with_its_counterfactual(self):
        db = database()
        db.execute('BEGIN')
        attach_control(db, self.payload(), config(control_fraction='1'),
                       ({'sig_exchange_id': '101'}, book('0.46', '0.50')))
        db.commit()
        row = db.execute('SELECT * FROM patient_orders').fetchone()
        self.assertEqual((row['arm'], row['priced'], row['improvable']), ('control', 'control', 1))

    def test_disabled_pilot_records_nothing(self):
        db = database()
        off = config(); off['patient_entry']['enabled'] = False
        db.execute('BEGIN')
        attach_control(db, self.payload(), off, ({'sig_exchange_id': '101'}, book('0.46', '0.50')))
        db.commit()
        self.assertEqual(db.execute('SELECT * FROM patient_orders').fetchall(), [])

    def test_a_failure_is_recorded_rather_than_raised(self):
        db = database()
        db.execute('BEGIN')
        attach_control(db, self.payload(), {'strategy': {}, 'execution': {'enabled': True},
                                            'patient_entry': {'bogus': 1}}, None)
        db.commit()
        row = db.execute('SELECT * FROM patient_orders').fetchone()
        self.assertEqual(row['arm'], 'unknown')
        self.assertEqual(json.loads(row['detail'])['status'], 'attribution_failed')

    def test_missing_book_is_marked_unknown_not_improvable(self):
        db = database()
        db.execute('BEGIN')
        attach_control(db, self.payload(), config(control_fraction='1'), None)
        db.commit()
        row = db.execute('SELECT * FROM patient_orders').fetchone()
        self.assertIsNone(row['improvable'])
        self.assertEqual(json.loads(row['detail'])['reason'], 'no_book_for_counterfactual')


class Report(unittest.TestCase):
    def test_absent_table_reads_as_not_started(self):
        plain = sqlite3.connect(':memory:')
        plain.row_factory = sqlite3.Row
        self.assertEqual(report(plain)['status'], 'not_started')

    def test_expired_orders_count_as_attempts_not_fills(self):
        db = database()
        fill(db, 'a', '101', quantity='10')
        fill(db, 'b', '101', quantity='0', amount='0')
        group = report(db)['by_group']['patient_improved']
        self.assertEqual((group['attempts'], group['fills']), (2, 1))
        self.assertEqual(group['fill_rate'], '0.5')

    def test_separates_price_improvement_from_the_later_price_path(self):
        db = database()
        # Patient fills 1c better but the market then drops a further 3c against it.
        fill(db, 'p', '101', quantity='100', price='0.49', arm_name='patient',
             priced='improved', markouts=[(300, '-5')])
        fill(db, 'c', '102', quantity='100', price='0.50', arm_name='control',
             priced='control', markouts=[(300, '-2')])
        db.execute("UPDATE patient_orders SET detail=? WHERE key='p'",(json.dumps({'control_price':'.50','benchmark_bid':'.48'}),))
        db.execute("UPDATE patient_orders SET detail=? WHERE key='c'",(json.dumps({'control_price':'.50','benchmark_bid':'.49'}),))
        result = report(db)
        self.assertEqual(result['by_group']['patient_improved']['mean_improvement_vs_own_quoted_ask'],'0.01')
        self.assertEqual(result['comparison']['bid_move_difference_per_share'][300],'-0.03')
        self.assertEqual(result['comparison']['markout_difference_per_share'][300],'-0.03')
        self.assertNotIn('price_improvement_per_share',result['comparison'])
        self.assertIn('Not yet evidence',result['note'])

    def test_unimprovable_controls_are_excluded_from_the_comparison(self):
        db = database()
        fill(db, 'p', '101', arm_name='patient', priced='improved', price='0.49')
        fill(db, 'c', '102', arm_name='control', priced='control', price='0.30', improvable=0)
        result = report(db)
        self.assertIn('control_not_improvable', result['by_group'])
        self.assertNotIn('price_improvement_per_share', result['comparison'],
                         'a narrow-spread control order is not a matched comparison')


class ThroughTheEngine(unittest.TestCase):
    """End to end: decide() swaps the price, reserve_order() re-derives it, attach() records it.

    These are the tests that matter. The unit tests above can agree with a mistake;
    this one places a real order through the real reservation path.
    """
    def engine(self, **patient):
        import test_active
        from election_bot.active_engine import ActiveEngine
        from election_bot.clients import contract_record, fingerprint
        from election_bot.demo import fixture
        from election_bot.state import Journal
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        conf, original, refs = fixture()
        conf['execution'] = dict(test_active.EXECUTION)
        conf['patient_entry'] = {**DEFAULTS, 'enabled': True, 'allocation': '5000',
                                 'control_fraction': '0', 'pilot_fraction':'1', **patient}
        self.sig = test_active.Broker(original)
        market = conf['markets'][0]
        market['sig_market_id'] = '2'
        market['contract_fingerprint'] = fingerprint(
            contract_record(self.sig.market('2'), market, refs.metadata(market)))
        self.journal = Journal(root/'j.db', 'test')
        self.addCleanup(self.journal.close)
        return ActiveEngine(conf, self.sig, refs, self.journal, root, live=True)

    def rows(self):
        return self.journal.db.execute('SELECT * FROM patient_orders').fetchall()

    def test_patient_arm_places_an_improved_price_and_records_the_arm(self):
        engine = self.engine()
        engine.cycle()
        self.assertEqual(len(self.sig.orders), 1)
        order = self.sig.orders[1]
        self.assertEqual(D(order['price']), D('.595'), 'one tick inside the 0.60 ask')
        self.assertEqual(order['action'], 'buy')
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['arm'], 'patient')
        self.assertEqual(json.loads(rows[0]['detail'])['control_price'], '0.60')

    def test_control_arm_places_the_unimproved_price_and_records_the_counterfactual(self):
        engine = self.engine(control_fraction='1')
        engine.cycle()
        self.assertEqual(D(self.sig.orders[1]['price']), D('.60'))
        rows = self.rows()
        self.assertEqual(len(rows), 1, 'the control arm needs a denominator')
        self.assertEqual(rows[0]['arm'], 'control')
        self.assertEqual(rows[0]['priced'], 'control')
        self.assertEqual(rows[0]['improvable'], 1, 'a 0.05 spread was improvable')
        self.assertEqual(json.loads(rows[0]['detail'])['would_be_price'], '0.595')

    def test_disabled_route_leaves_the_entry_untouched(self):
        engine = self.engine(enabled=False)
        engine.cycle()
        self.assertEqual(D(self.sig.orders[1]['price']), D('.60'))
        self.assertEqual(self.rows(), [])

    def test_unfilled_patient_order_consumes_no_allocation(self):
        engine = self.engine()
        self.sig.partial = 0                      # the order expires without trading
        engine.cycle()
        self.assertEqual(D(self.sig.orders[1]['price']), D('.595'))
        self.assertEqual(len(self.rows()), 1, 'the attempt is still attributed')
        remaining, spent = allowance(self.journal.db, settings(
            {'strategy': {}, 'execution': {'enabled': True},
             'patient_entry': {**DEFAULTS, 'enabled': True, 'allocation': '5000'}}))
        self.assertEqual(spent, D(0), 'a non-fill must not spend the pilot allocation')
        self.assertEqual(remaining, D(5000))

    def test_reservation_refuses_a_patient_payload_without_context(self):
        from election_bot.active_engine import ReservationRejected
        engine = self.engine()
        payload = dict(idempotencyKey='forged', exchangeId='2', action='buy',
                       side='yes', quantity=1, price=D('.595'))
        with self.assertRaises(ReservationRejected):
            engine.reserve_order(payload, D('.605'), self.sig.account(), self.sig.positions(),
                                 exit_reason=REASON)

    def test_patient_order_is_charged_once_filled(self):
        engine = self.engine()
        engine.cycle()
        _, spent = allowance(self.journal.db, settings(
            {'strategy': {}, 'execution': {'enabled': True},
             'patient_entry': {**DEFAULTS, 'enabled': True, 'allocation': '5000'}}))
        self.assertGreater(spent, D(0), 'a filled patient order consumes the allocation')


if __name__ == '__main__':
    unittest.main()
