import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock

from election_bot.active_engine import ActiveEngine
from election_bot.clients import APIError
from election_bot.demo import fixture
from election_bot.election_night import ElectionNight
from election_bot.gap_radar import GapRadar, Rules, rank, screen, settings
from election_bot.scanner import ScanInterest, Scanner
from election_bot.state import Journal
from election_bot.strategy import Book, D

import test_active
from test_election_night import live_window

STRATEGY = {'minimum_edge': '0.05', 'cost_buffer_per_share': '0.005', 'max_reference_spread': '0.08',
            'max_reference_disagreement': '0.08', 'max_age_seconds': 15, 'min_reference_depth': '20',
            'max_shares_per_order': 1500}
CONFIG = {'strategy': STRATEGY, 'execution': {'exit_edge': '0.02'},
          'limits': {'per_order': '1000', 'per_market': '7500'}}


def book(bid, ask, bid_size=100, ask_size=100, venue='Kalshi', at=None):
    return Book.make([(bid, bid_size)], [(ask, ask_size)], source_at=at, observed_at=at, venue=venue)


def refs(kalshi=('.97', '.98'), poly=('.96', '.98'), size=300, at=None):
    return [book(*kalshi, size, size, 'Kalshi', at), book(*poly, size, size, 'Polymarket', at)]


def rules(night=None, funding=None):
    return Rules(settings({'gap_radar': {'enabled': True}}), CONFIG, night, funding)


def target(race='other', ask='0.90', edge='0.06', need='500'):
    return {'race': race, 'ask': D(ask), 'edge': D(edge), 'need_coins': D(need)}


def funding(targets, called_only=False):
    return {'targets': targets, 'max_giveup': D('0.02'), 'min_gain': D('0.03'), 'called_only': called_only}


def run(quote, books, held=0, basket=0, room='7500', headroom=None, rule=None, race='this'):
    return screen(quote, books, D(held), D(basket), D(room), headroom, rule or rules(), race)


class SettingsTests(unittest.TestCase):
    def test_disabled_by_default_and_validated(self):
        p = settings({})
        self.assertEqual((p['enabled'], p['seconds'], p['night_seconds'], p['max_flags']), (False, 60, 20, 8))
        self.assertEqual((p['margin'], p['min_value'], p['max_age_seconds']), (D('0.01'), D('5'), 120))
        for bad in ([], {'enabled': 'yes'}, {'seconds': 5}, {'night_seconds': 601}, {'max_flags': 0},
                    {'margin': '0.2'}, {'min_value': '-1'}, {'max_age_seconds': 10}, {'unknown': 1}):
            with self.assertRaises(ValueError, msg=bad):
                settings({'gap_radar': bad})

    def test_cadence_and_flag_lifetime(self):
        radar = GapRadar({'gap_radar': {'enabled': True}})
        self.assertTrue(radar.due(1000, False))
        radar.last_run = 1000
        self.assertFalse(radar.due(1059, False))
        self.assertTrue(radar.due(1060, False))
        self.assertTrue(radar.due(1020, True))
        self.assertEqual((radar.ttl(False), radar.ttl(True)), (150, 70))
        self.assertFalse(GapRadar({}).due(10**9, True))


class ScreenTests(unittest.TestCase):
    def test_entry_uses_lower_reference_bid_reference_depth_and_room(self):
        # .96 (lower bid) - .90 - .005 = 5.5c; size is the smaller reference bid depth.
        books = [book('.97', '.98', 300, 300, 'Kalshi'), book('.96', '.98', 200, 300, 'Polymarket')]
        c = run((0.89, 0.90), books)
        self.assertEqual((c['route'], c['side'], c['edge'], c['shares']), ('entry', 'yes', D('0.055'), 200))
        self.assertEqual(c['value'], D('11'))
        # Room and exposure headroom cap the order the bot could place.
        self.assertEqual(run((0.89, 0.90), books, room='90')['shares'], 99)          # 90 / .905
        self.assertEqual(run((0.89, 0.90), books, headroom={'yes': 50, 'no': 0})['shares'], 50)
        self.assertIsNone(run((0.89, 0.90), books, room='0'))

    def test_entry_threshold_includes_margin_and_depth_gate(self):
        self.assertEqual(run((0.90, 0.905), refs(('.95', '.97'), ('.95', '.97')))['edge'], D('0.04'))
        self.assertIsNone(run((0.90, 0.91), refs(('.95', '.97'), ('.95', '.97'))))   # 3.5c
        thin = [book('.97', '.98', 19, 300, 'Kalshi'), book('.97', '.98', 300, 300, 'Polymarket')]
        self.assertIsNone(run((0.89, 0.90), thin))                                    # depth gate

    def test_no_side_and_never_buys_against_inventory(self):
        books = refs(('.02', '.04'), ('.02', '.04'))
        c = run((0.10, 0.11), books)                    # NO ask .90 vs NO bids .96
        self.assertEqual((c['route'], c['side'], c['edge']), ('entry', 'no', D('0.055')))
        # Holding YES: the NO buy is skipped, and the YES holding is overpriced on SIG
        # (a race called against it): .10 bid - .04 ask - .005 = 5.5c, sell first.
        c = run((0.10, 0.11), books, held=100)
        self.assertEqual((c['route'], c['side'], c['edge'], c['shares']), ('exit', 'yes', D('0.055'), 100))

    def test_exit_never_counts_basket_shares(self):
        books = refs(('.96', '.97'), ('.96', '.97'))   # NO bid on SIG .15 vs NO asks .04
        self.assertEqual(run((0.80, 0.85), books, held=-100)['shares'], 100)
        self.assertEqual(run((0.80, 0.85), books, held=-100, basket=40)['shares'], 60)
        self.assertIsNone(run((0.80, 0.85), books, held=-100, basket=100))

    def test_election_night_entry_only_inside_window(self):
        night = ElectionNight({'strategy': STRATEGY, 'election_night': live_window()})
        books = refs(('.97', '.99'), ('.97', '.99'))    # called; fair .98
        # Ordinary entry fails (.97 - .95 - .005 = 1.5c); the night rule needs 2c - 1c margin.
        self.assertIsNone(run((0.94, 0.95), books))
        c = run((0.94, 0.95), books, rule=rules(night))
        self.assertEqual((c['route'], c['side'], c['edge'], c['shares']), ('night_entry', 'yes', D('0.03'), 1047))
        # Exposure caps are bypassed for called races by default.
        self.assertEqual(run((0.94, 0.95), books, headroom={'yes': 10, 'no': 10}, rule=rules(night))['shares'], 1047)

    def test_funding_sale_for_a_pending_coin_blocked_buy(self):
        books = refs(('.96', '.98'), ('.96', '.98'))   # fair .97; SIG bid .95 gives up 2c
        rule = rules(funding=funding([target()]))
        c = run((0.95, 0.955), books, held=1000, rule=rule)
        # Per share sold: .95/.90 x 6c - 2c = 4.33c; covers the 500-coin need (527 shares).
        self.assertEqual((c['route'], c['side'], c['shares']), ('funding', 'yes', 527))
        self.assertAlmostEqual(float(c['edge']), 0.95/0.90*0.06-0.02, places=6)
        self.assertIsNone(run((0.95, 0.955), books, held=1000, rule=rules(funding=funding([target(race='this')]))))
        self.assertIsNone(run((0.935, 0.94), books, held=1000, rule=rule))        # gives up 3.5c > 2c + margin
        self.assertIsNone(run((0.95, 0.955), books, held=1000, rule=rules(funding=funding([target()], True))))

    def test_unusable_references_are_skipped(self):
        quote = (0.89, 0.90)
        self.assertIsNone(run(quote, refs(at=time.time()-200)))                                # stale
        self.assertIsNone(run(quote, refs(('.80', '.98'), ('.80', '.98'))))                    # wide
        self.assertIsNone(run(quote, refs(('.97', '.98'), ('.86', '.87'))))                    # disagree
        self.assertIsNone(run(quote, [Book.make([('.97', 300)], []), book('.97', '.98')]))     # one-sided
        self.assertIsNone(run((None, None), refs()))

    def test_rank_orders_by_value_with_floor_and_cap(self):
        policy = settings({'gap_radar': {'max_flags': 2}})
        candidates = [('a', {'value': D(10)}), ('b', {'value': D(30)}), ('c', None),
                      ('d', {'value': D(4)}), ('e', {'value': D(30)})]
        self.assertEqual([name for name, _ in rank(candidates, policy)], ['b', 'e'])
        self.assertEqual(rank([('a', {'value': D(0)})], settings({'gap_radar': {'min_value': '0'}})), [])


class SchedulingTests(unittest.TestCase):
    def test_gap_flags_rank_by_value_expire_and_are_replaced(self):
        interest, now = ScanInterest(), time.time()
        interest.set_gaps({'a': D('12.5'), 'b': D(40)}, now+60)
        hints = interest.hints({'a'}, {}, now)
        self.assertEqual((hints['a']['score'], hints['a']['reasons'], hints['a']['value']),
                         (260, ['gap_radar', 'owned_position'], 12.5))
        self.assertEqual(interest.hints(set(), {}, now+61)['a']['value'], 0.)
        interest.checked('a')
        self.assertNotIn('gap_radar', interest.hints(set(), {}, now).get('a', {'reasons': []})['reasons'])
        interest.set_gaps({'c': 5}, now+60)
        self.assertEqual(set(interest.gaps), {'c'})

    def test_priority_lane_takes_the_most_valuable_gap_first(self):
        interest = ScanInterest()
        interest.set_gaps({'a': 12.5, 'b': 40}, time.time()+60)
        with tempfile.TemporaryDirectory() as folder:
            scan = Scanner([{'name': 'x'}, {'name': 'a'}, {'name': 'b'}], folder)
            visits = []
            for mapping in scan.batch({}, 3, interest.hints(set(), {})):
                visits.append((mapping['name'], scan.selection['lane'], scan.selection.get('gap_value')))
        self.assertEqual(visits, [('b', 'priority', 40.0), ('x', 'regular', None), ('a', 'priority', 12.5)])


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def engine(self, gap=None, **sections):
        config, original, references = fixture()
        config['execution'] = dict(test_active.EXECUTION)
        if gap is not None:
            config['gap_radar'] = gap
        config.update(sections)
        sig = test_active.Broker(original)
        journal = Journal(Path(self.temp.name)/('j%d.db' % id(config)), 'test')
        self.addCleanup(journal.close)
        engine = ActiveEngine(config, sig, references, journal, self.temp.name, live=True)
        references.radar = Mock(return_value=({'2': refs()}, {}))
        return engine, sig, references, journal

    def events(self, journal, kind):
        return [json.loads(r[0]) for r in journal.db.execute('SELECT detail FROM events WHERE kind=?', (kind,))]

    def test_flags_a_screened_entry_with_one_bulk_sig_read(self):
        engine, sig, references, journal = self.engine({'enabled': True, 'min_value': '0'})
        sig.prices = Mock(return_value={'2': (0.89, 0.90)})
        engine.gap_radar_step()
        sig.prices.assert_called_once_with(['2'])
        name = engine.markets[0]['name']
        # Example limits: 25 coins per order -> 27 shares at .90 + .01 buffer; edge .96 - .90 - .01.
        self.assertAlmostEqual(engine.scan_interest.gaps[name][0], 1.35)
        event = self.events(journal, 'gap_radar')[-1]
        self.assertEqual((event['phase'], event['screened'], event['candidates']), ('before_night', 1, 1))
        self.assertEqual({k: event['flagged'][0][k] for k in ('exchange', 'route', 'side', 'shares')},
                         {'exchange': '2', 'route': 'entry', 'side': 'yes', 'shares': 27})
        engine.gap_radar_step()                                   # not due again yet
        sig.prices.assert_called_once()

    def test_disabled_radar_reads_nothing(self):
        engine, sig, references, _ = self.engine()
        sig.prices = Mock()
        engine.gap_radar_step()
        sig.prices.assert_not_called()
        references.radar.assert_not_called()

    def test_failures_are_reported_but_rate_limits_and_auth_raise(self):
        engine, sig, references, journal = self.engine({'enabled': True})
        references.radar.side_effect = RuntimeError('bulk endpoint changed')
        engine.gap_radar_step()
        self.assertEqual(self.events(journal, 'gap_radar_skip')[-1]['error_type'], 'RuntimeError')
        references.radar.side_effect = None
        for status, raises in ((500, False), (429, True), (401, True)):
            engine.gap_radar.last_run, engine._radar_books = 0, None
            sig.prices = Mock(side_effect=APIError('SIG error', status=status))
            if raises:
                with self.assertRaises(APIError):
                    engine.gap_radar_step()
            else:
                engine.gap_radar_step()
                self.assertEqual(self.events(journal, 'gap_radar_skip')[-1]['error_type'], 'APIError')

    def test_shares_one_reference_read_with_the_election_radar(self):
        engine, sig, references, journal = self.engine({'enabled': True, 'min_value': '0'},
                                                       election_night=live_window())
        sig.prices = Mock(return_value={'2': (0.89, 0.90)})
        engine.election_radar()
        engine.gap_radar_step()
        references.radar.assert_called_once()
        self.assertEqual(self.events(journal, 'gap_radar')[-1]['phase'], 'election_night')

    def test_a_visit_uses_up_the_flag(self):
        engine, sig, _, journal = self.engine({'enabled': True})
        name = engine.markets[0]['name']
        engine.gap_radar.last_run = time.time()                   # not due during this cycle
        engine.scan_interest.set_gaps({name: 10}, time.time()+60)
        engine.cycle()
        self.assertEqual(engine.scan_interest.gaps, {})
        visit = self.events(journal, 'scan_visit')[-1]
        self.assertEqual((visit['lane'], visit['gap_value']), ('priority', 10.0))
        self.assertIn('gap_radar', visit['priority_reasons'])

    def test_funding_targets_follow_rotation_before_and_recycling_during_the_night(self):
        engine, *_ = self.engine({'enabled': True}, capital_rotation={'enabled': True})
        now = time.time()
        engine.rotation.pending = {
            '5': {'race': 'r5', 'side': 'yes', 'ask': D('.9'), 'edge': D('.06'), 'need_coins': D(100), 'at': now},
            '6': {'race': 'r6', 'side': 'yes', 'ask': D('.9'), 'edge': D('.06'), 'need_coins': D(100), 'at': now-3600},
            '7': {'race': 'r7', 'side': 'yes', 'ask': D('.9'), 'edge': D('.06'), 'need_coins': D(0), 'at': now}}
        f = engine.funding_targets(now, False)
        self.assertEqual(f['targets'], [{'race': 'r5', 'ask': D('.9'), 'edge': D('.06'), 'need_coins': D(100)}])
        self.assertEqual((f['max_giveup'], f['min_gain'], f['called_only']), (D('0.02'), D('0.03'), False))
        self.assertIsNone(engine.funding_targets(now, True))      # recycling disabled
        engine, *_ = self.engine({'enabled': True}, election_night=dict(live_window(), recycle_enabled=True))
        engine.election_night.pending = {'2': {'edge': D('.05'), 'ask': D('.93'), 'need_coins': D(50), 'at': now}}
        f = engine.funding_targets(now, True)
        self.assertEqual(f['targets'][0]['race'], engine.races.race('2'))
        self.assertEqual((f['max_giveup'], f['min_gain'], f['called_only']), (D('0.02'), D('0.01'), True))
        self.assertIsNone(engine.funding_targets(now, False))     # rotation disabled here


class DashboardTests(unittest.TestCase):
    def test_dashboard_shows_what_the_radar_checks_first(self):
        from election_bot.dashboard import format_report, report
        with tempfile.TemporaryDirectory() as folder:
            journal = Journal(Path(folder)/'live.sqlite3', 'test')
            journal.event('gap_radar', {'phase': 'before_night', 'screened': 109, 'flagged': [
                {'exchange': '961', 'market': 'ms-senate-democratic', 'route': 'entry', 'side': 'no',
                 'edge': '0.04', 'shares': 1219, 'value': '48.76'}]})
            journal.close()
            config, *_ = fixture()
            config['gap_radar'] = {'enabled': True}
            r = report(folder, config)
        self.assertEqual(r['gap_radar']['flagged'], [{'market': 'ms-senate-democratic', 'route': 'entry',
                                                      'side': 'no', 'value': '48.76'}])
        self.assertTrue(r['features']['gap_radar'])
        self.assertIn('Gap radar: 0s ago, 109 screened; checking first: ms-senate-democratic entry no (48.76)',
                      format_report(r))


if __name__ == '__main__':
    unittest.main()
