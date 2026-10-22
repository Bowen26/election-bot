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


class Hanging:
    """A worker whose process stays alive; optional beat(now) runs on each poll."""
    def __init__(self, beat=None, pid=101):
        self.returncode, self.pid, self.beat, self.signals = None, pid, beat, []
    def poll(self):
        if self.returncode is None and self.beat:
            self.returncode = self.beat()
        return self.returncode
    def send_signal(self, sig):
        self.signals.append(sig)
        self.returncode = -sig
    def wait(self, timeout=None):
        return self.returncode


class StallTests(unittest.TestCase):
    setUp = WatchTests.setUp

    def launch_sequence(self, processes, stall_seconds=600):
        self.config.write_text(json.dumps({'auto_recovery': {'enabled': True, 'stall_seconds': stall_seconds}}))
        children = []
        def launch(command, **kwargs):
            children.append(command)
            return processes[len(children)-1]
        with patch('election_bot.supervisor.time', self.clock), \
             patch('election_bot.supervisor.subprocess.Popen', side_effect=launch):
            code = watch(self.root, self.runtime, self.config, True, self.messages.append)
        return code, ['recover' if 'recover' in c else 'run' for c in children]

    def test_stalled_worker_is_killed_reconciled_and_restarted(self):
        hung = Hanging()
        code, kinds = self.launch_sequence([hung, Process(0), Process(0)])
        self.assertEqual(kinds, ['run', 'recover', 'run'])
        self.assertTrue(hung.signals)
        recovering = [json.loads(m) for m in self.messages if json.loads(m)['status'] == 'auto_recovering']
        self.assertEqual(recovering[0]['reason'], 'stalled worker')
        self.assertEqual(recovering[0]['halt']['type'], 'Stall')
        self.assertEqual(code, 0)

    def test_beating_worker_is_left_alone(self):
        import os
        start = self.clock.now
        beat = self.runtime/'supervisor'/'heartbeat'
        def alive():
            beat.touch()
            os.utime(beat, (self.clock.now, self.clock.now))
            return 0 if self.clock.now > start+3000 else None
        hung = Hanging(alive)
        code, kinds = self.launch_sequence([hung])
        self.assertEqual((kinds, hung.signals, code), (['run'], [], 0))

    def test_disabled_stall_check(self):
        start = self.clock.now
        hung = Hanging(lambda: 0 if self.clock.now > start+3000 else None)
        code, kinds = self.launch_sequence([hung], stall_seconds=0)
        self.assertEqual((kinds, hung.signals), (['run'], []))

    def test_stall_setting_validation(self):
        self.assertEqual(auto_settings({})['stall_seconds'], 600)
        for bad in (60, 10000, '600', True):
            with self.assertRaises(ValueError):
                auto_settings({'auto_recovery': {'stall_seconds': bad}})


class HeartbeatTests(unittest.TestCase):
    def test_main_thread_only_and_throttled(self):
        import threading
        from election_bot import runner
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        path = Path(temp.name)/'heartbeat'
        with patch.object(runner, 'HEARTBEAT', path), patch.object(runner, '_last_beat', None):
            worker = threading.Thread(target=runner.beat, kwargs={'force': True})
            worker.start(); worker.join()
            self.assertFalse(path.exists())                 # helper threads cannot mask a hang
            runner.beat()
            self.assertTrue(path.exists())
            first = path.stat().st_mtime_ns
            runner.beat()                                   # throttled
            self.assertEqual(path.stat().st_mtime_ns, first)
            runner.beat(force=True)
            self.assertTrue(path.exists())

    def test_runtime_prefers_nosync(self):
        from election_bot.__main__ import runtime_dir
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        self.assertEqual(runtime_dir(root), root/'.runtime')
        (root/'.runtime.nosync').mkdir()
        self.assertEqual(runtime_dir(root), root/'.runtime.nosync')
