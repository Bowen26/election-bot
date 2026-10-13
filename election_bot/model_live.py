"""Explicit opt-in fair-value entry route; accounting shares the order transaction."""
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import time

from .fair_value import estimate, VERSION as MODEL_VERSION
from .polling import PollStore, validate_race, validate_poll
from .strategy import D, Signal, choose

REASON = 'model_fair_value'
DEFAULTS = dict(enabled=False, acknowledge_uncalibrated=False,
    total='1000', daily='250', per_race='250', per_order='25',
    minimum_edge='.08', max_shares=50, depth_fraction='.25', max_sig_spread='.04',
    min_price='.10', max_price='.90', max_collector_age_seconds=7200)


def settings(config):
    raw=config.get('model_trading',{})
    if not isinstance(raw,dict) or set(raw)-set(DEFAULTS):
        raise ValueError('Invalid model_trading configuration')
    p=dict(DEFAULTS,**raw)
    for k in ('enabled','acknowledge_uncalibrated'):
        if type(p[k]) is not bool:raise ValueError('model_trading '+k+' must be boolean')
    for k in set(p)-{'enabled','acknowledge_uncalibrated'}:
        p[k]=D(p[k])
        if p[k]<=0:raise ValueError('model_trading '+k+' must be positive')
    if not 0 < p['min_price'] < p['max_price'] < 1:raise ValueError('Invalid model price range')
    if not D('.05') <= p['minimum_edge'] < 1:raise ValueError('Model edge must be at least five cents')
    if p['depth_fraction']>1 or p['max_sig_spread']>=1:raise ValueError('Invalid model depth/spread')
    if p['max_shares']!=int(p['max_shares']) or p['max_shares']>1000:raise ValueError('Invalid model share cap')
    if not 60<=p['max_collector_age_seconds']<=7200:raise ValueError('Model collector age must be 60–7200 seconds')
    if not p['per_order']<=p['per_race']<=p['total'] or p['daily']>p['total']:
        raise ValueError('Invalid nested model purchase caps')
    if p['enabled'] and (not p['acknowledge_uncalibrated'] or
            config.get('execution',{}).get('enabled') is not True or
            config.get('execution',{}).get('sell_enabled') is not True or
            config.get('research_polling',{}).get('enabled') is not True):
        raise ValueError('Live model requires explicit uncalibrated acknowledgement, active selling and polling collection')
    return p


def initialize(db):
    # Separate from event logs, which may be archived. Written with the reservation.
    db.execute('''CREATE TABLE IF NOT EXISTS model_orders (
        key TEXT PRIMARY KEY, race TEXT NOT NULL, detail TEXT NOT NULL)''')
    db.commit()


def allowance(db, p, race, now):
    day=datetime.fromtimestamp(now,timezone.utc).date().isoformat()
    totals={k:D(0) for k in ('total','daily','per_race')}
    for r in db.execute('''SELECT o.amount,o.day,m.race FROM model_orders m
                          JOIN orders o ON o.key=m.key'''):
        amount=D(r['amount'])
        if amount<0:raise ValueError('Invalid model reservation accounting')
        totals['total']+=amount
        if r['day']==day:totals['daily']+=amount
        if r['race']==race:totals['per_race']+=amount
    # Gross confirmed purchase cost plus unresolved reservations, not reusable capital.
    return max(D(0),min(p['per_order'],*(p[k]-totals[k] for k in totals))),totals


def forecast(runtime, mapping, config, now, p):
    """Fresh read-only evidence snapshot; no forecast cache authorizes an order."""
    status=json.loads((Path(runtime)/'poll-collector/status.json').read_text())
    last=D(status['last_success_at'])
    if status['status']!='ok' or not 0<=D(now)-last<=p['max_collector_age_seconds']:
        raise ValueError('model_collector_unhealthy_or_stale')
    store=PollStore(Path(runtime)/'fair-value.sqlite3',readonly=True)
    try:
        store.db.execute('BEGIN')
        latest=store.db.execute('SELECT MAX(received) FROM evidence').fetchone()[0]
        if latest is not None and latest>now:raise ValueError('model_evidence_clock_ahead')
        race=next((r for r in store.latest('race',now) if r['sig_exchange_id']==mapping['sig_exchange_id']),None)
        if race is None:raise ValueError('model_race_unmapped')
        if (mapping.get('office')!='senate' or mapping.get('exposure_mode')=='gross' or
                race['race_key']!=mapping.get('race_key')):
            raise ValueError('model_requires_reviewed_single_contract_senate')
        race=validate_race(race,{mapping['sig_exchange_id']:mapping})
        polls=[validate_poll(r) for r in store.latest('poll',now) if r['race_key']==race['race_key']]
        result=estimate(race,polls,now)
        if result['status']!='estimated':raise ValueError('model_'+result['reason'])
        return result
    finally:store.close()


def select(book, refs, strategy, execution, prediction, p, available, held, side_limits=None):
    # Keep venue health gates, but do not require an external price discount.
    choose(book,refs,strategy,0)
    if prediction.get('status')!='estimated' or prediction.get('version')!=MODEL_VERSION:
        raise ValueError('Invalid model forecast')
    low,mean,high=(D(prediction[k]) for k in ('yes_low','yes_probability','yes_high'))
    if not 0<=low<=mean<=high<=1:raise ValueError('Invalid model probability range')
    fee=D(strategy['cost_buffer_per_share']);candidates=[]
    threshold=max(p['minimum_edge'],D(strategy['minimum_edge']))
    for side,value,bound in [('yes',mean,low),('no',1-mean,1-high)]:
        if held and ((held<0)!=(side=='no')):continue
        target=book if side=='yes' else book.complement()
        aligned=refs if side=='yes' else [r.complement() for r in refs]
        bid,bid_size=target.bids[0];ask,ask_size=target.asks[0]
        if (not p['min_price']<=ask<=p['max_price'] or ask%D('.005') or
                ask-bid>p['max_sig_spread']):continue
        # Do not buy directly into the existing overpricing sell condition.
        if bid-max(r.asks[0][0] for r in aligned)-fee>=D(execution['exit_edge']):continue
        robust=bound-ask-2*fee
        if robust<threshold:continue
        q=int(min(D(available)/(ask+fee),p['max_shares'],D(strategy['max_shares_per_order']),
                  p['depth_fraction']*min(bid_size,ask_size),
                  D(side_limits[side]) if side_limits is not None else p['max_shares']))
        if q>0:candidates.append(Signal(side,ask,q,bound,robust,reason=REASON))
    return max(candidates,key=lambda s:s.edge) if candidates else None


def propose(runtime, mapping, config, db, book, refs, strategy, available, held, side_limits=None, now=None):
    now=time.time() if now is None else now
    p=settings(config)
    if not p['enabled']:return None,{'status':'disabled'}
    detail={'status':'blocked','model_version':MODEL_VERSION,'calibrated':False,'at':now}
    try:
        cap,used=allowance(db,p,mapping['race_key'],now)
        detail.update(used=used,available=min(D(available),cap))
        if cap<=0 or D(available)<=0:return None,dict(detail,reason='model_or_portfolio_budget')
        f=forecast(runtime,mapping,config,now,p)
        signal=select(book,refs,strategy,config['execution'],f,p,min(D(available),cap),held,side_limits)
        detail.update(forecast=f,contract_fingerprint=mapping['contract_fingerprint'],
                      settings={k:str(v) if not isinstance(v,bool) else v for k,v in p.items()},
                      status='eligible' if signal else 'blocked',reason='model_entry' if signal else 'model_price_depth_or_inventory')
        return signal,detail
    except (OSError,sqlite3.Error,ValueError,KeyError,TypeError,ArithmeticError) as error:
        # Missing/bad model inputs must never authorize a fallback model order.
        return None,dict(detail,reason='model_input_unavailable',error_type=type(error).__name__,error=str(error)[:200])


def attach(db, payload, race, detail):
    if not db.in_transaction:raise RuntimeError('Model attribution requires the reservation transaction')
    db.execute('INSERT INTO model_orders VALUES (?,?,?)',
               (payload['idempotencyKey'],race,json.dumps(detail,default=str,allow_nan=False)))
