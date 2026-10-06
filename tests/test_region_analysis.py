import copy
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

import test_active
import test_risk
from election_bot.engine import validate_config
from election_bot.entry_analysis import day_block_intervals, gap_bucket, price_bucket, report, format_report
from election_bot.ledger import Ledger
from election_bot.regions import CENSUS_REGIONS, STATE_REGIONS, mapping_region, region_for_state
from election_bot.risk import Exposure
from election_bot.state import Journal
from election_bot.strategy import D


def regional_config():
    config=test_risk.risk_config()
    config['limits']['net_shares_per_region']='20'
    config['markets'][0].update(state_code='NH',region='northeast')
    config['markets'][1].update(state_code='CA',region='west')
    return config


class RegionTests(unittest.TestCase):
    def test_census_table_covers_50_states_and_dc_once(self):
        self.assertEqual(len(STATE_REGIONS),51)
        self.assertEqual(sum(map(len,CENSUS_REGIONS.values())),51)
        self.assertEqual({k:len(v) for k,v in CENSUS_REGIONS.items()},
                         {'northeast':9,'midwest':12,'south':17,'west':13})
        for state, region in [('NH','northeast'),('PA','northeast'),('MO','midwest'),
                              ('DC','south'),('DE','south'),('TX','south'),('AK','west'),('HI','west')]:
            self.assertEqual(region_for_state(state),region)
        with self.assertRaises(ValueError):region_for_state('PR')

    def test_explicit_region_and_state_must_match_race(self):
        config=regional_config();mapping=config['markets'][0]
        mapping.update(race_key='2026:senate:NH')
        validate_config(config)
        for update in ({'region':'west'},{'state_code':'ZZ'},{'race_key':'2026:house:CA-01'}):
            bad=copy.deepcopy(config);bad['markets'][0].update(update)
            with self.subTest(update=update), self.assertRaises(ValueError):validate_config(bad)

    def test_invalid_or_incomplete_region_limits_rejected(self):
        for cap in (0,'1.5',True,101,'NaN'):
            config=regional_config();config['limits']['net_shares_per_region']=cap
            with self.subTest(cap=cap),self.assertRaises(ValueError):validate_config(config)
        config=regional_config();del config['limits']['net_shares_total']
        with self.assertRaises(ValueError):validate_config(config)
        config=regional_config();del config['markets'][0]['state_code']
        with self.assertRaises(ValueError):validate_config(config)

    def test_other_region_offset_does_not_hide_regional_concentration(self):
        exposure=Exposure(test_risk.fake_ledger(19,10),regional_config())
        self.assertEqual(exposure.net['total'],9)
        self.assertEqual(exposure.headroom('2','buy','yes'),1)
        self.assertEqual(exposure.summary()['net_shares_by_region']['west'],-10)

    def test_pending_partial_fills_reserve_region_including_sales(self):
        config=regional_config()
        # Pending YES purchase can increase Northeast from 10 to 18.
        exposure=Exposure(test_risk.fake_ledger(10,10,[test_risk.pending('2','buy','yes',8)]),config)
        self.assertEqual(exposure.bounds['region:northeast'],[10,18])
        self.assertEqual(exposure.headroom('2','buy','yes'),2)
        # A pending sale of NO can remove an offset; reserve the endpoint too.
        exposure=Exposure(test_risk.fake_ledger(10,19,[test_risk.pending('3','sell','no',5)]),config)
        self.assertEqual(exposure.bounds['region:west'],[-19,-14])
        self.assertEqual(exposure.headroom('3','sell','no'),34)

    def test_over_cap_region_allows_reduction_not_worsening(self):
        exposure=Exposure(test_risk.fake_ledger(25,10),regional_config())
        self.assertEqual(exposure.headroom('2','buy','yes'),0)
        self.assertEqual(exposure.headroom('2','sell','yes'),45)

    def test_disabled_owned_race_counted_and_unknown_region_halts(self):
        config=regional_config();config['markets'][0]['enabled']=False
        self.assertEqual(Exposure(test_risk.fake_ledger(19),config).net['region:northeast'],19)
        del config['markets'][0]['region']
        with self.assertRaisesRegex(RuntimeError,'Unmapped portfolio'):Exposure(test_risk.fake_ledger(19),config)

    def test_inverted_orientation_also_applies_to_regions(self):
        config=regional_config();config['markets'][0]['exposure_sign']=-1
        exposure=Exposure(test_risk.fake_ledger(19),config)
        self.assertEqual(exposure.net['region:northeast'],-19)
        self.assertEqual(exposure.headroom('2','buy','yes'),1)
        self.assertEqual(exposure.headroom('2','sell','yes'),39)


class RegionIntegrationTests(unittest.TestCase):
    def setUp(self):
        test_active.ActiveTests.setUp(self)
        self.config['limits'].update(net_shares_total='100',net_shares_per_office='100',net_shares_per_region='2')
        self.config['markets'][0].update(office='senate',exposure_sign=1,state_code='NH',region='northeast')
        validate_config(self.config)

    def test_region_limit_sizes_actual_order_and_preflight(self):
        self.engine.cycle()
        self.assertEqual(self.sig.orders[1]['quantity'],2)
        decisions=[json.loads(r[0]) for r in self.journal.db.execute("SELECT detail FROM events WHERE kind='decision'")]
        self.assertEqual([r['phase'] for r in decisions],['scan','preflight'])
        self.assertTrue(all(r['checks'][0]['exposure_headroom']=='2' for r in decisions))


class EntryAnalysisTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'live.sqlite3'
        self.journal=Journal(self.path,'test');self.addCleanup(self.journal.close)
        self.ledger=Ledger(self.journal)
        self.now=iso_day(2026,10,10)
        self.journal.db.execute("UPDATE performance_settings SET value=? WHERE name='short_horizons_started_at'",(str(self.now-10*86400),))
        self.journal.db.commit()

    def buy(self,key,price='.5',quantity=10,reference='.6',at=None,side='yes',exchange='2',linked=True,fill_price=None):
        at=self.now-2000 if at is None else at
        payload=dict(idempotencyKey=key,exchangeId=exchange,action='buy',side=side,quantity=quantity,price=price)
        self.journal.reserve(payload,D(quantity)*(D(price)+D('.01')))
        self.ledger.record(payload,quantity,fill_price or price,'.01',at)
        if linked:
            self.journal.event('signal',dict(order_key=key,exchange=exchange,action='buy',side=side,
                price=price,quantity=quantity,reference=reference,edge=str(D(reference)-D(price)-D('.01'))))
        return at

    def mark(self,key,pnl,horizon=300,delay=10):
        at=self.journal.db.execute('SELECT at FROM executions WHERE key=?',(key,)).fetchone()[0]
        self.journal.db.execute('INSERT INTO markouts VALUES (?,?,?,?,?)',(key,horizon,at+horizon+delay,pnl,None if pnl is not None else 'missed'))
        self.journal.db.commit()

    def stats(self,result,group,label,horizon=300):
        return next(b for b in result[group] if b['bucket']==label)['horizons'][str(horizon)]

    def test_exact_bucket_boundaries(self):
        self.assertEqual([price_bucket(D(x)) for x in ('.149','.15','.35','.65','.85')],
                         ['0–0.15','0.15–0.35','0.35–0.65','0.65–0.85','0.85–1.00'])
        self.assertEqual([gap_bucket(D(x)) for x in ('-.01','.049','.05','.06','.08','.12')],
                         ['below 0.05','below 0.05','0.05–0.06','0.06–0.08','0.08–0.12','0.12 and above'])
        self.assertEqual(gap_bucket(None),'unknown')

    def test_actual_fill_price_adjusts_linked_gap_and_no_outcome_is_not_inverted(self):
        self.buy('a',price='.5',fill_price='.49',reference='.56',side='no');self.mark('a','.2')
        result=report(self.path,now=self.now)
        self.assertEqual(result['gap_linkage']['filled_buy_link_status'],{'matched':1})
        self.assertEqual(self.stats(result,'by_entry_gap','0.06–0.08')['observations'],1)
        self.assertEqual(self.stats(result,'by_entry_price','0.35–0.65')['pnl_per_share_after_buffers'],'0.02')

    def test_legacy_signal_without_order_key_stays_unknown(self):
        self.buy('a',linked=False);self.mark('a','.2')
        self.journal.event('signal',dict(exchange='2',side='yes',action='buy',reference='.6',price='.5',edge='.09'))
        result=report(self.path,now=self.now)
        self.assertEqual(result['gap_linkage']['filled_buy_link_status'],{'missing_order_link':1})
        self.assertEqual(self.stats(result,'by_entry_gap','unknown')['observations'],1)
        self.assertEqual(result['gap_linkage']['legacy_unkeyed_signal_events'],1)

    def test_conflicting_or_wrong_identity_signals_are_unknown(self):
        self.buy('a');self.buy('b')
        self.journal.event('signal',dict(order_key='a',exchange='2',side='yes',action='buy',reference='.7',price='.5',quantity=10,edge='.19'))
        self.journal.event('signal',dict(order_key='b',exchange='wrong',side='yes',action='buy',reference='.6',price='.5',quantity=10,edge='.09'))
        result=report(self.path,now=self.now)
        self.assertEqual(result['gap_linkage']['filled_buy_link_status'],{'conflicting_signals':1,'invalid_linked_signal':1})

    def test_duplicate_identical_signal_is_not_double_counted(self):
        self.buy('a')
        signal=json.loads(self.journal.db.execute("SELECT detail FROM events WHERE kind='signal'").fetchone()[0])
        self.journal.event('signal',signal)
        result=report(self.path,now=self.now)
        self.assertEqual(result['filled_buys'],1)
        self.assertEqual(result['gap_linkage']['filled_buy_link_status'],{'matched':1})

    def test_measured_missing_not_due_and_legacy_are_distinct(self):
        self.buy('measured');self.mark('measured','.2');self.mark('measured','-.1',900)
        self.buy('missing');self.buy('notdue',at=self.now-10)
        self.buy('legacy',at=self.now-11*86400)
        result=report(self.path,now=self.now)
        stats=self.stats(result,'by_entry_price','0.35–0.65')
        self.assertEqual((stats['observations'],stats['missed'],stats['not_due'],stats['legacy_buys_excluded']),(1,1,1,1))
        self.assertEqual(stats['coverage_percent'],50)
        self.assertEqual(self.stats(result,'by_entry_price','0.35–0.65',900)['positive_percent'],0)

    def test_share_weighted_pnl_and_order_weighted_positive_percentage(self):
        self.buy('small',quantity=1);self.mark('small','.1')
        self.buy('big',quantity=9);self.mark('big','-.9')
        stats=self.stats(report(self.path,now=self.now),'by_entry_price','0.35–0.65')
        self.assertEqual(stats['pnl_per_share_after_buffers'],'-0.08')
        self.assertEqual(stats['positive_percent'],50)
        self.assertEqual(stats['uncertainty']['status'],'insufficient_entry_days')
        self.assertIsNone(stats['uncertainty']['pnl_per_share_after_buffers'])

    def test_uncertainty_clusters_days_not_individual_orders(self):
        rows=[dict(at=self.now-100,quantity=10,pnl='.1',exchange=str(i)) for i in range(100)]
        self.assertEqual(day_block_intervals(rows)['observed_entry_days'],1)
        rows=[dict(at=self.now-i*86400,quantity=10,pnl=str(i/10),exchange='2') for i in range(5)]
        interval=day_block_intervals(rows)
        self.assertEqual(interval['replicates'],1000)
        self.assertEqual(interval['status'],'exploratory')
        self.assertEqual(interval,day_block_intervals(rows))
        self.assertLessEqual(interval['pnl_per_share_after_buffers'][0],.02)
        self.assertGreaterEqual(interval['pnl_per_share_after_buffers'][1],.02)

    def test_reports_are_read_only_and_missing_files_not_created(self):
        self.buy('a');self.mark('a','.1')
        before=self.path.read_bytes();result=report(self.path,now=self.now)
        self.assertEqual(self.path.read_bytes(),before)
        self.assertIn('ENTRY PRICE',format_report(result));self.assertIn('Unknown gaps',format_report(result))
        missing=Path(self.temp.name)/'missing.db'
        self.assertEqual(report(missing)['status'],'No execution journal yet');self.assertFalse(missing.exists())

    def test_analysis_cli_needs_no_credentials_or_network(self):
        from election_bot import __main__ as cli
        self.buy('a')
        with patch.object(cli,'RUNTIME',Path(self.temp.name)),patch.object(cli,'key') as key, \
             patch.object(cli,'output') as output,patch('sys.argv',['bot','analysis','--json']):
            cli.main();key.assert_not_called();self.assertEqual(output.call_args.args[0]['filled_buys'],1)


def iso_day(year,month,day):
    return datetime(year,month,day,tzinfo=timezone.utc).timestamp()
