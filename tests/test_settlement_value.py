import copy
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import Mock

from election_bot.active_engine import ActiveEngine
from election_bot.clients import contract_record, fingerprint
from election_bot.demo import fixture
from election_bot.drawdown import DEFAULTS, Drawdown, report as drawdown_report, settings as drawdown_settings
from election_bot.ledger import Ledger
from election_bot.settlement_review import report as review, format_report
from election_bot.settlement_value import exit_settings, fair_value, gate_exit
from election_bot.state import Journal
from election_bot.strategy import Book, D, Signal

import test_active

STRATEGY = {'max_age_seconds': 15, 'max_reference_spread': '.08', 'max_reference_disagreement': '.08'}


def ref(bid, ask, at=None, venue='Kalshi'):
    return Book.make([(bid, 100)], [(ask, 100)], source_at=at, observed_at=at, venue=venue)


class FairValueTests(unittest.TestCase):
    def test_average_midpoint_and_no_side(self):
        refs = [ref('.80', '.82'), ref('.84', '.86', venue='Polymarket')]
        self.assertEqual(fair_value(refs, 'yes', STRATEGY), D('.83'))
        self.assertEqual(fair_value(refs, 'no', STRATEGY), D('.17'))

    def test_rejects_stale_wide_disagreeing_or_missing(self):
        old = time.time()-60
        for refs in ([ref('.80', '.82', old), ref('.80', '.82')],
                     [ref('.70', '.82'), ref('.80', '.82')],
                     [ref('.60', '.62'), ref('.80', '.82')],
                     [ref('.80', '.82')]):
            with self.assertRaises(ValueError):
                fair_value(refs, 'yes', STRATEGY)

    def test_exit_settings_bounds(self):
        self.assertFalse(exit_settings({})['enabled'])
        for bad in ({'enabled': 'yes'}, {'margin': '.2'}, {'margin': '-.01'}, {'other': 1}):
            with self.assertRaises(ValueError):
                exit_settings({'settlement_exits': bad})


class GateTests(unittest.TestCase):
    def setUp(self):
        self.refs = [ref('.80', '.82'), ref('.80', '.82', venue='Polymarket')]
        self.on = exit_settings({'settlement_exits': {'enabled': True, 'margin': '.01'}})

    def sale(self, price, side='yes', reason='trend_profit_target'):
        return Signal(side, D(price), 10, D('.8'), D('0'), 'sell', reason)

    def test_disabled_and_buys_pass_through(self):
        off = exit_settings({})
        self.assertIsNotNone(gate_exit(self.sale('.70'), self.refs, STRATEGY, off, {}))
        buy = Signal('yes', D('.70'), 10, D('.8'), D('.1'))
        self.assertIs(gate_exit(buy, self.refs, STRATEGY, self.on, {}), buy)

    def test_sale_needs_bid_above_fair_plus_margin(self):
        detail = {}
        self.assertIsNone(gate_exit(self.sale('.81'), self.refs, STRATEGY, self.on, detail))
        self.assertEqual(detail['reason'], 'below_settlement_value')
        self.assertEqual(D(detail['settlement_value']['required_bid']), D('.82'))
        self.assertIsNotNone(gate_exit(self.sale('.82'), self.refs, STRATEGY, self.on, {}))
        self.assertIsNotNone(gate_exit(self.sale('.20', 'no'), self.refs, STRATEGY, self.on, {}))

    def test_missing_references_block_sale(self):
        detail = {}
        self.assertIsNone(gate_exit(self.sale('.95'), [], STRATEGY, self.on, detail))
        self.assertEqual(detail['reason'], 'settlement_value_unavailable')


class FairDrawdownTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)/'live.sqlite3'
        self.j = Journal(self.path, 'test')
        self.addCleanup(self.j.close)
        self.l = Ledger(self.j)
        self.c, _, _ = fixture()
        self.c['execution'] = dict(test_active.EXECUTION)
        self.c['drawdown_control'] = dict(DEFAULTS, enabled=True, pause_coins='10', resume_coins='5',
                                          valuation='reference_fair')
        self.g = Drawdown(self.l, self.c)

    def fill(self, q=100, price='.5'):
        p = dict(idempotencyKey='k%d' % q, exchangeId='2', quantity=q, price=price, action='buy', side='yes')
        self.j.reserve(p, D(q)*(D(price)+D('.01')))
        self.l.record(p, q, price, '.01')

    def test_settings_validate_valuation(self):
        for bad in ({'valuation': 'mid'}, {'max_fair_mark_age_seconds': 10}):
            c = copy.deepcopy(self.c)
            c['drawdown_control'].update(bad)
            with self.assertRaises(ValueError):
                drawdown_settings(c)

    def test_values_at_reference_midpoint_without_buffer(self):
        self.fill()
        book = Book.make([('.40', 200)], [('.42', 200)])
        self.g.observe('2', book)                       # SIG-only: no fair mark yet
        self.assertEqual(self.g.status()['reason'], 'valuation_incomplete')
        self.g.observe('2', book, [ref('.60', '.62'), ref('.60', '.62', venue='Polymarket')])
        s = self.g.status()
        self.assertEqual(s['valuation'], 'reference_fair')
        self.assertEqual(s['portfolio_pnl'], D('11'))    # 100*(.61-.50); bid-based would be -12
        self.assertTrue(s['buy_allowed'])
        self.g.observe('2', book, [ref('.49', '.51'), ref('.49', '.51', venue='Polymarket')])
        s = self.g.status()
        self.assertEqual(s['drawdown'], D('11'))
        self.assertTrue(s['paused'])
        # The bid-based state is untouched, so switching back does not inherit this peak.
        self.assertEqual(self.j.db.execute('SELECT peak,paused FROM drawdown_state').fetchone()[:], ('0', 0))
        self.assertEqual(drawdown_report(self.path, self.c)['valuation'], 'reference_fair')

    def test_inventory_change_invalidates_fair_mark(self):
        self.fill()
        self.g.observe('2', Book.make([('.40', 200)], [('.42', 200)]),
                       [ref('.60', '.62'), ref('.60', '.62', venue='Polymarket')])
        self.fill(50)
        self.assertEqual(self.g.status()['missing'][0]['reason'], 'inventory_changed')

    def test_default_valuation_unchanged(self):
        c = copy.deepcopy(self.c)
        del c['drawdown_control']['valuation']
        self.assertEqual(drawdown_settings(c)['valuation'], 'sig_bid')


class EngineFairRefreshTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config, original, self.refs = fixture()
        self.config['execution'] = dict(test_active.EXECUTION)
        self.config['drawdown_control'] = dict(DEFAULTS, enabled=True, valuation='reference_fair')
        self.sig = test_active.Broker(original)
        mapping = self.config['markets'][0]
        mapping['sig_market_id'] = '2'
        mapping['contract_fingerprint'] = fingerprint(contract_record(self.sig.market('2'), mapping,
                                                                      self.refs.metadata(mapping)))
        self.journal = Journal(Path(self.temp.name)/'journal.db', 'test')
        self.addCleanup(self.journal.close)
        self.engine = ActiveEngine(self.config, self.sig, self.refs, self.journal, self.temp.name, live=True)

    def test_refresh_fetches_references_for_fair_marks(self):
        self.engine.cycle()                      # buys; the mark taken before the buy had no holding
        self.assertTrue(self.engine.ledger.inventory(refresh=True)[0])
        self.assertEqual(self.engine.drawdown.status()['reason'], 'valuation_incomplete')
        self.refs.books = Mock(wraps=self.refs.books)
        self.engine.refresh_drawdown()
        self.refs.books.assert_called()
        self.assertEqual(self.journal.db.execute('SELECT COUNT(*) FROM drawdown_fair_marks').fetchone()[0], 1)
        self.assertEqual(self.engine.drawdown.status()['valuation'], 'reference_fair')


def snap(sid, at, sig, k, p, exchange='2'):
    quotes = []
    for venue, (bid, ask) in zip(('SIG', 'Kalshi', 'Polymarket'), (sig, k, p)):
        quotes.append({'venue': venue, 'bid': [bid, 100], 'ask': [ask, 100], 'observed_at': at, 'source_at': at,
                       'timestamp_basis': 'fetch_time', 'quality': {'issues': []}})
    return {'version': 1, 'snapshot_id': sid, 'exchange': exchange, 'phase': 'scan', 'captured_at': at,
            'quotes': quotes}


class SettlementReviewTests(unittest.TestCase):
    def test_current_value_sales_and_pause_replay(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        path = Path(temp.name)/'live.sqlite3'
        j = Journal(path, 'test')
        Ledger(j)
        now = time.time()
        t0 = now-3*3600
        rows = [('b', '2', 'buy', 'yes', '100', '.50', '.01', t0), ('s', '2', 'sell', 'yes', '20', '.55', '.01', t0+3600)]
        j.db.executemany('INSERT INTO executions VALUES (?,?,?,?,?,?,?,?)', rows)
        events = [snap('a', t0+1, ('.50', '.52'), ('.60', '.62'), ('.60', '.62')),
                  snap('c', t0+3590, ('.55', '.57'), ('.70', '.72'), ('.70', '.72')),
                  snap('d', now-60, ('.45', '.47'), ('.80', '.82'), ('.80', '.82'))]
        for e in events:
            j.db.execute('INSERT INTO events VALUES (?,?,?)', (e['captured_at'], 'quote_snapshot', json.dumps(e)))
        j.db.commit()
        j.close()
        r = review(path, hours=3.5, now=now)
        # Realized 20*(.55-.50) plus 80 held at reference mid .81 against .50 cost; no buffer.
        self.assertEqual(D(r['current']['pnl']['reference_fair']), D('1.0')+80*(D('.81')-D('.50')))
        self.assertEqual(D(r['current']['pnl']['sig_bid']), D('1.0')+80*(D('.45')-D('.50')))
        self.assertEqual(r['sales']['would_block'], 1)
        self.assertEqual(D(r['sales']['settlement_value_given_up_by_blocked_sales']), 20*(D('.71')-D('.55')))
        self.assertIn('SETTLEMENT-VALUE REVIEW', format_report(r))


if __name__ == '__main__':
    unittest.main()


class EngineExitGateTests(unittest.TestCase):
    """Buy at .60; references sit at fair .745. A fixed profit target would sell at .70."""
    def engine(self, gate):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        config, original, refs = fixture()
        config['execution'] = dict(test_active.EXECUTION, profit_target_enabled=True,
                                   profit_exit_mode='fixed', reentry_cooldown_seconds=60)
        if gate is not None:
            config['settlement_exits'] = gate
        sig = test_active.Broker(original)
        mapping = config['markets'][0]
        mapping['sig_market_id'] = '2'
        mapping['contract_fingerprint'] = fingerprint(contract_record(sig.market('2'), mapping, refs.metadata(mapping)))
        journal = Journal(Path(temp.name)/'journal.db', 'test')
        self.addCleanup(journal.close)
        engine = ActiveEngine(config, sig, refs, journal, temp.name, live=True)
        engine.cycle()
        journal.db.execute('UPDATE orders SET created=?', (time.time()-1000,))
        journal.db.commit()
        sig.prices['2'] = ('.70', '.71')
        engine.cycle()
        return sig, journal

    def test_profit_target_sells_without_gate(self):
        sig, _ = self.engine(None)
        self.assertEqual([o['action'] for o in sig.orders.values()], ['buy', 'sell'])

    def test_gate_blocks_sale_below_settlement_value(self):
        sig, journal = self.engine({'enabled': True, 'margin': '0.01'})
        self.assertEqual([o['action'] for o in sig.orders.values()], ['buy'])
        decisions = [json.loads(r[0]) for r in journal.db.execute(
            "SELECT detail FROM events WHERE kind='decision'")]
        self.assertTrue(any(d['exit_check'].get('reason') == 'below_settlement_value' for d in decisions))

    def test_invalid_gate_config_is_rejected_at_startup(self):
        from election_bot.engine import validate_config
        config, _, _ = fixture()
        config['settlement_exits'] = {'enabled': 'true'}
        with self.assertRaises(ValueError):
            validate_config(config)


class RiskExitBlockTests(unittest.TestCase):
    def test_gate_blocks_sig_only_risk_exits_unless_disabled(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        config, original, refs = fixture()
        config['execution'] = dict(test_active.EXECUTION)
        config['risk_exit'] = {'enabled': True}
        sig = test_active.Broker(original)
        journal = Journal(Path(temp.name)/'journal.db', 'test')
        self.addCleanup(journal.close)
        engine = ActiveEngine(config, sig, refs, journal, temp.name, live=True)
        engine.risk_exit.propose = Mock()
        engine.settlement_exits = exit_settings({'settlement_exits': {'enabled': True}})
        self.assertEqual(engine.risk_exit_decision(config['markets'][0], []), (None, None))
        engine.risk_exit.propose.assert_not_called()
        engine.settlement_exits = exit_settings({'settlement_exits': {'enabled': True, 'block_risk_exits': False}})
        engine.held = Mock(return_value=(0, 0))
        self.assertEqual(engine.risk_exit_decision(config['markets'][0], []), (None, None))
        engine.held.assert_called()
