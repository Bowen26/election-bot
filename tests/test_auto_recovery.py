import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from election_bot.supervisor import HALT, auto_settings, read_halt, record_halt, recoverable, watch

from test_supervisor import FakeClock, Process


class ClassificationTests(unittest.TestCase):
    def test_allowlist(self):
        safe = [{'type': 'APIError', 'message': 'GET /x failed (connection timed out)', 'status': None},
                {'type': 'APIError', 'message': 'GET /x returned HTTP 429', 'status': 429},
                {'type': 'JSONDecodeError', 'message': 'Unterminated string'},
                {'type': 'OperationalError', 'message': 'database is locked'},
                {'type': 'RuntimeError', 'message': 'Account portfolio differs from bot ledger; reconcile before portfolio trading'}]
        unsafe = [{'type': 'APIError', 'message': 'GET /x returned HTTP 401', 'status': 401},
                  {'type': 'RuntimeError', 'message': 'Sell exceeds bot-owned inventory; accounting halted'},
                  {'type': 'ValueError', 'message': 'Contract changed or unreviewed; inspect and pin'},
                  {'type': 'RuntimeError', 'message': 'Realized loss limit reached before reservation'}, None]
        for record in safe:
            self.assertIsNotNone(recoverable(record), record)
        for record in unsafe:
            self.assertIsNone(recoverable(record), record)
        self.assertFalse(auto_settings({})['enabled'])
        for bad in ({'enabled': 'yes'}, {'max_per_hour': 0}, {'x': 1}):
            with self.assertRaises(ValueError):
                auto_settings({'auto_recovery': bad})

    def test_record_freshness(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        record_halt(temp.name, RuntimeError('database is locked'), HALT)
        record = read_halt(temp.name, 0)
        self.assertEqual(record['type'], 'RuntimeError')
        self.assertIsNone(read_halt(temp.name, record['at']+1))          # older than this worker


class WatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.runtime = self.root/'runtime'
        self.config = self.root/'config.json'
        self.clock = FakeClock()
        self.messages = []

    def run_watch(self, codes, halts, auto=True, max_per_hour=6):
        self.config.write_text(json.dumps({'auto_recovery': {'enabled': auto, 'max_per_hour': max_per_hour}}))
        children = []
        def launch(command, **kwargs):
            index = len(children)
            process = Process(codes[index])
            children.append(command)
            halt = halts[index] if index < len(halts) else None
            if halt:
                with patch('election_bot.supervisor.time', self.clock):
                    record_halt(self.runtime, halt, codes[index])
            return process
        with patch('election_bot.supervisor.time', self.clock), \
             patch('election_bot.supervisor.subprocess.Popen', side_effect=launch):
            code = watch(self.root, self.runtime, self.config, True, self.messages.append)
        return code, ['recover' if 'recover' in c else 'run' for c in children]

    def statuses(self):
        return [json.loads(m)['status'] for m in self.messages]

    def test_safe_halt_reconciles_then_resumes(self):
        lock = RuntimeError('database is locked')
        code, kinds = self.run_watch([HALT, 0, 0], [lock])
        self.assertEqual(kinds, ['run', 'recover', 'run'])
        self.assertIn('auto_recovering', self.statuses())
        self.assertEqual(code, 0)

    def test_unsafe_or_disabled_halts(self):
        bad = RuntimeError('Sell exceeds bot-owned inventory; accounting halted')
        code, kinds = self.run_watch([HALT], [bad])
        self.assertEqual((code, kinds, self.statuses()[-1]), (HALT, ['run'], 'halted'))
        self.messages.clear()
        code, kinds = self.run_watch([HALT], [RuntimeError('database is locked')], auto=False)
        self.assertEqual((code, kinds), (HALT, ['run']))

    def test_hourly_cap_sets_stop(self):
        lock = RuntimeError('database is locked')
        code, kinds = self.run_watch([HALT, 0, HALT, 0, HALT], [lock, None, lock, None, lock], max_per_hour=2)
        self.assertEqual(kinds, ['run', 'recover', 'run', 'recover', 'run'])
        self.assertEqual(code, HALT)
        self.assertTrue((self.runtime/'STOP').exists())
        self.assertIn('limit', json.loads(self.messages[-1])['reason'])

    def test_failed_reconciliation_does_not_loop(self):
        code, kinds = self.run_watch([HALT, HALT], [RuntimeError('database is locked'), RuntimeError('database is locked')])
        self.assertEqual(kinds, ['run', 'recover'])
        self.assertEqual(code, HALT)
