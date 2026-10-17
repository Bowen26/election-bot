"""Read-only FIFO attribution by entry route. No broker, writes or trading decisions."""
from collections import Counter, defaultdict, deque
from pathlib import Path
import json
import sqlite3
import time

from .ledger import inventory_from_executions
from .measurement import WINDOWS, eligible, tracking_start
from .model_live import allowance, settings
from .performance import markout_summary
from .strategy import D

GROUPS = ('model', 'other_entries')


def empty():
    return dict(filled_buy_orders=0, bought_shares=D(0), gross_purchase_cost=D(0),
                sold_shares=D(0), net_sale_proceeds=D(0), released_cost_basis=D(0),
                realized_pnl_after_buffers=D(0), open_shares=D(0), open_cost=D(0))


def attribute(rows, model_keys, races):
    """Allocate every sale across entry lots in the ledger's exact FIFO order."""
    groups={g:empty() for g in GROUPS};by_race={};lots=defaultdict(deque)
    def bucket(group, race):
        return groups[group],by_race.setdefault((group,race),empty())
    for row in rows:
        q,price,fee=(D(row[k]) for k in ('quantity','price','buffer'))
        if q<0 or q!=int(q) or fee<0 or (q and not 0<price<1):
            raise ValueError('Invalid confirmed execution for attribution')
        if row['side'] not in ('yes','no') or row['action'] not in ('buy','sell'):
            raise ValueError('Unknown execution side or action')
        if row['key'] in model_keys and row['action']!='buy':
            raise ValueError('Model attribution attached to a sell')
        if not q:continue
        asset=(row['exchange'],row['side']);race=races.get(row['exchange'],'exchange:'+row['exchange'])
        if row['action']=='buy':
            group='model' if row['key'] in model_keys else 'other_entries'
            lots[asset].append([q,price+fee,group,race])
            for b in bucket(group,race):
                b['filled_buy_orders']+=1;b['bought_shares']+=q
                b['gross_purchase_cost']+=q*(price+fee)
        else:
            left=q
            while left and lots[asset]:
                lot=lots[asset][0];take=min(left,lot[0]);basis=take*lot[1];proceeds=take*(price-fee)
                for b in bucket(lot[2],lot[3]):
                    b['sold_shares']+=take;b['net_sale_proceeds']+=proceeds
                    b['released_cost_basis']+=basis;b['realized_pnl_after_buffers']+=proceeds-basis
                lot[0]-=take;left-=take
                if not lot[0]:lots[asset].popleft()
            if left:raise ValueError('Sell exceeds bot-owned inventory in attribution')
    for remaining in lots.values():
        for q,price,group,race in remaining:
            for b in bucket(group,race):b['open_shares']+=q;b['open_cost']+=q*price
    # Accounting parity, independent of route classification.
    holdings,realized=inventory_from_executions(rows)
    if (sum((b['realized_pnl_after_buffers'] for b in groups.values()),D(0))!=sum(realized.values(),D(0)) or
        sum((b['open_cost'] for b in groups.values()),D(0))!=sum((v['cost'] for v in holdings.values()),D(0))):
        raise ValueError('Strategy attribution does not reconcile with the ledger')
    return groups,[dict(route=g,race_key=r,**v) for (g,r),v in sorted(by_race.items())]


def report(path, config=None, now=None):
    path=Path(path).resolve();now=time.time() if now is None else float(now)
    if not path.exists():return {'status':'no_journal','path':str(path)}
    db=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True);db.row_factory=sqlite3.Row
    try:
        db.execute('BEGIN')
        tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {'orders','executions'}<=tables:return {'status':'no_execution_ledger'}
        rows=[dict(r) for r in db.execute('SELECT * FROM executions ORDER BY at,rowid')]
        orders={r['key']:dict(r) for r in db.execute('SELECT * FROM orders')}
        tags={r['key']:dict(r) for r in db.execute('SELECT * FROM model_orders')} if 'model_orders' in tables else {}
        races={r['exchange']:r['race'] for r in db.execute('SELECT * FROM race_bindings')} if 'race_bindings' in tables else {}
        executions={r['key']:r for r in rows}
        for key,tag in tags.items():
            order=orders.get(key)
            if order is None:raise ValueError('Orphan model attribution; report unavailable')
            payload=json.loads(order['payload'])
            if payload.get('action')!='buy':raise ValueError('Model attribution attached to a non-buy order')
            race=races.get(order['market'])
            if race is not None and race!=tag['race']:raise ValueError('Model race attribution mismatch')
            races.setdefault(order['market'],tag['race'])
        groups,by_race=attribute(rows,set(tags),races)
        for g,b in groups.items():
            selected=[o for key,o in orders.items() if ('model' if key in tags else 'other_entries')==g]
            pending=[o for o in selected if o['state']=='pending']
            b.update(pending_orders=len(pending),pending_reserved_coins=sum((D(o['amount']) for o in pending),D(0)),
                     zero_fill_orders=sum(o['key'] in executions and D(executions[o['key']]['quantity'])==0 for o in selected),
                     closed_orders_missing_execution=sum(o['state']=='closed' and o['key'] not in executions for o in selected))
        # Existing markout observations are hypothetical and remain separate from FIFO profits.
        started=tracking_start(db)
        for g,b in groups.items():
            b['buy_markouts_seconds']={}
            if 'markouts' not in tables:continue
            for horizon in WINDOWS:
                samples=[dict(r) for r in db.execute('''SELECT e.*,m.at AS sample_at,m.pnl,m.reason
                    FROM executions e LEFT JOIN markouts m ON e.key=m.key AND m.horizon=?
                    WHERE e.action='buy' AND CAST(e.quantity AS REAL)>0''',(horizon,))
                    if ('model' if r['key'] in tags else 'other_entries')==g and eligible(r['at'],horizon,started)]
                b['buy_markouts_seconds'][str(horizon)]=markout_summary(samples,horizon,now)
        decisions={};latest=None
        if 'events' in tables:
            for r in db.execute("SELECT at,detail FROM events WHERE kind='model_decision' AND at>=? AND at<=? ORDER BY at,rowid",(now-86400,now)):
                d=json.loads(r['detail']);phase=d.get('phase','unknown')
                phase_summary=decisions.setdefault(phase,dict(observations=0,statuses=Counter(),reasons=Counter(),input_errors=Counter()))
                phase_summary['observations']+=1;phase_summary['statuses'][d.get('status','unknown')]+=1
                phase_summary['reasons'][d.get('reason','unknown')]+=1
                if d.get('reason')=='model_input_unavailable':phase_summary['input_errors'][d.get('error','unspecified')]+=1
                latest=r['at']
        result=dict(status='ok',as_of=now,attribution_available='model_orders' in tables,
                    scope='All retained confirmed executions; model diagnostics cover the last 24 hours.',
                    routes=groups,by_race=by_race,model_decisions_last_24h=decisions,
                    latest_model_decision_at=latest,latest_model_decision_age_seconds=now-latest if latest is not None else None,
                    reconciles_with_ledger=True,
                    accounting_complete=not any(b['closed_orders_missing_execution'] for b in groups.values()),
                    notes=[
                        'Other entries means buys without a durable model tag, including legacy buys; no retroactive classification.',
                        'Sales follow portfolio FIFO, even when entry routes share a contract. Route results are accounting attribution, not independent strategy backtests.',
                        'PnL uses estimated entry and exit cost buffers. Unsold positions have cost, not realized profit; no unrealized value is inferred here.',
                        'Markouts are hypothetical full-size exits, not realized PnL or election-probability calibration. Inspect missing coverage and delays.',
                        'Scan and preflight diagnostics are separated; repeated observations are correlated. A recent event is not proof the worker is alive.',
                        'This report does not change sizing, exits, model coefficients or live settings.'])
        if config is not None:
            p=settings(config)
            result['model_budget']=dict(enabled_in_current_config=p['enabled'],limits={k:p[k] for k in ('total','daily','per_race','per_order')},
                note='Current file settings, not proof the running worker loaded them. Gross purchases plus pending reservations; sales do not recycle the allowance.')
            if 'model_orders' in tables:
                bound=sorted({t['race'] for t in tags.values()}|{m['race_key'] for m in config.get('markets',[]) if m.get('enabled') and m.get('office')=='senate' and 'race_key' in m})
                _,used=allowance(db,p,None,now)
                result['model_budget'].update(used={k:used[k] for k in ('total','daily')},
                    remaining={k:max(D(0),p[k]-used[k]) for k in ('total','daily')},
                    by_race=[dict(race_key=r,per_race_used=allowance(db,p,r,now)[1]['per_race'],
                                  next_order_coin_ceiling=allowance(db,p,r,now)[0]) for r in bound])
            else:result['model_budget']['status']='not_initialized'
        return result
    finally:db.close()


def format_report(result):
    if result['status']!='ok':return 'STRATEGY PERFORMANCE — '+result['status']
    lines=['STRATEGY PERFORMANCE — confirmed fills, FIFO attribution',result['scope']]
    if not result['accounting_complete']:
        lines.append('INCOMPLETE: closed orders are missing confirmed executions; totals cover recorded executions only.')
    for name,b in result['routes'].items():
        lines.append(f"{name}: {b['filled_buy_orders']} filled buys; cost {b['gross_purchase_cost']}; "
            f"sold {b['sold_shares']} shares; realized buffered PnL {b['realized_pnl_after_buffers']}; "
            f"open cost {b['open_cost']}; pending {b['pending_orders']}")
        for h,m in b['buy_markouts_seconds'].items():
            lines.append(f"  {h}s hypothetical markouts: {m['observations']}/{m['eligible_buys']} measured; "
                f"missed {m['missed']}; PnL/share {m['pnl_per_share_after_buffers']}")
    budget=result.get('model_budget')
    if budget:
        lines.append('Model allowance: '+json.dumps(budget.get('remaining',{'status':budget.get('status')}),default=str)+' remaining (total / UTC daily)')
    for phase,d in result['model_decisions_last_24h'].items():
        lines.append(f"Model {phase}: {d['observations']} observations; "+json.dumps(d['reasons']))
        if d['input_errors']:lines.append('  Input blockers: '+json.dumps(d['input_errors']))
    lines.extend(result['notes'])
    return '\n'.join(lines)
