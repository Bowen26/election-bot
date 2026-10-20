import copy
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

from election_bot.active_engine import ActiveEngine
from election_bot.clients import APIError, contract_record, fingerprint
from election_bot.demo import fixture
from election_bot import entry_timing
from election_bot.entry_timing import confirmation, outcome, side_quote
from election_bot.leadership import analyze as lead_analyze, load_events
from election_bot.polling import PollStore, validate_poll
from election_bot.research_followups import ResearchFollowups, settings
from election_bot.state import Journal
from election_bot.strategy import Book, D
from election_bot.votehub import adapt, bindings, normalize
from election_bot import model_live

from test_active import Broker, EXECUTION
from test_leadership import book, shadow, snapshot
from test_poll_collector import T as POLL_T, row as poll_row

T = 1700000000.
ON = {'research_followups': {'enabled': True, 'max_books_per_cycle': 2, 'max_books_per_hour': 3}}


def followup_event(sid, at, bid, ask, exchange='2', depth=100):
    return {'version': 1, 'snapshot_id': sid, 'exchange': exchange, 'phase': 'research_followup',
            'captured_at': at, 'quotes': [{'venue': 'SIG', 'bid': [bid, depth], 'ask': [ask, depth],
                                           'observed_at': at, 'source_at': at, 'timestamp_basis': 'fetch_time',
                                           'quality': {'issues': []}}]}


def journal_db(events):
    db = sqlite3.connect(':memory:')
    db.execute('CREATE TABLE events (at REAL NOT NULL, kind TEXT NOT NULL, detail TEXT NOT NULL)')
    for at, kind, item in events:
        db.execute('INSERT INTO events VALUES (?,?,?)', (at, kind, json.dumps(item, default=str)))
    return db


class FollowupSchedulerTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.f = ResearchFollowups(self.db, ON)

    def test_disabled_by_default_and_settings_are_bounded(self):
        off = ResearchFollowups(sqlite3.connect(':memory:'), {})
        self.assertEqual(off.schedule('2', 'a', T), 0)
        self.assertEqual(off.due(T+1000), [])
        for bad in ({'enabled': 'yes'}, {'max_books_per_cycle': 9}, {'max_books_per_hour': -1},
                    {'anchor_offsets_seconds': [10]}, {'surprise': 1}, {'position_offsets_seconds': [60, 60]}):
            with self.assertRaises(ValueError):
                settings({'research_followups': bad})

    def test_anchor_grid_matches_non_overlapping_study_windows(self):
        self.assertEqual(self.f.schedule('2', 'a', T), 2)
        self.assertEqual(self.f.schedule('2', 'b', T+100), 0)      # inside both windows
        self.assertEqual(self.f.schedule('2', 'c', T+420), 1)      # 300s grid reopens first
        self.assertEqual(self.f.schedule('3', 'd', T+100), 2)      # other race independent

    def test_scan_quote_satisfies_tasks_without_api_use(self):
        self.f.schedule('2', 'a', T)
        early = Book.make([('.5', 10)], [('.6', 10)], source_at=T+299, observed_at=T+301)
        self.assertEqual(self.f.observed('2', early, T+301), 0)    # source predates due
        late = Book.make([('.5', 10)], [('.6', 10)], source_at=T+301, observed_at=T+301)
        self.assertEqual(self.f.observed('2', late, T+301), 1)
        self.assertEqual(self.f.due(T+301), [])

    def test_positions_first_and_caps_per_cycle_and_hour(self):
        for i, ex in enumerate(('1', '2', '3', '4')):
            self.f.schedule(ex, 'a'+ex, T+i)
        self.f.schedule('9', 'p', T, kind='position')
        due = self.f.due(T+310)
        self.assertEqual(due[0], '9')
        self.assertEqual(len(due), 2)
        for ex in due:
            self.f.attempted(ex, T+310)
        self.assertEqual(len(self.f.due(T+311)), 1)                # hourly cap of 3
        self.f.attempted('1', T+311)
        self.assertEqual(self.f.due(T+312), [])

    def test_expired_tasks_are_missed_and_backoff_pauses(self):
        self.f.schedule('2', 'a', T)
        self.f.back_off(T+300)
        self.assertEqual(self.f.due(T+301), [])
        self.f.due(T+3000)
        statuses = dict(self.db.execute('SELECT offset,status FROM research_followups').fetchall())
        self.assertEqual(statuses, {300: 'missed', 900: 'missed'})


class FollowupStudyTests(unittest.TestCase):
    def test_followup_rows_are_outcome_only_and_opt_in(self):
        start = snapshot('s', T)
        db = journal_db([(T, 'quote_snapshot', start), (T, 'shadow_decision', shadow(start)),
                         (T+305, 'quote_snapshot', followup_event('f', T+305, '.60', '.62'))])
        rows, counts = load_events(db, T-10, T+400)
        self.assertEqual([r['id'] for r in rows], ['s'])
        rows, counts = load_events(db, T-10, T+400, include_followups=True)
        self.assertEqual(counts['research_followup_scans'], 1)
        self.assertEqual(counts['valid_SIG_scans'], 1)
        follow = next(r for r in rows if r['id'] == 'f')
        self.assertFalse(follow['aligned'])
        result = lead_analyze(rows, T-10, T+400)['300']
        self.assertEqual(result['coverage']['measured'], 1)
        self.assertEqual(result['shadow']['both_bids']['measured'], 1)

    def test_malformed_followup_is_rejected(self):
        bad = followup_event('f', T+305, '.60', '.62')
        bad['quotes'].append(dict(bad['quotes'][0], venue='Kalshi'))
        rows, counts = load_events(journal_db([(T+305, 'quote_snapshot', bad)]), T, T+400, include_followups=True)
        self.assertEqual(rows, [])
        self.assertEqual(counts['invalid_SIG_or_snapshot'], 1)


class EngineFollowupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config, original, self.refs = fixture()
        self.config['execution'] = dict(EXECUTION)
        self.config.update(copy.deepcopy(ON))
        self.sig = Broker(original)
        mapping = self.config['markets'][0]
        mapping['sig_market_id'] = '2'
        mapping['contract_fingerprint'] = fingerprint(contract_record(self.sig.market('2'), mapping,
                                                                      self.refs.metadata(mapping)))
        self.journal = Journal(Path(self.temp.name) / 'journal.db', 'test')
        self.addCleanup(self.journal.close)
        self.engine = ActiveEngine(self.config, self.sig, self.refs, self.journal, self.temp.name, live=True)

    def test_followup_journals_sig_only_snapshot_without_marks_or_orders(self):
        self.engine.research_followups.schedule('2', 'anchor', 0.)
        self.journal.db.execute('UPDATE research_followups SET due=?, deadline=?',
                                (__import__('time').time()-1, __import__('time').time()+100))
        self.journal.db.commit()
        self.engine.drawdown.observe = Mock()
        self.engine.ledger.observe = Mock()
        self.sig.place = Mock()
        self.engine.observe_research_due()
        rows = [json.loads(r[0]) for r in self.journal.db.execute(
            "SELECT detail FROM events WHERE kind='quote_snapshot'")]
        self.assertEqual([r['phase'] for r in rows], ['research_followup'])
        self.assertEqual([q['venue'] for q in rows[0]['quotes']], ['SIG'])
        self.engine.drawdown.observe.assert_not_called()
        self.engine.ledger.observe.assert_not_called()
        self.sig.place.assert_not_called()
        statuses = {r[0] for r in self.journal.db.execute('SELECT status FROM research_followups')}
        self.assertIn('fetched', statuses)

    def test_rate_limit_backs_off_and_propagates(self):
        self.engine.research_followups.schedule('2', 'anchor', 0.)
        now = __import__('time').time()
        self.journal.db.execute('UPDATE research_followups SET due=?, deadline=?', (now-1, now+100))
        self.journal.db.commit()
        self.sig.book = Mock(side_effect=APIError('429', status=429, method='GET'))
        with self.assertRaises(APIError):
            self.engine.observe_research_due()
        self.assertEqual(self.engine.research_followups.due(), [])

    def test_scan_cycle_schedules_anchor_and_position_followups(self):
        self.engine.cycle()
        kinds = {r[0] for r in self.journal.db.execute('SELECT kind FROM research_followups')}
        self.assertEqual(kinds, {'anchor', 'position'})


class EntryTimingTests(unittest.TestCase):
    def rows(self, quotes, start_bid='.50', start_ask='.55'):
        start = {'id': 's', 'exchange': '2', 'at': T, 'aligned': True,
                 'SIG': {'bid': D(start_bid), 'ask': D(start_ask), 'bid_size': D(100), 'ask_size': D(100),
                         'source': T, 'observed': T}}
        rows = [start]
        for i, (dt, bid, ask) in enumerate(quotes):
            stamp = T+dt
            rows.append({'id': str(i), 'exchange': '2', 'at': stamp, 'aligned': False,
                         'SIG': {'bid': D(bid), 'ask': D(ask), 'bid_size': D(100), 'ask_size': D(100),
                                 'source': stamp, 'observed': stamp}})
        return start, rows

    def test_requires_fresh_sustained_improvement(self):
        start, rows = self.rows([(10, '.51', '.56'), (20, '.50', '.55'), (30, '.51', '.56'), (65, '.52', '.57')])
        self.assertEqual(confirmation(rows, start, 'yes')['at'], T+65)
        start, rows = self.rows([(10, '.51', '.56'), (30, '.51', '.56')])
        self.assertIsNone(confirmation(rows, start, 'yes'))          # span under 30s
        start, rows = self.rows([(10, '.51', '.56'), (200, '.51', '.56')])
        self.assertIsNone(confirmation(rows, start, 'yes'))          # gap resets streak
        start, rows = self.rows([(10, '.51', '.56'), (50, '.51', '.56')])
        rows[2]['SIG']['source'] = T+5                               # refetched old book
        self.assertIsNone(confirmation(rows, start, 'yes'))
        start, rows = self.rows([(10, '.44', '.48'), (50, '.44', '.48')])
        self.assertEqual(confirmation(rows, start, 'no')['at'], T+50)  # NO bid = 1 - YES ask

    def test_decomposition_separates_movement_spread_and_buffers(self):
        start, rows = self.rows([(900, '.60', '.62')])
        result = outcome(start, rows[1], 'yes', 10, D('.01'))
        self.assertEqual(result['price_movement'], D('.085'))
        self.assertEqual(result['entry_half_spread'], D('.025'))
        self.assertEqual(result['exit_half_spread'], D('.01'))
        self.assertEqual(result['cost_buffers'], D('.02'))
        self.assertEqual(result['net_per_share'], D('.60')-D('.55')-D('.02'))
        self.assertEqual(side_quote(start['SIG'], 'no')['ask'], D('.50'))

    def events(self, at, follow):
        start = snapshot('s', at)
        events = [(at, 'quote_snapshot', start), (at, 'shadow_decision', shadow(start))]
        for i, (dt, bid, ask) in enumerate(follow):
            events.append((at+dt, 'quote_snapshot', followup_event('f%d' % i, at+dt, bid, ask)))
        return events

    def test_report_pairs_policies_and_respects_preregistration(self):
        at = entry_timing.PREREGISTERED_AT+100
        follow = [(60, '.52', '.56'), (100, '.53', '.57'), (905, '.58', '.60')]
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        path = Path(temp.name)/'journal.db'
        db = sqlite3.connect(path)
        db.execute('CREATE TABLE events (at REAL NOT NULL, kind TEXT NOT NULL, detail TEXT NOT NULL)')
        for t, kind, item in self.events(at, follow):
            db.execute('INSERT INTO events VALUES (?,?,?)', (t, kind, json.dumps(item, default=str)))
        db.commit()
        db.close()
        result = entry_timing.report(path, hours=1, now=at+1200)
        item = result['horizons']['900']
        self.assertEqual(item['prospective_proposals'], 1)
        self.assertEqual(item['immediate']['measured'], 1)
        self.assertEqual(item['wait_for_bid']['measured'], 1)
        paired = item['paired_both_entered']
        self.assertEqual(paired['samples'], 1)
        # Waiting bought .02 higher; movement is smaller by the mid change, spread unchanged.
        self.assertEqual(D(paired['wait_minus_immediate']['net_per_share']), D('-.02'))
        self.assertIn('ENTRY TIMING', entry_timing.format_report(result))
        with patch.object(entry_timing, 'PREREGISTERED_AT', at+1):
            self.assertEqual(entry_timing.report(path, hours=1, now=at+1200)['horizons']['900']['proposals'], 0)
            self.assertEqual(entry_timing.report(path, hours=1, now=at+1200, include_retrospective=True)
                             ['horizons']['900']['proposals'], 1)

    def test_no_confirmation_records_what_waiting_skipped(self):
        at = entry_timing.PREREGISTERED_AT+100
        rows, _ = load_events(journal_db(self.events(at, [(60, '.49', '.55'), (905, '.40', '.45')])),
                              at-10, at+1200, include_followups=True)
        horizons, _ = entry_timing.analyze(rows, at+1200)
        item = horizons['900']
        self.assertEqual(item['wait_for_bid']['statuses'], {'no_confirmation': 1})
        self.assertEqual(item['immediate_outcome_when_wait_skipped']['samples'], 1)
        self.assertLess(D(item['immediate_outcome_when_wait_skipped']['net_per_share']), 0)


def senate_config(state='NH'):
    from test_poll_collector import config
    c = config()
    c['markets'][0]['race_key'] = '2026:senate:' + state
    return c


class PollingCoverageTests(unittest.TestCase):
    def test_live_tier_poll_record_is_unchanged(self):
        p = normalize(poll_row(), {'2026:senate:NH'}, POLL_T)
        self.assertNotIn('live_model_eligible', p)

    def test_research_tier_pollster_and_rounding_are_flagged(self):
        p = normalize(poll_row(pollster='SurveyUSA'), {'2026:senate:NH'}, POLL_T)
        self.assertIs(p['live_model_eligible'], False)
        self.assertEqual(p['research_reasons'], ['research_tier_pollster'])
        rounded = poll_row(answers=[dict(choice='Chris Pappas', pct=53), dict(choice='John Sununu', pct=46),
                                    dict(choice='Other', pct=2)])
        p = normalize(rounded, {'2026:senate:NH'}, POLL_T)
        self.assertEqual(p['research_reasons'], ['rounded_total_above_100_5'])
        too_high = poll_row(answers=[dict(choice='Chris Pappas', pct=53.2), dict(choice='John Sununu', pct=48.1)])
        with self.assertRaisesRegex(ValueError, 'answer_total_exceeds_rounding'):
            normalize(too_high, {'2026:senate:NH'}, POLL_T)
        for unreviewed in ('Guidant Polling and Strategy', 'ActiVote', 'Slingshot Strategies'):
            with self.assertRaisesRegex(ValueError, 'unreviewed_pollster_family'):
                normalize(poll_row(pollster=unreviewed), {'2026:senate:NH'}, POLL_T)

    def test_research_only_race_binding(self):
        c = senate_config('MN')
        races = bindings(c)
        self.assertEqual(len(races), 1)
        self.assertIs(races[0]['live_model_eligible'], False)
        row = poll_row(subject='2026 Minnesota', answers=[dict(choice='Peggy Flanagan', pct=48),
                                                          dict(choice='Michele Tafoya', pct=47)])
        p = normalize(row, {'2026:senate:MN'}, POLL_T)
        self.assertEqual(p['research_reasons'], ['research_only_race'])

    def test_research_row_cannot_displace_live_row(self):
        live = poll_row(id='a')
        research = poll_row(id='b', pollster='ReconMR/Siena University')
        live_siena = dict(live, pollster='The New York Times/Siena University')
        evidence, rejected = adapt([live_siena, research], senate_config(), {}, POLL_T)
        self.assertEqual(len([p for p in evidence['polls'] if not p.get('withdrawn')]), 2)
        self.assertEqual(rejected, [])

    def test_model_live_ignores_research_inputs(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        runtime = Path(temp.name)
        (runtime/'poll-collector').mkdir()
        (runtime/'poll-collector'/'status.json').write_text(json.dumps({'status': 'ok', 'last_success_at': POLL_T}))
        c = senate_config()
        rows = [poll_row(id='a', pollster='YouGov'), poll_row(id='b', pollster='SurveyUSA', end_date='2026-10-02'),
                poll_row(id='c', pollster='Emerson College', end_date='2026-10-02')]
        evidence, _ = adapt(rows, c, {}, POLL_T)
        store = PollStore(runtime/'fair-value.sqlite3')
        store.ingest(evidence, c, POLL_T)
        store.close()
        p = model_live.settings(dict(c, model_trading={}))
        result = model_live.forecast(runtime, c['markets'][0], c, POLL_T+1, p)
        self.assertEqual(result['pollster_count'], 2)
        self.assertNotIn('votehub:b', result['poll_ids'])
        store = PollStore(runtime/'fair-value.sqlite3')
        race = store.latest('race', POLL_T)[0]
        race = {k: v for k, v in race.items() if not k.startswith('_')}
        store.ingest({'schema_version': 1, 'races': [dict(race, live_model_eligible=False)], 'polls': []}, c, POLL_T+2)
        store.close()
        with self.assertRaisesRegex(ValueError, 'model_race_research_only'):
            model_live.forecast(runtime, c['markets'][0], c, POLL_T+3, p)

    def test_validate_poll_rejects_non_boolean_tier(self):
        p = normalize(poll_row(pollster='SurveyUSA'), {'2026:senate:NH'}, POLL_T)
        with self.assertRaises(ValueError):
            validate_poll(dict(p, live_model_eligible='no'))


if __name__ == '__main__':
    unittest.main()
