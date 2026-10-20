import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import MagicMock, Mock

from election_bot.alerts import message, notify, settings


class AlertTests(unittest.TestCase):
    def test_disabled_by_default_and_validated(self):
        self.assertFalse(settings({})['enabled'])
        for bad in ({'enabled': 'yes'}, {'push_url': 'http://ntfy.sh/x'}, {'push_url': 'https://u:p@ntfy.sh/x'},
                    {'statuses': 'halted'}, {'other': 1}):
            with self.assertRaises(ValueError):
                settings({'alerts': bad})

    def test_selected_statuses_only_and_short_text(self):
        policy = settings({'alerts': {'enabled': True, 'push_url': 'https://ntfy.sh/my-topic'}})
        run, opener = Mock(), MagicMock()
        payload = {'status': 'halted', 'exit_code': 78, 'reason': 'Restart limit reached; STOP set for review',
                   'supervisor_pid': 1}
        self.assertEqual(notify(policy, payload, run, opener), ['macos', 'push'])
        self.assertEqual(run.call_args[0][0][0], 'osascript')
        request = opener.call_args[0][0]
        self.assertEqual(request.data.decode(), 'Election bot: halted; exit 78; Restart limit reached; STOP set for review')
        self.assertEqual(notify(policy, {'status': 'running'}, run, opener), [])

    def test_failures_never_raise(self):
        policy = settings({'alerts': {'enabled': True, 'push_url': 'https://ntfy.sh/t'}})
        self.assertEqual(notify(policy, {'status': 'stopped'}, Mock(side_effect=OSError()),
                                Mock(side_effect=RuntimeError())), [])
        self.assertIn('"quoted"', message({'status': 'stopped', 'reason': '"quoted"'}))


class ReentryExemptionTests(unittest.TestCase):
    def test_election_night_buy_passes_reentry_cooldown_at_reservation(self):
        import copy
        from election_bot.active_engine import ActiveEngine
        from election_bot.clients import contract_record, fingerprint
        from election_bot.demo import fixture
        from election_bot.state import Journal
        from election_bot.strategy import Book
        import test_active
        from test_election_night import live_window
        for window, expected in ((live_window(), 1), (dict(live_window(), ignore_reentry_cooldown=False), 0)):
            temp = tempfile.TemporaryDirectory()
            self.addCleanup(temp.cleanup)
            config, original, refs = fixture()
            config['execution'] = dict(test_active.EXECUTION, reentry_cooldown_seconds=1800)
            config['election_night'] = window
            sig = test_active.Broker(original)
            mapping = config['markets'][0]
            mapping['sig_market_id'] = '2'
            mapping['contract_fingerprint'] = fingerprint(contract_record(sig.market('2'), mapping, refs.metadata(mapping)))
            refs.books = Mock(side_effect=lambda m, md: [Book.make([('.96', 500)], [('.98', 500)], venue='Kalshi'),
                                                          Book.make([('.96', 500)], [('.98', 500)], venue='Polymarket')])
            sig.prices['2'] = ('.90', '.92')
            journal = Journal(Path(temp.name)/'j.db', 'test')
            self.addCleanup(journal.close)
            engine = ActiveEngine(config, sig, refs, journal, temp.name, live=True)
            engine.reentry_wait = Mock(return_value=900)
            engine.cycle()
            self.assertEqual(len(sig.orders), expected)
