"""Partial risk-reduction sales confirmed from SIG-only loss observations."""
import hashlib
import json
import time
from .clients import fingerprint,sig_contract_record
from .strategy import D,Signal

REASON='risk_reduction_exit'
DEFAULTS=dict(enabled=False,loss_per_share='.05',partial_fraction='.25',
    cooldown_seconds=1800,window_seconds=1800,sample_spacing_seconds=60,
    max_sample_gap_seconds=600,minimum_span_seconds=120)


def settings(config):
    raw=config.get('risk_exit',{})
    if not isinstance(raw,dict) or set(raw)-set(DEFAULTS):raise ValueError('Invalid risk_exit configuration')
    p=dict(DEFAULTS,**raw)
    if type(p['enabled']) is not bool:raise ValueError('Risk exit enabled must be boolean')
    for k in ('loss_per_share','partial_fraction'):p[k]=D(p[k])
    if not D('.01')<=p['loss_per_share']<1 or not 0<p['partial_fraction']<=D('.25'):raise ValueError('Invalid risk exit loss/fraction')
    for k,low,high in [('cooldown_seconds',300,86400),('window_seconds',180,7200),('sample_spacing_seconds',30,300),('max_sample_gap_seconds',60,1800),('minimum_span_seconds',60,3600)]:
        if type(p[k]) is not int or not low<=p[k]<=high:raise ValueError('Invalid risk exit '+k)
    if not p['sample_spacing_seconds']<=p['max_sample_gap_seconds']<=p['window_seconds'] or max(2*p['sample_spacing_seconds'],p['minimum_span_seconds'])>p['window_seconds']:raise ValueError('Invalid risk exit timing relationships')
    if p['enabled'] and (config.get('execution',{}).get('enabled') is not True or config['execution'].get('sell_enabled') is not True):raise ValueError('Risk exits require active execution and selling')
    return p


class RiskExit:
    def __init__(self,ledger,config):
        self.ledger=ledger;self.db=ledger.db;self.config=config;self.policy=settings(config)
        if not self.policy['enabled']:return
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS risk_exit_contracts (
            exchange TEXT PRIMARY KEY, full_pin TEXT NOT NULL, sig_hash TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS risk_exit_samples (
            exchange TEXT NOT NULL, epoch INTEGER NOT NULL, policy TEXT NOT NULL,
            at REAL NOT NULL, bid TEXT NOT NULL, depth TEXT NOT NULL,
            PRIMARY KEY(exchange,epoch,policy,at));
          CREATE TABLE IF NOT EXISTS risk_exit_orders (
            key TEXT PRIMARY KEY, exchange TEXT NOT NULL, detail TEXT NOT NULL);
        ''')

    def bind(self,mapping,record):
        if not self.policy['enabled']:return
        if fingerprint(record)!=mapping['contract_fingerprint']:raise ValueError('Unverified risk exit contract')
        with self.db:self.db.execute('INSERT OR REPLACE INTO risk_exit_contracts VALUES (?,?,?)',
            (mapping['sig_exchange_id'],mapping['contract_fingerprint'],fingerprint(record['sig'])))

    def bound(self,mapping):
        if not self.policy['enabled']:return False
        row=self.db.execute('SELECT * FROM risk_exit_contracts WHERE exchange=?',(mapping['sig_exchange_id'],)).fetchone()
        return row is not None and row['full_pin']==mapping['contract_fingerprint']

    def verify_market(self,mapping,market):
        if not self.bound(mapping):raise ValueError('Risk exit requires a previously verified contract binding')
        expected=self.db.execute('SELECT sig_hash FROM risk_exit_contracts WHERE exchange=?',(mapping['sig_exchange_id'],)).fetchone()[0]
        if market['status']!='open' or fingerprint(sig_contract_record(market,mapping))!=expected:
            raise ValueError('SIG contract changed or closed; risk exit blocked')

    def propose(self,mapping,book,held,cost,quantity_cap=None,phase='scan',persist=True,now=None):
        if not self.policy['enabled'] or not held:return None,{'reason':'disabled_or_no_position'}
        if not self.bound(mapping):raise ValueError('Missing verified risk exit binding')
        now=time.time() if now is None else now;p=self.policy;ex=mapping['sig_exchange_id']
        book.check(self.config['strategy']['max_age_seconds'],venue='SIG')
        owned,basis=self.ledger.held(ex)
        if D(held)!=owned or D(cost)!=basis:raise ValueError('Risk exit inventory changed')
        side='yes' if held>0 else 'no';target=book if held>0 else book.complement()
        bid,depth=target.bids[0];fee=D(self.config['strategy']['cost_buffer_per_share'])
        if bid%D('.005'):raise ValueError('Invalid SIG risk exit tick')
        avg=D(cost)/abs(D(held));epoch=self.db.execute('SELECT MAX(rowid) FROM executions WHERE exchange=? AND CAST(quantity AS REAL)>0',(ex,)).fetchone()[0]
        signature=hashlib.sha256(json.dumps(dict(policy=p,fee=fee,side=side,pin=mapping['contract_fingerprint']),sort_keys=True,default=str).encode()).hexdigest()
        detail=dict(reason='risk_exit_history_insufficient',exchange=ex,side=side,epoch=epoch,
                    average_cost_with_entry_buffer=avg,bid=bid,loss_per_share=avg+fee-bid,threshold=p['loss_per_share'])
        last=self.db.execute('''SELECT MAX(e.at) FROM executions e JOIN risk_exit_orders r ON e.key=r.key
            WHERE r.exchange=? AND CAST(e.quantity AS REAL)>0''',(ex,)).fetchone()[0]
        if last is not None and now-last<p['cooldown_seconds']:
            return None,dict(detail,reason='risk_exit_cooldown',retry_in_seconds=p['cooldown_seconds']-(now-last))
        rows=[dict(r) for r in self.db.execute('SELECT at,bid,depth FROM risk_exit_samples WHERE exchange=? AND epoch=? AND policy=? AND at>=? ORDER BY at',
             (ex,epoch,signature,now-p['window_seconds']))]
        if any(r['at']>now for r in rows):return None,dict(detail,reason='risk_exit_clock_reversal')
        # Even a rebound between scheduled samples breaks a sustained-loss run.
        # Preflight may invalidate history, but can never add a confirmation.
        if persist and rows and avg+fee-bid<p['loss_per_share']:
            if self.db.in_transaction:raise RuntimeError('Risk exit sampling requires its own transaction')
            with self.db:self.db.execute('DELETE FROM risk_exit_samples WHERE exchange=?',(ex,))
            rows=[]
        current={'at':now,'bid':str(bid),'depth':str(depth)}
        if phase=='scan' and persist and (not rows or now-rows[-1]['at']>=p['sample_spacing_seconds']):
            if self.db.in_transaction:raise RuntimeError('Risk exit sampling requires its own transaction')
            with self.db:
                self.db.execute('DELETE FROM risk_exit_samples WHERE at<?',(now-p['window_seconds'],))
                self.db.execute('DELETE FROM risk_exit_samples WHERE exchange=? AND (epoch<>? OR policy<>?)',(ex,epoch,signature))
                self.db.execute('INSERT INTO risk_exit_samples VALUES (?,?,?,?,?,?)',(ex,epoch,signature,now,str(bid),str(depth)))
            rows.append(current)
        quantity=int(min(abs(D(held))*p['partial_fraction'],depth,D(self.config['limits']['per_order'])/bid,
                         D(self.config['strategy']['max_shares_per_order']),D(quantity_cap) if quantity_cap is not None else abs(D(held))))
        detail['quantity']=quantity
        if quantity<1:return None,dict(detail,reason='risk_exit_size_below_one')
        chain=[]
        for r in rows:
            if (chain and r['at']-chain[-1]['at']>p['max_sample_gap_seconds']) or D(r['depth'])<quantity or avg+fee-D(r['bid'])<p['loss_per_share']:
                chain=[]
            if D(r['depth'])>=quantity and avg+fee-D(r['bid'])>=p['loss_per_share']:chain.append(r)
        detail['confirmations']=len(chain)
        if avg+fee-bid<p['loss_per_share']:return None,dict(detail,reason='risk_exit_loss_below_threshold')
        if len(chain)<3 or now-chain[-1]['at']>p['max_sample_gap_seconds'] or chain[-1]['at']-chain[-3]['at']<p['minimum_span_seconds']:return None,detail
        fifo=self.ledger.sale_basis(ex,side,quantity)
        detail.update(reason=REASON,fifo_cost=fifo,estimated_realized_pnl=quantity*(bid-fee)-fifo,
                      current_held=abs(D(held)),policy=p)
        return Signal(side,bid,quantity,avg,bid-fee-avg,'sell',REASON),detail

    def attach(self,payload,detail):
        if not self.db.in_transaction:raise RuntimeError('Risk exit attribution requires reservation transaction')
        self.db.execute('INSERT INTO risk_exit_orders VALUES (?,?,?)',
                        (payload['idempotencyKey'],payload['exchangeId'],json.dumps(detail,default=str)))
