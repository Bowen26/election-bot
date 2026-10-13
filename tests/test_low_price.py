import copy
import json
import io
from pathlib import Path
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from election_bot.low_price import settings, entry, analyze, report, format_report
from election_bot.leadership import load_events
from election_bot.strategy import D
from test_leadership import snapshot, T


def row(at=T,bid='.02',ask='.03',depth=400,ref='.08',exchange='2'):
    def book(b,a):return dict(bid=D(b),ask=D(a),bid_size=D(depth),ask_size=D(depth),observed=at,source=at)
    return dict(at=at,id=str(at)+exchange,exchange=exchange,aligned=True,
                SIG=book(bid,ask),Kalshi=book(ref,D(ref)+D('.01')),Polymarket=book(ref,D(ref)+D('.01')))


class PathTests(unittest.TestCase):
    def run_path(self,rows,now=None,**overrides):
        return analyze(rows,rows[-1]['at'] if now is None else now,settings(overrides))

    def test_entry_blockers_reported_without_inventing_opportunities(self):
        r=self.run_path([row(ref='.04')])
        self.assertEqual(r['episode_count'],0)
        self.assertEqual(r['entry_side_checks']['net_reference_gap'],1)
        self.assertEqual(r['entry_side_checks']['outside_price_band'],1)

    def test_entry_cost_target_and_no_mutation(self):
        r=row();before=copy.deepcopy(r);p=settings();c=entry(r,p)
        self.assertEqual(c['quantity'],100)
        self.assertEqual(c['entry_cost'],D('4'))
        self.assertEqual(c['target_bid'],D('.07'))
        self.assertEqual(r,before)
        self.assertEqual(entry(row(ref='.05'),p),None)

    def test_full_target_and_runner_retains_upside(self):
        r=self.run_path([row(),row(T+60,'.07','.08'),row(T+120,'.09','.10'),row(T+180,'.08','.09')])
        paths=r['episodes'][0]['paths']
        self.assertEqual(paths['full_target']['realized_pnl'],D('2'))
        self.assertEqual(paths['half_then_trail']['realized_pnl'],D('2.5'))
        self.assertEqual([f['quantity'] for f in paths['half_then_trail']['fills']],[50,50])
        self.assertEqual(paths['half_then_trail']['fills'][-1]['reason'],'trailing_exit')
        self.assertEqual(r['episode_count'],1)

    def test_stop_uses_actual_bid_not_promised_stop_price(self):
        r=self.run_path([row(),row(T+60,'.005','.015')])
        for p in r['episodes'][0]['paths'].values():
            self.assertEqual(p['fills'][0]['reason'],'stop')
            self.assertEqual(p['realized_pnl'],D('-4.5'))

    def test_timeout_uses_first_observed_bid_after_deadline(self):
        r=self.run_path([row(),row(T+121,'.025','.035')],max_hold_seconds=120,max_quote_gap_seconds=120)
        self.assertTrue(all(p['status']=='censored_quote_gap' for p in r['episodes'][0]['paths'].values()))
        r=self.run_path([row(),row(T+60),row(T+121,'.025','.035')],max_hold_seconds=120,max_quote_gap_seconds=120)
        for p in r['episodes'][0]['paths'].values():
            self.assertEqual(p['fills'][0]['at'],T+121)
            self.assertEqual(p['fills'][0]['reason'],'time_limit')

    def test_partial_depth_and_repeated_source_cannot_fill_again(self):
        a=row(T+60,'.07','.08',depth=40)
        duplicate=row(T+61,'.07','.08',depth=400);duplicate['SIG']['source']=T+60
        r=self.run_path([row(),a,duplicate])
        self.assertEqual(r['counts']['nonadvancing_quotes'],1)
        self.assertEqual(r['episodes'][0]['paths']['full_target']['remaining'],90)
        self.assertEqual(r['episodes'][0]['paths']['half_then_trail']['target_quantity_left'],40)

    def test_thin_exit_cannot_be_recorded_as_closed(self):
        r=self.run_path([row(),row(T+60,'.07','.08',depth=1)])
        for p in r['episodes'][0]['paths'].values():
            self.assertEqual(p['fills'],[])
            self.assertEqual(p['status'],'open')
        self.assertEqual(r['summary']['full_target']['unresolved_entry_cost'],4)

    def test_gap_does_not_manufacture_target_fill(self):
        r=self.run_path([row(),row(T+901,'.09','.10')])
        self.assertEqual(r['episode_count'],1)
        for p in r['episodes'][0]['paths'].values():
            self.assertEqual(p['status'],'censored_quote_gap');self.assertEqual(p['fills'],[])
        self.assertEqual(self.run_path([row()],now=T+901)['summary']['full_target']['closed_episodes'],0)

    def test_no_future_reference_required_to_exit_and_missing_not_signal_loss(self):
        a=row(T+60,'.04','.05');a['aligned']=False
        b=row(T+120,'.07','.08');b['aligned']=False
        r=self.run_path([row(),a,b])
        self.assertEqual(r['episodes'][0]['paths']['full_target']['fills'][0]['reason'],'profit_target')
        self.assertEqual(r['episodes'][0]['paths']['full_target']['fills'][0]['at'],T+120)

    def test_reference_deterioration(self):
        r=self.run_path([row(),row(T+60,ref='.025')])
        self.assertEqual(r['episodes'][0]['paths']['full_target']['fills'][0]['reason'],'reference_deteriorated')

    def test_no_orientation_complements_books(self):
        rows=[row(),row(T+60,'.07','.08')]
        for r in rows:
            for v in ('SIG','Kalshi','Polymarket'):
                b=r[v];b['bid'],b['ask']=1-b['ask'],1-b['bid']
                b['bid_size'],b['ask_size']=b['ask_size'],b['bid_size']
        r=self.run_path(rows)
        self.assertEqual(r['episodes'][0]['entry']['side'],'no')
        self.assertEqual(r['summary']['full_target']['closed_episode_net_pnl'],2)

    def test_entry_quality_spread_depth_and_reference_disagreement(self):
        p=settings()
        for r in [row(ask='.11'),row(bid='.005',ask='.03'),row(depth=7)]:
            self.assertIsNone(entry(r,p))
        r=row();r['Polymarket']['bid']=D('.13');r['Polymarket']['ask']=D('.14')
        self.assertIsNone(entry(r,p))
        r=row();r['aligned']=False;self.assertIsNone(entry(r,p))

    def test_entries_do_not_overlap_or_reenter_at_exit_snapshot(self):
        r=self.run_path([row(),row(T+60),row(T+120,'.005','.015'),row(T+180)])
        self.assertEqual(r['episode_count'],2)
        self.assertEqual(r['episodes'][1]['at'],T+180)

    def test_open_runner_not_counted_as_completed_profit(self):
        r=self.run_path([row(),row(T+60,'.07','.08')])
        s=r['summary']['half_then_trail']
        self.assertEqual(s['closed_episode_net_pnl'],0)
        self.assertEqual(s['unresolved_realized_pnl'],1)
        self.assertEqual(s['unresolved_entry_cost'],2)

    def test_settings_fail_on_invalid_values(self):
        for change in [dict(cost_buffer='nan'),dict(depth_fraction=2),dict(max_quantity=1.5),
                       dict(min_price='.11'),dict(max_hold_seconds=0),dict(unknown=3)]:
            with self.assertRaises(ValueError):settings(change)


class JournalTests(unittest.TestCase):
    def test_cli_needs_no_key_config_or_runtime_creation(self):
        from election_bot import __main__ as cli
        with tempfile.TemporaryDirectory() as tmp:
            runtime=Path(tmp)/'absent-runtime'
            buf=io.StringIO()
            with patch.object(cli,'RUNTIME',runtime), patch.object(cli,'key',side_effect=AssertionError('No credentials')) as key:
                with patch('sys.argv',['election_bot','--config',str(Path(tmp)/'missing.json'),'low-price-study','--json']), redirect_stdout(buf):
                    cli.main()
                key.assert_not_called()
            self.assertEqual(json.loads(buf.getvalue())['status'],'no_journal')
            self.assertFalse(runtime.exists())

    def test_report_readonly_no_creation_and_reuses_freshness_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'quotes.sqlite3'
            self.assertEqual(report(path,now=T)['status'],'no_journal')
            self.assertFalse(path.exists())
            db=sqlite3.connect(path);db.execute('CREATE TABLE events(at REAL,kind TEXT,detail TEXT)')
            a=snapshot(at=T,sig=('.02','.03'),k=('.08','.09'),p=('.08','.09'),depth=400)
            bad=snapshot('bad',T+60,sig=('.07','.08'),depth=400)
            bad['quotes'][0]['source_at']=T
            b=snapshot('b',T+120,sig=('.07','.08'),k=('.08','.09'),p=('.08','.09'),depth=400)
            for s in (a,bad,b):db.execute('INSERT INTO events VALUES(?,?,?)',(s['captured_at'],'quote_snapshot',json.dumps(s)))
            db.commit();db.close();before=path.read_bytes()
            r=report(path,now=T+120,hours=1)
            self.assertEqual(path.read_bytes(),before)
            self.assertEqual(r['quote_events']['invalid_SIG_or_snapshot'],1)
            self.assertEqual(r['episode_count'],1)
            self.assertEqual(r['summary']['full_target']['closed_episode_net_pnl'],2)
            self.assertIn('simulated, no orders',format_report(r))
            self.assertFalse((Path(tmp)/'paper.sqlite3').exists())
            # Entry before the requested window cannot leak from load_events' alignment padding.
            self.assertEqual(report(path,now=T+120,hours=.02)['episode_count'],0)

    def test_conflicting_snapshot_ids_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'quotes.sqlite3';db=sqlite3.connect(path)
            db.execute('CREATE TABLE events(at REAL,kind TEXT,detail TEXT)')
            for ask in ('.03','.04'):
                s=snapshot(sig=('.02',ask),k=('.08','.09'),p=('.08','.09'),depth=400)
                db.execute('INSERT INTO events VALUES(?,?,?)',(T,'quote_snapshot',json.dumps(s)))
            db.commit();db.close()
            r=report(path,now=T,hours=1)
            self.assertEqual(r['episode_count'],0)
            self.assertEqual(r['quote_events']['conflicting_snapshot_ids'],1)


class MomentumTests(unittest.TestCase):
    def history(self):
        return [row(T,'.02','.03',ref='.02'),row(T+60,'.025','.035',ref='.02'),
                row(T+120,'.03','.04',ref='.02')]

    def test_momentum_enters_without_reference_discount_or_valid_references(self):
        rows=self.history()
        for r in rows:r['aligned']=False
        r=analyze(rows,T+120,settings(),'sig_momentum')
        self.assertEqual(r['episode_count'],1)
        c=r['episodes'][0]['entry']
        self.assertEqual(c['entry_mode'],'sig_momentum')
        self.assertEqual(c['momentum']['snapshot_ids'],[r['id'] for r in rows])
        self.assertEqual(c['momentum']['breakout_level'],D('.025'))
        self.assertIsNone(c['reference_bid'])
        self.assertEqual(analyze(rows,T+120,settings())['episode_count'],0)

    def test_features_are_causal_no_future_row_changes_entry(self):
        rows=self.history()
        a=analyze(rows,T+120,settings(),'sig_momentum')['episodes'][0]
        b=analyze(rows+[row(T+180,'.09','.10')],T+180,settings(),'sig_momentum')['episodes'][0]
        self.assertEqual(a['entry'],b['entry']);self.assertEqual(a['at'],T+120)
        # A directly supplied future history item is excluded too.
        self.assertEqual(entry(rows[-1],settings(),entry_mode='sig_momentum',history=rows[:2]+[row(T+180)])['momentum'],a['entry']['momentum'])

    def test_time_spacing_and_observation_gap(self):
        rows=[row(T,'.02','.03'),row(T+10,'.025','.035'),row(T+20,'.03','.04')]
        self.assertEqual(analyze(rows,T+20,settings(),'sig_momentum')['episode_count'],0)
        rows=self.history();rows.append(row(T+1100,'.05','.06'))
        r=analyze(rows,T+1100,settings(),'sig_momentum')
        self.assertEqual(r['episode_count'],1)
        self.assertEqual(r['episodes'][0]['paths']['full_target']['status'],'censored_quote_gap')
        # Gap during warmup cannot be bridged to manufacture a trend.
        self.assertEqual(analyze([rows[0],rows[1],rows[-1]],T+1100,settings(),'sig_momentum')['episode_count'],0)

    def test_duplicate_source_does_not_build_history(self):
        rows=self.history();rows[1]['SIG']['source']=T
        self.assertEqual(analyze(rows,T+120,settings(),'sig_momentum')['episode_count'],0)

    def test_ask_only_rise_reversal_and_no_new_high_fail(self):
        scenarios=[
            [row(T,'.02','.03'),row(T+60,'.02','.035'),row(T+120,'.02','.04')],
            [row(T,'.02','.03'),row(T+60,'.04','.05'),row(T+120,'.03','.04')],
            [row(T,'.02','.03'),row(T+60,'.03','.04'),row(T+120,'.03','.04')]]
        for rows in scenarios:
            with self.subTest(rows=rows):
                self.assertEqual(analyze(rows,T+120,settings(),'sig_momentum')['episode_count'],0)

    def test_same_risk_exits_but_not_absolute_external_bid_exit(self):
        rows=self.history()+[row(T+180,'.04','.05',ref='.02'),row(T+240,'.085','.095',ref='.02')]
        r=analyze(rows,T+240,settings(),'sig_momentum')
        path=r['episodes'][0]['paths']['full_target']
        self.assertEqual(path['fills'][0]['at'],T+240)
        self.assertEqual(path['fills'][0]['reason'],'profit_target')
        self.assertEqual(path['realized_pnl'],D('2.5'))
        r=analyze(self.history()+[row(T+180,'.025','.035')],T+180,settings(),'sig_momentum')
        self.assertEqual(r['episodes'][0]['paths']['full_target']['fills'][0]['reason'],'failed_breakout')

    def test_no_contract_orientation_uses_complemented_momentum(self):
        rows=self.history()
        for r in rows:
            b=r['SIG'];b['bid'],b['ask']=1-b['ask'],1-b['bid']
        r=analyze(rows,T+120,settings(),'sig_momentum')
        self.assertEqual(r['episodes'][0]['entry']['side'],'no')
        self.assertEqual(r['episodes'][0]['entry']['momentum']['bid_rise'],D('.01'))

    def test_comparison_cohorts_are_separate_same_window_and_cli(self):
        from election_bot import __main__ as cli
        with tempfile.TemporaryDirectory() as tmp:
            runtime=Path(tmp);path=runtime/'live.sqlite3';db=sqlite3.connect(path)
            db.execute('CREATE TABLE events(at REAL,kind TEXT,detail TEXT)')
            for r in self.history():
                q=r['SIG'];s=snapshot(r['id'],r['at'],sig=(str(q['bid']),str(q['ask'])),k=('.02','.03'),p=('.02','.03'),depth=400)
                db.execute('INSERT INTO events VALUES(?,?,?)',(r['at'],'quote_snapshot',json.dumps(s)))
            db.commit();db.close();before=path.read_bytes()
            r=report(path,hours=1,now=T+120,entry_mode='compare')
            self.assertEqual(r['studies']['external_gap']['episode_count'],0)
            self.assertEqual(r['studies']['sig_momentum']['episode_count'],1)
            self.assertIn('Entry mode: sig_momentum',format_report(r))
            with patch.object(cli,'RUNTIME',runtime),patch('election_bot.low_price.time.time',return_value=T+120):
                with patch('sys.argv',['election_bot','low-price-study','--entry-mode','compare','--json']),redirect_stdout(io.StringIO()) as buf:
                    cli.main()
            self.assertEqual(json.loads(buf.getvalue())['studies']['sig_momentum']['episode_count'],1)
            self.assertEqual(path.read_bytes(),before)

    def test_invalid_mode_and_history_window_fail(self):
        with self.assertRaises(ValueError):analyze([],T,settings(),'typo')
        with self.assertRaises(ValueError):settings(dict(momentum_spacing_seconds=100))
        with self.assertRaises(ValueError):settings(dict(momentum_window_seconds=100))
