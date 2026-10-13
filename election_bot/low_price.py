"""Sampled low-price round-trip research. No broker, credentials or journal writes."""
from collections import Counter, defaultdict
from pathlib import Path
import sqlite3
import time

from .leadership import load_events
from .strategy import D

VERSION = 'low_price_paths_v2'
ENTRY_MODES = ('external_gap', 'sig_momentum')
POLICIES = ('full_target', 'half_then_trail')
DEFAULTS = dict(min_price='.01', max_price='.10', cost_buffer='.01',
    minimum_net_reference_gap='.01', max_sig_spread='.02', max_reference_spread='.04',
    max_reference_disagreement='.03', min_reference_depth='20',
    depth_fraction='.25', max_quantity=100, max_entry_coins='10',
    target_net_per_share='.02', target_return='.50', stop_fraction='.50',
    trail_drop='.01', max_hold_seconds=21600, max_quote_gap_seconds=900,
    momentum_window_seconds=1800, momentum_spacing_seconds=60,
    momentum_min_span_seconds=120, momentum_min_rise='.01',
    momentum_min_return='.25', momentum_breakout='.005')


def settings(overrides=None):
    p = dict(DEFAULTS)
    if overrides:
        if set(overrides)-set(p):
            raise ValueError('Unknown low-price study setting')
        p.update(overrides)
    p = {k:D(v) for k,v in p.items()}
    if not 0 < p['min_price'] <= p['max_price'] <= D('.20'):
        raise ValueError('Study prices must be between zero and 20 cents')
    if not 0 <= p['cost_buffer'] <= D('.10'):
        raise ValueError('Invalid cost buffer')
    for k in ('depth_fraction','stop_fraction','target_return'):
        if not 0 < p[k] <= 1:raise ValueError('Invalid fraction: '+k)
    for k in set(p)-{'min_price','max_price','cost_buffer','depth_fraction','stop_fraction','target_return'}:
        if p[k] <= 0:raise ValueError('Study settings must be positive: '+k)
    if p['max_quantity'] != int(p['max_quantity']) or p['max_quantity'] > 10000:
        raise ValueError('Invalid maximum quantity')
    if p['max_hold_seconds'] > 7*86400 or p['max_quote_gap_seconds'] > p['max_hold_seconds']:
        raise ValueError('Invalid observation/holding window')
    if not 2*p['momentum_spacing_seconds'] <= p['momentum_min_span_seconds'] <= p['momentum_window_seconds'] <= 86400:
        raise ValueError('Invalid momentum observation window')
    if p['momentum_min_return'] > 1:
        raise ValueError('Invalid momentum return threshold')
    return p


def side_book(book, side):
    if side == 'yes':return book
    return dict(book, bid=1-book['ask'], ask=1-book['bid'],
                bid_size=book['ask_size'], ask_size=book['bid_size'])


def reference_bid(row, side, p):
    if not row['aligned']:return None
    refs = [side_book(row[v],side) for v in ('Kalshi','Polymarket')]
    if any(r['ask']-r['bid'] > p['max_reference_spread'] or
           r['bid_size'] < p['min_reference_depth'] for r in refs):return None
    if abs(sum((refs[0][k]-refs[1][k] for k in ('bid','ask')),D(0))/2) > p['max_reference_disagreement']:
        return None
    return min(r['bid'] for r in refs)


def momentum_evidence(row, side, history, p):
    """Use three spaced, advancing SIG observations available at entry time only."""
    selected=[row]
    for old in reversed(history):
        if row['at']-old['at'] > float(p['momentum_window_seconds']):break
        if old['exchange'] != row['exchange']:continue
        # Strict chronology also protects callers other than the replay loader.
        latest=selected[-1]
        if latest['at']-old['at'] < float(p['momentum_spacing_seconds']):continue
        if any(old['SIG'][k] >= latest['SIG'][k] for k in ('source','observed')):continue
        selected.append(old)
        if len(selected)==3:break
    if len(selected)<3 or row['at']-selected[-1]['at'] < float(p['momentum_min_span_seconds']):
        return None,'momentum_history'
    selected.reverse()
    if any(b['at']-a['at'] > float(p['max_quote_gap_seconds']) for a,b in zip(selected,selected[1:])):
        return None,'momentum_history_gap'
    books=[side_book(r['SIG'],side) for r in selected]
    if any(b['ask']-b['bid'] > p['max_sig_spread'] for b in books):
        return None,'momentum_history_spread'
    bid_rise=books[-1]['bid']-books[0]['bid'];ask_rise=books[-1]['ask']-books[0]['ask']
    if (bid_rise < p['momentum_min_rise'] or ask_rise < p['momentum_min_rise'] or
            bid_rise/books[0]['bid'] < p['momentum_min_return']):
        return None,'momentum_rise'
    if any(b[k] < a[k] for a,b in zip(books,books[1:]) for k in ('bid','ask')):
        return None,'momentum_reversal'
    prior_high=max(b['bid'] for b in books[:-1])
    if books[-1]['bid']-prior_high < p['momentum_breakout']:
        return None,'momentum_no_breakout'
    return dict(snapshot_ids=[r['id'] for r in selected],
                timestamps=[r['at'] for r in selected],
                bids=[b['bid'] for b in books],asks=[b['ask'] for b in books],
                bid_rise=bid_rise,ask_rise=ask_rise,
                bid_return=bid_rise/books[0]['bid'],breakout_level=prior_high),None


def entry(row, p, checks=None, entry_mode='external_gap', history=()):
    if entry_mode not in ENTRY_MODES:raise ValueError('Unknown entry mode')
    def record(reason):
        if checks is not None:checks[reason]+=1
    candidates = []
    for side in ('yes','no'):
        sig=side_book(row['SIG'],side);ask=sig['ask']
        if not p['min_price'] <= ask <= p['max_price']:
            record('outside_price_band');continue
        if sig['ask']-sig['bid'] > p['max_sig_spread']:
            record('sig_spread');continue
        evidence=None;ref=None;gap=None;depth_limits=[]
        if entry_mode=='external_gap':
            ref=reference_bid(row,side,p)
            if ref is None:
                record('reference_quality_or_depth');continue
            gap=ref-ask-2*p['cost_buffer']
            if gap < p['minimum_net_reference_gap']:
                record('net_reference_gap');continue
            depth_limits=[side_book(row[v],side)['bid_size'] for v in ('Kalshi','Polymarket')]
        else:
            evidence,reason=momentum_evidence(row,side,history,p)
            if reason:
                record(reason);continue
        q=int(min(p['max_quantity'],p['max_entry_coins']/(ask+p['cost_buffer']),
                  p['depth_fraction']*min(sig['ask_size'],sig['bid_size']),*depth_limits))
        if q < 2:
            record('insufficient_entry_size');continue
        target=ask+2*p['cost_buffer']+max(p['target_net_per_share'],
                                                   p['target_return']*(ask+p['cost_buffer']))
        if target >= 1:
            record('unreachable_target');continue
        record('eligible')
        candidates.append(dict(side=side,quantity=q,ask=ask,entry_bid=sig['bid'],reference_bid=ref,net_reference_gap=gap,
                               entry_mode=entry_mode,momentum=evidence,
                               target_bid=target,entry_cost=q*(ask+p['cost_buffer'])))
    score=lambda c:c['net_reference_gap'] if entry_mode=='external_gap' else c['momentum']['bid_rise']
    return max(candidates,key=score) if candidates else None


def new_path(policy, c, at):
    return dict(policy=policy,remaining=c['quantity'],realized_pnl=D(0),fills=[],
                status='open',pending_exit=None,target_quantity_left=(c['quantity']+1)//2,
                peak_bid=c['entry_bid'],observed_best_bid=c['entry_bid'],observed_worst_bid=c['entry_bid'],
                last_quote_at=at,terminal_at=None)


def advance(path, row, c, at, p):
    if path['status'] != 'open':return
    if row['at']-path['last_quote_at'] > float(p['max_quote_gap_seconds']):
        path.update(status='censored_quote_gap',terminal_at=row['at'])
        return
    path['last_quote_at']=row['at']
    sig=side_book(row['SIG'],c['side']);bid=sig['bid']
    path['peak_bid']=bid if path['peak_bid'] is None else max(path['peak_bid'],bid)
    for field,op in [('observed_best_bid',max),('observed_worst_bid',min)]:
        path[field]=bid if path[field] is None else op(path[field],bid)
    reason=path['pending_exit']
    # Stops/time/signal exits are latched, but filled only at subsequently observed bids.
    if reason is None:
        if bid <= c['ask']*(1-p['stop_fraction']):reason='stop'
        elif row['at'] >= at+float(p['max_hold_seconds']):reason='time_limit'
        else:
            if c.get('entry_mode','external_gap')=='sig_momentum':
                if bid <= c['momentum']['breakout_level']:reason='failed_breakout'
            else:
                ref=reference_bid(row,c['side'],p)
                if ref is not None and ref <= c['ask']:reason='reference_deteriorated'
            if reason is None and path['policy']=='half_then_trail' and path['target_quantity_left']==0:
                if bid <= path['peak_bid']-p['trail_drop']:reason='trailing_exit'
            elif reason is None and bid >= c['target_bid']:reason='profit_target'
    if reason is None:return
    partial=reason=='profit_target' and path['policy']=='half_then_trail'
    if not partial:path['pending_exit']=reason
    wanted=min(path['remaining'],path['target_quantity_left']) if partial else path['remaining']
    q=min(wanted,int(sig['bid_size']*p['depth_fraction']))
    if q <= 0:return
    pnl=q*(bid-c['ask']-2*p['cost_buffer'])
    path['fills'].append(dict(at=row['at'],reason=reason,quantity=q,bid=bid,net_pnl=pnl))
    path['remaining']-=q;path['realized_pnl']+=pnl
    if partial:path['target_quantity_left']-=q
    if path['remaining']==0:path.update(status='closed',terminal_at=row['at'])


def analyze(rows, now, p, entry_mode='external_gap'):
    """Paired episodes with one shared entry; never overlap entries in an exchange.

    Each exit policy is a separate counterfactual, not competing for the same book.
    No account-wide portfolio, queue, intraframe price path, or fill claim is made.
    """
    if entry_mode not in ENTRY_MODES:raise ValueError('Unknown entry mode')
    grouped=defaultdict(list)
    for row in rows:grouped[row['exchange']].append(row)
    episodes=[];counts=Counter();checks=Counter()
    for exchange,quotes in sorted(grouped.items()):
        active=None;last_clock=None;history=[]
        for row in sorted(quotes,key=lambda r:(r['at'],r['id'])):
            clocks=(row['at'],row['SIG']['observed'],row['SIG']['source'])
            if last_clock and any(a <= b for a,b in zip(clocks,last_clock)):
                counts['nonadvancing_quotes']+=1
                continue
            if last_clock and row['at']-last_clock[0] > float(p['max_quote_gap_seconds']):
                history=[]
            last_clock=clocks
            history=[h for h in history if row['at']-h['at'] <= float(p['momentum_window_seconds'])]
            if active:
                for path in active['paths'].values():advance(path,row,active['entry'],active['at'],p)
                if all(s['status']!='open' for s in active['paths'].values()):
                    episodes.append(active);active=None
                history.append(row)
                continue  # No exit and re-entry using the same displayed liquidity.
            c=entry(row,p,checks,entry_mode,history)
            history.append(row)
            if c:
                active=dict(exchange=exchange,snapshot_id=row['id'],at=row['at'],entry=c,
                            paths={policy:new_path(policy,c,row['at']) for policy in POLICIES})
        if active:
            for path in active['paths'].values():
                if path['status']=='open' and now-path['last_quote_at'] > float(p['max_quote_gap_seconds']):
                    path.update(status='censored_quote_gap',terminal_at=None)
            episodes.append(active)
    summary={}
    for policy in POLICIES:
        paths=[e['paths'][policy] for e in episodes]
        closed=[(e,e['paths'][policy]) for e in episodes if e['paths'][policy]['status']=='closed']
        summary[policy]=dict(statuses=dict(Counter(s['status'] for s in paths)),
            exit_reasons=dict(Counter(f['reason'] for s in paths for f in s['fills'])),
            first_filled_exit_reason=dict(Counter(s['fills'][0]['reason'] for s in paths if s['fills'])),
            target_fill_episodes=sum(any(f['reason']=='profit_target' for f in s['fills']) for s in paths),
            closed_episodes=len(closed),
            profitable_closed_episodes=sum(s['realized_pnl']>0 for e,s in closed),
            closed_episode_net_pnl=sum((s['realized_pnl'] for e,s in closed),D(0)),
            unresolved_realized_pnl=sum((s['realized_pnl'] for s in paths if s['status']!='closed'),D(0)),
            unresolved_entry_cost=sum((e['paths'][policy]['remaining']*(e['entry']['ask']+p['cost_buffer'])
                for e in episodes if e['paths'][policy]['status']!='closed'),D(0)),
            mean_closed_hold_seconds=(sum(s['terminal_at']-e['at'] for e,s in closed)/len(closed) if closed else None))
    return dict(episodes=episodes,episode_count=len(episodes),races=len({e['exchange'] for e in episodes}),
                summary=summary,counts=dict(counts),entry_side_checks=dict(checks))


def report(path, hours=24, now=None, overrides=None, entry_mode='external_gap'):
    now=time.time() if now is None else float(D(now));hours=float(D(hours));p=settings(overrides)
    if entry_mode not in (*ENTRY_MODES,'compare'):raise ValueError('Unknown entry mode')
    if not 0 < hours <= 720:raise ValueError('Hours must be positive and at most 720')
    result=dict(version=VERSION,entry_mode=entry_mode,research_only=True,live_orders=False,as_of=now,hours=hours,
        settings={k:str(v) for k,v in p.items()},status='no_journal',
        limitations=['Retrospective sampled-price scenarios; displayed depth does not guarantee fills.',
            'External-gap entries use reference bids; SIG momentum uses past SIG quotes only; neither uses the polling model.',
            'No inferred intraframe target/stop hits; gaps are censored, not zero returns.',
            'Only normal recorded scan coverage; reference outages/news pauses can hide opportunities.',
            'Per-exchange paired episodes, not a portfolio backtest or deployable profit total.',
            'Target/stop order within an unobserved interval is unknown; parameters are not calibrated.',
            'Lookback starts flat; retained history may be incomplete; exchange mappings are assumed unchanged.',
            'Closed-only outcomes omit unresolved risk; do not select a strategy from those profits alone.'])
    path=Path(path).resolve()
    if not path.exists():return result
    db=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)
    try:
        db.execute('BEGIN')
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='events'").fetchone():return result
        from .maintenance import retention_info
        result['event_retention']=retention_info(db)
        rows,counts=load_events(db,now-hours*3600,now)
        rows=[r for r in rows if r['at'] >= now-hours*3600]
    finally:db.close()
    result.update(status='ok',quote_events=counts)
    if entry_mode=='compare':
        result['studies']={mode:analyze(rows,now,p,mode) for mode in ENTRY_MODES}
        result['limitations'].append('Entry modes have different cohorts. These are separate scenarios, not paired portfolio returns.')
    else:
        result.update(analyze(rows,now,p,entry_mode))
    return result


def format_report(r):
    lines=['LOW-PRICE EXIT STUDY — simulated, no orders',
           f"Target: net {r['settings']['target_net_per_share']}/share or "
           f"{D(r['settings']['target_return'])*100}% on entry cost, whichever is larger.",
           'Costs, policy settings and full paths: add --json.']
    if r['status']!='ok':return '\n'.join(lines+[r['status']])
    if 'studies' in r:
        for mode,study in r['studies'].items():
            child={k:v for k,v in r.items() if k!='studies'}
            child.update(study,entry_mode=mode,limitations=[])
            lines.extend(format_report(child).splitlines()[3:])
        lines.extend(r['limitations'])
        return '\n'.join(lines)
    lines.append(f"Entry mode: {r.get('entry_mode','external_gap')}")
    lines.append(f"Paired entries: {r['episode_count']}; exchanges: {r['races']}")
    lines.append(f"Entry checks by side: {r['entry_side_checks']}")
    for policy,s in r['summary'].items():
        lines.append(f"{policy}: {s['statuses']}; closed-path net P&L {s['closed_episode_net_pnl']}; "
                     f"unresolved entry cost {s['unresolved_entry_cost']}; exits {s['exit_reasons']}")
    lines.extend(r['limitations'])
    return '\n'.join(lines)
