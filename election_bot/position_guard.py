"""Pause additions to an open position after a confirmed decline in SIG prices."""
import hashlib
import json
import time
from .strategy import D

DEFAULTS=dict(enabled=False,window_seconds=1800,sample_spacing_seconds=60,
              max_sample_gap_seconds=600,minimum_span_seconds=120,
              decline='0.02',recovery_distance='0.01')


def settings(config):
    raw=config.get('position_entry_guard',{})
    if not isinstance(raw,dict) or set(raw)-set(DEFAULTS):raise ValueError('Invalid position_entry_guard settings')
    p=dict(DEFAULTS,**raw)
    if type(p['enabled']) is not bool:raise ValueError('Position guard enabled must be boolean')
    for k,low,high in [('window_seconds',180,7200),('sample_spacing_seconds',30,300),
                       ('max_sample_gap_seconds',60,1800),('minimum_span_seconds',60,3600)]:
        if type(p[k]) is not int or not low<=p[k]<=high:raise ValueError('Invalid position guard '+k)
    if not p['sample_spacing_seconds']<=p['max_sample_gap_seconds']<=p['window_seconds']:raise ValueError('Invalid position guard spacing')
    if max(2*p['sample_spacing_seconds'],p['minimum_span_seconds'])>p['window_seconds']:raise ValueError('Position guard window too short')
    for k in ('decline','recovery_distance'):p[k]=D(p[k])
    if not 0<=p['recovery_distance']<p['decline']<1:raise ValueError('Invalid position guard decline/recovery distance')
    if p['enabled'] and config.get('execution',{}).get('enabled') is not True:raise ValueError('Position guard requires active execution')
    return p


def episode(db,exchange,side):
    """Entry key since the last flat inventory; adding or partial selling preserves it."""
    q=D(0);start=None
    for r in db.execute('SELECT key,action,quantity FROM executions WHERE exchange=? AND side=? ORDER BY at,rowid',(exchange,side)):
        size=D(r['quantity'])
        if not size:continue
        if r['action']=='buy':
            if not q:start=r['key']
            q+=size
        elif r['action']=='sell':q-=size
        else:raise ValueError('Unknown position guard execution action')
        if q<0:raise ValueError('Position guard inventory mismatch')
        if not q:start=None
    return start,q


class PositionGuard:
    def __init__(self,db,config):
        self.db=db;self.policy=settings(config);self.quote_age=config['strategy']['max_age_seconds']
        self.signature=hashlib.sha256(json.dumps(self.policy,sort_keys=True,default=str).encode()).hexdigest()
        if not self.policy['enabled']:return
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS position_guard_state (
            exchange TEXT PRIMARY KEY, episode TEXT NOT NULL, binding TEXT NOT NULL,
            policy TEXT NOT NULL, detail TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS position_guard_samples (
            exchange TEXT NOT NULL, episode TEXT NOT NULL, policy TEXT NOT NULL,
            at REAL NOT NULL, bid TEXT NOT NULL, ask TEXT NOT NULL,
            PRIMARY KEY(exchange,episode,policy,at));
        ''')

    def check(self,mapping,book,held,phase='scan',persist=True,now=None):
        if not self.policy['enabled']:return dict(buy_allowed=True,reason='disabled')
        if not held:return dict(buy_allowed=True,reason='no_existing_position')
        book.check(self.quote_age,venue='SIG')
        now=time.time() if now is None else now
        ex=mapping['sig_exchange_id'];side='yes' if held>0 else 'no'
        ep,qty=episode(self.db,ex,side)
        if not ep or qty!=abs(D(held)):raise ValueError('Position guard inventory mismatch')
        binding=mapping['contract_fingerprint']+':'+side
        saved=self.db.execute('SELECT * FROM position_guard_state WHERE exchange=?',(ex,)).fetchone()
        state={'paused':False,'anchor':None,'paused_at':None,'last_sample_at':None}
        if saved and saved['episode']==ep:
            if saved['binding']!=binding:raise ValueError('Position guard contract binding changed during holding')
            state=json.loads(saved['detail'])
        rows=[dict(r) for r in self.db.execute('SELECT at,bid,ask FROM position_guard_samples WHERE exchange=? AND episode=? AND policy=? AND at>=? ORDER BY at',
            (ex,ep,self.signature,now-self.policy['window_seconds']))]
        outcome=dict(buy_allowed=False,reason='position_history_insufficient',paused=state['paused'],
                     exchange=ex,side=side,episode=ep,samples=0,anchor=state['anchor'])
        if (state['last_sample_at'] is not None and state['last_sample_at']>now) or any(r['at']>now for r in rows):
            return dict(outcome,reason='position_history_clock_reversal')
        target=book if side=='yes' else book.complement()
        current={'at':now,'bid':str(target.bids[0][0]),'ask':str(target.asks[0][0])}
        # Preflight and transaction rechecks never manufacture confirmations.
        append=(phase=='scan' and persist and (not rows or now-rows[-1]['at']>=self.policy['sample_spacing_seconds']))
        if append:rows.append(current);state['last_sample_at']=now
        chain=[]
        for row in rows:
            if chain and row['at']-chain[-1]['at']>self.policy['max_sample_gap_seconds']:chain=[]
            chain.append(row)
        ready=(len(chain)>=3 and now-chain[-1]['at']<=self.policy['max_sample_gap_seconds'] and
               chain[-1]['at']-chain[0]['at']>=self.policy['minimum_span_seconds'])
        outcome.update(samples=len(chain),current=current)
        if ready:
            if not state['paused']:
                # One actual earlier quote supplies both prices; spread widening alone is insufficient.
                anchors=[a for a in chain[:-2] if all(
                    D(a[k])-D(r[k])>=self.policy['decline']
                    for r in chain[-2:]+[current] for k in ('bid','ask'))]
                if anchors:
                    anchor=max(anchors,key=lambda r:D(r['bid'])+D(r['ask']))
                    state.update(paused=True,anchor=anchor,paused_at=now)
            elif append:
                anchor=state['anchor']
                recovered=lambda r: r['at']>state['paused_at'] and all(D(r[k])>=D(anchor[k])-self.policy['recovery_distance'] for k in ('bid','ask'))
                if all(recovered(r) for r in chain[-2:]) and recovered(current):state.update(paused=False,anchor=None,paused_at=None)
            outcome.update(buy_allowed=not state['paused'],reason='position_decline_paused' if state['paused'] else 'position_trend_allowed')
        outcome.update(paused=state['paused'],anchor=state['anchor'])
        if persist:
            if self.db.in_transaction:raise RuntimeError('Position guard persistence requires its own transaction')
            with self.db:
                self.db.execute('DELETE FROM position_guard_samples WHERE at<?',(now-self.policy['window_seconds'],))
                self.db.execute('DELETE FROM position_guard_samples WHERE exchange=? AND (episode<>? OR policy<>?)',(ex,ep,self.signature))
                if append:self.db.execute('INSERT INTO position_guard_samples VALUES (?,?,?,?,?,?)',(ex,ep,self.signature,now,current['bid'],current['ask']))
                self.db.execute('INSERT OR REPLACE INTO position_guard_state VALUES (?,?,?,?,?)',(ex,ep,binding,self.signature,json.dumps(state)))
        return outcome
