import json
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from election_bot import __main__ as cli
from election_bot.clients import APIError
from election_bot.clock_guard import ClockCheckError
from election_bot.state import exclusive_lock
from election_bot.supervisor import failure_code, restartable, stop_child, watch, HALT, RETRY


class FakeClock:
    def __init__(self):self.now=1000
    def monotonic(self):return self.now
    def time(self):return self.now
    def sleep(self,seconds):self.now+=seconds


class Process:
    def __init__(self,code,pid=100):self.returncode=code;self.pid=pid
    def poll(self):return self.returncode


class SupervisorTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.runtime=self.root/'runtime';self.config=self.root/'config file.json'
        self.clock=FakeClock();self.messages=[]

    def run_watch(self,codes,live=False,callback=None):
        children=[]
        def launch(*args,**kwargs):
            p=Process(codes[len(children)]);children.append((p,args,kwargs))
            if callback:callback(len(children))
            return p
        with patch('election_bot.supervisor.time',self.clock),patch('election_bot.supervisor.subprocess.Popen',side_effect=launch):
            code=watch(self.root,self.runtime,self.config,live,self.messages.append)
        return code,children

    def status(self):return json.loads((self.runtime/'supervisor/status.json').read_text())

    def test_crash_restarts_same_interpreter_and_absolute_config_without_shell(self):
        code,children=self.run_watch([1,0],live=True)
        self.assertEqual(code,0);self.assertEqual(len(children),2)
        command=children[0][1][0]
        self.assertEqual(command,[sys.executable,'-u','-m','election_bot','--config',str(self.config.resolve()),'run','--live'])
        self.assertEqual(children[0][2],{'cwd':str(self.root.resolve()),'start_new_session':True})
        self.assertEqual(self.status()['restarts'],1)
        self.assertFalse((self.runtime/'STOP').exists())

    def test_default_stays_paper(self):
        _,children=self.run_watch([0]);self.assertNotIn('--live',children[0][1][0])

    def test_no_restart_for_clean_stop_interrupt_or_operational_halt(self):
        for code in [0,130,HALT,2,-signal.SIGINT,-signal.SIGTERM,-signal.SIGHUP]:
            with self.subTest(code=code):
                result,children=self.run_watch([code]);self.assertEqual(len(children),1)
                self.assertNotEqual(self.status()['status'],'restart_wait')

    def test_crash_limit_persists_STOP_and_prevents_later_watch(self):
        code,children=self.run_watch([1,1,1,1]);self.assertEqual(code,HALT);self.assertEqual(len(children),4)
        self.assertTrue((self.runtime/'STOP').exists())
        delays=[json.loads(s)['retry_in_seconds'] for s in self.messages if 'retry_in_seconds' in json.loads(s)]
        self.assertEqual(delays,[5,10,20])
        _,children=self.run_watch([]);self.assertFalse(children)
        self.assertTrue((self.runtime/'STOP').exists())

    def test_crash_budget_ages_out_after_healthy_window(self):
        def advance(n):
            if n==4:self.clock.now+=901
        code,children=self.run_watch([1,1,1,1,0],callback=advance)
        self.assertEqual(code,0);self.assertEqual(len(children),5)
        self.assertFalse((self.runtime/'STOP').exists())

    def test_STOP_during_backoff_prevents_next_launch(self):
        def report(message):
            self.messages.append(message)
            if json.loads(message)['status']=='restart_wait':(self.runtime/'STOP').touch()
        with patch('election_bot.supervisor.time',self.clock),patch('election_bot.supervisor.subprocess.Popen',return_value=Process(1)) as launch:
            self.assertEqual(watch(self.root,self.runtime,self.config,report=report),0)
            self.assertEqual(launch.call_count,1)

    def test_existing_bot_and_existing_watcher_are_not_disturbed(self):
        for directory in [self.runtime,self.runtime/'supervisor']:
            with self.subTest(directory=directory),exclusive_lock(directory),patch('election_bot.supervisor.subprocess.Popen') as launch:
                with self.assertRaisesRegex(RuntimeError,'Another bot'):
                    watch(self.root,self.runtime,self.config,report=self.messages.append)
                launch.assert_not_called()

    def test_ctrl_c_sends_interrupt_once_and_does_not_restart(self):
        child=Mock(pid=123);child.returncode=None;child.poll.return_value=None
        def finished(*args,**kwargs):child.returncode=130;child.poll.return_value=130;return 130
        child.wait.side_effect=finished
        self.clock.sleep=Mock(side_effect=KeyboardInterrupt)
        with patch('election_bot.supervisor.time',self.clock),patch('election_bot.supervisor.subprocess.Popen',return_value=child) as launch:
            self.assertEqual(watch(self.root,self.runtime,self.config,report=self.messages.append),130)
        child.send_signal.assert_called_once_with(signal.SIGINT)
        self.assertEqual(launch.call_count,1);self.assertEqual(self.status()['status'],'stopped')

    def test_status_failure_cleans_up_child(self):
        child=Mock(pid=123);child.poll.return_value=None
        def finished(*args,**kwargs):child.poll.return_value=130;return 130
        child.wait.side_effect=finished
        with patch('election_bot.supervisor.subprocess.Popen',return_value=child),patch('election_bot.supervisor.os.fsync',side_effect=OSError('disk')):
            with self.assertRaises(OSError):watch(self.root,self.runtime,self.config)
        child.send_signal.assert_called_once_with(signal.SIGINT)

    def test_supervisor_never_changes_trading_database(self):
        self.runtime.mkdir();db=self.runtime/'live.sqlite3';db.write_bytes(b'unchanged trading journal sentinel')
        self.run_watch([1,0]);self.assertEqual(db.read_bytes(),b'unchanged trading journal sentinel')

    def test_operational_error_classification(self):
        for error in [RuntimeError('unknown order'),ClockCheckError('bad clock'),sqlite3.OperationalError('readonly'),
                      PermissionError('denied'),ValueError('risk'),KeyError('malformed'),TypeError('schema'),
                      APIError('unauthorized',status=401,method='GET'),APIError('unknown POST',method='POST'),
                      APIError('limit',status=429,method='GET',retry_after=3600),
                      APIError('later',status=503,method='GET',retry_after=120)]:
            with self.subTest(error=error):self.assertEqual(failure_code(error),HALT)
        self.assertEqual(failure_code(APIError('offline',status=503,method='GET')),RETRY)
        self.assertEqual(failure_code(Exception('unhandled crash')),1)

    def test_only_known_crash_exit_codes_restart(self):
        for code in [1,RETRY,-signal.SIGKILL,-signal.SIGABRT,-signal.SIGSEGV]:self.assertTrue(restartable(code))
        for code in [0,130,HALT,2,-signal.SIGHUP,-signal.SIGTERM]:self.assertFalse(restartable(code))

    def test_cleanup_escalates_only_after_grace_period(self):
        child=Mock();child.poll.return_value=None
        child.wait.side_effect=[subprocess.TimeoutExpired('child',30),subprocess.TimeoutExpired('child',5),-9]
        stop_child(child)
        child.send_signal.assert_called_once_with(signal.SIGINT)
        child.terminate.assert_called_once();child.kill.assert_called_once()

    def test_real_subprocess_halt_does_not_restart(self):
        original=subprocess.Popen
        def harmless(command,**kwargs):
            return original([sys.executable,'-c','raise SystemExit(78)'],**kwargs)
        with patch('election_bot.supervisor.subprocess.Popen',side_effect=harmless) as launch:
            self.assertEqual(watch(self.root,self.runtime,self.config,report=self.messages.append),HALT)
            self.assertEqual(launch.call_count,1)

    def test_cli_watch_dispatches_without_loading_credentials_or_broker(self):
        # Validation is tested independently; this asserts watch does not open an API client.
        self.config.write_text('{}')
        with patch.object(sys,'argv',['election_bot','--config',str(self.config),'watch','--live']),\
             patch.object(cli,'validate_config'),patch.object(cli,'ROOT',self.root),\
             patch.object(cli,'RUNTIME',self.runtime),patch.object(cli,'key') as key,\
             patch.object(cli,'Sig') as sig,patch('election_bot.supervisor.watch',return_value=0) as mocked:
            with self.assertRaises(SystemExit) as e:cli.main()
        self.assertEqual(e.exception.code,0);key.assert_not_called();sig.assert_not_called()
        mocked.assert_called_once_with(self.root,self.runtime,self.config,live=True)
