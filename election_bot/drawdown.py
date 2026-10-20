"""Persistent buy-only drawdown gate using complete SIG bid-depth valuations."""
import json
from pathlib import Path
import sqlite3
import time

from .ledger import inventory_from_executions, liquidation
from .strategy import D

DEFAULTS=dict(enabled=False, pause_coins='2500', resume_coins='1250',
              max_mark_age_seconds=300, refresh_books_per_cycle=8,
              valuation='sig_bid', max_fair_mark_age_seconds=1800)
VALUATIONS=('sig_bid','reference_fair')


def settings(config):
    raw=config.get('drawdown_control',{})
    if not isinstance(raw,dict) or set(raw)-set(DEFAULTS):raise ValueError('Invalid drawdown_control settings')
    p=dict(DEFAULTS,**raw)
    if type(p['enabled']) is not bool:raise ValueError('Drawdown enabled must be boolean')
    for k in ('pause_coins','resume_coins'):p[k]=D(p[k])
    if not 0<=p['resume_coins']<p['pause_coins']:raise ValueError('Drawdown resume must be below positive pause threshold')
    if p['valuation'] not in VALUATIONS:raise ValueError('Drawdown valuation must be sig_bid or reference_fair')
    for k,low,high in [('max_mark_age_seconds',30,600),('refresh_books_per_cycle',1,20),('max_fair_mark_age_seconds',60,7200)]:
        if type(p[k]) is not int or not low<=p[k]<=high:raise ValueError('Invalid drawdown '+k)
    if p['enabled'] and (config.get('execution',{}).get('enabled') is not True or config['execution'].get('sell_enabled') is not True):
        raise ValueError('Drawdown control requires active execution with selling enabled')
    return p


def epochs(db):
    return dict(db.execute('SELECT exchange,MAX(rowid) FROM executions GROUP BY exchange'))


def evaluate(db, holdings, realized, policy, fee, now):
    state=db.execute('SELECT * FROM drawdown_state WHERE id=1').fetchone()
    if state is None:raise ValueError('Missing drawdown state')
    if D(state['exit_buffer'])!=fee:raise ValueError('Drawdown exit buffer changed; valuation baseline requires review')
    peak=D(state['peak']);paused=bool(state['paused'])
    marks={r['exchange']:r for r in db.execute('SELECT * FROM drawdown_marks')};current=epochs(db)
    missing=[];values=[];unrealized=D(0)
    for (ex,side),v in sorted(holdings.items()):
        mark=marks.get(ex);reason=None
        if mark is None:reason='missing'
        elif mark['side']!=side or mark['epoch']!=current.get(ex) or D(mark['quantity'])!=v['quantity'] or D(mark['cost'])!=v['cost']:reason='inventory_changed'
        elif not 0<=now-mark['at']<=policy['max_mark_age_seconds']:reason='stale_or_future'
        elif D(mark['executable_quantity'])!=v['quantity']:reason='insufficient_bid_depth'
        elif D(mark['bid_value'])<0 or D(mark['bid_value'])>v['quantity']:reason='invalid_value'
        if reason:missing.append(dict(exchange=ex,reason=reason));continue
        pnl=D(mark['bid_value'])-v['cost']-fee*v['quantity']
        unrealized+=pnl;values.append(dict(exchange=ex,side=side,age_seconds=now-mark['at'],pnl=pnl))
    realized_pnl=sum(realized.values(),D(0))
    result=dict(enabled=True,complete=not missing,buy_allowed=False,paused=paused,peak_pnl=peak,
                pause_coins=policy['pause_coins'],resume_coins=policy['resume_coins'],
                peak_policy='persistent_high_water_mark',automatic_timeout=False,
                resume_requires_portfolio_pnl_above=peak-policy['resume_coins'],
                recovery_gap_coins=None,
                realized_pnl=realized_pnl,missing=missing,marks=values,at=now,
                unrealized_pnl=None,portfolio_pnl=None,drawdown=None,reason='valuation_incomplete')
    # Missing marks must not raise the peak, clear a pause, or count as zero value.
    if missing:return result
    pnl=realized_pnl+unrealized;peak=max(peak,D(0),pnl);drop=max(D(0),peak-pnl)
    if drop>=policy['pause_coins']:paused=True
    elif paused and drop<policy['resume_coins']:paused=False
    return dict(result,paused=paused,peak_pnl=peak,
                resume_requires_portfolio_pnl_above=peak-policy['resume_coins'],
                recovery_gap_coins=max(D(0),peak-policy['resume_coins']-pnl) if paused else D(0),unrealized_pnl=unrealized,portfolio_pnl=pnl,
                drawdown=drop,buy_allowed=not paused,reason='drawdown_paused' if paused else 'allowed')


def unbuffered(rows):
    """FIFO holdings and realized P&L at actual prices; the platform charges no fees."""
    return inventory_from_executions([dict(dict(r),buffer='0') for r in rows])


def evaluate_fair(db, holdings, plain, realized, policy, now):
    """Settlement-value drawdown: positions at reference midpoints, no buffer.

    holdings (buffered) detects inventory changes; plain/realized are unbuffered.
    Separate peak/pause state, so switching valuation never reuses a bid-based peak.
    """
    state=db.execute('SELECT * FROM drawdown_fair_state WHERE id=1').fetchone()
    if state is None:raise ValueError('Missing fair-value drawdown state')
    peak=D(state['peak']);paused=bool(state['paused'])
    marks={r['exchange']:r for r in db.execute('SELECT * FROM drawdown_fair_marks')};current=epochs(db)
    missing=[];values=[];unrealized=D(0)
    for (ex,side),v in sorted(holdings.items()):
        mark=marks.get(ex);reason=None
        if mark is None:reason='missing'
        elif mark['side']!=side or mark['epoch']!=current.get(ex) or D(mark['quantity'])!=v['quantity']:reason='inventory_changed'
        elif not 0<=now-mark['at']<=policy['max_fair_mark_age_seconds']:reason='stale_or_future'
        elif not 0<=D(mark['fair_value'])<=v['quantity']:reason='invalid_value'
        if reason:missing.append(dict(exchange=ex,reason=reason));continue
        pnl=D(mark['fair_value'])-plain[(ex,side)]['cost']
        unrealized+=pnl;values.append(dict(exchange=ex,side=side,age_seconds=now-mark['at'],pnl=pnl))
    realized_pnl=sum(realized.values(),D(0))
    result=dict(enabled=True,valuation='reference_fair',complete=not missing,buy_allowed=False,paused=paused,
                peak_pnl=peak,pause_coins=policy['pause_coins'],resume_coins=policy['resume_coins'],
                peak_policy='persistent_high_water_mark',automatic_timeout=False,
                resume_requires_portfolio_pnl_above=peak-policy['resume_coins'],recovery_gap_coins=None,
                realized_pnl=realized_pnl,missing=missing,marks=values,at=now,
                unrealized_pnl=None,portfolio_pnl=None,drawdown=None,reason='valuation_incomplete')
    if missing:return result
    pnl=realized_pnl+unrealized;peak=max(peak,D(0),pnl);drop=max(D(0),peak-pnl)
    if drop>=policy['pause_coins']:paused=True
    elif paused and drop<policy['resume_coins']:paused=False
    return dict(result,paused=paused,peak_pnl=peak,
                resume_requires_portfolio_pnl_above=peak-policy['resume_coins'],
                recovery_gap_coins=max(D(0),peak-policy['resume_coins']-pnl) if paused else D(0),
                unrealized_pnl=unrealized,portfolio_pnl=pnl,drawdown=drop,buy_allowed=not paused,
                reason='drawdown_paused' if paused else 'allowed')


class Drawdown:
    def __init__(self,ledger,config):
        self.ledger=ledger;self.db=ledger.db;self.policy=settings(config)
        self.fee=D(config['strategy']['cost_buffer_per_share']);self.quote_age=config['strategy']['max_age_seconds']
        self.strategy=config['strategy']
        if not self.policy['enabled']:return
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS drawdown_state (
            id INTEGER PRIMARY KEY CHECK(id=1), peak TEXT NOT NULL, paused INTEGER NOT NULL,
            at REAL NOT NULL, exit_buffer TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS drawdown_marks (
            exchange TEXT PRIMARY KEY, side TEXT NOT NULL, epoch INTEGER NOT NULL,
            quantity TEXT NOT NULL, cost TEXT NOT NULL, executable_quantity TEXT NOT NULL,
            bid_value TEXT NOT NULL, at REAL NOT NULL);
          CREATE TABLE IF NOT EXISTS drawdown_attempts (exchange TEXT PRIMARY KEY, at REAL NOT NULL);
          CREATE TABLE IF NOT EXISTS drawdown_fair_state (
            id INTEGER PRIMARY KEY CHECK(id=1), peak TEXT NOT NULL, paused INTEGER NOT NULL, at REAL NOT NULL);
          CREATE TABLE IF NOT EXISTS drawdown_fair_marks (
            exchange TEXT PRIMARY KEY, side TEXT NOT NULL, epoch INTEGER NOT NULL,
            quantity TEXT NOT NULL, fair_value TEXT NOT NULL, at REAL NOT NULL);
        ''')
        with self.db:self.db.execute('INSERT OR IGNORE INTO drawdown_fair_state VALUES (1,?,?,?)',('0',0,time.time()))
        with self.db:self.db.execute('INSERT OR IGNORE INTO drawdown_state VALUES (1,?,?,?,?)',('0',0,time.time(),str(self.fee)))
        if D(self.db.execute('SELECT exit_buffer FROM drawdown_state WHERE id=1').fetchone()[0])!=self.fee:
            raise ValueError('Drawdown exit buffer changed; valuation baseline requires review')

    @property
    def fair(self):
        return self.policy['valuation']=='reference_fair'

    def observe(self,exchange,book,refs=None):
        if not self.policy['enabled']:return
        book.check(self.quote_age,venue='SIG');now=time.time()
        holdings,_=self.ledger.inventory(refresh=True)
        matches=[(side,v) for (ex,side),v in holdings.items() if ex==exchange]
        if len(matches)>1:raise ValueError('Opposing inventory cannot be marked for drawdown')
        if not matches:return
        side,v=matches[0];q,value=liquidation(book if side=='yes' else book.complement(),v['quantity'])
        stamp=min(now,book.observed_at,book.source_at)
        with self.db:self.db.execute('INSERT OR REPLACE INTO drawdown_marks VALUES (?,?,?,?,?,?,?,?)',
            (exchange,side,epochs(self.db)[exchange],str(v['quantity']),str(v['cost']),str(q),str(value),stamp))
        if refs:self.observe_fair(exchange,side,v['quantity'],refs,now)

    def observe_fair(self,exchange,side,quantity,refs,now):
        """Record a settlement-value mark; invalid references leave the old mark to age out."""
        from .settlement_value import fair_value
        try:fair=fair_value(refs,side,self.strategy)
        except ValueError:return
        stamp=min([now]+[t for r in refs for t in (r.observed_at,r.source_at)])
        with self.db:self.db.execute('INSERT OR REPLACE INTO drawdown_fair_marks VALUES (?,?,?,?,?,?)',
            (exchange,side,epochs(self.db)[exchange],str(quantity),str(fair*quantity),stamp))

    def status(self,persist=True):
        if not self.policy['enabled']:return dict(enabled=False,buy_allowed=True,reason='disabled')
        holdings,realized=self.ledger.inventory(refresh=True)
        if self.fair:
            plain,plain_realized=unbuffered(self.db.execute('SELECT * FROM executions ORDER BY at,rowid').fetchall())
            result=evaluate_fair(self.db,holdings,plain,plain_realized,self.policy,time.time())
            table='drawdown_fair_state'
        else:
            result=evaluate(self.db,holdings,realized,self.policy,self.fee,time.time())
            table='drawdown_state'
        if persist and result['complete']:
            if self.db.in_transaction:raise RuntimeError('Drawdown state persistence requires its own transaction')
            with self.db:self.db.execute('UPDATE '+table+' SET peak=?,paused=?,at=? WHERE id=1',
                (str(result['peak_pnl']),int(result['paused']),result['at']))
        return result

    def due(self):
        if not self.policy['enabled']:return []
        status=self.status();bad={r['exchange'] for r in status['missing']}
        age=self.policy['max_fair_mark_age_seconds' if self.fair else 'max_mark_age_seconds']
        bad.update(r['exchange'] for r in status['marks'] if r['age_seconds']>=age/2)
        attempted=dict(self.db.execute('SELECT exchange,at FROM drawdown_attempts'))
        return sorted(bad,key=lambda ex:(attempted.get(ex,0),ex))[:self.policy['refresh_books_per_cycle']]

    def attempted(self,exchange):
        with self.db:self.db.execute('INSERT OR REPLACE INTO drawdown_attempts VALUES (?,?)',(exchange,time.time()))


def report(path,config):
    policy=settings(config)
    if not policy['enabled']:return dict(enabled=False,buy_allowed=None,reason='disabled_in_current_config')
    path=Path(path).resolve()
    if not path.exists():return dict(enabled=True,buy_allowed=False,reason='not_initialized')
    db=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True);db.row_factory=sqlite3.Row
    try:
        db.execute('BEGIN')
        tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {'drawdown_state','drawdown_marks','executions'}<=tables:return dict(enabled=True,buy_allowed=False,reason='not_initialized')
        rows=db.execute('SELECT * FROM executions ORDER BY at,rowid').fetchall()
        holdings,realized=inventory_from_executions(rows)
        note='Evaluation using current config and retained marks; not process liveness. This command does not persist transitions.'
        if policy['valuation']=='reference_fair':
            if not {'drawdown_fair_state','drawdown_fair_marks'}<=tables:
                return dict(enabled=True,valuation='reference_fair',buy_allowed=False,reason='fair_marks_not_initialized',
                            note='Restart the watcher with valuation=reference_fair to begin fair-value marks.')
            plain,plain_realized=unbuffered(rows)
            return dict(evaluate_fair(db,holdings,plain,plain_realized,policy,time.time()),read_only=True,note=note)
        return dict(evaluate(db,holdings,realized,policy,D(config['strategy']['cost_buffer_per_share']),time.time()),
                    read_only=True,note=note)
    finally:db.close()
