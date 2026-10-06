import copy
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from election_bot.leadership import analyze, correlation, format_report, load_events, report
from election_bot.shadow import decisions, VARIANTS
from election_bot.strategy import Book, D, choose

T = 1700000000.
SETTINGS = {'minimum_edge': '.05', 'cost_buffer_per_share': '.01',
            'max_reference_spread': '.08', 'max_reference_disagreement': '.08',
            'max_age_seconds': 15, 'min_reference_depth': '20', 'max_shares_per_order': 100}


def book(bid, ask, at=T, depth=100):
    return Book([(D(bid), D(depth))], [(D(ask), D(depth))], at, at, 'test', 'fetch_time')


def snapshot(sid='a', at=T, sig=('.50', '.55'), k=('.65', '.67'), p=('.67', '.69'), depth=100):
    quotes = []
    for venue, prices in zip(('SIG', 'Kalshi', 'Polymarket'), (sig, k, p)):
        quotes.append({'venue': venue, 'bid': [prices[0], depth], 'ask': [prices[1], depth],
                       'observed_at': at, 'source_at': at, 'timestamp_basis': 'fetch_time',
                       'quality': {'issues': []}})
    return {'version': 1, 'snapshot_id': sid, 'exchange': '2', 'phase': 'scan',
            'captured_at': at, 'quotes': quotes}


def shadow(snap):
    books = [book(q['bid'][0], q['ask'][0], snap['captured_at'], q['bid'][1]) for q in snap['quotes']]
    with patch('election_bot.strategy.time.time', return_value=snap['captured_at']):
        result = decisions(books[0], books[1:], SETTINGS, D(50), 0)
    return {**result, 'snapshot_id': snap['snapshot_id'], 'exchange': snap['exchange'], 'phase': 'scan'}


class ShadowTests(unittest.TestCase):
    def setUp(self):
        self.sig = book('.50', '.55')
        self.refs = [book('.65', '.67'), book('.67', '.69')]
        self.clock = patch('election_bot.strategy.time.time', return_value=T)
        self.clock.start(); self.addCleanup(self.clock.stop)

    def test_baseline_matches_existing_strategy_and_inputs_unchanged(self):
        settings = copy.deepcopy(SETTINGS)
        wanted = choose(self.sig, self.refs, settings, 50)
        result = decisions(self.sig, self.refs, settings, 50, 0)
        self.assertEqual(result['decisions'][0]['candidate'], vars(wanted))
        self.assertEqual(settings, SETTINGS)
        self.assertEqual([r['variant'] for r in result['decisions']], list(VARIANTS))
        self.assertEqual(result['decisions'][1]['candidate']['reference'], D('.65'))
        self.assertEqual(result['decisions'][2]['candidate']['reference'], D('.67'))

    def test_alternative_can_propose_when_conservative_reference_fails(self):
        refs = [book('.59', '.61'), book('.65', '.67')]
        result = decisions(self.sig, refs, SETTINGS, 50, 0)['decisions']
        self.assertIsNone(result[0]['candidate'])
        self.assertIsNone(result[1]['candidate'])
        self.assertIsNotNone(result[2]['candidate'])

    def test_both_venues_depth_gate_and_size_preserved(self):
        self.refs[0] = book('.65', '.67', depth=19)
        self.assertTrue(all(r['candidate'] is None for r in decisions(self.sig, self.refs, SETTINGS, 50, 0)['decisions']))
        self.refs[0] = book('.65', '.67', depth=25)
        self.assertTrue(all(r['candidate']['quantity'] == 25 for r in decisions(self.sig, self.refs, SETTINGS, 50, 0)['decisions']))

    def test_stale_or_disagreeing_reference_rejects_all_variants(self):
        self.refs[0].source_at = T-20
        with self.assertRaises(ValueError): decisions(self.sig, self.refs, SETTINGS, 50, 0)
        self.refs[0] = book('.30', '.32')
        with self.assertRaises(ValueError): decisions(self.sig, self.refs, SETTINGS, 50, 0)

    def test_exposure_budget_inventory_and_exit_constraints(self):
        self.assertTrue(all(r['candidate'] is None for r in decisions(self.sig, self.refs, SETTINGS, 0, 0)['decisions']))
        result = decisions(self.sig, self.refs, SETTINGS, 50, 0, {'yes': 3, 'no': 0})
        self.assertTrue(all(r['candidate']['quantity'] == 3 for r in result['decisions']))
        for held, exit_selected, reason in ((-3, False, 'opposite_inventory'), (0, True, 'live_exit_has_priority')):
            result = decisions(self.sig, self.refs, SETTINGS, 50, held, exit_selected=exit_selected)
            self.assertTrue(all(r['candidate'] is None and r['reason'] == reason for r in result['decisions']))

    def test_no_side_uses_complement_asks_for_reference_bid(self):
        refs = [book('.30', '.32'), book('.28', '.30')]
        result = decisions(self.sig, refs, SETTINGS, 50, 0)['decisions']
        self.assertEqual(result[0]['candidate']['side'], 'no')
        self.assertEqual(result[0]['candidate']['price'], D('.50'))
        self.assertEqual(result[0]['candidate']['reference'], D('.68'))
        self.assertEqual(result[2]['candidate']['reference'], D('.70'))


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)/'live.sqlite3'
        self.db = sqlite3.connect(self.path); self.addCleanup(self.db.close)
        self.db.execute('CREATE TABLE events(at REAL,kind TEXT,detail TEXT)')

    def put(self, snap, kind='quote_snapshot', at=None):
        at = snap.get('captured_at', T) if at is None else at
        self.db.execute('INSERT INTO events VALUES(?,?,?)', (at, kind, json.dumps(snap, default=str)))
        self.db.commit()

    def result(self, now=T+2000):
        return report(self.path, now=now, hours=1)

    def test_no_lookahead_pre_target_or_old_source_as_endpoint(self):
        self.put(snapshot())
        self.put(snapshot('before', T+299, sig=('.90', '.95')))
        old = snapshot('old', T+300, sig=('.80', '.85')); old['quotes'][0]['source_at'] = T+299
        self.put(old)
        self.put(snapshot('after', T+310, sig=('.60', '.65')))
        r = self.result()['horizons']['300']
        self.assertEqual(r['coverage']['measured'], 1)
        self.assertAlmostEqual(r['leadership']['Kalshi_gap']['mean_signed_SIG_change'], .10)
        self.assertEqual(r['mean_followup_delay_seconds'], 10)

    def test_read_only_missing_file_and_no_credentials_cli(self):
        before = self.path.read_bytes()
        r = self.result()
        self.assertEqual(before, self.path.read_bytes())
        self.assertIn('SHADOW', format_report(r))
        missing = Path(self.temp.name)/'missing'/'live.sqlite3'
        self.assertEqual(report(missing)['status'], 'No quote journal yet'); self.assertFalse(missing.parent.exists())
        from election_bot import __main__ as cli
        with patch.object(cli, 'RUNTIME', missing.parent), patch.object(cli, 'key') as key, \
             patch.object(cli, 'output') as output, patch('sys.argv', ['bot', 'leadership', '--json']):
            cli.main(); key.assert_not_called()
            self.assertEqual(output.call_args.args[0]['status'], 'No quote journal yet')
        self.assertFalse(missing.parent.exists())

    def test_duplicate_and_conflicting_ids_preflight_excluded(self):
        a = snapshot(); self.put(a); self.put(a)
        pre = snapshot('pre', T+300); pre['phase'] = 'preflight'; self.put(pre)
        r = self.result()
        self.assertEqual(r['events']['aligned_three_venue_scans'], 1)
        self.assertEqual(r['horizons']['300']['coverage']['missed'], 1)
        conflict = snapshot(sig=('.51', '.56')); self.put(conflict)
        self.assertEqual(self.result()['events']['valid_SIG_scans'], 0)

    def test_stale_future_nan_and_misaligned_quotes_excluded(self):
        for i, stamp in enumerate((T-16, T+1, float('nan'))):
            a = snapshot(str(i)); a['quotes'][0]['source_at'] = stamp; self.put(a)
        a = snapshot('skew'); a['quotes'][1]['source_at'] -= 6; self.put(a)
        a = snapshot('flag'); a['quotes'][1]['quality']['issues'] = ['source_stale']; self.put(a)
        a = snapshot('cross'); a['quotes'][0]['bid'][0] = '.90'; self.put(a)
        self.put(snapshot('valid'))
        r = self.result()['events']
        self.assertEqual(r['valid_SIG_scans'], 3)
        self.assertEqual(r['aligned_three_venue_scans'], 1)

    def test_future_SIG_can_be_used_with_bad_external_quote(self):
        self.put(snapshot())
        later = snapshot('later', T+310, sig=('.60', '.65'))
        later['quotes'][1]['quality']['issues'] = ['source_stale']; self.put(later)
        r = self.result()['horizons']['300']
        self.assertEqual(r['coverage']['measured'], 1)

    def test_nonoverlap_and_missing_statuses(self):
        for offset in (0, 100, 200, 300, 420, 800): self.put(snapshot(str(offset), T+offset))
        r = self.result(T+850)['horizons']['300']['coverage']
        self.assertEqual(r['anchors'], 2); self.assertEqual(r['overlap_excluded'], 4)
        self.assertEqual(r['measured'], 2)
        # A still-open observation window is not a missed result.
        r = self.result(T+350)['horizons']['900']['coverage']
        self.assertEqual(r['not_due'], 1)
        self.db.execute('DELETE FROM events'); self.db.commit(); self.put(snapshot())
        self.assertEqual(self.result(T+350)['horizons']['300']['coverage']['awaiting_quote'], 1)
        self.assertEqual(self.result(T+421)['horizons']['300']['coverage']['missed'], 1)

    def test_movement_feature_uses_only_recent_past_and_has_threshold(self):
        self.put(snapshot('prior', T-100, k=('.63', '.65'), p=('.65', '.67')))
        self.put(snapshot())
        self.put(snapshot('future', T+310, sig=('.60', '.65')))
        rows, _ = load_events(self.db, T, T+500)
        r = analyze(rows, T, T+500)['300']['leadership']
        self.assertEqual(r['Kalshi_recent_move']['samples'], 1)
        self.assertEqual(r['Kalshi_recent_move']['follow_rate_including_flat'], 1)
        # With no prior observation, the later price cannot manufacture a past move.
        self.db.execute("DELETE FROM events WHERE at<?", (T,)); self.db.commit()
        rows, _ = load_events(self.db, T, T+500)
        self.assertEqual(analyze(rows, T, T+500)['300']['leadership']['Kalshi_recent_move']['samples'], 0)

    def test_flat_outcome_is_not_success_and_constant_correlation_unknown(self):
        self.put(snapshot()); self.put(snapshot('later', T+300))
        r = self.result()['horizons']['300']['leadership']['Kalshi_gap']
        self.assertEqual(r['flat_SIG_outcomes'], 1)
        self.assertEqual(r['follow_rate_including_flat'], 0)
        self.assertIsNone(r['correlation_with_future_SIG_change'])
        self.assertIsNone(correlation([(1, 1), (1, 2), (1, 3)]))
        self.assertAlmostEqual(correlation([(1, 2), (2, 4), (3, 6)]), 1)

    def test_shadow_quotes_full_size_and_buffers_and_paired_results(self):
        a = snapshot(); self.put(a); self.put(shadow(a), 'shadow_decision')
        self.put(snapshot('later', T+300, sig=('.60', '.65')))
        r = self.result()['horizons']['300']
        for v in VARIANTS:
            self.assertEqual(r['shadow'][v]['measured'], 1)
            self.assertEqual(D(r['shadow'][v]['hypothetical_pnl_per_share_after_buffers']), D('.03'))
        self.assertEqual(r['paired_shadow_comparison']['kalshi_bid']['paired_measured'], 1)
        self.assertEqual(D(r['paired_shadow_comparison']['kalshi_bid']['mean_pnl_per_share_difference_vs_both_bids']), 0)

    def test_NO_shadow_liquidates_against_one_minus_SIG_ask(self):
        a = snapshot(k=('.30', '.32'), p=('.28', '.30')); self.put(a); self.put(shadow(a), 'shadow_decision')
        self.put(snapshot('later', T+300, sig=('.38', '.40')))
        r = self.result()['horizons']['300']['shadow']['both_bids']
        self.assertEqual(D(r['hypothetical_pnl_per_share_after_buffers']), D('.08'))

    def test_insufficient_exit_depth_not_zero_or_later_cherry_pick(self):
        a = snapshot(); self.put(a); self.put(shadow(a), 'shadow_decision')
        self.put(snapshot('thin', T+300, sig=('.60', '.65'), depth=1))
        self.put(snapshot('deep', T+310, sig=('.70', '.75')))
        r = self.result()['horizons']['300']['shadow']['both_bids']
        self.assertEqual(r['statuses']['insufficient_exit_depth'], 1)
        self.assertIsNone(r['hypothetical_pnl_per_share_after_buffers'])

    def test_unknown_legacy_not_backfilled_and_forged_record_excluded(self):
        a = snapshot(); self.put(a); self.put(snapshot('later', T+300))
        r = self.result()['horizons']['300']['shadow']['both_bids']
        self.assertEqual(r['statuses']['no_prospective_record'], 1)
        s = shadow(a); s['decisions'][0]['candidate']['price'] = '.01'; self.put(s, 'shadow_decision')
        self.assertEqual(self.result()['horizons']['300']['shadow']['both_bids']['statuses']['invalid_prospective_record'], 1)

    def test_extra_variant_proposal_is_not_a_paired_baseline_result(self):
        a = snapshot(k=('.59', '.61'), p=('.65', '.67'))
        self.put(a); self.put(shadow(a), 'shadow_decision')
        self.put(snapshot('later', T+300, sig=('.60', '.65')))
        r = self.result()['horizons']['300']
        self.assertEqual(r['shadow']['both_bids']['proposed_entries'], 0)
        self.assertEqual(r['shadow']['polymarket_bid']['proposed_entries'], 1)
        comparison = r['paired_shadow_comparison']['polymarket_bid']
        self.assertEqual(comparison['additional_proposals_vs_baseline'], 1)
        self.assertEqual(comparison['paired_measured'], 0)
        self.assertIsNone(comparison['mean_pnl_per_share_difference_vs_both_bids'])

    def test_measurement_unavailable_separate_from_invalid_record(self):
        a = snapshot(); self.put(a)
        self.put({'version': 1, 'snapshot_id': 'a', 'phase': 'scan', 'exchange': '2',
                  'status': 'unavailable'}, 'shadow_decision')
        r = self.result()['horizons']['300']['shadow']['both_bids']
        self.assertEqual(r['statuses']['measurement_unavailable'], 1)

    def test_conflicting_shadow_records_excluded(self):
        a = snapshot(); self.put(a); s = shadow(a); self.put(s, 'shadow_decision'); self.put(s, 'shadow_decision')
        s['available'] = 40; self.put(s, 'shadow_decision')
        r = self.result(); self.assertEqual(r['events']['conflicting_shadow_ids'], 1)
        self.assertEqual(r['horizons']['300']['shadow']['both_bids']['statuses']['no_prospective_record'], 1)

    def test_malformed_events_and_invalid_hours(self):
        self.put({'version': 1})
        self.assertEqual(self.result()['events']['malformed_events'], 1)
        for hours in (0, -1, 721, float('nan')):
            with self.assertRaises(ValueError): report(self.path, hours=hours)
