import copy
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock

from election_bot.active_engine import ActiveEngine
from election_bot.clients import contract_record, fingerprint
from election_bot.demo import fixture
from election_bot.election_night import ElectionNight, REASON, rehearse, format_rehearsal, settings
from election_bot.state import Journal
from election_bot.strategy import Book, D

import test_active

STRATEGY = {'max_age_seconds': 15, 'cost_buffer_per_share': '.01', 'max_shares_per_order': 500}
OPEN = {'enabled': True, 'start_at': '2020-01-01T00:00:00+00:00', 'end_at': '2020-01-03T00:00:00+00:00'}


def book(bid, ask, size=100, venue='SIG', at=None):
    return Book.make([(bid, size)], [(ask, size)], source_at=at, observed_at=at, venue=venue)


def night(**changes):
    return ElectionNight({'strategy': STRATEGY, 'election_night': dict(OPEN, **changes)})


def live_window():
    now = time.time()
    fmt = lambda t: time.strftime('%Y-%m-%dT%H:%M:%S+00:00', time.gmtime(t))
    return {'enabled': True, 'start_at': fmt(now-3600), 'end_at': fmt(now+3600)}


class SettingsTests(unittest.TestCase):
    def test_disabled_by_default_and_validated(self):
        self.assertFalse(settings({})['enabled'])
        for bad in ({'start_at': '2026-11-03T18:00:00'}, {'end_at': '2026-11-03T17:00:00-05:00'},
                    {'end_at': '2026-11-09T12:00:00-05:00'}, {'min_reference_bid': '.5'},
                    {'min_edge': '0'}, {'max_shares': 0}, {'enabled': 'yes'}, {'other': 1}):
            with self.assertRaises(ValueError):
                settings({'election_night': bad})

    def test_window_and_news_overrides(self):
        n = night()
        t = n.policy['start']+60
        self.assertTrue(n.active(t))
        self.assertFalse(n.active(n.policy['end']))
        self.assertTrue(n.overrides('News uncertainty pause until 2020-01-01T01:00:00+00:00', t))
        self.assertFalse(n.overrides('Disputed-result review required (article 4); entries and exits paused', t))
        self.assertFalse(n.overrides('News monitor has no recently healthy feed; new entries paused', t))
        self.assertFalse(n.overrides('News uncertainty pause until x', n.policy['start']-1))
        self.assertFalse(night(override_news_pauses=False).overrides('News uncertainty pause until x', t))
        self.assertFalse(night(enabled=False).active(t))


class SelectTests(unittest.TestCase):
    def test_called_side_requires_both_fresh_references(self):
        n = night()
        self.assertEqual(n.called_side([book('.96', '.98'), book('.95', '.97')]), 'yes')
        self.assertEqual(n.called_side([book('.02', '.04'), book('.03', '.05')]), 'no')
        self.assertIsNone(n.called_side([book('.96', '.98'), book('.90', '.92')]))
        self.assertIsNone(n.called_side([book('.96', '.98', at=time.time()-60), book('.96', '.98')]))
        self.assertIsNone(n.called_side([book('.96', '.98')]))

    def test_buys_lagging_sig_ask_with_size_caps(self):
        n = night()
        refs = [book('.96', '.98'), book('.96', '.98')]
        detail = {}
        signal = n.select(book('.90', '.92', size=300), refs, D(1000), 0, None, detail)
        self.assertEqual((signal.side, signal.price, signal.reason), ('yes', D('.92'), REASON))
        self.assertEqual(signal.quantity, 150)                    # half of the 300 displayed
        self.assertEqual(signal.edge, D('.05'))
        self.assertEqual(n.select(book('.90', '.92', 300), refs, D(93), 0).quantity, 100)   # 93/(.92+.01)
        self.assertEqual(n.select(book('.90', '.92', 300), refs, D(1000), 0, {'yes': 7, 'no': 0}).quantity, 7)
        no = n.select(book('.06', '.08'), [book('.02', '.04'), book('.02', '.04')], D(1000), 0)
        self.assertEqual((no.side, no.price), ('no', D('.94')))   # NO ask = 1 - YES bid

    def test_blocks(self):
        n = night()
        refs = [book('.96', '.98'), book('.96', '.98')]
        for sig, held, reason in ((book('.94', '.96'), 0, 'edge_below_minimum'),
                                  (book('.90', '.92'), -5, 'opposite_inventory')):
            detail = {}
            self.assertIsNone(n.select(sig, refs, D(1000), held, None, detail))
            self.assertEqual(detail['reason'], reason)
        detail = {}
        self.assertIsNone(night(max_price='.90').select(book('.90', '.92'), refs, D(1000), 0, None, detail))
        self.assertEqual(detail['reason'], 'price_cap_or_tick')
        detail = {}
        self.assertIsNone(n.select(book('.90', '.92'), [book('.80', '.82')]*2, D(1000), 0, None, detail))
        self.assertEqual(detail['reason'], 'not_called_by_references')


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def engine(self, window=None, news_reason=None):
        config, original, refs = fixture()
        config['execution'] = dict(test_active.EXECUTION)
        if window is not None:
            config['election_night'] = window
        sig = test_active.Broker(original)
        mapping = config['markets'][0]
        mapping['sig_market_id'] = '2'
        mapping['contract_fingerprint'] = fingerprint(contract_record(sig.market('2'), mapping, refs.metadata(mapping)))
        refs.books = Mock(side_effect=lambda m, md: [book('.96', '.98', 500, 'Kalshi'),
                                                      book('.96', '.98', 500, 'Polymarket')])
        sig.prices['2'] = ('.90', '.92')
        journal = Journal(Path(self.temp.name)/('j%d.db' % id(config)), 'test')
        self.addCleanup(journal.close)
        engine = ActiveEngine(config, sig, refs, journal, self.temp.name, live=True)
        if news_reason is not None:
            news = Mock()
            news.drain.return_value = {}
            news.block_reason.return_value = news_reason
            engine.news = news
        engine.cycle()
        return sig, journal

    def test_outside_window_ordinary_rule_does_not_buy(self):
        sig, _ = self.engine()                      # .96 - .92 - .01 = 3c < 5c minimum edge
        self.assertEqual(sig.orders, {})

    def test_inside_window_buys_called_race(self):
        sig, journal = self.engine(live_window())
        self.assertEqual([(o['action'], o['side']) for o in sig.orders.values()], [('buy', 'yes')])
        events = [json.loads(r[0]) for r in journal.db.execute(
            "SELECT detail FROM events WHERE kind='election_night_decision'")]
        self.assertTrue(any(e.get('reason') == REASON for e in events))

    def test_overrides_uncertainty_pause_but_never_disputes(self):
        sig, _ = self.engine(live_window(), 'News uncertainty pause until 2099-01-01T00:00:00+00:00')
        self.assertEqual(len(sig.orders), 1)
        sig, _ = self.engine(live_window(), 'Disputed-result review required (article 9); entries and exits paused')
        self.assertEqual(sig.orders, {})
        sig, _ = self.engine(dict(live_window(), override_news_pauses=False),
                             'News uncertainty pause until 2099-01-01T00:00:00+00:00')
        self.assertEqual(sig.orders, {})


class RehearsalTests(unittest.TestCase):
    def test_reports_called_lagging_races(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        path = Path(temp.name)/'live.sqlite3'
        j = Journal(path, 'test')
        now = time.time()
        quotes = []
        for venue, (bid, ask) in (('SIG', ('.90', '.92')), ('Kalshi', ('.96', '.98')), ('Polymarket', ('.96', '.98'))):
            quotes.append({'venue': venue, 'bid': [bid, 100], 'ask': [ask, 100], 'observed_at': now-100,
                           'source_at': now-100, 'timestamp_basis': 'fetch_time', 'quality': {'issues': []}})
        snap = {'version': 1, 'snapshot_id': 's', 'exchange': '2', 'phase': 'scan', 'captured_at': now-100,
                'quotes': quotes}
        j.db.execute('INSERT INTO events VALUES (?,?,?)', (now-100, 'quote_snapshot', json.dumps(snap)))
        j.db.commit()
        j.close()
        config, _, _ = fixture()
        r = rehearse(path, config, hours=1, now=now)
        self.assertEqual(r['races_with_proposals'], 1)
        self.assertEqual(D(r['proposals'][0]['edge']), D('.05'))
        self.assertIn('ELECTION-NIGHT', format_rehearsal(r))


if __name__ == '__main__':
    unittest.main()


class RadarTests(unittest.TestCase):
    def test_reports_only_newly_called_races(self):
        from election_bot.election_night import Radar
        n = ElectionNight({'strategy': STRATEGY, 'election_night': live_window()})
        radar = Radar(n)
        called = [book('.96', '.98', venue='Kalshi'), book('.96', '.98', venue='Polymarket')]
        open_race = [book('.60', '.62', venue='Kalshi'), book('.60', '.62', venue='Polymarket')]
        now = time.time()
        self.assertTrue(radar.due(now))
        self.assertEqual(radar.update({'a': called, 'b': open_race}, now), {'a': 'yes'})
        self.assertFalse(radar.due(now+1))
        self.assertEqual(radar.update({'a': called, 'b': called}, now+30), {'b': 'yes'})
        # Older bulk timestamps are accepted for detection only (radar_max_age_seconds).
        old = [book('.96', '.98', venue='Kalshi', at=now-60), book('.96', '.98', venue='Polymarket', at=now-60)]
        self.assertEqual(n.called_side(old), None)
        self.assertEqual(n.called_side(old, 120), 'yes')

    def test_engine_radar_flags_called_race_and_pauses_research_reads(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        config, original, refs = fixture()
        config['execution'] = dict(test_active.EXECUTION)
        config['election_night'] = live_window()
        config['research_followups'] = {'enabled': True}
        sig = test_active.Broker(original)
        journal = Journal(Path(temp.name)/'j.db', 'test')
        self.addCleanup(journal.close)
        engine = ActiveEngine(config, sig, refs, journal, temp.name, live=True)
        name = config['markets'][0]['name']
        exchange = config['markets'][0]['sig_exchange_id']
        refs.radar = Mock(return_value=({exchange: [book('.97', '.99', venue='Kalshi'),
                                                    book('.97', '.99', venue='Polymarket')]}, {}))
        engine.observe_research_due = Mock()
        engine.election_radar()
        refs.radar.assert_called_once()
        self.assertIn('election_night', engine.scan_interest.watches[name])
        engine.radar.last_run = 0
        refs.radar.side_effect = RuntimeError('bulk endpoint changed')
        engine.election_radar()                          # reported, not raised
        engine.cycle()
        engine.observe_research_due.assert_not_called()


class RecycleTests(unittest.TestCase):
    def night(self, **changes):
        return ElectionNight({'strategy': STRATEGY, 'election_night': dict(OPEN, **dict({'recycle_enabled': True}, **changes))})

    def test_pending_buy_recorded_only_for_cash_shortfall(self):
        n = self.night()
        refs = [book('.96', '.98'), book('.96', '.98')]
        n.select(book('.90', '.92', 300), refs, D(1000), 0, exchange='a', cash=D(50000))
        self.assertNotIn('a', n.pending)                       # per-order cap, cash plenty
        n.select(book('.90', '.92', 300), refs, D(40), 0, exchange='a', cash=D(40))
        self.assertEqual(n.pending['a']['need_coins'], 150*D('.93')-40)
        n.select(book('.90', '.92', 300), refs, D(1000), 0, exchange='a', cash=D(50000))
        self.assertNotIn('a', n.pending)                       # cleared once affordable

    def test_sells_called_holding_when_pending_edge_beats_giveup(self):
        from election_bot.election_night import RECYCLE, recycle
        n = self.night()
        now = time.time()
        n.pending['target'] = {'edge': D('.08'), 'ask': D('.90'), 'need_coins': D('50'), 'at': now}
        refs = [book('.97', '.99'), book('.97', '.99')]       # fair .98, called YES
        detail = {}
        sale = recycle(n, book('.965', '.975', 500), refs, 400, 'held', 1000, detail, now)
        self.assertEqual((sale.action, sale.side, sale.price, sale.reason), ('sell', 'yes', D('.965'), RECYCLE))
        self.assertEqual(sale.quantity, 52)                    # ceil(50 / .965)
        self.assertGreaterEqual(D(detail['net_gain_per_share']), D('.01'))

    def test_recycle_blocks(self):
        from election_bot.election_night import recycle
        now = time.time()
        refs = [book('.97', '.99'), book('.97', '.99')]
        target = {'edge': D('.08'), 'ask': D('.90'), 'need_coins': D('50'), 'at': now}
        cases = [(self.night(recycle_enabled=False), book('.965', '.975'), 400, 'disabled_or_no_position'),
                 (self.night(), book('.95', '.975'), 400, 'not_worth_recycling'),       # gives up 3c
                 (self.night(), book('.965', '.975'), -400, 'holding_not_called_our_way')]
        for n, sig, held, reason in cases:
            n.pending['target'] = dict(target)
            detail = {}
            self.assertIsNone(recycle(n, sig, refs, held, 'held', 1000, detail, now))
            self.assertEqual(detail['reason'], reason)
        n = self.night()
        n.pending['target'] = dict(target, at=now-600)          # expired
        detail = {}
        self.assertIsNone(recycle(n, book('.965', '.975'), refs, 400, 'held', 1000, detail, now))
        self.assertEqual(detail['reason'], 'no_pending_buy')
        n.pending['held'] = dict(target, at=now)               # never sell to fund the same race
        self.assertIsNone(recycle(n, book('.965', '.975'), refs, 400, 'held', 1000, {}, now))

    def test_engine_places_recycling_sale_only_when_enabled(self):
        for enabled, expected in ((True, ['buy', 'sell']), (False, ['buy'])):
            temp = tempfile.TemporaryDirectory()
            self.addCleanup(temp.cleanup)
            config, original, refs = fixture()
            config['execution'] = dict(test_active.EXECUTION)
            config['election_night'] = dict(live_window(), recycle_enabled=enabled)
            sig = test_active.Broker(original)
            mapping = config['markets'][0]
            mapping['sig_market_id'] = '2'
            mapping['contract_fingerprint'] = fingerprint(contract_record(sig.market('2'), mapping, refs.metadata(mapping)))
            prices = {'refs': ('.96', '.98')}
            refs.books = Mock(side_effect=lambda m, md: [book(*prices['refs'], 500, 'Kalshi'),
                                                          book(*prices['refs'], 500, 'Polymarket')])
            sig.prices['2'] = ('.90', '.92')
            journal = Journal(Path(temp.name)/'j.db', 'test')
            self.addCleanup(journal.close)
            engine = ActiveEngine(config, sig, refs, journal, temp.name, live=True)
            engine.cycle()                                      # election-night buy
            journal.db.execute('UPDATE orders SET created=?', (time.time()-1000,))
            journal.db.commit()
            prices['refs'] = ('.97', '.99')                     # fair .98; no convergence exit at .965
            sig.prices['2'] = ('.965', '.975')                  # no new buy edge
            engine.election_night.pending['other'] = {'edge': D('.08'), 'ask': D('.90'),
                                                      'need_coins': D('50'), 'at': time.time()}
            engine.cycle()
            self.assertEqual([o['action'] for o in sig.orders.values()], expected)


class FakeExposure:
    """Net-share caps that are already full in every direction."""
    enabled = True
    def __init__(self, ledger, config):
        pass
    def headroom(self, exchange, action, side):
        return D(0)
    def summary(self):
        return {'enabled': True}


class NightControlTests(unittest.TestCase):
    """Called-race buys bypass the drawdown pause, position guard and net-share caps."""
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def run_cycle(self, window, prepare):
        from unittest.mock import patch
        config, original, refs = fixture()
        config['execution'] = dict(test_active.EXECUTION)
        config['election_night'] = window
        sig = test_active.Broker(original)
        mapping = config['markets'][0]
        mapping['sig_market_id'] = '2'
        mapping['contract_fingerprint'] = fingerprint(contract_record(sig.market('2'), mapping, refs.metadata(mapping)))
        refs.books = Mock(side_effect=lambda m, md: [book('.96', '.98', 500, 'Kalshi'),
                                                      book('.96', '.98', 500, 'Polymarket')])
        sig.prices['2'] = ('.90', '.92')
        journal = Journal(Path(self.temp.name)/('j%d.db' % id(config)), 'test')
        self.addCleanup(journal.close)
        from contextlib import nullcontext
        with patch('election_bot.active_engine.Exposure', FakeExposure) if prepare == 'exposure' else nullcontext():
            engine = ActiveEngine(config, sig, refs, journal, self.temp.name, live=True)
            if prepare == 'drawdown':
                paused = {'buy_allowed': False, 'reason': 'drawdown_paused', 'enabled': True}
                engine.drawdown.status = Mock(return_value=paused)
            if prepare == 'guard':
                engine.position_guard.check = Mock(return_value={'buy_allowed': False,
                                                                 'reason': 'position_history_insufficient'})
            engine.cycle()
        decisions = [json.loads(r[0]) for r in journal.db.execute(
            "SELECT detail FROM events WHERE kind='election_night_decision'")]
        return sig, decisions

    def test_drawdown_pause_does_not_stop_called_race_buys(self):
        sig, decisions = self.run_cycle(live_window(), 'drawdown')
        self.assertEqual([(o['action'], o['side']) for o in sig.orders.values()], [('buy', 'yes')])
        self.assertTrue(any('drawdown_pause' in d.get('bypassed', []) for d in decisions))
        sig, decisions = self.run_cycle(dict(live_window(), ignore_drawdown_pause=False), 'drawdown')
        self.assertEqual(sig.orders, {})
        self.assertTrue(any(d.get('blocking') == ['drawdown_pause'] for d in decisions))

    def test_position_guard_does_not_stop_called_race_buys(self):
        sig, _ = self.run_cycle(live_window(), 'guard')
        self.assertEqual(len(sig.orders), 1)
        sig, _ = self.run_cycle(dict(live_window(), ignore_position_guard=False), 'guard')
        self.assertEqual(sig.orders, {})

    def test_full_exposure_caps_do_not_stop_called_race_buys(self):
        sig, decisions = self.run_cycle(live_window(), 'exposure')
        self.assertEqual(len(sig.orders), 1)
        self.assertTrue(any(d.get('exposure_caps') == 'not_applied_to_called_race' for d in decisions))
        sig, _ = self.run_cycle(dict(live_window(), ignore_exposure_caps=False), 'exposure')
        self.assertEqual(sig.orders, {})

    def test_bypass_flags_validated(self):
        self.assertTrue(settings({'election_night': OPEN})['ignore_drawdown_pause'])
        with self.assertRaises(ValueError):
            settings({'election_night': dict(OPEN, ignore_exposure_caps='yes')})

    def test_outside_window_nothing_is_bypassed(self):
        n = night()
        self.assertFalse(n.exempt('ignore_drawdown_pause', now=time.time()))   # OPEN window is in 2020
        self.assertTrue(n.exempt('ignore_drawdown_pause', now=n.policy['start']+1))
