import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from election_bot.state import Journal
from election_bot.ledger import Ledger
from election_bot import model_live
from election_bot.strategy import D
from election_bot.strategy_performance import report,attribute,format_report


class StrategyPerformanceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'live.sqlite3'
        self.journal=Journal(self.path,'fixture');self.addCleanup(self.journal.close)
        self.ledger=Ledger(self.journal);model_live.initialize(self.journal.db)
        self.config={'model_trading':dict(model_live.DEFAULTS),'markets':[]}
        self.at=100

    def fill(self,key,action,q,price,model=False,exchange='1',side='yes',at=None,requested=None):
        p=dict(idempotencyKey=key,exchangeId=exchange,action=action,side=side,quantity=q if requested is None else requested,price=price)
        def guard():
            if model:model_live.attach(self.journal.db,p,'race:'+exchange,{})
        self.journal.reserve(p,D(p['quantity'])*(D(price)+D('.01')),guard)
        self.ledger.record(p,q,price,'.01',at=self.at if at is None else at)
        self.at+=1
        return p

    def test_mixed_fifo_sale_splits_realized_profit_by_entry_route(self):
        self.fill('a','buy',10,'.20')
        self.fill('b','buy',10,'.30',True)
        self.fill('c','sell',15,'.40')
        r=report(self.path);other=r['routes']['other_entries'];model=r['routes']['model']
        self.assertEqual(other['realized_pnl_after_buffers'],D('1.80'))
        self.assertEqual(model['realized_pnl_after_buffers'],D('.40'))
        self.assertEqual(model['open_cost'],D('1.55'))
        self.assertEqual(model['sold_shares'],5)
        self.assertEqual(other['sold_shares'],10)
        self.assertTrue(r['reconciles_with_ledger'])
        self.assertEqual(model['gross_purchase_cost'],D('3.10'))
        self.assertEqual(sum(v['realized_pnl_after_buffers'] for v in r['by_race']),D('2.20'))

    def test_same_time_ties_follow_execution_insertion_order(self):
        self.fill('z','buy',3,'.20',True,at=100)
        self.fill('a','buy',5,'.40',at=100)
        self.fill('sell','sell',4,'.50',at=100)
        r=report(self.path)
        self.assertEqual(r['routes']['model']['sold_shares'],3)
        self.assertEqual(r['routes']['other_entries']['sold_shares'],1)

    def test_no_side_and_losses_are_attributed(self):
        self.fill('buy','buy',4,'.60',True,side='no')
        self.fill('sell','sell',2,'.40',side='no')
        m=report(self.path)['routes']['model']
        self.assertEqual(m['realized_pnl_after_buffers'],D('-.44'))
        self.assertEqual(m['open_cost'],D('1.22'))

    def test_pending_is_not_a_fill_and_uses_pilot_allowance(self):
        p=dict(idempotencyKey='pending',exchangeId='1',action='buy',side='yes',quantity=10,price='.5')
        self.journal.reserve(p,D('5.1'),lambda:model_live.attach(self.journal.db,p,'race:1',{}))
        r=report(self.path,self.config)
        self.assertEqual(r['routes']['model']['filled_buy_orders'],0)
        self.assertEqual(r['routes']['model']['pending_reserved_coins'],D('5.1'))
        self.assertEqual(r['model_budget']['remaining']['total'],D('994.9'))

    def test_zero_and_partial_fills_count_confirmed_cost_only(self):
        self.fill('zero','buy',0,'.50',True,requested=10)
        self.fill('partial','buy',3,'.50',True,requested=10)
        r=report(self.path,self.config)
        self.assertEqual(r['routes']['model']['zero_fill_orders'],1)
        self.assertEqual(r['routes']['model']['filled_buy_orders'],1)
        self.assertEqual(r['model_budget']['used']['total'],D('1.53'))

    def test_sales_do_not_recycle_model_gross_allowance(self):
        self.fill('buy','buy',10,'.50',True)
        self.fill('sell','sell',10,'.60')
        r=report(self.path,self.config)
        self.assertEqual(r['model_budget']['used']['total'],D('5.10'))
        self.assertEqual(r['routes']['model']['open_cost'],0)

    def test_markouts_separate_from_realized_profit_and_missing_stays_missing(self):
        self.fill('buy','buy',10,'.50',True)
        with self.journal.db:
            self.journal.db.execute("UPDATE performance_settings SET value='0'")
            self.journal.db.execute('INSERT INTO markouts VALUES (?,?,?,?,?)',('buy',300,410,'2',None))
        r=report(self.path,now=5000);m=r['routes']['model']
        self.assertEqual(m['realized_pnl_after_buffers'],0)
        self.assertEqual(m['buy_markouts_seconds']['300']['observations'],1)
        self.assertEqual(m['buy_markouts_seconds']['300']['sum_pnl_after_buffers'],'2')
        self.assertEqual(m['buy_markouts_seconds']['900']['missed'],1)
        self.assertIsNone(m['buy_markouts_seconds']['900']['pnl_per_share_after_buffers'])

    def test_no_marker_table_is_legacy_not_reconstructed_model(self):
        self.fill('old','buy',2,'.20')
        self.journal.db.execute('DROP TABLE model_orders');self.journal.db.commit()
        r=report(self.path,self.config)
        self.assertFalse(r['attribution_available'])
        self.assertEqual(r['model_budget']['status'],'not_initialized')
        self.assertEqual(r['routes']['other_entries']['filled_buy_orders'],1)

    def test_orphan_and_wrong_race_tags_fail(self):
        with self.journal.db:self.journal.db.execute("INSERT INTO model_orders VALUES ('missing','r','{}')")
        with self.assertRaises(ValueError):report(self.path)
        with self.journal.db:self.journal.db.execute('DELETE FROM model_orders')
        self.fill('buy','buy',2,'.20',True)
        with self.journal.db:
            self.journal.db.execute('CREATE TABLE race_bindings(exchange TEXT,race TEXT)')
            self.journal.db.execute("INSERT INTO race_bindings VALUES ('1','changed')")
        with self.assertRaises(ValueError):report(self.path)

    def test_oversell_fails_instead_of_reporting_profit(self):
        rows=[dict(key='s',action='sell',exchange='1',side='yes',quantity='1',price='.5',buffer='.01')]
        with self.assertRaises(ValueError):attribute(rows,set(),{})

    def test_closed_order_missing_execution_is_explicitly_incomplete(self):
        p=dict(idempotencyKey='missing',exchangeId='1',action='buy',side='yes',quantity=2,price='.5')
        self.journal.reserve(p,D('1.02'))
        self.journal.complete('missing',D('1.02'))
        r=report(self.path)
        self.assertFalse(r['accounting_complete'])
        self.assertEqual(r['routes']['other_entries']['closed_orders_missing_execution'],1)
        self.assertEqual(r['routes']['other_entries']['zero_fill_orders'],0)
        self.assertIn('INCOMPLETE',format_report(r))

    def test_diagnostics_separate_phases_and_bound_time(self):
        for at,phase,reason in [(99,'scan','old'),(90000,'scan','model_price_depth_or_inventory'),(90001,'preflight','model_entry'),(90002,'scan','model_input_unavailable'),(100000,'scan','future')]:
            self.journal.db.execute('INSERT INTO events VALUES (?,?,?)',(at,'model_decision',json.dumps(dict(phase=phase,reason=reason,status='blocked',error='model_no_recent_poll'))))
        self.journal.db.commit()
        r=report(self.path,now=90003)
        self.assertEqual(r['model_decisions_last_24h']['scan']['observations'],2)
        self.assertEqual(r['model_decisions_last_24h']['preflight']['observations'],1)
        self.assertEqual(r['latest_model_decision_age_seconds'],1)
        self.assertIn('model_no_recent_poll',format_report(r))

    def test_report_is_read_only_and_missing_path_not_created(self):
        before=hashlib.sha256(self.path.read_bytes()).hexdigest()
        report(self.path,self.config)
        self.assertEqual(before,hashlib.sha256(self.path.read_bytes()).hexdigest())
        missing=self.path.parent/'missing.sqlite3'
        self.assertEqual(report(missing)['status'],'no_journal');self.assertFalse(missing.exists())

    def test_cli_does_not_read_key_or_call_broker(self):
        from election_bot import __main__ as cli
        with patch.object(cli,'RUNTIME',self.path.parent),patch.object(cli,'key',side_effect=AssertionError('key accessed')),patch.object(cli,'Sig',side_effect=AssertionError('broker accessed')),patch('sys.argv',['election_bot','--config',str(self.path.parent/'missing.json'),'strategy-performance','--json']),patch('builtins.print') as output:
            cli.main()
        self.assertEqual(json.loads(output.call_args.args[0])['status'],'ok')
