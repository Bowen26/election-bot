import copy
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from election_bot.position_guard import PositionGuard,DEFAULTS,settings
from election_bot.state import Journal
from election_bot.ledger import Ledger
from election_bot.strategy import D,Book
from election_bot.demo import fixture
from election_bot.clients import contract_record,fingerprint
from election_bot.active_engine import ActiveEngine,ReservationRejected
import test_active


class PositionGuardTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.j=Journal(Path(self.temp.name)/'j.db','test');self.addCleanup(self.j.close);self.l=Ledger(self.j)
        self.c,_,_=fixture();self.c['execution']=dict(test_active.EXECUTION)
        self.c['position_entry_guard']=dict(DEFAULTS,enabled=True);self.g=PositionGuard(self.j.db,self.c)
        self.m=self.c['markets'][0];self.n=0;self.base=time.time()-400
        self.fill()

    def fill(self,q=10,action='buy',side='yes'):
        self.n+=1;p=dict(idempotencyKey=str(self.n),exchangeId='2',quantity=q,price='.50',action=action,side=side)
        self.j.reserve(p,D(q)*D('.51'));self.l.record(p,q,'.50','.01')

    def sample(self,t,bid,ask=None,phase='scan',persist=True):
        b=Book.make([(str(bid),200)],[(str(D(bid)+D('.01') if ask is None else ask),200)])
        return self.g.check(self.m,b,self.l.held('2')[0],phase,persist,now=self.base+t)

    def decline(self):
        self.sample(0,'.80');self.sample(60,'.77');return self.sample(120,'.77')

    def test_confirmed_decline_pauses_and_recovery_needs_two_later_scans(self):
        self.assertFalse(self.sample(0,'.80')['buy_allowed'])
        self.assertFalse(self.sample(60,'.77')['buy_allowed'])
        r=self.sample(120,'.77');self.assertTrue(r['paused']);self.assertEqual(r['reason'],'position_decline_paused')
        self.assertFalse(self.sample(180,'.79')['buy_allowed'])
        r=self.sample(240,'.79');self.assertTrue(r['buy_allowed']);self.assertFalse(r['paused'])

    def test_stable_and_rising_history_allows_additions(self):
        self.sample(0,'.70');self.sample(60,'.71')
        self.assertTrue(self.sample(120,'.72')['buy_allowed'])

    def test_spread_widening_alone_does_not_trigger_decline(self):
        self.sample(0,'.80','.81');self.sample(60,'.77','.81')
        self.assertTrue(self.sample(120,'.77','.81')['buy_allowed'])

    def test_wide_quote_cannot_mask_another_valid_decline_anchor(self):
        self.sample(0,'.50','.99');self.sample(60,'.70','.71');self.sample(120,'.67','.68')
        self.assertTrue(self.sample(180,'.67','.68')['paused'])

    def test_single_drop_and_preflight_cannot_manufacture_confirmation(self):
        self.sample(0,'.80');self.sample(60,'.80')
        self.assertTrue(self.sample(120,'.77')['buy_allowed'])
        for t in (121,140,179):self.assertTrue(self.sample(t,'.77',phase='preflight')['buy_allowed'])
        self.assertTrue(self.sample(180,'.77')['paused'])
        self.assertFalse(self.sample(240,'.80',phase='preflight')['buy_allowed'])

    def test_duplicate_scans_do_not_supply_samples(self):
        self.sample(0,'.80')
        for t in (1,20,59):self.assertEqual(self.sample(t,'.77')['samples'],1)

    def test_restart_and_window_expiry_preserve_paused_anchor(self):
        self.decline();self.g=PositionGuard(self.j.db,self.c)
        self.assertTrue(self.sample(2000,'.77')['paused'])
        self.sample(2060,'.77');r=self.sample(2120,'.77')
        self.assertFalse(r['buy_allowed']);self.assertEqual(r['anchor']['bid'],'0.80')

    def test_gap_and_clock_reversal_block_additions(self):
        self.sample(0,'.80');self.sample(60,'.80');self.sample(120,'.80')
        self.assertEqual(self.sample(721,'.80')['reason'],'position_history_insufficient')
        self.assertEqual(self.sample(700,'.80')['reason'],'position_history_clock_reversal')

    def test_partial_sale_and_addition_keep_episode_and_pause(self):
        r=self.decline();ep=r['episode'];self.fill(q=2,action='sell')
        r=self.sample(180,'.77');self.assertEqual(r['episode'],ep);self.assertTrue(r['paused'])
        self.fill(q=2);self.assertTrue(self.sample(240,'.77')['paused'])

    def test_flat_then_reopened_position_has_new_episode(self):
        old=self.decline()['episode'];self.fill(action='sell')
        self.assertTrue(self.sample(180,'.77')['buy_allowed'])
        self.fill();r=self.sample(240,'.77')
        self.assertNotEqual(old,r['episode']);self.assertFalse(r['paused']);self.assertFalse(r['buy_allowed'])

    def test_no_orientation_uses_complemented_sig_prices(self):
        self.fill(action='sell');self.fill(side='no')
        self.sample(0,'.19','.20');self.sample(60,'.22','.23')
        r=self.sample(120,'.22','.23');self.assertTrue(r['paused']);self.assertEqual(r['side'],'no')

    def test_binding_and_policy_changes_do_not_clear_pause(self):
        self.decline();c=copy.deepcopy(self.c);c['position_entry_guard']['decline']='.03'
        self.g=PositionGuard(self.j.db,c);self.assertTrue(self.sample(180,'.77')['paused'])
        self.m['contract_fingerprint']='0'*64
        with self.assertRaises(ValueError):self.sample(240,'.77')

    def test_readonly_reservation_check_does_not_write(self):
        self.decline();before=self.j.db.total_changes
        self.j.db.execute('BEGIN IMMEDIATE')
        self.assertFalse(self.sample(180,'.77',phase='preflight',persist=False)['buy_allowed'])
        self.j.db.rollback();self.assertEqual(self.j.db.total_changes,before)

    def test_invalid_config_and_stale_book_fail(self):
        for k,v in [('enabled','true'),('decline','0'),('recovery_distance','.03'),('sample_spacing_seconds',0),('window_seconds',100)]:
            c=copy.deepcopy(self.c);c['position_entry_guard'][k]=v
            with self.assertRaises(ValueError):settings(c)
        b=Book.make([('.8',200)],[('.81',200)],source_at=time.time()-100)
        with self.assertRaises(ValueError):self.g.check(self.m,b,10)


class PositionGuardIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.c,original,self.refs=fixture();self.c['execution']=dict(test_active.EXECUTION)
        self.c['position_entry_guard']=dict(DEFAULTS,enabled=True)
        self.sig=test_active.Broker(original);m=self.c['markets'][0];m['sig_market_id']='2'
        m['contract_fingerprint']=fingerprint(contract_record(self.sig.market('2'),m,self.refs.metadata(m)))
        self.m=m;self.j=Journal(self.root/'j.db','test');self.addCleanup(self.j.close)
        self.e=ActiveEngine(self.c,self.sig,self.refs,self.j,self.root,live=True)
        self.e.cycle();self.assertEqual(len(self.sig.orders),1)
        with self.j.db:self.j.db.execute('UPDATE orders SET created=?',(time.time()-10000,))

    def history(self,falling=False):
        now=time.time();held=self.e.ledger.held('2')[0]
        for t,bid in [(now-180,'.60' if falling else '.55'),(now-120,'.55'),(now-60,'.55')]:
            self.e.position_guard.check(self.m,Book.make([(bid,200)],[(str(D(bid)+D('.05')),200)]),held,now=t)

    def test_initial_entry_allowed_but_warmup_blocks_additions(self):
        self.e.cycle();self.assertEqual(len(self.sig.orders),1)

    def test_healthy_history_permits_addition_through_reservation(self):
        self.history();self.e.cycle();self.assertEqual(len(self.sig.orders),2)

    def test_pause_blocks_buys_and_allows_eligible_sell(self):
        self.history(True);self.e.cycle();self.assertEqual(len(self.sig.orders),1)
        self.sig.prices['2']=('.85','.86');self.e.cycle()
        self.assertEqual(self.sig.orders[2]['action'],'sell')

    def test_model_cannot_bypass_pause(self):
        self.history(True);self.c['model_trading']={'enabled':True};self.sig.prices['2']=('.69','.70')
        with patch('election_bot.model_live.propose',side_effect=AssertionError('model bypass')):self.e.cycle()
        self.assertEqual(len(self.sig.orders),1)

    def test_reservation_rechecks_stale_book_and_missing_context(self):
        self.history();p=dict(idempotencyKey='blocked',exchangeId='2',action='buy',side='yes',quantity=1,price=.6)
        with self.assertRaises(ReservationRejected):self.e.reserve_order(p,D('.61'),self.sig.account(),self.sig.positions())
        b=Book.make([('.55',200)],[('.60',200)],source_at=time.time()-100)
        with self.assertRaises(ReservationRejected):self.e.reserve_order(p,D('.61'),self.sig.account(),self.sig.positions(),entry_context=(self.m,b))
        self.assertFalse(self.j.pending())
