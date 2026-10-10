import copy
from datetime import datetime, timezone
import json
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from election_bot.demo import fixture
from election_bot.polling import PollStore
from election_bot.poll_collector import collect, status, fetch, PollCollector, CollectionStopped, INTERVAL
from election_bot.votehub import adapt, normalize, bindings

T = datetime(2026, 10, 10, 15, tzinfo=timezone.utc).timestamp()


def config():
    c, _, _ = fixture()
    c['markets'][0].update(enabled=True, office='senate', race_key='2026:senate:NH', exposure_sign=1)
    return c


def row(**changes):
    r = dict(id='test1', poll_type='us-senator', subject='2026 New Hampshire', seat_name=None,
        pollster='YouGov', sample_size=800, population='lv', internal=False, partisan=None,
        start_date='2026-10-01', end_date='2026-10-03', created_at='2026-10-04',
        answers=[dict(choice='Chris Pappas', pct=51), dict(choice='John Sununu', pct=45)],
        url='https://example.test/survey', sponsors=[])
    return dict(r, **changes)


def fetcher(rows):
    return lambda office, now, stopped: ('https://api.votehub.com/polls', rows if office == 'us-senator' else [])


class AdapterTests(unittest.TestCase):
    def adapt(self, rows, previous=None):
        return adapt(rows, config(), previous or {}, T)

    def test_unknown_residual_is_not_fabricated_as_zero(self):
        p = normalize(row(), {'2026:senate:NH'}, T)
        self.assertEqual(p['other_pct'], 4)
        self.assertIsNone(p['partisan'])
        self.assertIn('including_undecided', p['other_pct_basis'])
        self.assertEqual(p['published_at'], '2026-10-10T15:00:00+00:00')
        self.assertIn('23:59:59', p['field_end'])

    def test_reject_invalid_inputs(self):
        examples = [dict(subject='2026 New Hampshire Democratic'), dict(poll_type='governor'),
            dict(seat_name='Special'), dict(partisan='REP'), dict(internal=True),
            dict(internal=None), dict(pollster='New pollster'), dict(sample_size=True),
            dict(sample_size='unknown'), dict(population='a'), dict(start_date='2026-10-06'),
            dict(end_date='2026-10-11'), dict(created_at='2026-10-11'),
            dict(created_at='2026-10-01'), dict(created_at='2026-1-04'),
            dict(url='http://example.test/survey'), dict(url='https://user:secret@example.test'),
            dict(answers=[dict(choice='Chris Pappas', pct=53), dict(choice='John Sununu', pct=49)]),
            dict(answers=[dict(choice='Chris Pappas', pct=51), dict(choice='Someone else', pct=45)]),
            dict(answers=[dict(choice='Chris Pappas', pct=51), dict(choice='Chris Pappas', pct=45)])]
        for changes in examples:
            with self.subTest(changes=changes):
                evidence, rejected = self.adapt([row(**changes)])
                self.assertEqual(evidence['polls'], [])
                self.assertEqual(len(rejected), 1)

    def test_missing_flags_and_malformed_rows(self):
        r = row(); del r['partisan']
        evidence, rejected = self.adapt([r, None, 'bad', {'id': 'broken'}])
        self.assertEqual(evidence['polls'], [])
        self.assertEqual(len(rejected), 4)

    def test_exact_duplicate_collapses_and_conflicting_id_rejects(self):
        evidence, rejected = self.adapt([row(), row()])
        self.assertEqual(len(evidence['polls']), 1)
        self.assertEqual(rejected, [])
        evidence, rejected = self.adapt([row(), row(sample_size=900)])
        self.assertEqual(evidence['polls'], [])
        self.assertEqual(rejected[0]['reason'], 'conflicting_provider_id')

    def test_lv_preferred_over_rv_same_original_survey(self):
        evidence, rejected = self.adapt([row(id='rv', population='rv', sample_size=1000), row(id='lv')])
        self.assertEqual([p['poll_id'] for p in evidence['polls']], ['votehub:lv'])
        self.assertEqual(rejected[0]['reason'], 'duplicate_or_superseded_subsample')

    def test_same_population_conflicting_variants_quarantined_even_different_urls(self):
        evidence, rejected = self.adapt([row(id='one'), row(id='two', sample_size=900, url='https://example.test/other')])
        self.assertEqual(evidence['polls'], [])
        self.assertEqual(len(rejected), 2)

    def test_late_bad_duplicate_does_not_keep_earlier_good(self):
        evidence, rejected = self.adapt([row(), row(sample_size=None)])
        self.assertEqual(evidence['polls'], [])

    def test_changed_identity_withdraws_old(self):
        p = normalize(row(), {'2026:senate:NH'}, T-1)
        evidence, rejected = self.adapt([row(pollster='Emerson College')], {p['poll_id']:p})
        self.assertTrue(evidence['polls'][0]['withdrawn'])
        self.assertEqual(evidence['polls'][0]['pollster'], 'yougov')

    def test_only_enabled_signed_exact_office_bound(self):
        c=config()
        for field, value in [('enabled',False), ('office','governor'), ('exposure_sign',True), ('race_key','2026:senate:MA')]:
            bad=copy.deepcopy(c); bad['markets'][0][field]=value
            self.assertEqual(bindings(bad), [])
        c['markets'][0]['exposure_sign']=-1
        self.assertEqual(bindings(c)[0]['yes_party'], 'REP')


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name); self.config=config()

    def collect(self, rows=None, now=T, **kwargs):
        return collect(self.root, self.config, force=True, now=now,
                       fetcher=fetcher([row()] if rows is None else rows), **kwargs)

    def latest(self):
        store=PollStore(self.root/'fair-value.sqlite3', readonly=True)
        try:return store.latest('poll', T+10000)
        finally:store.close()

    def test_no_credentials_or_trading_journal_and_persisted_cadence(self):
        result=self.collect()
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['accepted_polls'], 1)
        self.assertEqual(result['bound_races'], 1)
        self.assertEqual(result['staged_only_offices'], ['governor','us-representative'])
        self.assertFalse((self.root/'live.sqlite3').exists())
        with patch('election_bot.poll_collector.fetch') as network:
            deferred=collect(self.root,self.config,now=T+30,fetcher=network)
        self.assertTrue(deferred['deferred']); network.assert_not_called()
        self.assertFalse(status(self.root,T+30)['stale'])
        self.assertTrue(status(self.root,T+2*INTERVAL+1)['stale'])

    def test_idempotence_keeps_first_availability_and_receipt_time(self):
        self.collect(); before=self.latest()[0]
        result=self.collect(now=T+3600); after=self.latest()[0]
        self.assertEqual(result['ingestion']['inserted_versions'],0)
        self.assertEqual(before,after)

    def test_revision_retains_asof_history_and_new_availability(self):
        self.collect()
        self.collect([row(sample_size=900)], now=T+3600)
        store=PollStore(self.root/'fair-value.sqlite3',readonly=True)
        try:
            old=store.latest('poll',T+1)[0]; new=store.latest('poll',T+3601)[0]
            self.assertEqual(old['sample_size'],800); self.assertEqual(new['sample_size'],900)
            self.assertNotEqual(old['published_at'],new['published_at'])
        finally:store.close()

    def test_invalid_revision_withdraws_then_valid_correction_can_return(self):
        self.collect()
        result=self.collect([row(sample_size=None)],now=T+100)
        self.assertEqual(result['withdrawn_polls'],1)
        self.assertTrue(self.latest()[0]['withdrawn'])
        result=self.collect(now=T+200)
        self.assertFalse(self.latest()[0]['withdrawn'])
        self.assertEqual(self.latest()[0]['_received_at'],T+200)

    def test_conflicting_variant_withdraws_previous_valid_row(self):
        self.collect()
        result=self.collect([row(),row(id='second',sample_size=900)],now=T+1)
        self.assertEqual(result['accepted_polls'],0)
        self.assertEqual(result['withdrawn_polls'],1)

    def test_network_error_never_refreshes_success_or_raises_into_trading(self):
        self.collect(); previous=self.latest()
        result=collect(self.root,self.config,force=True,now=T+3600,fetcher=Mock(side_effect=TimeoutError))
        self.assertEqual(result['status'],'error')
        self.assertEqual(result['last_success_at'],T)
        self.assertEqual(self.latest(),previous)

    def test_governor_failure_does_not_partially_import(self):
        call=Mock(side_effect=[('https://api.votehub.com/polls',[row()]),TimeoutError()])
        result=collect(self.root,self.config,force=True,now=T,fetcher=call)
        self.assertEqual(result['status'],'error')
        self.assertFalse((self.root/'fair-value.sqlite3').exists())

    def test_stop_during_fetch_does_not_import(self):
        flag=[False]
        def feed(office, now, stopped):
            flag[0]=True
            return 'https://api.votehub.com/polls',[row()]
        result=collect(self.root,self.config,force=True,now=T,fetcher=feed,stopped=lambda:flag[0])
        self.assertEqual(result['status'],'stopped')
        self.assertFalse((self.root/'fair-value.sqlite3').exists())

    def test_backward_clock_does_not_create_retroactive_version(self):
        self.collect()
        result=self.collect([row(sample_size=900)],now=T-1)
        self.assertEqual(result['status'],'error')
        self.assertEqual(self.latest()[0]['sample_size'],800)

    def test_manual_prior_is_preserved(self):
        self.collect()
        store=PollStore(self.root/'fair-value.sqlite3')
        try:
            race={k:v for k,v in store.latest('race',T)[0].items() if not k.startswith('_')}
            race['prior']=dict(margin_pp=2,sd_pp=10,source_url='https://example.test/prior',
                               method='test sourced prior',published_at='2026-10-01T00:00:00Z')
            store.ingest(dict(schema_version=1,races=[race],polls=[]),self.config,T+1)
        finally:store.close()
        self.collect(now=T+2)
        store=PollStore(self.root/'fair-value.sqlite3',readonly=True)
        try:self.assertEqual(store.latest('race',T+3)[0]['prior']['margin_pp'],2)
        finally:store.close()

    def test_exclusive_collector_lock_and_worker_stop(self):
        from election_bot.state import exclusive_lock
        with exclusive_lock(self.root/'poll-collector'):
            with self.assertRaises(RuntimeError):self.collect()
        worker=PollCollector(self.root,self.config)
        (self.root/'STOP').touch()
        with patch('election_bot.poll_collector.collect') as call:
            worker.start(); worker.close()
        call.assert_not_called();self.assertFalse(worker.thread.is_alive())


class FetchTests(unittest.TestCase):
    def response(self, payload):
        response=Mock();response.read.side_effect=[json.dumps(payload).encode(),b'']
        response.__enter__=Mock(return_value=response);response.__exit__=Mock(return_value=False)
        return response

    def test_supported_envelopes_and_unknown_pagination(self):
        for payload, valid in [([row()],True),({'polls':[row()]},True),({'polls':[row()],'next':'page2'},False)]:
            with patch('urllib.request.build_opener') as opener:
                opener.return_value.open.return_value=self.response(payload)
                if valid:
                    url,rows=fetch('us-senator',T,lambda:False)
                    self.assertEqual(len(rows),1); self.assertIn('from_date=',url)
                    self.assertEqual(opener.return_value.open.call_args.kwargs['timeout'],10)
                else:
                    with self.assertRaises(ValueError):fetch('us-senator',T,lambda:False)

    def test_oversized_response_and_stop(self):
        for stopped in (False,True):
            with patch('urllib.request.build_opener') as opener:
                response=self.response([]);response.read.side_effect=[b'x'*4000001]
                opener.return_value.open.return_value=response
                with self.assertRaises(CollectionStopped if stopped else ValueError):
                    fetch('us-senator',T,lambda:stopped)


class CLITests(unittest.TestCase):
    def test_collection_dispatch_needs_no_key_or_broker(self):
        from election_bot import __main__ as cli
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/'config.json';path.write_text(json.dumps(config()))
            with patch.object(cli,'RUNTIME',Path(root)), patch.object(cli,'key',side_effect=AssertionError('key requested')), \
                 patch.object(cli,'Sig',side_effect=AssertionError('broker created')), \
                 patch('election_bot.poll_collector.collect',return_value={'status':'ok'}) as call, \
                 patch('sys.argv',['bot','--config',str(path),'fair-value','--collect-public']), \
                 patch('sys.stdout',new_callable=io.StringIO):
                cli.main()
            self.assertTrue(call.call_args.kwargs['force'])

    def test_worker_survives_collection_failure_and_closes(self):
        with tempfile.TemporaryDirectory() as root:
            worker=PollCollector(root,config())
            def fail(*args,**kwargs):
                worker.halt.set()
                raise OSError('disk unavailable')
            with patch('election_bot.poll_collector.collect',side_effect=fail) as call:
                worker.start();worker.thread.join(timeout=1);worker.close()
            call.assert_called_once()
            self.assertFalse(worker.thread.is_alive())
