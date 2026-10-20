import time
import unittest
from pathlib import Path
import tempfile
from unittest.mock import Mock

from election_bot.active_engine import ActiveEngine
from election_bot.cash_reserve import floor, settings
from election_bot.clients import contract_record, fingerprint
from election_bot.demo import fixture
from election_bot.state import Journal
from election_bot.strategy import Book, D

import test_active
from test_election_night import live_window


def iso(t):
    return time.strftime('%Y-%m-%dT%H:%M:%S+00:00', time.gmtime(t))


class FloorTests(unittest.TestCase):
    def test_settings_and_window(self):
        self.assertEqual(floor(settings({})), 0)                        # off by default
        p = settings({'cash_reserve': {'enabled': True, 'coins': '500', 'until': iso(time.time()+60)}})
        self.assertEqual(floor(p), D(500))
        self.assertEqual(floor(p, time.time()+120), 0)                  # window opened
        default_until = settings({'cash_reserve': {'enabled': True}})['until']
        self.assertEqual(default_until, '2026-11-03T18:00:00-05:00')    # election-night start
        for bad in ({'coins': '-1'}, {'enabled': 'yes'}, {'until': '2026-11-03T18:00:00'}, {'x': 1}):
            with self.assertRaises(ValueError):
                settings({'cash_reserve': bad})


class EngineTests(unittest.TestCase):
    def engine(self, reserve, night=None, prices=('.55', '.60'), refs_called=False):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        config, original, refs = fixture()
        config['execution'] = dict(test_active.EXECUTION)
        config['cash_reserve'] = reserve
        if night:
            config['election_night'] = night
        sig = test_active.Broker(original)
        sig.cash = D(1000)
        mapping = config['markets'][0]
        mapping['sig_market_id'] = '2'
        mapping['contract_fingerprint'] = fingerprint(contract_record(sig.market('2'), mapping, refs.metadata(mapping)))
        if refs_called:
            refs.books = Mock(side_effect=lambda m, md: [Book.make([('.96', 500)], [('.98', 500)], venue='Kalshi'),
                                                          Book.make([('.96', 500)], [('.98', 500)], venue='Polymarket')])
        sig.prices['2'] = prices
        journal = Journal(Path(temp.name)/'j.db', 'test')
        self.addCleanup(journal.close)
        engine = ActiveEngine(config, sig, refs, journal, temp.name, live=True)
        engine.cycle()
        return sig

    def test_ordinary_buy_blocked_by_floor(self):
        self.assertEqual(len(self.engine({'enabled': False}).orders), 1)
        self.assertEqual(self.engine({'enabled': True, 'coins': '1000', 'until': iso(time.time()+3600)}).orders, {})
        partial = self.engine({'enabled': True, 'coins': '995', 'until': iso(time.time()+3600)}).orders
        self.assertEqual(partial[1]['quantity'], 8)                     # only the 5 coins above the floor
        self.assertEqual(len(self.engine({'enabled': True, 'coins': '1000', 'until': iso(time.time()-60)}).orders), 1)

    def test_election_night_buy_is_exempt(self):
        sig = self.engine({'enabled': True, 'coins': '995', 'until': iso(time.time()+3600)},
                          night=live_window(), prices=('.90', '.92'), refs_called=True)
        self.assertEqual([o['side'] for o in sig.orders.values()], ['yes'])
