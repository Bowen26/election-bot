"""Prospective route sizing from spaced, mature, complete-day observations.

No broker access, no retrospective route inference, no changes to sells.
"""
from collections import defaultdict
from dataclasses import replace
from datetime import datetime,timezone
import json
from pathlib import Path
import sqlite3
import time
from .strategy import D

VERSION='route_throttle_v1'
ROUTES={'entry_gap':'reference','patient_entry':'patient','model_fair_value':'model','election_night':'reference'}
POLICY={'version':VERSION,'horizon':3600,'window':900,'spacing':3600,'days':2,
        'minimum_measured_per_day':20,'minimum_races_per_day':5,'minimum_coverage':'.70',
        'bad_net_mean':'-.02','bad_bid_move_mean':'-.01','reduced_multiplier':'.5'}


def enabled(config):
    p=config.get('performance_throttle',{})
    if not isinstance(p,dict) or set(p)-{'enabled'} or type(p.get('enabled',False)) is not bool:
        raise ValueError('performance_throttle requires a boolean enabled setting')
    return p.get('enabled',False)


def initialize(db):
    db.executescript('''CREATE TABLE IF NOT EXISTS route_entry_evidence (
        key TEXT PRIMARY KEY,route TEXT,race TEXT,at REAL,detail TEXT);
        CREATE TABLE IF NOT EXISTS route_throttle_state (route TEXT PRIMARY KEY,detail TEXT);
        CREATE TABLE IF NOT EXISTS route_throttle_meta (key TEXT PRIMARY KEY,value TEXT);''')
    value=json.dumps(POLICY,sort_keys=True)
    old=db.execute("SELECT value FROM route_throttle_meta WHERE key='policy'").fetchone()
    if old and old[0]!=value:raise ValueError('Performance throttle policy changed; reviewed migration required')
    with db:db.execute("INSERT OR IGNORE INTO route_throttle_meta VALUES ('policy',?)",(value,))


def evaluate(db,now):
    # A UTC day is usable only after its last fill's horizon and window finish.
    end=int((now-POLICY['horizon']-POLICY['window'])//86400)*86400
    start=end-2*86400
    groups=defaultdict(list);last={}
    rows=db.execute('''SELECT t.route,t.race,t.detail,e.key,e.at,e.quantity,e.price,e.buffer,
                       m.at AS observed,m.pnl,m.reason FROM route_entry_evidence t
        JOIN executions e ON e.key=t.key LEFT JOIN markouts m ON m.key=e.key AND m.horizon=3600
        WHERE e.action='buy' AND CAST(e.quantity AS REAL)>0 AND e.at>=? AND e.at<?
        ORDER BY e.at,e.rowid''',(start,end)).fetchall()
    for r in rows:
        identity=(r['route'],r['race'])
        if r['at']-last.get(identity,float('-inf'))<POLICY['spacing']:continue
        last[identity]=r['at']
        groups[(r['route'],int(r['at']//86400)*86400)].append(r)
    result={}
    for route in sorted(set(ROUTES.values())):
        days=[]
        for day in (start,start+86400):
            samples=groups[(route,day)];net=[];moves=[];races=set();invalid=0
            for r in samples:
                if r['pnl'] is None or r['observed'] is None:continue
                try:
                    if not r['at']+3600<=r['observed']<=min(now,r['at']+4500):raise ValueError('outside window')
                    d=json.loads(r['detail']);q=D(r['quantity']);price=D(r['price']);fee=D(r['buffer'])
                    bid=D(d['bid']);value=D(r['pnl'])/q
                    if not 0<bid<1 or not 0<price<1 or fee<0:raise ValueError('bad benchmark')
                    move=value+price+2*fee-bid
                except (ValueError,KeyError,TypeError,ArithmeticError):invalid+=1;continue
                net.append(value);moves.append(move);races.add(r['race'])
            coverage=D(len(net))/len(samples) if samples else D(0)
            ready=(len(net)>=POLICY['minimum_measured_per_day'] and len(races)>=POLICY['minimum_races_per_day']
                   and coverage>=D(POLICY['minimum_coverage']))
            mean_net=sum(net,D(0))/len(net) if net else None
            mean_move=sum(moves,D(0))/len(moves) if moves else None
            days.append({'day':datetime.fromtimestamp(day,timezone.utc).date().isoformat(),
                'eligible_spaced_fills':len(samples),'measured':len(net),'races':len(races),
                'coverage':str(coverage),'invalid':invalid,'unmeasured':len(samples)-len(net),
                'mean_net_per_share':str(mean_net) if mean_net is not None else None,
                'mean_bid_move':str(mean_move) if mean_move is not None else None,'ready':ready,
                'bad':ready and mean_net<=D(POLICY['bad_net_mean']) and mean_move<=D(POLICY['bad_bid_move_mean']),
                'recovered':ready and mean_net>=0 and mean_move>=0})
        prior=db.execute('SELECT detail FROM route_throttle_state WHERE route=?',(route,)).fetchone()
        previous=json.loads(prior[0]) if prior else {}
        mult=previous.get('suggested_multiplier','1')
        if all(d['bad'] for d in days):mult=POLICY['reduced_multiplier'];reason='persistent_underperformance'
        elif all(d['recovered'] for d in days):mult='1';reason='two_days_recovered'
        else:reason='retain_previous_insufficient_or_mixed_evidence'
        result[route]={'route':route,'suggested_multiplier':mult,'reason':reason,'days':days,
                       'evaluated_at':now,'window_end':end,'previous_multiplier':previous.get('suggested_multiplier','1')}
    return result


class PerformanceThrottle:
    def __init__(self,db,config):
        self.db,self.config=db,config;self.error=None;enabled(config);initialize(db)
    def refresh(self,now=None):
        now=time.time() if now is None else now
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            result=evaluate(self.db,now)
            for route,d in result.items():
                self.db.execute('INSERT OR REPLACE INTO route_throttle_state VALUES (?,?)',(route,json.dumps(d)))
        self.error=None
        return result
    def multiplier(self,reason,now=None):
        if not enabled(self.config):return D(1)
        if self.error is not None:raise ValueError('Route throttle assessment unavailable')
        route=ROUTES.get(reason)
        if route is None:raise ValueError('Unclassified buy cannot bypass route throttle')
        row=self.db.execute('SELECT detail FROM route_throttle_state WHERE route=?',(route,)).fetchone()
        if row is None:raise ValueError('Route throttle has no refreshed state')
        d=json.loads(row[0]);now=time.time() if now is None else now
        if not 0<=now-d['evaluated_at']<=600:raise ValueError('Route throttle state stale')
        value=D(d['suggested_multiplier'])
        if value not in (D(1),D('.5')):raise ValueError('Invalid route throttle state')
        return value
    def scale(self,signal):
        if signal is None or signal.action!='buy':return signal
        q=max(1,int(signal.quantity*self.multiplier(signal.reason)))
        return replace(signal,quantity=min(signal.quantity,q))
    def attach(self,payload,mapping,book,raw):
        if not self.db.in_transaction:raise RuntimeError('Route evidence requires reservation transaction')
        route=ROUTES.get(raw.reason)
        if route is None:raise ValueError('Unclassified route evidence')
        target=book if payload['side']=='yes' else book.complement()
        book.check(self.config['strategy']['max_age_seconds'],venue='SIG')
        d={'bid':str(target.bids[0][0]),'ask':str(target.asks[0][0]),'raw_quantity':raw.quantity,
           'submitted_quantity':payload['quantity'],'price':str(payload['price']),
           'side':payload['side'],'fingerprint':mapping['contract_fingerprint'],
           'source_at':book.source_at,'observed_at':book.observed_at,'live_enabled':enabled(self.config),
           'multiplier':str(self.multiplier(raw.reason))}
        self.db.execute('INSERT INTO route_entry_evidence VALUES (?,?,?,?,?)',
            (payload['idempotencyKey'],route,mapping.get('race_key','sig-market:'+mapping['sig_market_id']),time.time(),json.dumps(d)))


def report(path,config,now=None):
    result={'initialized':False,'enabled_in_current_config':enabled(config),'policy':POLICY,
            'note':'Prospective tagged buys only. Hypothetical buffered markouts and decision-bid moves, not realized returns. Spaced observations remain correlated. Exits unchanged; no automatic increases above ordinary size.'}
    path=Path(path)
    if not path.exists():return result
    db=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True);db.row_factory=sqlite3.Row
    try:
        db.execute('BEGIN')
        tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {'route_entry_evidence','route_throttle_state','executions','markouts'}<=tables:return result
        result.update(initialized=True,tagged_attempts=db.execute('SELECT COUNT(*) FROM route_entry_evidence').fetchone()[0],
                      assessment=evaluate(db,time.time() if now is None else now),
                      stored_state=[json.loads(r[0]) for r in db.execute('SELECT detail FROM route_throttle_state')])
        return result
    finally:db.close()


def format_report(r):
    lines=['PERFORMANCE THROTTLE — '+('live sizing enabled in config' if r['enabled_in_current_config'] else 'report-only')]
    if not r['initialized']:lines.append('Not initialized; restart worker to collect prospective evidence.')
    for route,d in r.get('assessment',{}).items():
        lines.append(f"{route}: suggested size {d['suggested_multiplier']}x; {d['reason']}")
        for day in d['days']:lines.append('  '+json.dumps(day))
    lines.append(r['note']);return '\n'.join(lines)
