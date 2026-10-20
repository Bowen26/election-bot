import unittest,tempfile
from pathlib import Path
from election_bot.votehub import adapt,bindings
from election_bot.poll_collector import collect
import test_poll_collector as fixture
class CoverageTests(unittest.TestCase):
    def test_new_reviewed_bindings_keep_orientation_and_residual(self):
        c=fixture.config();base=c['markets'][0]
        for i,(state,subject,dem,rep) in enumerate([('MA','2026 Massachusetts','Ed Markey','John Deaton'),('RI','2026 Rhode Island','Jack Reed','Raymond McKay')]):
            c['markets'].append(dict(base,name=state,sig_exchange_id=str(100+i),race_key='2026:senate:'+state,exposure_sign=-1))
            r=fixture.row(id=state,subject=subject,pollster='University of New Hampshire',answers=[{'choice':dem,'pct':53},{'choice':rep,'pct':33}])
            evidence,_=adapt([r],c,{},fixture.T)
            self.assertEqual(evidence['polls'][0]['other_pct'],14)
            bound=next(x for x in evidence['races'] if x['race_key']=='2026:senate:'+state)
            self.assertEqual(bound['yes_party'],'REP')
    def test_failed_senate_cannot_be_masked_by_auxiliary_success(self):
        def fetch(office,*args):
            if office=='us-senator':raise TimeoutError()
            return 'https://example.test',[]
        with tempfile.TemporaryDirectory() as d:
            r=collect(d,fixture.config(),force=True,now=fixture.T,fetcher=fetch)
            self.assertEqual(r['status'],'error');self.assertIsNone(r['last_success_at'])
            self.assertFalse((Path(d)/'fair-value.sqlite3').exists())
    def test_both_auxiliary_failures_do_not_drop_senate(self):
        def fetch(office,*args):
            if office!='us-senator':raise TimeoutError()
            return 'https://example.test',[fixture.row()]
        with tempfile.TemporaryDirectory() as d:
            r=collect(d,fixture.config(),force=True,now=fixture.T,fetcher=fetch)
            self.assertEqual(r['status'],'ok');self.assertEqual(len(r['source_failures']),2)
            self.assertEqual(r['accepted_polls'],1);self.assertEqual(r['staged_only_offices'],[])
