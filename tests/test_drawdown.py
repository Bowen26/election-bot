import copy
import hashlib
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from election_bot.drawdown import Drawdown,DEFAULTS,settings,report
from election_bot.ledger import Ledger
from election_bot.state import Journal
from election_bot.strategy import Book,D
from election_bot.active_engine import ActiveEngine,ReservationRejected
from election_bot.demo import fixture
import test_active


class DrawdownTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'live.sqlite3'
        self.j=Journal(self.path,'test');self.addCleanup(self.j.close);self.l=Ledger(self.j)
        self.c,_,_=fixture();self.c['execution']=dict(test_active.EXECUTION)
        self.c['drawdown_control']=dict(DEFAULTS,enabled=True,pause_coins='10',resume_coins='5')
        self.g=Drawdown(self.l,self.c);self.seq=0

    def fill(self,q=100,price='.5',action='buy',side='yes',ex='2'):
        self.seq+=1;p=dict(idempotencyKey=str(self.seq),exchangeId=ex,quantity=q,price=price,action=action,side=side)
        self.j.reserve(p,D(q)*(D(price)+D('.01')));self.l.record(p,q,price,'.01')
        return p

    def mark(self,bid='.60',ask='.61',size=200,ex='2'):
        self.g.observe(ex,Book.make([(bid,size)],[(ask,size)]))

    def test_profit_peak_and_pause_hysteresis_persist(self):
        self.fill();self.mark()
        s=self.g.status();self.assertEqual(s['peak_pnl'],D('8'));self.assertTrue(s['buy_allowed'])
        self.mark('.50','.51');s=self.g.status()
        self.assertEqual(s['portfolio_pnl'],D('-2'));self.assertEqual(s['drawdown'],10);self.assertTrue(s['paused'])
        self.g=Drawdown(self.l,self.c);self.assertFalse(self.g.status()['buy_allowed'])
        self.mark('.55','.56');self.assertFalse(self.g.status()['buy_allowed'])  # Exactly resume: remain paused.
        self.mark('.56','.57');self.assertTrue(self.g.status()['buy_allowed'])
        self.assertEqual(self.g.status()['peak_pnl'],8)

    def test_initial_loss_is_measured_from_zero_without_prior_peak(self):
        self.fill();self.mark('.40','.41')
        s=self.g.status();self.assertEqual(s['portfolio_pnl'],-12);self.assertEqual(s['peak_pnl'],0);self.assertTrue(s['paused'])

    def test_realized_losses_remain_when_flat(self):
        self.fill();self.fill(action='sell',price='.40')
        s=self.g.status();self.assertEqual(s['realized_pnl'],-12);self.assertEqual(s['unrealized_pnl'],0);self.assertTrue(s['paused'])

    def test_missing_partial_stale_and_future_marks_block_without_peak_change(self):
        self.fill();self.assertEqual(self.g.status()['missing'][0]['reason'],'missing')
        self.mark(size=50);s=self.g.status();self.assertEqual(s['missing'][0]['reason'],'insufficient_bid_depth');self.assertEqual(s['peak_pnl'],0)
        self.mark();self.g.status()
        for stamp in (time.time()-301,time.time()+1):
            with self.j.db:self.j.db.execute('UPDATE drawdown_marks SET at=?',(stamp,))
            s=self.g.status();self.assertFalse(s['buy_allowed']);self.assertEqual(s['peak_pnl'],8)
            self.assertIsNone(s['portfolio_pnl'])

    def test_incomplete_marks_cannot_clear_persisted_pause(self):
        self.fill();self.mark('.40','.41');self.g.status()
        with self.j.db:self.j.db.execute('DELETE FROM drawdown_marks')
        self.assertTrue(Drawdown(self.l,self.c).status()['paused'])

    def test_inventory_epoch_prevents_reusing_same_quantity_cost_after_roundtrip(self):
        self.fill();self.mark();self.g.status()
        self.fill(action='sell',price='.50');self.fill()
        s=self.g.status();self.assertEqual(s['missing'][0]['reason'],'inventory_changed')

    def test_no_side_and_bid_depth_values(self):
        self.fill(side='no')
        self.g.observe('2',Book.make([('.30',100)],[('.39',50),('.41',50)]))
        self.assertEqual(self.g.status()['portfolio_pnl'],8)  # NO bids .61/.59, minus both buffers.

    def test_all_positions_required_including_disabled_mapping(self):
        self.fill();self.fill(ex='99');self.mark()
        s=self.g.status();self.assertFalse(s['buy_allowed']);self.assertEqual(s['missing'][0]['exchange'],'99')
        self.mark(ex='99');self.assertTrue(self.g.status()['buy_allowed'])

    def test_refresh_rotation_does_not_starve_after_failure(self):
        self.c['drawdown_control']['refresh_books_per_cycle']=1;self.g=Drawdown(self.l,self.c)
        self.fill(ex='2');self.fill(ex='3')
        self.assertEqual(self.g.due(),['2']);self.g.attempted('2');self.assertEqual(self.g.due(),['3'])

    def test_freshness_uses_book_timestamp_and_rejects_invalid_book(self):
        self.fill();old=Book.make([('.60',200)],[('.61',200)],source_at=time.time()-100)
        with self.assertRaises(ValueError):self.g.observe('2',old)
        self.assertFalse(self.g.status()['buy_allowed'])

    def test_config_and_valuation_definition_changes_fail(self):
        for k,v in [('enabled','yes'),('pause_coins','NaN'),('pause_coins','0'),('resume_coins','11'),('refresh_books_per_cycle',0),('max_mark_age_seconds',1000)]:
            c=copy.deepcopy(self.c);c['drawdown_control'][k]=v
            with self.assertRaises(ValueError):settings(c)
        c=copy.deepcopy(self.c);c['strategy']['cost_buffer_per_share']='.02'
        with self.assertRaises(ValueError):Drawdown(self.l,c)

    def test_report_readonly_and_no_initialization(self):
        self.fill();self.mark()
        before=hashlib.sha256(self.path.read_bytes()).hexdigest()
        r=report(self.path,self.c);self.assertEqual(r['peak_pnl'],8)
        self.assertEqual(before,hashlib.sha256(self.path.read_bytes()).hexdigest())
        self.assertEqual(self.j.db.execute('SELECT peak FROM drawdown_state').fetchone()[0],'0')
        missing=self.path.parent/'missing.sqlite3';self.assertEqual(report(missing,self.c)['reason'],'not_initialized');self.assertFalse(missing.exists())


class DrawdownIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.c,original,self.refs=fixture();self.c['execution']=dict(test_active.EXECUTION)
        self.c['drawdown_control']=dict(DEFAULTS,enabled=True,pause_coins='1',resume_coins='.5')
        self.sig=test_active.Broker(original);self.j=Journal(self.root/'live.sqlite3','test');self.addCleanup(self.j.close)
        from election_bot.clients import contract_record,fingerprint
        mapping=self.c['markets'][0];mapping['sig_market_id']='2'
        mapping['contract_fingerprint']=fingerprint(contract_record(self.sig.market('2'),mapping,self.refs.metadata(mapping)))
        self.e=ActiveEngine(self.c,self.sig,self.refs,self.j,self.root,live=True)

    def age(self):
        with self.j.db:self.j.db.execute('UPDATE orders SET created=?',(time.time()-10000,))

    def test_pause_blocks_additional_buy_but_allows_existing_sell(self):
        self.e.cycle();self.assertEqual(len(self.sig.orders),1)
        self.age();self.e.cycle();self.assertEqual(len(self.sig.orders),1);self.assertTrue(self.e.drawdown.status()['paused'])
        # Raised peak keeps drawdown paused even though this position now qualifies for a sale.
        with self.j.db:self.j.db.execute("UPDATE drawdown_state SET peak='100'")
        self.sig.prices['2']=('.85','.86');self.age();self.e.cycle()
        self.assertEqual(self.sig.orders[2]['action'],'sell');self.assertFalse((self.root/'STOP').exists())

    def test_atomic_reservation_rechecks_mark_age(self):
        self.e.cycle();self.age()
        self.e.drawdown.observe('2',self.sig.book('2'))
        with self.j.db:self.j.db.execute('UPDATE drawdown_marks SET at=?',(time.time()-301,))
        payload=dict(idempotencyKey='blocked',exchangeId='2',action='buy',side='yes',quantity=1,price=.6)
        with self.assertRaises(ReservationRejected):self.e.reserve_order(payload,D('.61'),self.sig.account(),self.sig.positions())
        self.assertFalse(self.j.pending())

    def test_pause_first_detected_in_reservation_survives_rollback(self):
        self.e.cycle();self.age();self.e.drawdown.observe('2',self.sig.book('2'))
        payload=dict(idempotencyKey='blocked',exchangeId='2',action='buy',side='yes',quantity=1,price=.6)
        with self.assertRaises(ReservationRejected):self.e.reserve_order(payload,D('.61'),self.sig.account(),self.sig.positions())
        self.assertEqual(self.j.db.execute('SELECT paused FROM drawdown_state').fetchone()[0],1)
        self.assertFalse(self.j.pending())

    def test_sig_only_refresh_works_when_reference_fetch_fails(self):
        self.e.cycle();self.age()
        with self.j.db:self.j.db.execute('DELETE FROM drawdown_marks')
        with patch.object(self.refs,'books',side_effect=ValueError('references unavailable')):
            self.e.cycle()
        self.assertTrue(self.e.drawdown.status()['complete']);self.assertTrue(self.e.drawdown.status()['paused'])

    def test_disabled_feature_keeps_existing_behavior(self):
        self.c['drawdown_control']['enabled']=False
        self.e=ActiveEngine(self.c,self.sig,self.refs,self.j,self.root,live=True)
        self.e.cycle();self.age();self.e.cycle();self.assertEqual(len(self.sig.orders),2)

    def test_model_route_cannot_bypass_pause(self):
        self.e.cycle();self.age()
        self.c['model_trading']={'enabled':True}
        self.sig.prices['2']=('.69','.70')
        with self.j.db:self.j.db.execute("UPDATE drawdown_state SET peak='100'")
        with patch('election_bot.model_live.propose',side_effect=AssertionError('Model bypassed drawdown')):
            self.e.cycle()
        self.assertEqual(len(self.sig.orders),1)

    def test_refresh_is_bounded_and_honors_stop(self):
        self.e.cycle()
        with self.j.db:self.j.db.execute('DELETE FROM drawdown_marks')
        (self.root/'STOP').touch()
        with patch.object(self.sig,'book',side_effect=AssertionError('Read after STOP')):
            self.e.refresh_drawdown()
