import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from election_bot import __main__ as cli
from election_bot.catalog import verify_candidate
from election_bot.clients import fingerprint
from election_bot.contract_review import assess, identity, event_markets, race_budgets, refresh_race, report, book_record
from election_bot.ledger import Ledger
from election_bot.state import Journal
from election_bot.strategy import Book, D


def example():
    rows=json.loads((Path(__file__).parent/'data/catalog_examples.json').read_text())
    return copy.deepcopy(next(r for r in rows if ':senate:' in r['mapping']['race_key']))


def republican(review):
    # Test fixture only; production discovers actual external tickers.
    return json.loads(json.dumps(review).replace('Democratic','Republican').replace('Democrats','Republicans').replace('-26-D','-26-R'))


class IdentityTests(unittest.TestCase):
    def test_discovery_hints_accept_parties_but_not_candidate_or_control(self):
        for party in ('Democratic Party','Republican Party','Independent Party'):
            self.assertEqual(identity('Will the '+party+' win the Nebraska Senate?')['race_key'],'2026:senate:NE')
        self.assertEqual(identity('Will the Republican Party win the NH-01 House race?')['race_key'],'2026:house:NH-01')
        for title in ('Will the Republican Party win the U.S. Senate?','Will Person A win the Nebraska Senate?', 'Will the Democratic Party win the Nebraska Senate in 2028?'):
            self.assertIsNone(identity(title))

    def test_republican_three_offices_strict_identity(self):
        rows=json.loads((Path(__file__).parent/'data/catalog_examples.json').read_text())
        for row in rows:
            if 'DFL' in row['contract']['kalshi']['rules_primary']: continue
            self.assertTrue(verify_candidate(republican(row),'Republican Party'))
        with self.assertRaises(ValueError): verify_candidate(example(),'Republican Party')
        with self.assertRaises(ValueError): verify_candidate(example(),'Independent Party')

    def test_wrong_state_year_stage_and_party_cannot_pass_new_verifier(self):
        for field,value in (('electionDate','2028-11-07'),('raceStage','Primary'),('usState','ZZ'),('winnerName','Democratic Party')):
            r=republican(example());r['contract']['sig']['resolution_tree']['contract_details'][field]=value
            with self.assertRaises(ValueError):verify_candidate(r,'Republican Party')

    def test_basis_flags_not_equivalence_or_calibrated_edge(self):
        r=example();result=assess(r['contract'],r['mapping'],'Democratic Party')
        self.assertTrue(result['structural_identity_verified'])
        self.assertFalse(result['settlement_equivalence_verified']);self.assertFalse(result['new_trading_authorized'])
        self.assertIsNone(result['additional_edge_required'])
        self.assertIn('office_holder_vs_election_winner',{f['code'] for f in result['flags']})
        self.assertIn('SIG_party_attribution_unspecified',{f['code'] for f in result['flags']})
        self.assertEqual(result['record_fingerprint'],fingerprint(r['contract']))

    def test_missing_rules_and_changed_party_stay_unverified(self):
        r=example();r['contract']['kalshi']['rules_primary']=''
        result=assess(r['contract'],r['mapping'],'Democratic Party')
        self.assertFalse(result['structural_identity_verified']);self.assertGreaterEqual(result['review_score'],50)
        self.assertIn('missing_external_rules',{f['code'] for f in result['flags']})


class BudgetTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'live.sqlite3'
        self.j=Journal(self.path,'test');self.addCleanup(self.j.close);self.ledger=Ledger(self.j)
        self.config={'limits':{'per_market':'250'},'markets':[{'sig_exchange_id':'2','race_key':'race','enabled':True}, {'sig_exchange_id':'3','race_key':'race','enabled':False}]}

    def fill(self,key,exchange,action,quantity,price):
        p={'idempotencyKey':key,'exchangeId':exchange,'action':action,'side':'yes','quantity':quantity,'price':price}
        self.j.reserve(p,quantity*(D(price)+D('.01')));self.ledger.record(p,quantity,price,'.01')

    def test_one_budget_includes_disabled_sibling_and_pending(self):
        self.fill('d','2','buy',100,'.50');self.fill('r','3','buy',100,'.40')
        self.j.reserve({'idempotencyKey':'pending','exchangeId':'3'},10)
        before=self.path.read_bytes();r=race_budgets(self.config,self.path)
        self.assertEqual(D(r['races']['race']['committed']),102)
        self.assertEqual(D(r['races']['race']['remaining']),148)
        self.assertEqual(before,self.path.read_bytes())

    def test_realized_losses_remain_reserved_after_sale(self):
        self.fill('d','2','buy',100,'.50');self.fill('sale','2','sell',100,'.40')
        self.assertEqual(D(race_budgets(self.config,self.path)['races']['race']['committed']),12)

    def test_unknown_inventory_and_missing_journal_never_imply_free_budget(self):
        self.fill('unknown','99','buy',10,'.5')
        r=race_budgets(self.config,self.path);self.assertEqual(r['status'],'unmapped_inventory_or_orders')
        self.assertIsNone(r['races']['race']['remaining'])
        missing=Path(self.temp.name)/'missing.sqlite3'
        self.assertEqual(race_budgets(self.config,missing)['status'],'journal_missing');self.assertFalse(missing.exists())

    def test_closed_unimported_orders_block_budget_estimate(self):
        self.j.reserve({'idempotencyKey':'x','exchangeId':'2'},10);self.j.complete('x',10)
        self.assertEqual(race_budgets(self.config,self.path)['status'],'unimported_orders')


class RefreshTests(unittest.TestCase):
    def setUp(self):
        self.d=example();self.r=republican(self.d)
        self.r['contract']['sig']['id']='999';self.r['contract']['sig']['exchange']='9999'
        self.r['mapping'].update(sig_market_id='999',sig_exchange_id='9999')
        self.d['mapping']['enabled']=True
        self.config={'tournament_id':'test','markets':[self.d['mapping']], 'limits':{'per_market':'250'},
                     'strategy':{'max_age_seconds':15,'max_reference_spread':'.08'}}
        self.sig=Mock();self.sig.tid='test'
        def market(review):
            s=review['contract']['sig']
            return {**s,'status':'open','exchanges':[{'id':s['exchange'],'option':'YES'}]}
        self.markets={self.d['mapping']['sig_market_id']:market(self.d),'999':market(self.r)}
        self.sig.markets.return_value=list(self.markets.values())
        self.sig.market.side_effect=lambda mid:self.markets[str(mid)]
        self.sig.book.side_effect=lambda ex:Book.make([('.5',100)],[('.55',100)])
        self.refs=Mock()
        self.ks=[{**r['contract']['kalshi'],'event_ticker':'event'} for r in (self.d,self.r)]
        self.refs.kalshi.request.side_effect=lambda path,params=None: {'market':self.ks[0]} if path!='/markets' else {'markets':self.ks,'cursor':''}
        self.refs.gamma.request.return_value={'slug':self.d['mapping']['polymarket_event'],
            'markets':[{**r['contract']['polymarket'],'slug':p} for r,p in ((self.d,'d'),(self.r,'r'))]}
        def metadata(mapping):
            r=self.d if mapping['polymarket_market']=='d' else self.r
            return r['contract']['kalshi'],r['contract']['polymarket'],r['contract']['poly_yes_token']
        self.refs.metadata.side_effect=metadata
        self.refs.books.side_effect=lambda *args:[Book.make([('.5',100)],[('.55',100)]) for _ in range(2)]

    def test_fresh_discovery_is_GET_only_preserves_config_and_no_inverse_assumption(self):
        before=copy.deepcopy(self.config)
        result=refresh_race(self.config,self.d['mapping']['race_key'],self.sig,self.refs)
        self.assertEqual(self.config,before);self.assertEqual(len(result['contracts']),2)
        for c in result['contracts']:
            self.assertTrue(c['basis']['structural_identity_verified']);self.assertFalse(c['new_trading_authorized'])
            self.assertFalse(c['mapping']['enabled']);self.assertNotIn('exposure_sign',c['mapping'])
            self.assertEqual(set(c['quotes']),{'SIG','Kalshi','Polymarket'})
        self.sig.place.assert_not_called();self.sig.cancel.assert_not_called()
        json.dumps(result,allow_nan=False)

    def test_wrong_tournament_and_unconfigured_race_stop_before_discovery(self):
        self.sig.tid='other'
        with self.assertRaises(ValueError):refresh_race(self.config,'x',self.sig,self.refs)
        self.sig.markets.assert_not_called()
        self.sig.tid='test'
        with self.assertRaises(ValueError):refresh_race(self.config,'2028:senate:NE',self.sig,self.refs)

    def test_ambiguous_party_and_candidate_only_reference_not_substituted(self):
        self.refs.gamma.request.return_value['markets'][1]['question']='Will Person A win?'
        result=refresh_race(self.config,self.d['mapping']['race_key'],self.sig,self.refs)
        r=next(c for c in result['contracts'] if c['party']=='Republican Party')
        self.assertIn('absent or ambiguous',r['reason']);self.assertNotIn('basis',r)
        self.sig.place.assert_not_called()

    def test_independent_title_with_nonpartisan_rule_is_flagged(self):
        independent=copy.deepcopy(self.markets['999'])
        independent.update(id='1000',title=independent['title'].replace('Republican Party','Independent Party'),
                           exchanges=[{'id':'10000','option':'YES'}])
        independent['resolution_tree']['contract_details']['winnerName']='Nonpartisan'
        self.markets['1000']=independent
        self.sig.markets.return_value=list(self.markets.values())
        result=refresh_race(self.config,self.d['mapping']['race_key'],self.sig,self.refs)
        row=next(c for c in result['contracts'] if c['party']=='Independent Party')
        self.assertEqual(row['reason'],'SIG winner identity mismatch')
        self.assertEqual(row['SIG_details']['winnerName'],'Nonpartisan')
        self.assertFalse(row['new_trading_authorized']);self.assertNotIn('mapping',row)

    def test_pagination_repeated_cursor_duplicate_or_foreign_event_rejected(self):
        http=Mock();http.request.side_effect=[{'markets':[{'ticker':'1','event_ticker':'e'}],'cursor':'a'},
            {'markets':[{'ticker':'2','event_ticker':'e'}],'cursor':''}]
        self.assertEqual(len(event_markets(http,'e')),2)
        for pages in ([{'markets':[{'ticker':'1','event_ticker':'other'}],'cursor':''}],
                      [{'markets':[{'ticker':'1','event_ticker':'e'}],'cursor':'a'}, {'markets':[{'ticker':'1','event_ticker':'e'}],'cursor':''}],
                      [{'markets':[{'ticker':'1','event_ticker':'e'}],'cursor':'a'}, {'markets':[{'ticker':'2','event_ticker':'e'}],'cursor':'a'}]):
            http.request.side_effect=pages
            with self.assertRaises(ValueError):event_markets(http,'e')


class CachedReportTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.d=example();self.r=republican(self.d)
        self.config={'tournament_id':'test','limits':{'per_market':'250'},'markets':[self.d['mapping']],
                     'strategy':{'max_age_seconds':15,'max_reference_spread':'.08'}}
        audit={'at':100,'reviews':[self.d], 'markets':[{'id':self.d['mapping']['sig_market_id'],'title':self.d['contract']['sig']['title']},
               {'id':'999','title':self.r['contract']['sig']['title']}]}
        self.d['sig_market_id']=self.d['mapping']['sig_market_id']
        (self.root/'catalog-review.json').write_text(json.dumps(audit))

    def test_cached_title_not_promoted_and_budget_not_duplicated(self):
        r=report(self.config,self.root,now=200)
        self.assertEqual(r['summary']['races'],1)
        contracts=r['races'][0]['contracts'];self.assertEqual(contracts[1]['status'],'rules_not_fetched')
        self.assertIsNone(r['races'][0]['shared_race_budget']['remaining'])
        self.assertFalse(r['races'][0]['new_trading_authorized'])
        self.assertIn('not verified equivalents',r['races'][0]['relationship'])

    def test_expired_quotes_stay_historical_and_foreign_refresh_rejected(self):
        directory=self.root/'contract-reviews';directory.mkdir()
        q=book_record(Book.make([('.4',20)],[('.5',30)]),self.config['strategy'],'SIG')
        item={'sig_market_id':'999','title':self.r['contract']['sig']['title'], 'race_key':self.d['mapping']['race_key'],
              'party':'Republican Party','quotes':{'SIG':q},'status':'review_required'}
        data={'version':1,'tournament_id':'test','race_key':item['race_key'],'contracts':[item]}
        (directory/'r.json').write_text(json.dumps(data))
        r=report(self.config,self.root,now=q['at']+16)
        self.assertTrue(all(not v['quote_fresh_at_report'] for v in r['races'][0]['SIG_quote_views']))
        data['tournament_id']='other';(directory/'r.json').write_text(json.dumps(data))
        self.assertEqual(report(self.config,self.root)['rejected_refresh_files'],['r.json'])

    def test_diagnostic_gap_requires_fresh_aligned_references_and_is_not_trade_signal(self):
        directory=self.root/'contract-reviews';directory.mkdir()
        self.config['strategy']['cost_buffer_per_share']='.01'
        quotes={v:book_record(Book.make([('.5' if v=='SIG' else '.6',100)],
                    [('.55' if v=='SIG' else '.65',100)]),self.config['strategy'],v)
                for v in ('SIG','Kalshi','Polymarket')}
        at=max(q['at'] for q in quotes.values())
        item={'sig_market_id':'999','title':self.r['contract']['sig']['title'],
              'race_key':self.d['mapping']['race_key'],'party':'Republican Party',
              'quotes':quotes,'basis':{'structural_identity_verified':True,'flags':[]}}
        data={'version':1,'tournament_id':'test','race_key':item['race_key'],'contracts':[item]}
        (directory/'r.json').write_text(json.dumps(data))
        row=report(self.config,self.root,now=at)['races'][0]['SIG_quote_views'][0]
        self.assertEqual(D(row['diagnostic_gap_after_entry_buffer']),D('.04'))
        self.assertFalse(row['gap_is_trade_signal'])
        rows=report(self.config,self.root,now=at+16)['races'][0]['SIG_quote_views']
        self.assertTrue(all(r['diagnostic_gap_after_entry_buffer'] is None for r in rows))

    def test_offline_cli_needs_no_credentials_and_writes_nothing(self):
        config=self.root/'config.json';config.write_text(json.dumps(self.config))
        before={p.name:p.read_bytes() for p in self.root.iterdir() if p.is_file()}
        with patch.object(cli,'RUNTIME',self.root),patch.object(cli,'key') as key,patch.object(cli,'output'),patch('sys.argv',['bot','--config',str(config),'contract-review','--json']):
            cli.main();key.assert_not_called()
        self.assertEqual(before,{p.name:p.read_bytes() for p in self.root.iterdir() if p.is_file()})

    def test_refresh_evidence_works_without_historical_catalog(self):
        (self.root/'catalog-review.json').unlink()
        self.assertEqual(report(self.config,self.root)['status'],'No catalog evidence yet')
        directory=self.root/'contract-reviews';directory.mkdir()
        item={'sig_market_id':'999','title':self.r['contract']['sig']['title'],
              'race_key':self.d['mapping']['race_key'],'party':'Republican Party',
              'quotes':{},'status':'review_required'}
        data={'version':1,'tournament_id':'test','race_key':item['race_key'],'contracts':[item]}
        (directory/'r.json').write_text(json.dumps(data))
        result=report(self.config,self.root)
        self.assertEqual(result['status'],'ok')
        self.assertEqual(result['summary']['party_contracts'],{'Republican Party':1})
        self.assertIsNone(result['catalog_saved_at'])
        self.assertFalse(result['races'][0]['new_trading_authorized'])
