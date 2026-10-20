"""Independent polling rotation trials: bounded capital, confirmation, sequential quotes.

No broker or execution ledger mutations. Trials compare equal starting source
inventory and must not be summed into a claimed whole-portfolio backtest.
"""
from collections import Counter
import hashlib
import json
from pathlib import Path
import sqlite3
import time
import uuid

from .strategy import D
from .rotation import RotationStudy, simulate_sale, HORIZONS, MAX_PAIR_DELAY
from .race_controls import configured_races, committed_by_race
from .risk import Exposure, LOSS_FIELD
from . import model_live

# Only stable allowlisted failure categories are persisted; arbitrary exception
# text can contain local paths and does not define a strategy gate.
MODEL_FAILURES={
    'model_collector_unhealthy_or_stale','model_evidence_clock_ahead',
    'model_race_unmapped','model_requires_reviewed_single_contract_senate',
    'model_race_not_known_at_prediction_time','model_election_started_or_passed',
    'model_prior_not_published','model_insufficient_independent_pollsters',
    'model_no_recent_poll'}


def model_failure(error):
    reason=str(error)
    if reason in MODEL_FAILURES:return reason
    if isinstance(error,FileNotFoundError):return 'model_evidence_file_missing'
    if isinstance(error,(OSError,sqlite3.Error)):return 'model_evidence_read_error'
    return 'model_validation_failed'


VERSION='independent_rotation_v1'
POLICY={'version':VERSION,'confirmation_count':3,'sample_spacing_seconds':60,
        'minimum_span_seconds':120,'max_sample_gap_seconds':180,'purchase_window_seconds':60,
        'max_model_age_seconds':15,'max_sig_age_seconds':15,'max_sig_spread':'.04',
        'valuation':'source upper sensitivity versus destination lower sensitivity',
        'execution':'simulated sale first; first new destination quote decides purchase or cash',
        'live_orders':False}


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,default=str,allow_nan=False).encode()).hexdigest()


def validate_forecast(f,mapping,now):
    if (f.get('version')!='poll_margin_v1' or f.get('status')!='estimated' or
            f.get('race_key')!=mapping['race_key'] or
            not 0<=now-f['as_of']<=POLICY['max_model_age_seconds']):
        raise ValueError('Unavailable, stale or mismatched model forecast')
    low,mean,high=(D(f[k]) for k in ('yes_low','yes_probability','yes_high'))
    if not 0<=low<=mean<=high<=1:raise ValueError('Invalid model probabilities')
    if not f.get('estimate_id') or not f.get('race_hash') or not f.get('weights'):
        raise ValueError('Missing model provenance')
    return f


def values(f,side):
    low,mean,high=(D(f[k]) for k in ('yes_low','yes_probability','yes_high'))
    return (low,mean,high) if side=='yes' else (1-high,1-mean,1-low)


def checked_book(book,side,config):
    book.check(min(POLICY['max_sig_age_seconds'],config['strategy']['max_age_seconds']),venue='SIG')
    target=book if side=='yes' else book.complement()
    if target.asks[0][0]-target.bids[0][0]>D(POLICY['max_sig_spread']):raise ValueError('Wide SIG spread')
    if any(p%D('.005') for p in (target.bids[0][0],target.asks[0][0])):raise ValueError('Invalid SIG tick')
    return target


class ModelRotationStudy:
    def __init__(self,ledger,config,runtime,forecast_provider=None):
        self.ledger,self.db,self.config,self.runtime=ledger,ledger.db,config,Path(runtime)
        self.base=RotationStudy(ledger,config)
        self.enabled=self.base.policy['enabled']
        self.cache={};self.epoch=None
        self.forecast_provider=forecast_provider or self.load_forecast
        if not self.enabled:return
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS model_rotation_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS model_rotation_confirmations(
            key TEXT PRIMARY KEY,source TEXT,target TEXT,detail TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS model_rotation_trials(
            id TEXT PRIMARY KEY,at REAL,source TEXT,target TEXT,status TEXT,detail TEXT NOT NULL);
          CREATE INDEX IF NOT EXISTS model_rotation_trials_at ON model_rotation_trials(at);
          CREATE TABLE IF NOT EXISTS model_rotation_outcomes(
            trial TEXT,horizon INTEGER,leg TEXT,at REAL,status TEXT,value TEXT,
            PRIMARY KEY(trial,horizon,leg));
          CREATE TABLE IF NOT EXISTS model_rotation_activity(reason TEXT PRIMARY KEY,count INTEGER,last_at REAL);
          CREATE TABLE IF NOT EXISTS model_rotation_rankings(id INTEGER PRIMARY KEY,at REAL,detail TEXT);
          CREATE TABLE IF NOT EXISTS model_rotation_diagnostics(
            id INTEGER PRIMARY KEY,at REAL NOT NULL,exchange TEXT NOT NULL,
            race TEXT NOT NULL,reason TEXT NOT NULL);
          CREATE INDEX IF NOT EXISTS model_rotation_diagnostics_at ON model_rotation_diagnostics(at);

        ''')
        frozen=json.dumps({'policy':POLICY,'sizing':self.base.policy},sort_keys=True,default=str)
        old=self.db.execute("SELECT value FROM model_rotation_meta WHERE key='policy'").fetchone()
        if old and old[0]!=frozen:raise ValueError('Model rotation policy changed; separate experiment required')
        with self.db:self.db.execute("INSERT OR IGNORE INTO model_rotation_meta VALUES ('policy',?)",(frozen,))
        saved=self.db.execute("SELECT value FROM model_rotation_meta WHERE key='epoch'").fetchone()
        self.epoch=json.loads(saved[0]) if saved else None
        self.last_diagnostic_cleanup=None

    def diagnose(self,mapping,reason,now):
        # Exact timestamps support the requested rolling window. Prune hourly;
        # reports never migrate or write to old journals.
        with self.db:
            self.db.execute('INSERT INTO model_rotation_diagnostics(at,exchange,race,reason) VALUES (?,?,?,?)',
                (now,mapping['sig_exchange_id'],mapping.get('race_key','unknown'),reason))
            if self.last_diagnostic_cleanup is None or now-self.last_diagnostic_cleanup>=3600:
                self.db.execute('DELETE FROM model_rotation_diagnostics WHERE at<?',(now-7*86400,))
                self.last_diagnostic_cleanup=now

    def load_forecast(self,mapping,now):
        # Same evidence and collector validation as live model, but no pilot budget or order route.
        return model_live.forecast(self.runtime,mapping,self.config,now,model_live.settings(self.config))

    def note(self,reason,now):
        with self.db:self.db.execute('INSERT INTO model_rotation_activity VALUES (?,1,?) ON CONFLICT(reason) DO UPDATE SET count=count+1,last_at=excluded.last_at',(reason,now))

    def epoch_now(self):
        return self.db.execute('SELECT MAX(rowid) FROM executions').fetchone()[0]

    def clear_confirmations(self,exchange=None):
        with self.db:
            if exchange is None:self.db.execute('DELETE FROM model_rotation_confirmations')
            else:self.db.execute('DELETE FROM model_rotation_confirmations WHERE source=? OR target=?',(exchange,exchange))

    def provider(self,item,side,role):
        book=checked_book(item['book'],side,self.config)
        f=validate_forecast(item['forecast'],item['mapping'],time.time())
        low,mean,high=values(f,side)
        return book, high if role=='hold' else low

    def busy_races(self,now):
        races=configured_races(self.config);busy=set()
        # No overlapping allocation of a source race or destination during a trial's longest outcome window.
        hold=max(max(h+w for h,w in HORIZONS.items()),self.base.policy['cooldown_seconds'])
        for row in self.db.execute('SELECT source,target FROM model_rotation_trials WHERE at>?',(now-hold,)):
            busy.update(races[ex] for ex in row if ex in races)
        return busy

    def simulated_daily_used(self,now,exclude=None):
        from datetime import datetime,timezone
        day=datetime.fromtimestamp(now,timezone.utc).date()
        used=D(0)
        for row in self.db.execute('SELECT id,at,status,detail FROM model_rotation_trials'):
            if row['id']==exclude:continue
            d=json.loads(row['detail'])
            if d['purchase']:
                if datetime.fromtimestamp(d['purchase']['at'],timezone.utc).date()==day:
                    used+=D(d['purchase']['cost'])
            elif row['status']=='awaiting_purchase' and now<=d['purchase_deadline']:
                used+=D(d['purchase_cost'])
        return used

    def constrain_trial(self,d,now):
        # Do not let simultaneous paper rotations spend the same daily allowance or
        # reinvest sale profits above the released source cost basis.
        fee=D(d['buffer']);ask=D(d['target']['ask']);proceeds=D(d['sale_proceeds'])
        budget=min(proceeds,D(d['fifo_basis']),D(self.config['limits']['daily'])-
                   self.ledger.journal.used(today=True)-self.simulated_daily_used(now))
        q=min(int(d['target']['quantity']),int(max(D(0),budget)/(ask+fee)))
        if q<1:return None
        spend=q*(ask+fee);residual=proceeds-spend
        improvement=q*(D(d['target']['proxy'])-fee)+residual-D(d['source']['quantity'])*(D(d['source']['proxy'])-fee)
        if improvement<proceeds*self.base.policy['minimum_improvement_fraction']:return None
        d['target']['quantity']=q
        d.update(purchase_cost=spend,residual_cash=residual,proxy_improvement_coins=improvement,
                 proxy_improvement_fraction=improvement/proceeds,
                 currently_fundable_without_sale=spend<=D(d['target_available']),
                 simulated_daily_used=self.simulated_daily_used(now))
        return d

    def observe(self,mapping,book,snapshot_id,buy_allowed,available,cash,blocked=lambda m:False,now=None):
        if not self.enabled:return
        now=time.time() if now is None else now
        ex=mapping['sig_exchange_id'];epoch=self.epoch_now()
        self.diagnose(mapping,'observed',now)
        self.cache.pop(ex,None)
        if self.epoch!=epoch:
            self.cache.clear();self.clear_confirmations();self.epoch=epoch
            with self.db:self.db.execute("INSERT INTO model_rotation_meta VALUES ('epoch',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(json.dumps(epoch),))
        # Finish purchase handling even when new entries become blocked; proceeds stay cash in that case.
        self.advance_pending(mapping,book,snapshot_id,buy_allowed,blocked,cash,now)
        pending=self.ledger.journal.pending()
        if pending or not buy_allowed:
            self.diagnose(mapping,'pending_order' if pending else 'buy_pause',now)
            self.cache.clear();self.clear_confirmations();self.note('pending_order_or_buy_pause',now);return
        gate=('mapping_disabled' if not mapping.get('enabled') else
              'gross_contract_unsupported' if mapping.get('exposure_mode')=='gross' else
              'news_or_race_cooldown' if blocked(mapping) else None)
        if gate:
            self.diagnose(mapping,gate,now)
            self.clear_confirmations(ex);self.note('mapping_or_race_gate',now);return
        try:
            checked_book(book,'yes',self.config)
        except (ValueError,KeyError,TypeError,ArithmeticError) as error:
            category={'Wide SIG spread':'sig_spread','Invalid SIG tick':'sig_tick',
                      'Stale or future-dated book':'sig_stale_or_future'}.get(str(error),'sig_book_invalid')
            self.diagnose(mapping,category,now)
            self.clear_confirmations(ex);self.note('model_or_SIG_unavailable',now);return
        try:
            f=validate_forecast(self.forecast_provider(mapping,now),mapping,now)
        except (ValueError,KeyError,TypeError,OSError,sqlite3.Error,ArithmeticError) as error:
            self.diagnose(mapping,model_failure(error),now)
            self.clear_confirmations(ex);self.note('model_or_SIG_unavailable',now);return
        self.diagnose(mapping,'model_and_quote_ready',now)
        self.cache[ex]={'mapping':dict(mapping),'book':book,'forecast':f,'snapshot':snapshot_id,
                        'available':D(available),'cash':D(cash),'at':now}
        for key,item in list(self.cache.items()):
            try:
                if blocked(item['mapping']):
                    self.clear_confirmations(key)
                    raise ValueError('blocked')
                if not 0<=now-item['at']<=POLICY['max_model_age_seconds']:raise ValueError('stale')
                self.provider(item,'yes','buy')
            except (ValueError,KeyError,TypeError,ArithmeticError):self.cache.pop(key,None)
        # Candidates include ALL fresh owned/unowned combinations, not only the last scanned race.
        races=configured_races(self.config);busy=self.busy_races(now);candidates=[];held_rows=[];buy_rows=[]
        fee=D(self.config['strategy']['cost_buffer_per_share'])
        for key,item in self.cache.items():
            held,_=self.ledger.held(key)
            for side in (('yes' if held>0 else 'no',) if held else ('yes','no')):
                lower,mean,upper=values(item['forecast'],side);target=checked_book(item['book'],side,self.config)
                price=target.bids[0][0] if held else target.asks[0][0]
                row={'exchange':key,'race':races[key],'side':side,'held':str(abs(held)),
                     'price':str(price),'lower':str(lower),'point':str(mean),'upper':str(upper),
                     'remaining_value_over_sale':str(upper-price) if held else None,
                     'conservative_edge_after_buffers':str(lower-price-2*fee) if not held else None,
                     'edge_per_purchase_coin':str((lower-price-2*fee)/(price+fee)) if not held else None,
                     'snapshot_id':item['snapshot'],'estimate_id':item['forecast']['estimate_id']}
                (held_rows if held else buy_rows).append(row)
            if not held or races[key] in busy:continue
            for dest,dst in self.cache.items():
                if races[key]==races[dest] or races[dest] in busy:continue
                if any(self.ledger.held(k)[0] for k in races if races[k]==races[dest]):continue
                for side in ('yes','no'):
                    proposal=self.base.compare(key,item,dest,dst,held,side,races,self.provider)
                    if proposal:
                        proposal['target_available']=dst['available']
                        proposal=self.constrain_trial(proposal,now)
                    if proposal:
                        proposal['source']['mapping']=item['mapping'];proposal['target']['mapping']=dst['mapping']
                        proposal['source']['forecast']=item['forecast'];proposal['target']['forecast']=dst['forecast']
                        proposal['source']['quantity']=int(proposal['source']['quantity'])
                        proposal['execution_epoch']=epoch
                        candidates.append(proposal)
        candidates.sort(key=lambda d:(-D(d['proxy_improvement_coins']),d['source']['exchange'],d['target']['exchange'],d['target']['side']))
        valid_keys=set();confirmed=[]
        for d in candidates:
            src,dst=d['source'],d['target'];key=':'.join((src['exchange'],src['side'],dst['exchange'],dst['side']))
            valid_keys.add(key)
            old=self.db.execute('SELECT detail FROM model_rotation_confirmations WHERE key=?',(key,)).fetchone()
            previous=json.loads(old[0]) if old else None
            # Ignore as_of/estimate IDs, which evolve every scan; revisions to evidence reset confirmation.
            # Weights include age-dependent values, so use evidence hashes rather than changing numerical weights.
            signature=digest([epoch,*[(leg['mapping']['contract_fingerprint'],leg['forecast']['race_hash'],
                                      sorted(w['hash'] for w in leg['forecast']['weights']),leg['forecast'].get('policy')) for leg in (src,dst)]])
            reset=not previous or previous['signature']!=signature or not 0<=now-previous['last_at']<=POLICY['max_sample_gap_seconds']
            state={'signature':signature,'first_at':now,'last_at':now,'count':1,
                   'source_snapshot':src['snapshot_id'],'target_snapshot':dst['snapshot_id']} if reset else previous
            if not reset and now-previous['last_at']>=POLICY['sample_spacing_seconds'] and all(
                state[leg+'_snapshot']!=d[leg]['snapshot_id'] for leg in ('source','target')):
                state=dict(state,last_at=now,count=state['count']+1,source_snapshot=src['snapshot_id'],target_snapshot=dst['snapshot_id'])
            with self.db:self.db.execute('INSERT OR REPLACE INTO model_rotation_confirmations VALUES (?,?,?,?)',(key,src['exchange'],dst['exchange'],json.dumps(state)))
            d['confirmation']=state
            if state['count']>=POLICY['confirmation_count'] and state['last_at']-state['first_at']>=POLICY['minimum_span_seconds']:
                confirmed.append(d)
        # A newly observed reversal invalidates confirmation; missing other books only ages it out.
        for row in self.db.execute('SELECT key,source,target FROM model_rotation_confirmations').fetchall():
            if row['source'] in self.cache and row['target'] in self.cache and row['key'] not in valid_keys:
                with self.db:self.db.execute('DELETE FROM model_rotation_confirmations WHERE key=?',(row['key'],))
        ranking={'simulation_only':True,'at':now,'fresh_contracts':len(self.cache),
                 'owned':sorted(held_rows,key=lambda r:D(r['remaining_value_over_sale'])),
                 'buys':sorted(buy_rows,key=lambda r:-D(r['edge_per_purchase_coin'])),
                 'switches':candidates[:10],'confirmed_switches':len(confirmed),
                 'ranking_scope':'All currently fresh model-covered contracts; not every configured market.'}
        with self.db:self.db.execute('INSERT OR REPLACE INTO model_rotation_rankings VALUES (1,?,?)',(now,json.dumps(ranking,default=str)))
        if not confirmed:
            reason=('awaiting_confirmation' if candidates else
                    'no_fresh_owned_source' if not held_rows else
                    'no_fresh_unowned_destination' if not buy_rows else
                    'no_qualifying_pair_after_limits_and_edge')
            self.diagnose(mapping,reason,now)
            self.note('awaiting_confirmation' if candidates else 'no_qualifying_model_rotation',now);return
        best=confirmed[0];pid=str(uuid.uuid4())
        best.update(id=pid,at=now,experiment=VERSION,simulation_only=True,
                    status='awaiting_purchase',purchase_deadline=now+POLICY['purchase_window_seconds'],
                    simulated_cash=str(best['sale_proceeds']),purchase=None,
                    valuation_basis=POLICY['valuation'],
                    direct_buy_baseline={'version':'spare_cash_v1',
                        'initial_cash':str(self.cache[best['target']['exchange']]['cash'])})
        with self.db:
            self.db.execute('INSERT INTO model_rotation_trials VALUES (?,?,?,?,?,?)',(pid,now,best['source']['exchange'],best['target']['exchange'],best['status'],json.dumps(best,default=str)))
            self.db.execute('DELETE FROM model_rotation_confirmations WHERE source IN (?,?) OR target IN (?,?)',(best['source']['exchange'],best['target']['exchange'])*2)
        self.diagnose(mapping,'trial_created',now)
        self.note('simulated_sale_waiting_for_fresh_purchase',now)

    def direct_buy_baseline(self,d,purchase,cash,now,trial_id):
        """Same target fill without selling source; prospective, paired research only.

        Called only after shared quote, forecast, gate and epoch validation. Keep
        the identical destination quantity; never resize to make this arm pass.
        """
        baseline=d.get('direct_buy_baseline')
        if not baseline:return None  # Never backfill historical trials.
        result=dict(baseline,eligible=False,reason='no_rotation_purchase')
        if not purchase:return result
        cost=D(purchase['cost']);quantity=D(purchase['quantity'])
        holdings,realized=self.ledger.inventory()
        loss=max(D(0),-sum(realized.values(),D(0)))
        committed=sum((v['cost'] for v in holdings.values()),D(0))+loss
        limits=self.config['limits'];ex=d['target']['exchange']
        race_cost=committed_by_race(holdings,realized,[],configured_races(self.config))[d['target']['race']]
        exposure=Exposure(self.ledger,self.config)
        budgets={'spare_cash':min(D(baseline['initial_cash']),D(cash)),
                 'total_capital':D(limits['total'])-committed,
                 'race_capital':D(limits['per_market'])-race_cost,
                 'daily':D(limits['daily'])-self.ledger.journal.used(today=True)-self.simulated_daily_used(now,exclude=trial_id),
                 'per_order':D(limits['per_order'])}
        reasons=[name for name,amount in budgets.items() if cost>amount]
        if exposure.enabled and quantity>exposure.headroom(ex,'buy',d['target']['side']):reasons.append('directional_exposure')
        if LOSS_FIELD in limits and loss>=D(limits[LOSS_FIELD])*D(limits['total']):reasons.append('realized_loss_stop')
        result.update(eligible=not reasons,reason=','.join(reasons) if reasons else 'same_purchase_fundable_without_sale',
                      checked_at=now,quantity=int(quantity),cost=str(cost),
                      budget_checks={k:str(v) for k,v in budgets.items()})
        return result

    def advance_pending(self,mapping,book,snapshot_id,buy_allowed,blocked,cash,now):
        rows=self.db.execute("SELECT * FROM model_rotation_trials WHERE status='awaiting_purchase'").fetchall()
        for row in rows:
            d=json.loads(row['detail']);reason=None;purchase=None
            if now>d['purchase_deadline']:reason='purchase_window_expired'
            elif mapping['sig_exchange_id']!=row['target']:continue
            elif now<=row['at'] or snapshot_id==d['target']['snapshot_id'] or min(book.source_at,book.observed_at)<=row['at']:continue
            else:
                try:
                    if self.epoch_now()!=d['execution_epoch']:raise ValueError('live_inventory_changed')
                    if self.ledger.journal.pending() or not buy_allowed:raise ValueError('buy_gate_or_pending')
                    if any(blocked(leg['mapping']) for leg in (d['source'],d['target'])):raise ValueError('race_gate')
                    if mapping.get('contract_fingerprint')!=d['target']['mapping']['contract_fingerprint'] or not mapping.get('enabled'):raise ValueError('binding_changed')
                    for leg in (d['source'],d['target']):
                        current=next((m for m in self.config['markets'] if m['sig_exchange_id']==leg['exchange']),None)
                        if not current or not current.get('enabled') or current.get('contract_fingerprint')!=leg['mapping']['contract_fingerprint']:raise ValueError('binding_changed')
                    target=checked_book(book,d['target']['side'],self.config)
                    f=validate_forecast(self.forecast_provider(mapping,now),mapping,now)
                    sf=validate_forecast(self.forecast_provider(d['source']['mapping'],now),d['source']['mapping'],now)
                    low=values(f,d['target']['side'])[0];high=values(sf,d['source']['side'])[2]
                    fee=D(d['buffer']);ask,depth=target.asks[0];proceeds=D(d['sale_proceeds'])
                    if low-ask-2*fee<D(self.config['strategy']['minimum_edge']):raise ValueError('destination_edge_disappeared')
                    limits=self.config['limits']
                    simulated,_,_=simulate_sale(self.ledger,d['source']['exchange'],d['source']['side'],D(d['source']['quantity']),D(d['source']['bid']),fee)
                    exposure=Exposure(simulated,self.config)
                    cap=exposure.headroom(row['target'],'buy',d['target']['side']) if exposure.enabled else D(self.config['strategy']['max_shares_per_order'])
                    holdings,realized=simulated.inventory()
                    net_loss=max(D(0),-sum(realized.values(),D(0)))
                    if LOSS_FIELD in limits and net_loss>=D(limits[LOSS_FIELD])*D(limits['total']):raise ValueError('realized_loss_stop')
                    committed=sum((v['cost'] for v in holdings.values()),D(0))+net_loss
                    race_cost=committed_by_race(holdings,realized,[],configured_races(self.config))[d['target']['race']]
                    if fee!=D(self.config['strategy']['cost_buffer_per_share']):raise ValueError('cost_buffer_changed')
                    budget=min(proceeds,D(d['fifo_basis']),max(D(0),D(cash)+proceeds),D(limits['per_order']),
                        D(limits['daily'])-self.ledger.journal.used(today=True)-self.simulated_daily_used(now,exclude=row['id']),
                        D(limits['total'])-committed,D(limits['per_market'])-race_cost)
                    q=int(min(max(D(0),budget)/(ask+fee),depth*self.base.policy['depth_fraction'],target.bids[0][1]*self.base.policy['depth_fraction'],D(self.config['strategy']['max_shares_per_order']),cap))
                    if q<1:raise ValueError('purchase_cash_depth_or_exposure')
                    residual=proceeds-q*(ask+fee)
                    improvement=q*(low-fee)+residual-D(d['source']['quantity'])*(high-fee)
                    if improvement<proceeds*self.base.policy['minimum_improvement_fraction']:raise ValueError('rotation_improvement_disappeared')
                    purchase={'at':now,'snapshot_id':snapshot_id,'quantity':q,'ask':str(ask),'cost':str(q*(ask+fee)),
                              'forecast':f,'source_forecast':sf,'conservative_improvement':str(improvement)}
                    d['simulated_cash']=str(residual)
                except (ValueError,KeyError,TypeError,OSError,sqlite3.Error,ArithmeticError) as error:reason=str(error)
            if 'direct_buy_baseline' in d:
                try:d['direct_buy_baseline']=self.direct_buy_baseline(d,purchase,cash,now,row['id'])
                except (ValueError,KeyError,TypeError,sqlite3.Error,ArithmeticError):
                    d['direct_buy_baseline']=dict(d['direct_buy_baseline'],eligible=False,reason='validation_unavailable')
            d.update(status='purchased' if purchase else 'cash_only',purchase=purchase,purchase_skip_reason=reason)
            with self.db:self.db.execute('UPDATE model_rotation_trials SET status=?,detail=? WHERE id=?',(d['status'],json.dumps(d,default=str),row['id']))
            self.note('simulated_purchase' if purchase else 'sale_proceeds_kept_as_cash',now)

    def observe_outcomes(self,mapping,book,now=None):
        if not self.enabled:return
        now=time.time() if now is None else now;ex=mapping['sig_exchange_id']
        book.check(min(15,self.config['strategy']['max_age_seconds']),venue='SIG')
        rows=self.db.execute('SELECT * FROM model_rotation_trials WHERE at>=? AND at<=? AND (source=? OR target=?)',(now-max(h+w for h,w in HORIZONS.items()),now-min(HORIZONS),ex,ex)).fetchall()
        with self.db:
            for row in rows:
                d=json.loads(row['detail']);leg='source' if row['source']==ex else 'target'
                if leg=='target' and not d['purchase']:continue
                if mapping.get('contract_fingerprint')!=d[leg]['mapping']['contract_fingerprint']:continue
                quantity=D(d['source']['quantity'] if leg=='source' else d['purchase']['quantity'])
                target=book if d[leg]['side']=='yes' else book.complement();bid,depth=target.bids[0]
                for horizon,window in HORIZONS.items():
                    due=row['at']+horizon
                    if not due<=now<=due+window or min(book.source_at,book.observed_at)<due:continue
                    if self.db.execute('SELECT 1 FROM model_rotation_outcomes WHERE trial=? AND horizon=? AND leg=?',(row['id'],horizon,leg)).fetchone():continue
                    status='measured' if depth*self.base.policy['depth_fraction']>=quantity and not bid%D('.005') else 'insufficient_depth_or_tick'
                    value=str(quantity*(bid-D(d['buffer']))) if status=='measured' else None
                    self.db.execute('INSERT INTO model_rotation_outcomes VALUES (?,?,?,?,?,?)',(row['id'],horizon,leg,now,status,value))


def allocation_attribution(samples):
    """Paired terminal-wealth comparisons using the same measured observations.

    Cash is the net proceeds of the already simulated sale, with no interest.
    The decomposition isolates the exit decision from destination selection.
    """
    groups={'all':samples,
            'purchased':[s for s in samples if s['purchased']],
            'cash_only':[s for s in samples if not s['purchased']],
            'cash_budget_fundable':[s for s in samples if s['fundable']],
            'not_cash_budget_fundable':[s for s in samples if not s['fundable']]}
    result={}
    for name,rows in groups.items():
        best=Counter();exit_deltas=[];replacement_deltas=[];total_deltas=[]
        for sample in rows:
            hold,cash,rotate=(D(sample[k]) for k in ('hold','cash','rotate'))
            exit_deltas.append(cash-hold)
            replacement_deltas.append(rotate-cash)
            total_deltas.append(rotate-hold)
            choices={'hold':hold,'cash':cash,'rotation_policy':rotate}
            winners=sorted(k for k,v in choices.items() if v==max(choices.values()))
            best['+'.join(winners)]+=1
        def mean(values):return str(sum(values,D(0))/len(values)) if values else None
        result[name]={'measured':len(rows),
            'mean_exit_vs_hold_coins':mean(exit_deltas),
            'mean_reinvestment_vs_cash_coins':mean(replacement_deltas),
            'mean_rotation_vs_hold_coins':mean(total_deltas),
            'best_terminal_value_counts':dict(best)}
    return result


def direct_buy_attribution(samples):
    """Compare equal initial inventory PLUS spare cash on complete paired marks."""
    rows=[]
    for sample in samples:
        baseline=sample.get('direct_buy_baseline') or {}
        if not baseline.get('eligible') or not sample['purchased']:continue
        cash=D(baseline['initial_cash']);cost=D(baseline['cost'])
        hold=D(sample['hold']);proceeds=D(sample['cash'])
        # Rotate = proceeds - cost + target liquidation. The direct arm keeps
        # the source and pays the SAME cost from its initial cash instead.
        target=D(sample['rotate'])-proceeds+cost
        choices={'hold_and_cash':hold+cash,
                 'sell_and_cash':proceeds+cash,
                 'rotate_and_cash':D(sample['rotate'])+cash,
                 'hold_and_direct_buy':hold+cash-cost+target}
        rows.append(choices)
    def mean_delta(left,right):
        return str(sum((r[left]-r[right] for r in rows),D(0))/len(rows)) if rows else None
    best=Counter('+'.join(sorted(k for k,v in r.items() if v==max(r.values()))) for r in rows)
    return {'measured':len(rows),
            'mean_direct_buy_vs_hold_and_cash_coins':mean_delta('hold_and_direct_buy','hold_and_cash'),
            'mean_direct_buy_vs_rotation_coins':mean_delta('hold_and_direct_buy','rotate_and_cash'),
            'best_terminal_value_counts':dict(best)}


def diagnostic_report(db,since,now):
    result={'initialized':False,'since':since,'until':now,'observations':0,
            'counts':{},'by_race':[],
            'scope':'Prospective study observations reaching the model-rotation hook, not all scans or rejected orders. '
                    'Counts overlap: observed and ready are stages; only the first blocking reason is recorded. '
                    'Pair reasons describe the fresh cache at that observation, not just the named race. '
                    'Coverage before this diagnostic was installed is unavailable.'}
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='model_rotation_diagnostics'").fetchone():return result
    result['initialized']=True
    counts=Counter();races={}
    for row in db.execute('SELECT exchange,race,reason,COUNT(*) AS n FROM model_rotation_diagnostics WHERE at>=? AND at<=? GROUP BY exchange,race,reason',(since,now)):
        counts[row['reason']]+=row['n']
        races.setdefault((row['exchange'],row['race']),{})[row['reason']]=row['n']
    result['counts']=dict(counts);result['observations']=counts['observed']
    result['by_race']=[{'exchange':ex,'race':race,'counts':v} for (ex,race),v in sorted(races.items())]
    stamp=db.execute('SELECT MIN(at),MAX(at) FROM model_rotation_diagnostics WHERE at>=? AND at<=?',(since,now)).fetchone()
    result['first_observation_at'],result['last_observation_at']=stamp
    return result


def report(path,hours=24,now=None):
    now=time.time() if now is None else now;hours=float(hours)
    if not 0<hours<=168:raise ValueError('Model rotation lookback must be between 0 and 168 hours')
    result={'initialized':False,'simulation_only':True,'experiment':VERSION,'trials':0,'outcomes':{},'recent':[],
            'limitations':['Forecasts are uncalibrated election-settlement estimates, not price-move timing predictions.',
                'Each trial starts with equal source inventory; purchase uses only simulated net sale proceeds.',
                'No overlapping trial races during outcome windows; cash/exposure limits checked, but trials are not a continuous portfolio replay.',
                'Displayed partial-position fills are assumed; real queue, latency, partial fills and exchange rejection are not modeled.',
                'Means are paired comparisons, not additive portfolio profit. No live rotations or automatic promotion.',
                'Prospectively eligible direct-buy arms retain the source and use spare cash for the same target fill; conditional on successful rotation purchase, not a standalone strategy or portfolio replay.',
                'Cash baseline earns no interest. Attribution uses identical complete observations; cohorts overlap and are not additive. Best outcomes are hindsight comparisons, not trading signals.']}
    path=Path(path)
    if not path.exists():return result
    db=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True);db.row_factory=sqlite3.Row
    try:
        db.execute('BEGIN')
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='model_rotation_trials'").fetchone():return result
        result['initialized']=True
        result['opportunity_diagnostics']=diagnostic_report(db,now-hours*3600,now)
        rows=db.execute('SELECT * FROM model_rotation_trials WHERE at>=? AND at<=? ORDER BY at',(now-hours*3600,now)).fetchall()
        result['trials']=len(rows)
        result['cash_budget_fundable_without_sale']=sum(bool(json.loads(r['detail'])['currently_fundable_without_sale']) for r in rows)
        result['activity']=[dict(r) for r in db.execute('SELECT * FROM model_rotation_activity ORDER BY last_at DESC')]
        latest=db.execute('SELECT at,detail FROM model_rotation_rankings WHERE id=1').fetchone()
        result['latest_ranking']=json.loads(latest['detail']) if latest else None
        result['ranking_age_seconds']=now-latest['at'] if latest else None
        result['confirmations_pending']=db.execute('SELECT COUNT(*) FROM model_rotation_confirmations').fetchone()[0]
        statuses=Counter()
        for row in rows:
            d=json.loads(row['detail']);statuses['cash_only_expired' if row['status']=='awaiting_purchase' and now>d['purchase_deadline'] else row['status']]+=1
        result['purchase_statuses']=dict(statuses)
        result['direct_buy_eligibility']=dict(Counter(
            (json.loads(row['detail']).get('direct_buy_baseline') or {}).get('reason','not_collected_or_pending') for row in rows))
        for horizon,window in HORIZONS.items():
            counts=Counter();deltas=[];samples=[]
            for row in rows:
                d=json.loads(row['detail']);legs={r['leg']:r for r in db.execute('SELECT * FROM model_rotation_outcomes WHERE trial=? AND horizon=? AND at<=?',(row['id'],horizon,now))}
                required={'source','target'} if d['purchase'] else {'source'}
                if now<row['at']+horizon:counts['not_due']+=1;continue
                if not required<=set(legs):counts['missing' if now>row['at']+horizon+window else 'pending']+=1;continue
                if any(legs[l]['status']!='measured' for l in required):counts['insufficient_depth_or_tick']+=1;continue
                if len(required)==2 and abs(legs['source']['at']-legs['target']['at'])>MAX_PAIR_DELAY:counts['unaligned']+=1;continue
                value=D(d['simulated_cash'])+(D(legs['target']['value']) if d['purchase'] else 0)
                samples.append({'hold':legs['source']['value'],'cash':d['sale_proceeds'],'rotate':value,
                    'purchased':bool(d['purchase']),'fundable':bool(d['currently_fundable_without_sale']),
                    'direct_buy_baseline':d.get('direct_buy_baseline')})
                delta=value-D(legs['source']['value']);deltas.append(delta);counts['measured']+=1
                counts['rotation_better' if delta>0 else 'holding_better' if delta<0 else 'equal']+=1
            result['outcomes'][str(horizon)]={**counts,'mean_incremental_coins':str(sum(deltas)/len(deltas)) if deltas else None,
                'allocation_attribution':allocation_attribution(samples),
                'direct_buy_attribution':direct_buy_attribution(samples)}
        result['recent']=[json.loads(r['detail']) for r in rows[-10:]]
        return result
    finally:db.close()


def format_report(r):
    lines=['Independent-model rotation: '+('collecting' if r['initialized'] else 'not initialized; restart worker to collect')]
    if r['initialized']:
        lines.append(f"  Trials: {r['trials']}; purchase states: {r['purchase_statuses']}; confirmations pending: {r['confirmations_pending']}")
        for horizon,value in r['outcomes'].items():lines.append(f"  {int(horizon)//60} min: {json.dumps(value)}")
        diagnostics=r.get('opportunity_diagnostics',{})
        if diagnostics.get('initialized'):
            lines.append(f"  Opportunity observations in window: {diagnostics['observations']}; counts: {json.dumps(diagnostics['counts'])}")
        else:lines.append('  Opportunity diagnostics unavailable for legacy collection; restart worker to collect.')
        ranking=r['latest_ranking']
        if ranking:lines.append(f"  Latest ranking age {r['ranking_age_seconds']:.0f}s; fresh contracts at capture {ranking['fresh_contracts']}; qualifying switches {len(ranking['switches'])}")
    lines.append('Model trials use simulated sale proceeds and a later purchase quote; no live rotations or guaranteed returns.')
    return '\n'.join(lines)
