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
