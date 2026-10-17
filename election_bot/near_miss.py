"""Prospective research-only threshold portfolios, isolated from order accounting."""
from collections import Counter, defaultdict
import json
import math
from pathlib import Path
import sqlite3

from .fair_value import VERSION as MODEL_VERSION
from .leadership import quote
from .strategy import D

VERSION = 'poll_threshold_portfolios_v1'
POLICY = {'thresholds':['.03','.04','.05'], 'models':['neutral_prior','historical_lean'],
          'horizons':{'300':120,'900':180,'3600':900}, 'initial_cash':'100',
          'quantity':1, 'depth_fraction':'.25', 'max_spread':'.04', 'max_age_seconds':15,
          'costs':'recorded per-share buffer at both entry and exit',
          'entry':'conservative probability minus ask minus two buffers',
          'fill_assumption':'displayed one-share fills; no queue or execution latency'}


def numeric(value):
    if isinstance(value,bool):raise ValueError('Boolean number')
    x=D(value)
    if not x.is_finite():raise ValueError('Nonfinite number')
    return x


def snapshot(mapping, book, research, fee, now):
    """Freeze diagnostics and threshold decisions at the original observation."""
    if not isinstance(research.get('snapshot_id'),str) or not research['snapshot_id']:
        raise ValueError('Invalid snapshot identity')
    if not mapping.get('contract_fingerprint'):
        raise ValueError('Missing contract binding')
    fee=numeric(fee)
    if not 0 <= fee < 1:raise ValueError('Invalid buffer')
    result={'experiment':VERSION,'policy':POLICY,'at':now,'snapshot_id':research['snapshot_id'],
            'exchange':mapping['sig_exchange_id'],'race':mapping.get('race_key') or mapping['sig_exchange_id'],
            'fingerprint':mapping.get('contract_fingerprint'), 'fee':str(fee),
            'quote':None,'quote_rejections':[], 'models':{}}
    raw={'bid':book.bids[0] if book.bids else None,'ask':book.asks[0] if book.asks else None,
         'observed_at':book.observed_at,'source_at':book.source_at,
         'timestamp_basis':book.timestamp_basis,'quality':{'issues':[]}}
    try:
        q=quote(raw,now)  # Same strict 15-second, positive-depth validation as reports.
        result['quote']={k:str(v) if k in ('bid','ask','bid_size','ask_size') else v for k,v in q.items()}
    except (KeyError,TypeError,ValueError,ArithmeticError) as error:
        result['quote_rejections'].append(str(error))
        q=None
    # Always record raw ages: distinguish actual stale/future source data from later report timing.
    result['ages_seconds']={k:(now-getattr(book,k)) if math.isfinite(getattr(book,k)) else None for k in ('observed_at','source_at')}
    for name in POLICY['models']:
        forecast=research.get('prior_variants',{}).get(name,{}).get('forecast')
        diagnostic={'status':'unavailable','reason':research.get('reason','prior_variant_unavailable'),
                    'sides':[], 'decisions':{}}
        result['models'][name]=diagnostic
        if not forecast:continue
        diagnostic.update(reason=forecast.get('reason'),pollster_count=forecast.get('pollster_count'),
                          excluded=forecast.get('excluded',{}))
        if forecast.get('status')!='estimated':continue
        try:
            low,prob,high=[numeric(forecast[k]) for k in ('yes_low','yes_probability','yes_high')]
            if (forecast['version']!=MODEL_VERSION or forecast['as_of']!=now or
                    forecast['race_key']!=result['race'] or not 0<=low<=prob<=high<=1):
                raise ValueError('Invalid forecast binding/time/probabilities')
            diagnostic.update(status='estimated',reason=None,estimate_id=forecast['estimate_id'])
            if q is None:continue
            for side,ask,depth,central,conservative in (
                ('yes',q['ask'],q['ask_size'],prob,low),
                ('no',1-q['bid'],q['bid_size'],1-prob,1-high)):
                central_edge=central-ask-2*fee; robust=conservative-ask-2*fee
                blockers=[]
                if q['ask']-q['bid']>D(POLICY['max_spread']):blockers.append('spread')
                if ask%D('.005'):blockers.append('tick')
                if depth*D(POLICY['depth_fraction'])<1:blockers.append('entry_depth')
                if q['bid_size' if side=='yes' else 'ask_size']*D(POLICY['depth_fraction'])<1:
                    blockers.append('exit_depth_at_entry')
                row={'side':side,'ask':str(ask),'depth':str(depth),'central_net_edge':str(central_edge),
                     'conservative_net_edge':str(robust),'uncertainty_deduction':str(central-conservative),
                     'thresholds':{}}
                for threshold in POLICY['thresholds']:
                    failures=list(blockers)
                    if central_edge<D(threshold):failures.append('price_gap')
                    elif robust<D(threshold):failures.append('uncertainty')
                    row['thresholds'][threshold]={'rejections':failures,
                        'edge_shortfall':str(max(D(0),D(threshold)-robust))}
                diagnostic['sides'].append(row)
            for threshold in POLICY['thresholds']:
                candidates=[r for r in diagnostic['sides'] if not r['thresholds'][threshold]['rejections']]
                diagnostic['decisions'][threshold]=({'side':candidates[0]['side'],'ask':candidates[0]['ask']}
                                                    if len(candidates)==1 else None)
        except (KeyError,TypeError,ValueError,ArithmeticError):
            diagnostic.update(status='unavailable',reason='invalid_forecast',sides=[],decisions={})
    return result


def path_for(runtime,paper=False):
    return Path(runtime)/('near-miss-paper.sqlite3' if paper else 'near-miss-live.sqlite3')


def connect(path):
    db=sqlite3.connect(path,timeout=.25)
    db.row_factory=sqlite3.Row
    db.executescript('''
      CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS portfolios(id TEXT PRIMARY KEY,model TEXT,threshold TEXT,horizon INTEGER,cash TEXT);
      CREATE TABLE IF NOT EXISTS observations(id TEXT PRIMARY KEY,at REAL,detail TEXT);
      CREATE INDEX IF NOT EXISTS observations_at ON observations(at);
      CREATE TABLE IF NOT EXISTS positions(id INTEGER PRIMARY KEY,portfolio TEXT,race TEXT,exchange_id TEXT,
        fingerprint TEXT,side TEXT,entry_at REAL,due REAL,deadline REAL,price TEXT,fee TEXT,cost TEXT,
        entry_snapshot TEXT,closed_at REAL,exit_price TEXT,pnl TEXT,exit_snapshot TEXT,first_exit_issue TEXT);
      CREATE UNIQUE INDEX IF NOT EXISTS one_open_race ON positions(portfolio,race) WHERE closed_at IS NULL;
      CREATE INDEX IF NOT EXISTS positions_exchange ON positions(exchange_id,closed_at);
      CREATE TABLE IF NOT EXISTS cooldowns(portfolio TEXT,race TEXT,until_at REAL,PRIMARY KEY(portfolio,race));
    ''')
    return db


def record(runtime,mapping,book,research,fee,now,paper=False):
    """One atomic simulation step. Never imports broker/ledger, or touches live funds."""
    data=snapshot(mapping,book,research,fee,now)
    path=path_for(runtime,paper)
    db=connect(path)
    try:
        db.execute('BEGIN IMMEDIATE')
        policy=json.dumps({'version':VERSION,'policy':POLICY},sort_keys=True)
        old=db.execute("SELECT value FROM meta WHERE key='policy'").fetchone()
        if old and old['value']!=policy:raise ValueError('Near-miss policy changed; new experiment required')
        if not old:
            db.execute('INSERT INTO meta VALUES (?,?)',('policy',policy))
            db.execute('INSERT INTO meta VALUES (?,?)',('started_at',str(now)))
            for model in POLICY['models']:
                for threshold in POLICY['thresholds']:
                    for horizon in POLICY['horizons']:
                        pid=f'{model}:{threshold}:{horizon}'
                        db.execute('INSERT INTO portfolios VALUES (?,?,?,?,?)',(pid,model,threshold,int(horizon),POLICY['initial_cash']))
        if db.execute('SELECT 1 FROM observations WHERE id=?',(data['snapshot_id'],)).fetchone():
            db.rollback();return
        last=db.execute("SELECT value FROM meta WHERE key='last_at'").fetchone()
        if last and now<float(last['value']):raise ValueError('Near-miss observation clock regressed')
        q=data['quote']; results=[]
        # Exits happen before entries, only on genuinely post-horizon fresh source/observation times.
        if q:
            for position in db.execute('SELECT * FROM positions WHERE exchange_id=? AND closed_at IS NULL',
                                       (data['exchange'],)).fetchall():
                if now<position['due'] or min(q['source'],q['observed'])<position['due']:continue
                if position['fingerprint']!=data['fingerprint']:
                    db.execute("UPDATE positions SET first_exit_issue=COALESCE(first_exit_issue,'binding_changed') WHERE id=?",(position['id'],))
                    continue
                price=numeric(q['bid']) if position['side']=='yes' else 1-numeric(q['ask'])
                depth=numeric(q['bid_size'] if position['side']=='yes' else q['ask_size'])
                if depth*D(POLICY['depth_fraction'])<1 or price<numeric(position['fee']) or price%D('.005'):
                    db.execute("UPDATE positions SET first_exit_issue=COALESCE(first_exit_issue,'thin_below_buffer_or_tick') WHERE id=?",(position['id'],))
                    continue
                proceeds=price-numeric(position['fee']);pnl=proceeds-numeric(position['cost'])
                cash=numeric(db.execute('SELECT cash FROM portfolios WHERE id=?',(position['portfolio'],)).fetchone()['cash'])
                db.execute('UPDATE portfolios SET cash=? WHERE id=?',(str(cash+proceeds),position['portfolio']))
                db.execute('UPDATE positions SET closed_at=?,exit_price=?,pnl=?,exit_snapshot=? WHERE id=?',
                           (now,str(price),str(pnl),data['snapshot_id'],position['id']))
        for portfolio in db.execute('SELECT * FROM portfolios ORDER BY id').fetchall():
            model=data['models'][portfolio['model']]
            action=model['decisions'].get(portfolio['threshold'])
            reason='unavailable' if model['status']!='estimated' else 'quote_rejected' if q is None else 'no_candidate'
            if action:
                cost=numeric(action['ask'])+numeric(data['fee'])
                owned=db.execute('SELECT 1 FROM positions WHERE portfolio=? AND race=? AND closed_at IS NULL',
                                 (portfolio['id'],data['race'])).fetchone()
                cooldown=db.execute('SELECT until_at FROM cooldowns WHERE portfolio=? AND race=?',
                                    (portfolio['id'],data['race'])).fetchone()
                if owned:reason='position_open'
                elif cooldown and now<cooldown['until_at']:reason='cooldown'
                elif numeric(portfolio['cash'])<cost:reason='cash_limit'
                else:
                    reason='simulated_buy'
                    due=now+portfolio['horizon'];deadline=due+POLICY['horizons'][str(portfolio['horizon'])]
                    db.execute('UPDATE portfolios SET cash=? WHERE id=?',(str(numeric(portfolio['cash'])-cost),portfolio['id']))
                    db.execute('INSERT INTO positions(portfolio,race,exchange_id,fingerprint,side,entry_at,due,deadline,price,fee,cost,entry_snapshot) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                               (portfolio['id'],data['race'],data['exchange'],data['fingerprint'],action['side'],now,due,deadline,action['ask'],data['fee'],str(cost),data['snapshot_id']))
                    db.execute('INSERT INTO cooldowns VALUES (?,?,?) ON CONFLICT(portfolio,race) DO UPDATE SET until_at=excluded.until_at',
                               (portfolio['id'],data['race'],deadline))
            results.append({'portfolio':portfolio['id'],'reason':reason})
        data['portfolio_decisions']=results
        db.execute('INSERT INTO observations VALUES (?,?,?)',(data['snapshot_id'],now,json.dumps(data,allow_nan=False)))
        db.execute("INSERT INTO meta VALUES ('last_at',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(str(now),))
        # Keep seven days of detailed scan diagnostics; balances and positions never expire.
        db.execute('DELETE FROM observations WHERE id IN (SELECT id FROM observations WHERE at<? ORDER BY at LIMIT 500)',(now-7*86400,))
        db.commit()
    except Exception:
        db.rollback();raise
    finally:db.close()


def report(runtime,paper=False,hours=24,now=None):
    import time
    now=time.time() if now is None else now
    if isinstance(hours,bool) or not 0<float(hours)<=168:raise ValueError('Invalid near-miss lookback')
    result={'experiment':VERSION,'research_only':True,'initialized':False,'policy':POLICY,'as_of':now,
            'hours':hours,'diagnostics':{},'portfolios':[],
            'limitations':['Simulated displayed fills with 25% top-depth allowance; no actual orders, queue or latency modeling.',
                'Independent 100-coin balances per model/threshold/horizon; not live account cash or portfolio limits.',
                'Entry edge subtracts TWO buffers, unlike the older one-buffer research candidate screen.',
                'Open/missing outcomes retain cash at risk; realized profit alone is not total portfolio performance.',
                'Late or initially thin exits recycle simulated proceeds but are not timely horizon outcomes.',
                'No retrospective entries or automatic live promotion. Repeated races/models are correlated.']}
    path=path_for(runtime,paper)
    if not path.exists():return result
    db=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True);db.row_factory=sqlite3.Row
    try:
        db.execute('BEGIN')
        meta=dict(db.execute('SELECT key,value FROM meta'))
        if not meta:return result
        result.update(initialized=True,started_at=float(meta['started_at']),last_observation_at=float(meta['last_at']))
        result['policy']=json.loads(meta['policy'])['policy']
        diagnostics=defaultdict(Counter);shortfalls=defaultdict(list);portfolio_counts=defaultdict(Counter)
        rows=db.execute('SELECT detail FROM observations WHERE at>=? AND at<=? ORDER BY at',(now-float(hours)*3600,now))
        count=0
        for row in rows:
            d=json.loads(row['detail']);count+=1
            for model,info in d['models'].items():
                key=d['race']+'/'+model
                if info['status']!='estimated':diagnostics[key]['polling:'+str(info['reason'])]+=1
                if d['quote_rejections']:
                    for reason in d['quote_rejections']:diagnostics[key]['quote:'+reason]+=1
                for side in info['sides']:
                    for threshold,state in side['thresholds'].items():
                        skey=key+'/'+side['side']+'/'+threshold
                        for reason in state['rejections']:diagnostics[skey][reason]+=1
                        if not state['rejections']:diagnostics[skey]['eligible_price_and_depth']+=1
                        shortfalls[skey].append(numeric(state['edge_shortfall']))
            for decision in d['portfolio_decisions']:portfolio_counts[decision['portfolio']][decision['reason']]+=1
        result['observations_in_window']=count
        result['diagnostics']={key:{'counts':dict(value),
            'minimum_edge_shortfall':str(min(shortfalls[key])) if shortfalls[key] else None,
            'latest_edge_shortfall':str(shortfalls[key][-1]) if shortfalls[key] else None}
            for key,value in diagnostics.items()}
        for portfolio in db.execute('SELECT * FROM portfolios ORDER BY id'):
            positions=list(db.execute('SELECT * FROM positions WHERE portfolio=?',(portfolio['id'],)))
            closed=[p for p in positions if p['closed_at'] is not None]
            window_closed=[p for p in closed if now-float(hours)*3600<=p['closed_at']<=now]
            opened=[p for p in positions if p['closed_at'] is None]
            timely=[p for p in window_closed if p['closed_at']<=p['deadline'] and not p['first_exit_issue']]
            result['portfolios'].append(dict(portfolio,
                simulated_cash=portfolio['cash'],initial_cash=result['policy']['initial_cash'],
                lifetime_entries=len(positions),lifetime_closed=len(closed),open_positions=len(opened),
                open_entry_cost=str(sum((numeric(p['cost']) for p in opened),D(0))),
                overdue_open_positions=sum(now>p['deadline'] for p in opened),
                pending_open_positions=sum(now<=p['deadline'] for p in opened),
                lifetime_realized_pnl=str(sum((numeric(p['pnl']) for p in closed),D(0))),
                closed_in_window=len(window_closed),realized_pnl_in_window=str(sum((numeric(p['pnl']) for p in window_closed),D(0))),
                timely_exits_in_window=len(timely),non_timely_exits_in_window=len(window_closed)-len(timely),
                mean_timely_net_coins=str(sum((numeric(p['pnl']) for p in timely),D(0))/len(timely)) if timely else None,
                decisions_in_window=dict(portfolio_counts[portfolio['id']]),
                missing_outcomes_are_not_zero=True))
        return result
    finally:db.close()


def format_report(result):
    if not result['initialized']:return 'Near-miss portfolios: not started; restart the worker to begin prospective collection.'
    lines=[f"Near-miss portfolios: {result['observations_in_window']} observations in window; simulated funds only"]
    for row in result['portfolios']:
        lines.append(f"  {row['id']}: cash {row['simulated_cash']}; open {row['open_positions']}; "
                     f"closed in window {row['closed_in_window']}; realized coins {row['realized_pnl_in_window']}; "
                     f"timely outcomes {row['timely_exits_in_window']}; overdue open {row['overdue_open_positions']}")
    reasons=Counter()
    for row in result['diagnostics'].values():reasons.update(row['counts'])
    lines.append('Rejection/eligibility counts (overlap across sides/thresholds): '+str(dict(reasons)))
    lines.append('Use --json for per-race shortfalls and cash/cooldown/position blocks. Realized coins exclude open-position losses.')
    return '\n'.join(lines)
