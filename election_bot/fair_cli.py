"""No-key research commands; live trading configuration is never modified."""
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import time

from .polling import PollStore, read_input
from .fair_research import report, format_report, observe


def template(config):
    races=[]
    for m in config['markets']:
        if not m.get('enabled') or m.get('office')!='senate' or m.get('exposure_sign') not in (-1,1):continue
        races.append({'sig_exchange_id':m['sig_exchange_id'],'contract_fingerprint':m['contract_fingerprint'],
            'race_key':m['race_key'],'yes_party':'DEM' if m['exposure_sign']==1 else 'REP',
            'dem_candidate':'REPLACE_WITH_VERIFIED_DEMOCRATIC_NOMINEE',
            'rep_candidate':'REPLACE_WITH_VERIFIED_REPUBLICAN_NOMINEE',
            'election_at':'REPLACE_WITH_ELECTION_DATE_TIME_AND_ZONE',
            'contest_type':'general_plurality','source_url':'https://REPLACE_WITH_RACE_SOURCE','prior':None})
        if len(races)==10:break
    return {'schema_version':1,'races':races,'polls':[{
        'poll_id':'REPLACE_WITH_STABLE_POLL_ID','survey_id':'REPLACE_WITH_ORIGINAL_SURVEY_ID',
        'race_key':races[0]['race_key'] if races else 'REPLACE_WITH_RACE_KEY',
        'pollster':'REPLACE_WITH_POLLSTER_OR_PANEL_FAMILY',
        'dem_candidate':'REPLACE_WITH_VERIFIED_DEMOCRATIC_NOMINEE',
        'rep_candidate':'REPLACE_WITH_VERIFIED_REPUBLICAN_NOMINEE','stage':'general',
        'population':'lv','partisan':False,'sample_size':None,
        'field_start':'REPLACE_WITH_ISO_TIMESTAMP','field_end':'REPLACE_WITH_ISO_TIMESTAMP',
        'published_at':'REPLACE_WITH_ISO_TIMESTAMP','dem_pct':None,'rep_pct':None,'other_pct':None,
        'methodology':'REPLACE_WITH_METHOD_DESCRIPTION','source_url':'https://REPLACE_WITH_POLL_RELEASE'}]}


def demo():
    from .demo import fixture
    from .strategy import Book
    now=time.time()
    iso=lambda t:datetime.fromtimestamp(t,timezone.utc).isoformat()
    config,_,_=fixture()
    m=config['markets'][0];m.update(race_key='SYNTHETIC:senate:TEST',exposure_sign=1)
    race={'sig_exchange_id':m['sig_exchange_id'],'contract_fingerprint':m['contract_fingerprint'],
          'race_key':m['race_key'],'yes_party':'DEM','dem_candidate':'Synthetic D','rep_candidate':'Synthetic R',
          'election_at':iso(now+30*86400),'contest_type':'general_plurality','source_url':'https://example.test/race'}
    polls=[]
    for i in range(3):
        polls.append({'poll_id':'demo-'+str(i),'survey_id':'demo-'+str(i),'pollster':'Demo pollster '+str(i),
            'race_key':m['race_key'],'dem_candidate':'Synthetic D','rep_candidate':'Synthetic R','stage':'general',
            'population':'lv','partisan':False,'sample_size':800,'field_start':iso(now-5*86400),
            'field_end':iso(now-3*86400),'published_at':iso(now-2*86400),'dem_pct':51+i,'rep_pct':44,
            'other_pct':2,'methodology':'Synthetic illustration','source_url':'https://example.test/poll/'+str(i)})
    with tempfile.TemporaryDirectory() as root:
        store=PollStore(Path(root)/'fair-value.sqlite3')
        try:store.ingest({'schema_version':1,'races':[race],'polls':polls},config,now-1)
        finally:store.close()
        book=Book.make([('.48',100)],[('.50',100)])
        refs=[Book.make([('.49',100)],[('.51',100)]) for _ in range(2)]
        return {'synthetic':True,'no_real_polling_or_trades':True,
                'example':observe(root,m,book,refs,config['strategy'],'demo',now)}


def run(args, runtime, config):
    if args.demo:return demo()
    if args.seed_public:
        data=read_input(Path(__file__).resolve().parent.parent/'data/research/nh-senate-2026-10-08.json')
        eligible=[m for m in config['markets'] if m.get('enabled') and m.get('race_key')=='2026:senate:NH'
                  and m.get('exposure_sign')==1 and m.get('name')=='nh-senate-democratic']
        if len(eligible)!=1:
            raise ValueError('The public seed requires one configured NH Democratic Senate contract')
        m=eligible[0]
        data['races']=[{'sig_exchange_id':m['sig_exchange_id'],'contract_fingerprint':m['contract_fingerprint'],
            'race_key':m['race_key'],'yes_party':'DEM','dem_candidate':'Chris Pappas','rep_candidate':'John E. Sununu',
            'contest_type':'general_plurality','election_at':'2026-11-03T00:00:00-05:00',
            'source_url':'https://scholars.unh.edu/survey_center_polls/1005/',
            'prior':None,'notes':'Research binding only; not a new live settlement-review attestation.'}]
        store=PollStore(Path(runtime)/'fair-value.sqlite3')
        try:return dict(store.ingest(data,config),pilot='NH Senate',automatic_feed=False,live_model_orders=False)
        finally:store.close()
    if args.fetch_public:
        from .poll_sources import stage_public
        return stage_public(runtime)
    if args.template:
        # Refuse to overwrite any user file or existing evidence.
        with args.template.open('x') as stream:json.dump(template(config),stream,indent=2)
        return {'template':str(args.template.resolve()),'status':'draft_placeholders_not_imported'}
    if args.import_json:
        data=read_input(args.import_json)
        store=PollStore(Path(runtime)/'fair-value.sqlite3')
        try:return store.ingest(data,config)
        finally:store.close()
    result=report(runtime,config,args.paper,args.hours)
    return result if args.json else format_report(result)
