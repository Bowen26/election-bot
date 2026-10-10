from datetime import datetime, timezone
from contextlib import ExitStack
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from election_bot.recovery_wait import prepare_recovery
from test_supervisor import FakeClock
import test_active
from election_bot import __main__ as cli


class RecoveryWaitTests(unittest.TestCase):
    def setUp(self):
        self.clock=FakeClock()
        self.engine=Mock()
        self.engine.stopped.return_value=False
        self.payload={'idempotencyKey':'original','expirationDate':
            datetime.fromtimestamp(1015,timezone.utc).isoformat()}
        self.row={'payload':json.dumps(self.payload),'created':1000,'response':None}
        self.engine.journal.pending.return_value=[self.row]

    def prepare(self):
        with patch('election_bot.recovery_wait.time',self.clock):
            return prepare_recovery(self.engine)

    def test_waits_past_expiry_and_refreshes_clock_without_placing_or_mutating(self):
        original=dict(self.row)
        self.engine.sig.account.side_effect=lambda:self.assertGreaterEqual(self.clock.now,1105)
        self.assertTrue(self.prepare())
        self.assertEqual(self.clock.now,1105)
        self.assertEqual(self.row,original)
        self.engine.sig.account.assert_called_once()
        self.engine.check_clock.assert_called_once()
        self.engine.sig.place.assert_not_called()
        self.engine.reconcile.assert_not_called()

    def test_STOP_during_wait_prevents_refresh_and_replay(self):
        self.engine.stopped.side_effect=lambda:self.clock.now>=1001
        self.assertFalse(self.prepare())
        self.engine.sig.account.assert_not_called()
        self.engine.sig.place.assert_not_called()

    def test_old_request_refreshes_immediately(self):
        self.clock.now=2000
        self.assertTrue(self.prepare())
        self.assertEqual(self.clock.now,2000)
        self.engine.check_clock.assert_called_once()

    def test_future_expiration_outside_supported_lifetime_halts(self):
        self.payload['expirationDate']=datetime.fromtimestamp(2000,timezone.utc).isoformat()
        self.row['payload']=json.dumps(self.payload)
        with self.assertRaisesRegex(RuntimeError,'far ahead'):self.prepare()
        self.engine.sig.account.assert_not_called()

    def test_clock_moving_backward_cannot_wait_forever(self):
        self.clock.time=lambda:1000
        with self.assertRaisesRegex(RuntimeError,'clock bound'):self.prepare()
        self.assertEqual(self.clock.now,1180)
        self.engine.sig.place.assert_not_called()

    def test_known_orders_need_no_expiration_wait_but_refresh_timing(self):
        self.row['response']='{}'
        self.assertTrue(self.prepare())
        self.assertEqual(self.clock.now,1000)
        self.engine.check_clock.assert_called_once()

    def test_no_pending_orders_and_STOP_do_not_touch_API(self):
        self.engine.journal.pending.return_value=[]
        self.assertTrue(self.prepare())
        self.engine.sig.account.assert_not_called()
        self.engine.stopped.return_value=True
        self.assertFalse(self.prepare())

    def test_bad_fresh_clock_prevents_recovery(self):
        self.clock.now=2000
        self.engine.check_clock.side_effect=RuntimeError('clock mismatch')
        with self.assertRaisesRegex(RuntimeError,'clock mismatch'):self.prepare()
        self.engine.sig.place.assert_not_called()

    def test_STOP_between_preparation_and_replay_prevents_POST(self):
        f=test_active.ActiveTests();f.setUp();self.addCleanup(f.doCleanups)
        p=dict(self.payload,exchangeId='2',tournamentId=f.sig.tid,action='buy',side='yes',quantity=1,price=.6)
        f.journal.reserve(p,.61)
        f.engine.recovery_stop_guard=True
        (f.engine.runtime/'STOP').touch()
        with self.assertRaises(KeyboardInterrupt):f.engine.reconcile(replay_unknown=True)
        self.assertFalse(f.sig.orders)
        self.assertTrue(f.journal.pending())


class RecoveryCLITests(unittest.TestCase):
    def invoke(self, wait, ready=True):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            root=Path(directory); config=root/'config.json'
            config.write_text(json.dumps({'tournament_slug':'test','execution':{'enabled':True}}))
            arguments=['bot','--config',str(config),'recover']+(['--wait'] if wait else [])
            stack.enter_context(patch.object(sys,'argv',arguments))
            stack.enter_context(patch.object(cli,'ROOT',root))
            stack.enter_context(patch.object(cli,'RUNTIME',root/'runtime'))
            stack.enter_context(patch.object(cli,'validate_config'))
            stack.enter_context(patch.object(cli,'key',return_value='test-token'))
            stack.enter_context(patch.object(cli,'References'))
            sig=stack.enter_context(patch.object(cli,'Sig'));sig.return_value.tid='test'
            stack.enter_context(patch.object(cli,'Journal'))
            stack.enter_context(patch.object(cli,'output'))
            factory=stack.enter_context(patch('election_bot.active_engine.ActiveEngine'))
            prepare=stack.enter_context(patch('election_bot.recovery_wait.prepare_recovery',return_value=ready))
            calls=[]
            prepare.side_effect=lambda engine:(calls.append('prepare') or ready)
            factory.return_value.reconcile.side_effect=lambda **kw:calls.append('replay' if kw.get('replay_unknown') else 'cleanup')
            if ready:cli.main()
            else:
                with self.assertRaises(KeyboardInterrupt):cli.main()
            return calls,factory.return_value,prepare

    def test_wait_dispatch_precedes_replay_and_arms_STOP_guard(self):
        calls,engine,prepare=self.invoke(True)
        self.assertEqual(calls,['prepare','replay','cleanup'])
        self.assertTrue(engine.recovery_stop_guard)
        prepare.assert_called_once_with(engine)

    def test_cancelled_preparation_never_replays(self):
        calls,_,_=self.invoke(True,False)
        self.assertEqual(calls,['prepare','cleanup'])

    def test_manual_recover_keeps_existing_no_wait_behavior(self):
        calls,_,prepare=self.invoke(False)
        self.assertEqual(calls,['replay','cleanup'])
        prepare.assert_not_called()
