import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

from election_bot import __main__ as cli
from election_bot.clients import HTTP, References
from election_bot.diagnostics import quote_summary
from election_bot.exit_study import checked_exit, experiment, report, format_report
from election_bot.strategy import Book, D, choose_exit
from test_leadership import snapshot, T

SETTINGS = {'max_age_seconds': 15, 'max_reference_spread': '.08',
            'max_reference_disagreement': '.08', 'minimum_edge': '.05',
            'cost_buffer_per_share': '.01', 'min_reference_depth': '20', 'max_shares_per_order': 100}
EXECUTION = {'sell_enabled': True, 'exit_edge': '.02', 'take_profit_min': '.02'}


def books(price='.745', bids=100, asks=5):
    return Book.make([(price, 200)], [(D(price)+D('.005'), 200)]), [
        Book.make([('.73', bids)], [('.75', asks)]),
        Book.make([('.74', bids)], [('.76', asks)])]


class ExitPolicyTests(unittest.TestCase):
    def evaluate(self, policy, price='.745', bid_depth=100, ask_depth=5, held=100, cost=60, cap=None, basis=None):
        book, refs = books(price, bid_depth, ask_depth)
        detail = {}
        signal = checked_exit(book, refs, SETTINGS, EXECUTION, D(held), D(cost), D(50),
                              basis or (lambda side, qty: D('.60')*qty), detail, cap, policy)
        return signal, detail

    def test_legacy_unchanged_and_route_depth_uses_bids_for_convergence(self):
        old, detail = self.evaluate('legacy')
        self.assertIsNone(old); self.assertEqual(detail['reason'], 'reference_ask_depth')
        new, detail = self.evaluate('route_depth')
        self.assertEqual(new.reason, 'convergence_take_profit')
        self.assertEqual(new.quantity, 67)
        self.assertTrue(detail['routes']['convergence_take_profit']['fifo_profit_passed'])

    def test_overpriced_route_still_requires_ask_depth(self):
        # No profitable convergence at this cost; thin asks cannot authorize overpricing.
        for policy in ('route_depth', 'sig_depth'):
            signal, detail = self.evaluate(policy, price='.80', cost=90)
            self.assertIsNone(signal); self.assertEqual(detail['reason'], 'route_reference_depth')
        signal, _ = self.evaluate('sig_depth', price='.80', cost=90, ask_depth=20)
        self.assertEqual(signal.reason, 'overpriced_exit')  # This route can realize a loss.

    def test_reference_bid_depth_is_gate_for_convergence(self):
        for policy in ('route_depth', 'sig_depth'):
            signal, detail = self.evaluate(policy, bid_depth=19, ask_depth=100)
            self.assertIsNone(signal); self.assertEqual(detail['reason'], 'route_reference_depth')
        signal, _ = self.evaluate('route_depth', bid_depth=20)
        self.assertEqual(signal.quantity, 20)
        signal, _ = self.evaluate('sig_depth', bid_depth=20)
        self.assertEqual(signal.quantity, 67)

    def test_sig_size_respects_held_exposure_notional_and_SIG_depth(self):
        signal, _ = self.evaluate('sig_depth', cap=D(3))
        self.assertEqual(signal.quantity, 3)
        signal, _ = self.evaluate('sig_depth', held=2, cost=1)
        self.assertEqual(signal.quantity, 2)
        signal, detail = self.evaluate('sig_depth', cap=D(0))
        self.assertIsNone(signal); self.assertEqual(detail['reason'], 'exposure_limit')
        book, refs = books(); book.bids[0] = (book.bids[0][0], D(4))
        signal = choose_exit(book, refs, SETTINGS, EXECUTION, 100, 60, 50, reference_policy='sig_depth')
        self.assertEqual(signal.quantity, 4)

    def test_FIFO_can_reject_average_cost_profit_for_every_policy(self):
        for policy in ('legacy', 'route_depth', 'sig_depth'):
            signal, detail = self.evaluate(policy, ask_depth=100, basis=lambda side, qty: D('.74')*qty)
            self.assertIsNone(signal); self.assertEqual(detail['reason'], 'fifo_profit_below_minimum')

    def test_no_side_mirrors_yes_and_never_flips_inventory(self):
        book, refs = books()
        detail = {}
        signal = checked_exit(book.complement(), [r.complement() for r in refs], SETTINGS,
            EXECUTION, -100, 60, 50, lambda side, qty: D('.60')*qty, detail, 3, 'sig_depth')
        self.assertEqual((signal.side, signal.price, signal.quantity), ('no', D('.745'), 3))

    def test_bad_quotes_and_unknown_policy_never_bypass_guards(self):
        book, refs = books(); refs[1].source_at -= 100
        for policy in ('legacy', 'route_depth', 'sig_depth'):
            with self.assertRaises(ValueError):
                choose_exit(book, refs, SETTINGS, EXECUTION, 100, 60, 50, reference_policy=policy)
        with self.assertRaisesRegex(ValueError, 'policy'):
            choose_exit(book, refs, SETTINGS, EXECUTION, 100, 60, 50, reference_policy='typo')

    def test_disabled_sells_suppress_all_experiments(self):
        book, refs = books(); execution = {**EXECUTION, 'sell_enabled': False}
        result = experiment(book, refs, SETTINGS, execution, 100, 60, 50,
                            lambda side, qty: D('.6')*qty, None, {})
        self.assertTrue(all(r['candidate'] is None for r in result['decisions']))
        self.assertTrue(all(r['check']['reason'] == 'selling_disabled' for r in result['decisions']))


class TimingTests(unittest.TestCase):
    def test_receipt_time_survives_construction_and_complement(self):
        transport = {'received_at': 100, 'request_seconds': .2, 'cache_age_seconds': 0,
                     'http_date_minus_cache_age_at': 99}
        with patch('election_bot.strategy.time.time', return_value=120):
            book = Book.make([('.5',100)],[('.6',100)], source_at=100, observed_at=100, transport=transport)
            self.assertEqual(book.observed_at,100)
            self.assertIn('local_stale', book.diagnostic(15)['issues'])
            self.assertEqual(book.complement().transport, transport)
            self.assertEqual(book.diagnostic(15)['transport']['http_origin_age_seconds'],21)

    def test_HTTP_records_only_timing_and_clears_after_failure(self):
        client = HTTP('https://clob.polymarket.com')
        response = Mock(); response.read.return_value=b'{}'
        response.headers={'Date':'Tue, 14 Nov 2023 22:13:20 GMT','Age':'4'}
        cm=Mock(); cm.__enter__=Mock(return_value=response);cm.__exit__=Mock(return_value=False)
        client.opener=Mock(); client.opener.open.return_value=cm
        with patch('election_bot.clients.time.time', side_effect=[T,T+2]):
            self.assertEqual(client.request('/book'),{})
        self.assertEqual(client.last_timing['request_seconds'],2)
        self.assertEqual(client.last_timing['http_date_minus_cache_age_at'],T-4)
        self.assertEqual(client.last_timing['received_at'],T+2)
        self.assertNotIn('headers',client.last_timing)
        client.opener.open.side_effect=TimeoutError()
        with self.assertRaises(RuntimeError): client.request('/book')
        self.assertIsNone(client.last_timing)

    def test_reference_books_use_each_responses_receipt_time(self):
        refs=References()
        refs.kalshi.request=Mock(return_value={'orderbook_fp':{'yes_dollars':[['.5',100]],'no_dollars':[['.4',100]]}})
        refs.clob.request=Mock(return_value={'asset_id':'token','market':'condition','timestamp':str(int(T*1000)),
            'bids':[{'price':'.5','size':'100'}],'asks':[{'price':'.6','size':'100'}]})
        refs.kalshi.server_at=T
        refs.kalshi.last_timing={'received_at':T+1};refs.clob.last_timing={'received_at':T+8}
        mapping={'kalshi_ticker':'X','kalshi_yes_matches_sig_yes':True,'polymarket_yes_matches_sig_yes':False}
        metadata=({'status':'active'}, {'active':True,'closed':False,'acceptingOrders':True,'conditionId':'condition'},'token')
        with patch('election_bot.strategy.time.time',return_value=T+9):
            kb,pb=refs.books(mapping,metadata)
        self.assertEqual(kb.observed_at,T+1);self.assertEqual(pb.observed_at,T+8)
        self.assertEqual(pb.source_at,T)  # Source time is never replaced by HTTP receipt.

    def test_quote_summary_distinguishes_transport_causes_without_changing_quality(self):
        cases=[({'request_seconds':1,'cache_age_seconds':0,'response_age_seconds':1,'http_origin_age_seconds':1},'old_source_timestamp_recent_HTTP_response'),
               ({'request_seconds':1,'cache_age_seconds':60},'cached_response_older_than_limit'),
               ({'request_seconds':20,'cache_age_seconds':0},'slow_request_or_local_delay'),
               ({},'old_source_timestamp_transport_unverified')]
        for transport,expected in cases:
            event={'phase':'scan','books':[{'venue':'Polymarket','issues':['source_stale'],'max_age_seconds':15,
                  'source_age_seconds':60,'local_age_seconds':1,'transport':transport}]}
            row=quote_summary([event],[])['by_venue_and_phase'][0]
            self.assertEqual(row['timing_classes'],{expected:1})
            self.assertEqual(row['issues'],{'source_stale':1})


class ExitReportTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'live.sqlite3'
        self.db=sqlite3.connect(self.path);self.addCleanup(self.db.close)
        self.db.execute('CREATE TABLE events(at REAL,kind TEXT,detail TEXT)')

    def put(self,kind,data,at=T):
        self.db.execute('INSERT INTO events VALUES(?,?,?)',(at,kind,json.dumps(data,default=str)));self.db.commit()

    def seed(self):
        with patch('election_bot.strategy.time.time',return_value=T):
            book,refs=books()
            detail={};basis=lambda side,qty:D('.60')*qty
            baseline=checked_exit(book,refs,SETTINGS,EXECUTION,100,60,50,basis,detail)
            data=experiment(book,refs,SETTINGS,EXECUTION,100,60,50,basis,baseline,detail)
        data.update(exchange='2',snapshot_id='a',phase='scan')
        snap=snapshot(sig=('.745','.750'),k=('.73','.75'),p=('.74','.76'),depth=200)
        self.put('quote_snapshot',snap)
        self.put('exit_shadow',data)
        return data

    def test_report_missing_and_no_credentials_is_read_only(self):
        missing=Path(self.temp.name)/'missing'/'live.sqlite3'
        self.assertEqual(report(missing)['status'],'No exit study journal yet')
        with patch.object(cli,'RUNTIME',missing.parent),patch.object(cli,'key') as key,patch.object(cli,'output'),patch('sys.argv',['bot','exit-study','--json']):
            cli.main();key.assert_not_called()
        self.assertFalse(missing.parent.exists())
        self.seed(); before=self.path.read_bytes();r=report(self.path,now=T+500)
        self.assertEqual(before,self.path.read_bytes());self.assertIn('EXIT DEPTH',format_report(r))

    def test_sell_now_advantage_positive_when_future_bid_falls(self):
        self.seed();self.put('quote_snapshot',snapshot('later',T+310,sig=('.70','.71')),T+310)
        r=report(self.path,now=T+500)
        self.assertEqual(r['proposals']['route_depth']['additional_vs_legacy'],1)
        p=r['horizons']['300']['sig_depth'];self.assertEqual(p['measured'],1)
        self.assertEqual(D(p['sell_now_advantage_per_share']),D('.045'))
        self.assertEqual(D(p['immediate_hypothetical_pnl_per_share']),D('.135'))
        self.assertEqual(p['mean_followup_delay_seconds'],10)
        self.assertEqual(r['horizons']['300']['legacy']['measured'],0)

    def test_thin_first_future_not_replaced_by_favorable_later_quote(self):
        self.seed();self.put('quote_snapshot',snapshot('thin',T+300,sig=('.70','.71'),depth=1),T+300)
        self.put('quote_snapshot',snapshot('deep',T+310,sig=('.60','.61')),T+310)
        p=report(self.path,now=T+500)['horizons']['300']['sig_depth']
        self.assertEqual(p['statuses'],{'insufficient_future_depth':1});self.assertIsNone(p['sell_now_advantage_per_share'])

    def test_missing_not_due_awaiting_and_legacy_quotes_not_invented(self):
        self.seed()
        for now,reason in ((T+100,'not_due'),(T+350,'awaiting_quote'),(T+500,'missed')):
            self.assertEqual(report(self.path,now=now)['horizons']['300']['sig_depth']['statuses'],{reason:1})
        self.db.execute("DELETE FROM events WHERE kind='exit_shadow'");self.db.commit()
        self.assertEqual(report(self.path,now=T+500)['events'].get('valid_comparisons',0),0)

    def test_duplicates_conflicts_and_invalid_economics(self):
        data=self.seed();self.put('exit_shadow',data)
        self.assertEqual(report(self.path,now=T+500)['events']['valid_comparisons'],1)
        data['decisions'][1]['candidate']['quantity']=999;self.put('exit_shadow',data)
        self.assertEqual(report(self.path,now=T+500)['events']['conflicting_ids'],1)
        self.db.execute("DELETE FROM events WHERE kind='exit_shadow'");self.db.commit();self.put('exit_shadow',data)
        self.assertEqual(report(self.path,now=T+500)['events']['invalid_economics'],1)

    def test_NO_exit_economics_and_negative_sell_now_advantage(self):
        data=self.seed()
        self.db.execute('DELETE FROM events');self.db.commit()
        data['held']=-100
        for row in data['decisions']:
            if row['candidate']:
                row['candidate']['side']='no'
        self.put('quote_snapshot',snapshot(sig=('.25','.255')))
        self.put('exit_shadow',data)
        self.put('quote_snapshot',snapshot('future',T+310,sig=('.19','.20')),T+310)
        p=report(self.path,now=T+500)['horizons']['300']['sig_depth']
        self.assertEqual(p['measured'],1)
        self.assertEqual(D(p['sell_now_advantage_per_share']),D('-.055'))

    def test_recent_pre_target_quote_and_preflight_not_followups(self):
        self.seed()
        self.put('quote_snapshot',snapshot('early',T+299,sig=('.70','.71')),T+299)
        pre=snapshot('pre',T+301,sig=('.60','.61'));pre['phase']='preflight'
        self.put('quote_snapshot',pre,T+301)
        old=snapshot('old',T+305,sig=('.60','.61'));old['quotes'][0]['source_at']=T+299
        self.put('quote_snapshot',old,T+305)
        self.put('quote_snapshot',snapshot('later',T+310,sig=('.72','.73')),T+310)
        p=report(self.path,now=T+500)['horizons']['300']['sig_depth']
        self.assertEqual(D(p['sell_now_advantage_per_share']),D('.025'))
        self.assertEqual(p['mean_followup_delay_seconds'],10)

    def test_followups_do_not_double_count_overlapping_exit_proposals(self):
        data=self.seed()
        newer=json.loads(json.dumps(data,default=str));newer['snapshot_id']='b'
        self.put('quote_snapshot',snapshot('b',T+100,sig=('.745','.750')),T+100)
        self.put('exit_shadow',newer,T+100)
        self.put('quote_snapshot',snapshot('future',T+310,sig=('.70','.71')),T+310)
        r=report(self.path,now=T+600)
        self.assertEqual(r['proposals']['sig_depth']['eligible_proposals'],2)
        self.assertEqual(r['horizons']['300']['sig_depth']['measured'],1)

    def test_measurement_unavailable_and_invalid_window(self):
        data=self.seed();self.db.execute("DELETE FROM events WHERE kind='exit_shadow'");self.db.commit()
        data['status']='unavailable';self.put('exit_shadow',data)
        self.assertEqual(report(self.path,now=T+500)['events']['measurement_unavailable'],1)
        for hours in (0,-1,721):
            with self.assertRaises(ValueError):report(self.path,hours=hours)
