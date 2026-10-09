"""Local research integration and read-only reporting. No broker dependency."""
from collections import Counter, defaultdict
import json
from pathlib import Path
import sqlite3
import time

from .fair_value import estimate, compare, VERSION
from .polling import PollStore
from .strategy import D


def observe(runtime, mapping, book, refs, strategy, snapshot_id, now=None):
    path=Path(runtime)/'fair-value.sqlite3'
    if not path.exists():return None
    now=time.time() if now is None else now
    base={'version':1,'model_version':VERSION,'snapshot_id':snapshot_id,
          'exchange':mapping['sig_exchange_id'],'captured_at':now,'research_only':True}
    store=None
    try:
        store=PollStore(path,readonly=True)
        store.db.execute('BEGIN')
        race=next((r for r in store.latest('race',now) if r['sig_exchange_id']==mapping['sig_exchange_id']),None)
        if race is None:return dict(base,status='unavailable',reason='no_reviewed_polling_mapping')
        if race['contract_fingerprint']!=mapping.get('contract_fingerprint') or race['race_key']!=mapping.get('race_key'):
            return dict(base,status='unavailable',reason='contract_binding_changed')
        forecast=estimate(race,store.latest('poll',now),now)
        comparison=compare(forecast,book,refs,strategy)
        return dict(base,status=forecast['status'],forecast=forecast,comparison=comparison,
                    cost_buffer_per_share=str(strategy['cost_buffer_per_share']))
    except (ValueError,KeyError,TypeError,sqlite3.Error,OSError,ArithmeticError) as error:
        return dict(base,status='unavailable',reason='research_input_or_quote_error',error_type=type(error).__name__)
    finally:
        if store is not None:store.close()


def report(runtime, config, paper=False, hours=24, now=None):
    from .leadership import load_events, future_quote
    from .maintenance import retention_info
    now=time.time() if now is None else now
    if isinstance(hours,bool) or not 0<float(hours)<=168:
        raise ValueError('Research lookback must be greater than zero and at most 168 hours')
    runtime=Path(runtime)
    result={'research_only':True,'model_version':VERSION,'calibrated':False,'as_of':now,
        'hours':hours,'forecasts':[],'coverage':{},'events':{},'markouts':{},
        'limitations':['One-share displayed-price scenarios; no fills or portfolio simulation.',
            'Polling coefficients and sensitivity bounds are uncalibrated; no proven edge.',
            'Repeated scans and races are correlated; counts are not independent trials.',
            'Observations require successful normal scans; feed failures/news/cooldowns can bias coverage.',
            'Future price markouts do not measure election-probability calibration or settlement profit.']}
    path=runtime/'fair-value.sqlite3'
    mappings={m['sig_exchange_id']:m for m in config['markets'] if m.get('enabled')}
    covered=set()
    if path.exists():
        store=PollStore(path,readonly=True)
        try:
            store.db.execute('BEGIN')
            polls=store.latest('poll',now)
            for race in store.latest('race',now):
                mapping=mappings.get(race['sig_exchange_id'])
                if not mapping:continue
                covered.add(race['sig_exchange_id'])
                if any(race[k]!=mapping.get(k) for k in ('contract_fingerprint','race_key')):
                    result['forecasts'].append({'exchange':race['sig_exchange_id'],'status':'unavailable','reason':'contract_binding_changed'})
                else:result['forecasts'].append(dict(estimate(race,polls,now),exchange=race['sig_exchange_id']))
        finally:store.close()
    result['coverage']={'enabled_contracts':len(mappings),'mapped_contracts':len(covered),
                        'unmapped_contracts':len(set(mappings)-covered)}
    journal=runtime/('paper.sqlite3' if paper else 'live.sqlite3')
    if not journal.exists():return result
    db=sqlite3.connect(journal.resolve().as_uri()+'?mode=ro',uri=True)
    try:
        db.execute('BEGIN')
        result['event_retention']=retention_info(db)
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='events'").fetchone():return result
        if not db.execute("SELECT 1 FROM events WHERE kind='fair_value_shadow' AND at>=? AND at<=? LIMIT 1",
                          (now-float(hours)*3600,now)).fetchone():return result
        quotes,_=load_events(db,now-float(hours)*3600,now)
        by_exchange=defaultdict(list)
        for q in quotes:by_exchange[q['exchange']].append(q)
        for rows in by_exchange.values():rows.sort(key=lambda r:r['at'])
        starts={r['id']:r for r in quotes}
        counters=Counter(); outcomes=defaultdict(list); statuses=defaultdict(Counter)
        seen=set()
        for at,body in db.execute("SELECT at,detail FROM events WHERE kind='fair_value_shadow' AND at>=? AND at<=? ORDER BY at,rowid",
                                  (now-float(hours)*3600,now)):
            try:
                event=json.loads(body)
                if event['version']!=1 or event['model_version']!=VERSION:continue
                sid=event['snapshot_id']
                if sid in seen:continue
                seen.add(sid);counters[event['status']]+=1
                comparison=event.get('comparison',{})
                candidates=[('model',r) for r in comparison.get('model',[]) if r['eligible']]
                market=comparison.get('market')
                if market:candidates.append(('market',dict(market,ask=market['price'])))
                start=starts.get(sid)
                if not start or event['exchange']!=start['exchange'] or not 0<=at-start['at']<=5:
                    counters['unmatched_snapshot']+=1;continue
                for route,candidate in candidates:
                    counters[route+'_candidates']+=1
                    side=candidate['side'];ask=D(candidate['ask']);fee=D(event['cost_buffer_per_share'])
                    expected=start['SIG']['ask'] if side=='yes' else 1-start['SIG']['bid']
                    if side not in ('yes','no') or ask!=expected:raise ValueError('Candidate quote mismatch')
                    rows=by_exchange[start['exchange']];stamps=[r['at'] for r in rows]
                    for horizon,window in ((300,120),(900,180),(3600,900)):
                        key=route+'_'+str(horizon)
                        future=future_quote(rows,stamps,start,horizon,window)
                        if future:
                            bid=future['SIG']['bid'] if side=='yes' else 1-future['SIG']['ask']
                            depth=future['SIG']['bid_size'] if side=='yes' else future['SIG']['ask_size']
                            if depth>=1:
                                outcomes[key].append(bid-ask-2*fee);statuses[key]['measured']+=1
                            else:statuses[key]['insufficient_exit_depth']+=1
                        else:statuses[key]['pending' if now<start['at']+horizon+window else 'missing']+=1
            except (ValueError,KeyError,TypeError,ArithmeticError):counters['malformed']+=1
        result['events']=dict(counters)
        for key,status in statuses.items():
            values=outcomes[key]
            result['markouts'][key]={'statuses':dict(status),'mean_net_change_per_share':str(sum(values,D(0))/len(values)) if values else None,
                                    'positive_fraction':sum(v>0 for v in values)/len(values) if values else None}
        return result
    finally:db.close()


def format_report(result):
    lines=['FAIR VALUE RESEARCH — uncalibrated, no model orders',str(result['coverage'])]
    for row in result['forecasts']:
        if row['status']=='estimated':
            lines.append(f"{row['exchange']}: YES {row['yes_probability']:.1%}; sensitivity {row['yes_low']:.1%}–{row['yes_high']:.1%}; {row['pollster_count']} pollsters")
        else:lines.append(f"{row['exchange']}: unavailable — {row['reason']}")
    lines.extend(['Recorded observations: '+str(result['events']), 'Forward one-share price scenarios: '+str(result['markouts']),
                  'Uncertainty range is a sensitivity scenario, not a confidence interval. Use --json for evidence and limitations.'])
    return '\n'.join(lines)
