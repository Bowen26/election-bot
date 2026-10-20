"""Read-only patient-entry diagnostics; descriptive cohorts, not causal A/B proof."""
from collections import defaultdict
import json
from pathlib import Path
import sqlite3
from .strategy import D

HORIZONS=(60,300,900,3600,86400)


def mean(values):
    return str(sum(values,D(0))/len(values)) if values else None


def report(source):
    if isinstance(source,(str,Path)):
        path=Path(source).resolve()
        if not path.exists():return {'status':'no_journal','note':'No execution journal yet.'}
        db=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True);db.row_factory=sqlite3.Row
        try:
            db.execute('BEGIN')
            return summarize(db)
        finally:db.close()
    return summarize(source)


def summarize(db):
    tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if 'patient_orders' not in tables:
        return {'status':'not_started','note':'No patient attribution table yet.'}
    rows=db.execute('''SELECT t.*,e.quantity,e.price,e.buffer FROM patient_orders t
                       LEFT JOIN executions e ON e.key=t.key''').fetchall()
    marks=defaultdict(dict)
    if 'markouts' in tables:
        for row in db.execute('SELECT key,horizon,pnl FROM markouts'):
            marks[row['key']][row['horizon']]=row['pnl']
    groups={};unknown=0
    for row in rows:
        if row['arm']=='unknown':unknown+=1;continue
        if row['priced']=='improved':name='patient_improved'
        elif row['arm']=='patient':name='patient_fallback_control'
        elif row['improvable']==1:name='control_improvable'
        elif row['improvable']==0:name='control_not_improvable'
        else:name='control_unknown_counterfactual'
        b=groups.setdefault(name,dict(attempts=0,closed=0,pending=0,fills=0,shares=D(0),
            prices=[],improvements=[],moves=defaultdict(list),markouts=defaultdict(list)))
        b['attempts']+=1
        if row['quantity'] is None:b['pending']+=1;continue
        b['closed']+=1
        q=D(row['quantity'])
        if q<=0:continue
        b['fills']+=1;b['shares']+=q
        price,fee=D(row['price']),D(row['buffer']);b['prices'].append(price)
        detail=json.loads(row['detail'])
        if detail.get('control_price') is not None:
            b['improvements'].append(D(detail['control_price'])-price)
        for horizon,pnl in marks.get(row['key'],{}).items():
            if pnl is None:continue
            value=D(pnl)/q;b['markouts'][horizon].append(value)
            if detail.get('benchmark_bid') is not None:
                b['moves'][horizon].append(value+price+2*fee-D(detail['benchmark_bid']))
    summary={}
    for name,b in sorted(groups.items()):
        summary[name]={'attempts':b['attempts'],'closed_attempts':b['closed'],'pending_attempts':b['pending'],
            'fills':b['fills'],'fill_rate':str(D(b['fills'])/b['closed']) if b['closed'] else None,
            'shares_filled':str(b['shares']),'average_entry_price':mean(b['prices']),
            'mean_improvement_vs_own_quoted_ask':mean(b['improvements']),
            'price_improvement_measured_fills':len(b['improvements']),
            'average_markout_per_share':{h:mean(b['markouts'][h]) for h in HORIZONS if b['markouts'][h]},
            'average_bid_move_from_decision':{h:mean(b['moves'][h]) for h in HORIZONS if b['moves'][h]},
            'measured_fills_by_horizon':{h:len(b['markouts'][h]) for h in HORIZONS},
            'bid_move_measured_fills_by_horizon':{h:len(b['moves'][h]) for h in HORIZONS},
            'unmeasured_fills_by_horizon':{h:b['fills']-len(b['markouts'][h]) for h in HORIZONS}}
    comparison={'basis':'Descriptive improved-patient versus assigned-control eligible for price improvement; not matched trials.'}
    patient,control=(summary.get(k,{}) for k in ('patient_improved','control_improvable'))
    for src,dest in (('average_markout_per_share','markout_difference_per_share'),
                     ('average_bid_move_from_decision','bid_move_difference_per_share')):
        comparison[dest]={}
        for h in HORIZONS:
            a,b=(g.get(src,{}).get(h) for g in (patient,control))
            if a is not None and b is not None:comparison[dest][h]=str(D(a)-D(b))
    return {'status':'measured' if patient.get('fills',0) and control.get('fills',0) else 'no_matched_fills',
        'by_group':summary,'comparison':comparison,'unclassified_orders':unknown,
        'note':'Not yet evidence of profitability or a causal A/B effect. Attempts are durable reservations, '
        'including no-submission cancellations; pending attempts are excluded from closed-attempt fill rates. '
        'Improvement is measured against each order’s own contemporaneous quoted ask, not another market’s price. '
        'Bid moves are relative to each order’s own decision-time bid. Missing benchmarks/markouts stay unmeasured. '
        'Prices, fill selection, time and race mix differ between groups; failed-patient fallbacks are separate. '
        'Means are equally weighted per filled order; horizons may cover different fills. Buffers are estimates. '
        'Assignment is deterministic by contract and attributed attempt count, including non-fills. '
        'No sample-count threshold alone validates this pilot; no automatic promotion.'}


def format_report(result):
    lines=['PATIENT ENTRY PILOT — descriptive execution comparison']
    if 'by_group' not in result:return '\n'.join(lines+[result['note']])
    for name,g in result['by_group'].items():
        lines.append(f"{name}: {g['attempts']} reservations, {g['pending_attempts']} pending, {g['fills']} filled; closed-attempt fill rate {g['fill_rate']}")
        lines.append(f"  Improvement versus own quoted ask: {g['mean_improvement_vs_own_quoted_ask']} coins/share ({g['price_improvement_measured_fills']} measured fills)")
        for h,v in g['average_markout_per_share'].items():
            lines.append(f"  {h}s: buffered markout {v}/share; bid move {g['average_bid_move_from_decision'].get(h)}; {g['measured_fills_by_horizon'][h]} measured fills")
    lines.append(json.dumps(result['comparison'],default=str));lines.append(result['note'])
    return '\n'.join(lines)
