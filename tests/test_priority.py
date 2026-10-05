import json
from pathlib import Path
import statistics
import tempfile
import unittest
from unittest.mock import patch

from election_bot.performance import scan_summary
from election_bot.scanner import ScanInterest, Scanner
from election_bot.strategy import Book, D


def owned_hint():
    return {'score': 100, 'reasons': ['owned_position'], 'not_before': 0}


def simulate_scans(priority, selections=500):
    """Scheduling-only comparison: 107 races, two simulated seconds per check."""
    with tempfile.TemporaryDirectory() as folder:
        now = [10000.0]
        with patch('election_bot.scanner.time.time', side_effect=lambda: now[0]):
            scan = Scanner([{'name': str(i)} for i in range(107)], folder)
            hints = {str(i): owned_hint() for i in (90, 91, 92)} if priority else None
            visits, intervals, previous = [], [], {}
            while len(visits) < selections:
                for mapping in scan.batch({}, 12, hints):
                    name = mapping['name']
                    visits.append(name)
                    if name in ('90', '91', '92') and name in previous:
                        intervals.append(now[0]-previous[name])
                    previous[name] = now[0]
                    now[0] += 2
                    if len(visits) >= selections:
                        break
            return {'unique_races': len(set(visits)),
                    'held_median_revisit_seconds': statistics.median(intervals),
                    'selections': len(visits)}


class PriorityScannerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.markets = [{'name': str(i)} for i in range(17)]
        self.now = 10000.0
        self.clock = patch('election_bot.scanner.time.time', side_effect=lambda: self.now)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.scan = Scanner(self.markets, self.temp.name)

    def test_news_then_score_then_oldest_with_regular_slots(self):
        hints = {'15': {'score': 200, 'reasons': ['near_entry']}, '16': owned_hint()}
        visits, lanes = [], []
        for m in self.scan.batch({'14': ['news']}, 6, hints):
            visits.append(m['name']); lanes.append(self.scan.selection['lane'])
        self.assertEqual(visits, ['14', '0', '15', '1', '16', '2'])
        self.assertEqual(lanes, ['priority', 'regular']*3)

    def test_priority_waits_for_revisit_interval_and_order_cooldown(self):
        self.scan.last_visits['15'] = self.now-10
        hints = {'15': owned_hint(), '16': dict(owned_hint(), not_before=self.now+10)}
        self.assertEqual(next(self.scan.batch({}, 1, hints))['name'], '0')
        self.assertEqual(self.scan.selection['lane'], 'regular')
        self.now += 31
        self.assertEqual(next(self.scan.batch({}, 1, hints))['name'], '16')

    def test_no_starvation_even_with_early_returns_and_restarts(self):
        hints = {str(i): owned_hint() for i in range(17)}
        visited, lanes = [], []
        for _ in range(34):
            m = next(self.scan.batch({'16': ['constant news']}, 12, hints))
            visited.append(m['name']); lanes.append(self.scan.selection['lane'])
            self.now += 31
            self.scan = Scanner(self.markets, self.temp.name)
        self.assertEqual(set(visited), {str(i) for i in range(17)})
        self.assertFalse(any(a == b == 'priority' for a, b in zip(lanes, lanes[1:])))

    def test_equal_priority_uses_oldest_visit(self):
        self.scan.last_visits.update({'15': self.now-100, '16': self.now-200})
        self.assertEqual(next(self.scan.batch({}, 1, {'15': owned_hint(), '16': owned_hint()}))['name'], '16')

    def test_batch_has_no_duplicates_with_all_markets_prioritized(self):
        names = [m['name'] for m in self.scan.batch({}, 17, {str(i): owned_hint() for i in range(17)})]
        self.assertEqual(len(set(names)), 17)

    def test_expiration_is_rechecked_during_long_batch(self):
        hints = {'16': {'score': 200, 'reasons': ['near_entry'], 'expires': {'near_entry': self.now+1}}}
        self.scan.priority_turn = False
        iterator = self.scan.batch({}, 2, hints)
        self.assertEqual(next(iterator)['name'], '0')
        self.now += 2
        self.assertEqual(next(iterator)['name'], '1')
        self.assertEqual(self.scan.selection['lane'], 'regular')

    def test_timing_persists_but_old_cursor_and_corrupt_times_are_safe(self):
        self.assertIsNone(self.scan.record_quote('0'))
        next(self.scan.batch({}, 1))
        self.now += 40
        self.scan = Scanner(self.markets, self.temp.name)
        self.assertEqual(self.scan.record_quote('0'), 40)
        self.assertEqual(self.scan.last_visits['0'], self.now-40)
        path = Path(self.temp.name)/'scan_cursor.json'
        path.write_text(json.dumps({'next_market': '5', 'priority_turn': True,
            'last_visits': {'0': 'bad', '1': float('nan'), '2': self.now+1000, 'deleted': 1},
            'last_quotes': []}))
        self.scan = Scanner(self.markets, self.temp.name)
        self.assertEqual(self.scan.last_visits, {})
        self.assertEqual(self.scan.last_quotes, {})
        self.assertEqual(next(self.scan.batch({}, 1))['name'], '5')

    def test_synthetic_priority_improves_held_revisits_and_keeps_coverage(self):
        ordinary, priority = simulate_scans(False), simulate_scans(True)
        self.assertEqual(ordinary['unique_races'], 107)
        self.assertEqual(priority['unique_races'], 107)
        self.assertLess(priority['held_median_revisit_seconds'], ordinary['held_median_revisit_seconds'])

    def test_policy_change_resets_timing_only_and_restart_keeps_same_policy_timing(self):
        self.scan.set_policy('news_round_robin')
        list(self.scan.batch({}, 4))
        self.scan.record_quote('0')
        self.now += 40
        self.scan = Scanner(self.markets, self.temp.name)
        self.scan.set_policy('news_round_robin')
        self.assertEqual(self.scan.record_quote('0'), 40)
        self.scan.set_policy('priority_v1')
        self.assertIsNone(self.scan.record_quote('0'))
        self.assertEqual(next(self.scan.batch({}, 1, {}))['name'], '4')


class InterestTests(unittest.TestCase):
    settings = {'minimum_edge': '.05', 'cost_buffer_per_share': '.01'}

    def refs(self, bid='.73'):
        return [Book.make([(bid, 100)], [(D(bid)+D('.02'), 100)]) for _ in range(2)]

    def check(self, **changes):
        return dict({'side': 'yes', 'reason': 'price_gap', 'available': '50',
                     'reference_bid': '.73', 'ask': '.69'}, **changes)

    def test_near_entry_is_hint_only_and_expires(self):
        interest = ScanInterest()
        interest.observe('a', [self.check()], self.refs(), self.settings, 0, now=100)
        self.assertEqual(interest.hints(set(), {}, now=101)['a']['reasons'], ['near_entry'])
        self.assertEqual(interest.hints(set(), {}, now=281)['a']['score'], 0)

    def test_ineligible_depth_budget_and_opposite_inventory_do_not_prioritize_entry(self):
        cases = [(self.check(reason='reference_depth'), 0), (self.check(available='0'), 0),
                 (self.check(ask='.70'), 0), (self.check(), -10)]
        for check, held in cases:
            with self.subTest(check=check, held=held):
                interest = ScanInterest()
                interest.observe('a', [check], self.refs(), self.settings, held, now=100)
                self.assertEqual(interest.hints(set(), {}, now=101)['a']['score'], 0)

    def test_observed_reference_move_requires_recent_previous_quote(self):
        interest = ScanInterest()
        interest.observe('a', [], self.refs(), self.settings, 0, now=100)
        interest.observe('a', [], self.refs('.75'), self.settings, 0, now=120)
        self.assertEqual(interest.hints(set(), {}, now=121)['a']['reasons'], ['reference_move'])
        interest.observe('a', [], self.refs('.80'), self.settings, 0, now=500)
        self.assertEqual(interest.hints(set(), {}, now=501)['a']['score'], 0)

    def test_invalid_scan_clears_price_hints_but_owned_position_remains(self):
        interest = ScanInterest()
        interest.observe('a', [self.check()], self.refs(), self.settings, 0, now=100)
        interest.invalidate('a')
        self.assertEqual(interest.hints({'a'}, {}, now=101)['a']['reasons'], ['owned_position'])
        self.assertEqual(ScanInterest().hints(set(), {}, now=101), {})


class ScanReportingTests(unittest.TestCase):
    def test_visits_are_separate_from_quotes_and_policies_are_not_mixed(self):
        visits = [dict(market='a', scan_policy='priority_v1', lane='priority',
                       priority_reasons=['owned_position'], revisit_seconds=v) for v in (None, 30, 50)]
        quotes = [dict(visits[0], quote_revisit_seconds=None), dict(visits[2], quote_revisit_seconds=80)]
        visits.append(dict(market='a', scan_policy='news_round_robin', lane='regular', revisit_seconds=200))
        result = scan_summary(visits, quotes)['policies']
        self.assertEqual(result['priority_v1']['scheduled_visits'], 3)
        self.assertEqual(result['priority_v1']['successful_quote_checks'], 2)
        self.assertEqual(result['priority_v1']['visit_intervals']['median_seconds'], 40)
        self.assertEqual(result['priority_v1']['quote_intervals']['median_seconds'], 80)
        self.assertEqual(result['news_round_robin']['quote_intervals']['samples'], 0)
