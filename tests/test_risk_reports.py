import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock

from election_bot.clients import APIError
from election_bot.demo import fixture
from election_bot.ledger import Ledger
from election_bot.pair_arbitrage import evaluate, groups, report as pair_report, scan, format_report as pair_format
from election_bot.state import Journal
from election_bot.strategy import Book, D
from election_bot.swing_risk import dem_probability, format_report, report, shifted


def book(bid, ask, size=100):
    return Book.make([(bid, size)], [(ask, size)], venue='SIG')


def market(mid, title, exchange, status='open'):
    return {'id': mid, 'title': title, 'status': status, 'isComposite': False, 'isMultiOutcome': False,
            'exchanges': [{'id': exchange, 'option': 'YES'}]}


class PairTests(unittest.TestCase):
    def test_groups_party_markets_by_race(self):
        found = groups([market('1', 'Will the Democratic Party win the Ohio Senate?', '11'),
                        market('2', 'Will the Republican Party win the Ohio Senate?', '12'),
                        market('3', 'Will the Independent Party win the Ohio Senate?', '13'),
                        market('4', 'Will the Democratic Party win the Iowa Senate?', '14'),
                        market('5', 'Will the Republican Party win the Iowa Senate?', '15', status='closed'),
                        market('6', 'Something else?', '16')])
        self.assertEqual(found, {'Ohio Senate': {'Democratic': ('1', '11'), 'Republican': ('2', '12'),
                                                 'Independent': ('3', '13')}})

    def test_basket_economics(self):
        r = evaluate({'Democratic': book('.55', '.57', 40), 'Republican': book('.48', '.50', 70)})
        self.assertEqual(D(r['no_basket']['guaranteed_edge_per_set']), D('.03'))
        self.assertEqual(D(r['no_basket']['cost_per_set']), D('.97'))
        self.assertEqual(D(r['no_basket']['sets_available']), 40)
        self.assertEqual(D(r['yes_basket']['edge_per_set_if_a_listed_party_wins']), D('-.07'))
        three = evaluate({'D': book('.30', '.32'), 'R': book('.30', '.32'), 'I': book('.30', '.32')})
        self.assertEqual(D(three['no_basket']['cost_per_set']), D('2.10'))   # pays at least 2
        self.assertEqual(D(three['yes_basket']['edge_per_set_if_a_listed_party_wins']), D('.04'))
        self.assertEqual(evaluate({'D': Book.make([], [('.5', 1)]), 'R': book('.4', '.5')})['status'], 'one_sided_book')

    def test_scan_paces_reads_and_stops_on_api_error(self):
        sig = Mock()
        sig.markets.return_value = [market('1', 'Will the Democratic Party win the Ohio Senate?', '11'),
                                    market('2', 'Will the Republican Party win the Ohio Senate?', '12'),
                                    market('3', 'Will the Democratic Party win the Iowa Senate?', '13'),
                                    market('4', 'Will the Republican Party win the Iowa Senate?', '14')]
        sig.book.side_effect = [book('.55', '.57'), book('.48', '.50'),
                                book('.60', '.62'), APIError('429', status=429, method='GET')]
        sleeps = []
        results = scan(sig, interval=2.5, sleep=sleeps.append, clock=lambda: 0.0)
        self.assertEqual(sleeps, [2.5, 2.5, 2.5])
        self.assertEqual([(r['race'], r['status']) for r in results],
                         [('Iowa Senate', 'ok'), ('Ohio Senate', 'api_error_scan_stopped')])
        out = pair_report(results)
        self.assertEqual([r['race'] for r in out['no_basket_opportunities']], ['Iowa Senate'])  # .55+.48 bids
        self.assertIn('PARTY-PAIR', pair_format(out))


    def test_transient_error_skips_race_and_continues(self):
        sig = Mock()
        sig.markets.return_value = [market('1', 'Will the Democratic Party win the Iowa Senate?', '11'),
                                    market('2', 'Will the Republican Party win the Iowa Senate?', '12'),
                                    market('3', 'Will the Democratic Party win the Ohio Senate?', '13'),
                                    market('4', 'Will the Republican Party win the Ohio Senate?', '14')]
        sig.book.side_effect = [APIError('GET failed (URLError)', method='GET'),
                                book('.55', '.57'), book('.48', '.50')]
        results = scan(sig, interval=0, sleep=lambda s: None, clock=lambda: 0.0)
        self.assertEqual([(r['race'], r['status']) for r in results],
                         [('Iowa Senate', 'read_error'), ('Ohio Senate', 'ok')])


class SwingTests(unittest.TestCase):
    def test_probability_orientation_and_shift(self):
        self.assertEqual(dem_probability(D('.9'), 1), D('.9'))
        self.assertEqual(dem_probability(D('.9'), -1), D('.1'))
        self.assertAlmostEqual(shifted(D('.5'), 0, 6), .5)
        self.assertGreater(shifted(D('.9'), 2, 6), .9)
        self.assertLess(shifted(D('.9'), -6, 6), .9)

    def test_report_values_holdings_under_swings(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        path = Path(temp.name)/'live.sqlite3'
        j = Journal(path, 'test')
        Ledger(j)
        now = time.time()
        j.db.execute('INSERT INTO executions VALUES (?,?,?,?,?,?,?,?)', ('b', '2', 'buy', 'yes', '100', '.80', '.01', now-600))
        quotes = [{'venue': v, 'bid': [b, 100], 'ask': [a, 100], 'observed_at': now-60, 'source_at': now-60,
                   'timestamp_basis': 'fetch_time', 'quality': {'issues': []}}
                  for v, (b, a) in (('SIG', ('.80', '.82')), ('Kalshi', ('.89', '.91')), ('Polymarket', ('.89', '.91')))]
        snap = {'version': 1, 'snapshot_id': 's', 'exchange': '2', 'phase': 'scan', 'captured_at': now-60, 'quotes': quotes}
        j.db.execute('INSERT INTO events VALUES (?,?,?)', (now-60, 'quote_snapshot', json.dumps(snap)))
        j.db.commit()
        j.close()
        config, _, _ = fixture()
        config['markets'][0]['exposure_sign'] = 1
        r = report(path, config, now)
        self.assertEqual(r['positions'], 1)
        self.assertEqual(D(r['expected_pnl_no_swing']).quantize(D('.01')), D('10.00'))   # 100*(.90-.80)
        row = r['scenarios']['6']
        self.assertLess(D(row['-6']), D(row['0']))
        self.assertGreater(D(row['6']), D(row['0']))
        self.assertEqual(r['direction']['shares_paying_if_democrat_wins'], '100')
        self.assertIn('SWING', format_report(r))


class DashboardTests(unittest.TestCase):
    def test_reports_without_journal_and_with_basket(self):
        from election_bot.dashboard import report as dash, format_report as dash_format
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        config, _, _ = fixture()
        runtime = Path(temp.name)
        self.assertEqual(dash(runtime, config)['status'], 'no live journal')
        j = Journal(runtime/'live.sqlite3', 'test')
        Ledger(j)
        from election_bot import pair_baskets
        pair_baskets.initialize(j.db)
        now = time.time()
        for key, ex, role, price in (('a', '2', 'anchor', '.43'), ('b', '3', 'leg', '.55')):
            j.db.execute('INSERT INTO executions VALUES (?,?,?,?,?,?,?,?)', (key, ex, 'buy', 'no', '10', price, '.01', now))
            j.db.execute('INSERT INTO basket_orders VALUES (?,?,?,?)', (key, 'r', ex, role))
        j.db.commit()
        j.close()
        r = dash(runtime, config, now)
        self.assertEqual(r['baskets']['matched_sets'], 10)
        self.assertAlmostEqual(r['baskets']['guaranteed_profit'], 0.2, places=6)
        self.assertIn('DASHBOARD', dash_format(r))


class DashboardValueTests(unittest.TestCase):
    def test_values_split_bid_fair_and_unmarked(self):
        import sqlite3
        from election_bot.dashboard import values
        db = sqlite3.connect(':memory:')
        db.executescript('''CREATE TABLE executions (exchange TEXT, side TEXT, action TEXT, quantity TEXT, price TEXT,
                                buffer TEXT, at REAL);
            CREATE TABLE drawdown_marks (exchange TEXT, side TEXT, quantity TEXT, bid_value TEXT);
            CREATE TABLE drawdown_fair_marks (exchange TEXT, side TEXT, quantity TEXT, fair_value TEXT);''')
        db.executemany('INSERT INTO executions VALUES (?,?,?,?,?,?,?)',
                       [('1', 'yes', 'buy', '100', '.50', '.01', 1), ('1', 'yes', 'sell', '20', '.60', '.01', 2),
                        ('2', 'no', 'buy', '10', '.30', '.01', 3)])
        db.execute("INSERT INTO drawdown_marks VALUES ('1','yes','80','36')")
        db.execute("INSERT INTO drawdown_fair_marks VALUES ('1','yes','80','56')")
        v = values(db)
        self.assertEqual((D(v['realized']), D(v['cost'])), (D('2'), D('43')))
        self.assertEqual(D(v['pnl_at_sig_bid']), D('-2'))         # 2 + (36+3) - 43
        self.assertEqual(D(v['pnl_at_reference_fair']), D('18'))  # 2 + (56+3) - 43
        self.assertEqual((v['holdings'], v['no_bid_mark'], v['no_fair_mark']), (2, 1, 1))
