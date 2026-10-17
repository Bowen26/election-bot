"""Read-only paired prior experiment; recorded decisions, never executable orders."""
from collections import Counter, defaultdict
import json

from .fair_value import VERSION
from .fundamentals import VERSION as EXPERIMENT
from .leadership import future_quote, number
from .strategy import D

NAMES = ('neutral_prior', 'historical_lean')
HORIZONS = ((300, 120), (900, 180), (3600, 900))


def decimal(value):
    if isinstance(value, bool):
        raise ValueError('Boolean number')
    result = D(value)
    if not result.is_finite():
        raise ValueError('Nonfinite number')
    return result


def pair(event, start, at):
    """Require both forecasts to use identical contemporaneous polling evidence."""
    captured = number(event['captured_at'])
    if (event['exchange'] != start['exchange'] or
            not 0 <= at-start['at'] <= 5 or abs(captured-start['at']) > 5):
        raise ValueError('Snapshot binding/time mismatch')
    fee = decimal(event['cost_buffer_per_share'])
    if not 0 <= fee < 1:
        raise ValueError('Invalid buffer')
    forecasts, actions = [], []
    for name in NAMES:
        variant = event['prior_variants'][name]
        f = variant['forecast']
        if (f['version'] != VERSION or f['experiment'] != EXPERIMENT or
                f['prior_variant'] != name or f['status'] != 'estimated' or
                not f['base_race_hash'] or not f['race_key'] or
                number(f['as_of']) != captured):
            raise ValueError('Invalid paired forecast identity')
        probabilities = [decimal(f[k]) for k in ('yes_low', 'yes_probability', 'yes_high')]
        if not 0 <= probabilities[0] <= probabilities[1] <= probabilities[2] <= 1:
            raise ValueError('Invalid probabilities')
        if not f['weights'] or len({w['poll_id'] for w in f['weights']}) != len(f['weights']):
            raise ValueError('Missing/duplicated polling evidence')
        for w in f['weights']:
            if not w['hash'] or number(w['received_at']) > captured:
                raise ValueError('Future/unknown evidence')
        rows = variant['comparison']['model']
        if len(rows) != 2 or {r['side'] for r in rows} != {'yes', 'no'}:
            raise ValueError('Expected both recorded sides')
        eligible = []
        for r in rows:
            if type(r['eligible']) is not bool:
                raise ValueError('Invalid recorded eligibility')
            side = r['side']
            ask, depth = ((start['SIG']['ask'], start['SIG']['ask_size']) if side == 'yes'
                          else (1-start['SIG']['bid'], start['SIG']['bid_size']))
            if decimal(r['ask']) != ask or decimal(r['depth']) != depth:
                raise ValueError('Recorded entry quote mismatch')
            if r['eligible']:
                if depth < 1 or ask % D('.005') != 0:
                    raise ValueError('Nonexecutable one-share entry')
                eligible.append({'side':side, 'ask':ask})
        if len(eligible) > 1:
            raise ValueError('Ambiguous two-sided action')
        actions.append(eligible[0] if eligible else None)
        forecasts.append(f)
    a, b = forecasts
    for key in ('base_race_hash', 'race_key', 'as_of', 'policy', 'weights'):
        if a[key] != b[key]:
            raise ValueError('Unpaired input evidence')
    return {'race':a['race_key'], 'fee':fee, 'actions':actions,
            'probability_delta':decimal(b['yes_probability'])-decimal(a['yes_probability'])}


def summary(statuses, measured):
    result = {'statuses':dict(statuses), 'measured_pairs':len(measured)}
    for key in ('neutral', 'historical', 'difference'):
        result['mean_'+key+'_net_coins'] = str(sum((r[key] for r in measured), D(0))/len(measured)) if measured else None
    result.update(historical_better=sum(r['difference'] > 0 for r in measured),
                  neutral_better=sum(r['difference'] < 0 for r in measured),
                  ties=sum(r['difference'] == 0 for r in measured))
    return result


def analyze(db, quotes, since, now):
    """One prospective observation per race/window; no model re-estimation."""
    starts = {q['id']:q for q in quotes}
    by_exchange = defaultdict(list)
    for q in quotes:
        by_exchange[q['exchange']].append(q)
    for rows in by_exchange.values():
        rows.sort(key=lambda q:(q['at'], q['id']))
    stamps = {key:[q['at'] for q in rows] for key,rows in by_exchange.items()}
    counters, unique, conflicts = Counter(), {}, set()
    for at, body in db.execute("SELECT at,detail FROM events WHERE kind='fair_value_shadow' AND at>=? AND at<=? ORDER BY at,rowid", (since,now)):
        try:
            event = json.loads(body)
            if not isinstance(event,dict):
                raise ValueError('Invalid event object')
            if event['version'] != 1 or event['model_version'] != VERSION:
                counters['unsupported_version'] += 1
                continue
            sid = event['snapshot_id']
            if not isinstance(sid,str) or not sid:
                raise ValueError('Invalid snapshot id')
            if sid in unique:
                counters['duplicate_events'] += 1
                if unique[sid][1] != event:
                    conflicts.add(sid)
            else:
                unique[sid] = (at,event)
        except (ValueError, KeyError, TypeError):
            counters['malformed_events'] += 1
    counters['conflicting_snapshot_ids'] = len(conflicts)
    statuses, values = defaultdict(Counter), defaultdict(list)
    race_statuses, race_values = defaultdict(Counter), defaultdict(list)
    next_at, delta = {}, []
    decisions = Counter()
    for sid,(at,event) in sorted(unique.items(),key=lambda item:(item[1][0],item[0])):
        if sid in conflicts:
            continue
        variants = event.get('prior_variants',{})
        if not isinstance(variants,dict) or any(name not in variants for name in NAMES):
            counters['missing_pair'] += 1
            continue
        try:
            if any(not isinstance(variants[name],dict) or not isinstance(variants[name].get('forecast'),dict) for name in NAMES):
                raise ValueError('Invalid variant object')
            if any(variants[name]['forecast']['status'] != 'estimated' for name in NAMES):
                counters['unavailable_pair'] += 1
                continue
            start = starts.get(sid)
            if start is None:
                counters['unmatched_snapshot'] += 1
                continue
            p = pair(event,start,at)
        except (ValueError, KeyError, TypeError, ArithmeticError):
            counters['invalid_pair'] += 1
            continue
        counters['valid_pairs'] += 1
        delta.append(p['probability_delta'])
        a,b = p['actions']
        category = ('both_cash' if not a and not b else 'neutral_only' if not b else
                    'historical_only' if not a else 'same_side' if a['side']==b['side'] else 'opposite_sides')
        decisions[category] += 1
        race = p['race']
        for horizon,window in HORIZONS:
            key = str(horizon)
            rk = (key,race)
            if start['at'] < next_at.get(rk, float('-inf')):
                statuses[key]['thinned'] += 1
                race_statuses[rk]['thinned'] += 1
                continue
            # Schedule before looking at outcomes, including cash/missing/thin samples.
            next_at[rk] = start['at']+horizon+window
            status = 'both_cash'
            if a or b:
                future = future_quote(by_exchange[start['exchange']], stamps[start['exchange']],start,horizon,window)
                if future is None:
                    status = 'pending' if now < start['at']+horizon+window else 'missing'
                else:
                    profits = []
                    for action in (a,b):
                        if action is None:
                            profits.append(D(0))
                            continue
                        bid,depth = ((future['SIG']['bid'],future['SIG']['bid_size']) if action['side']=='yes'
                                     else (1-future['SIG']['ask'],future['SIG']['ask_size']))
                        if depth < 1:
                            break
                        profits.append(bid-action['ask']-2*p['fee'])
                    if len(profits) != 2:
                        status = 'insufficient_exit_depth'
                    else:
                        status = 'measured'
                        value = dict(neutral=profits[0], historical=profits[1],difference=profits[1]-profits[0])
                        values[key].append(value)
                        race_values[rk].append(value)
            statuses[key][status] += 1
            race_statuses[rk][status] += 1
    return {'experiment':EXPERIMENT, 'research_only':True, 'coverage':dict(counters),
            'decisions':dict(decisions),
            'mean_recorded_yes_probability_shift':str(sum(delta,D(0))/len(delta)) if delta else None,
            'horizons':{key:{**summary(status,values[key]), 'by_race':{
                race:summary(rs,race_values[(key,race)]) for (h,race),rs in race_statuses.items() if h==key}}
                for key,status in statuses.items()},
            'limitations':[
                'One share or cash per recorded policy on the same snapshot and polling evidence; no live orders.',
                'Equal starting cash sufficient for either action; unused cash has zero return. Two cost buffers per trade.',
                'Both-cash observations are counted separately, not treated as profitable measured trades.',
                'First valid future SIG quote only; thin, missing and pending outcomes are not zero profits.',
                'At most one observation per race per horizon plus its outcome window; races and days remain correlated.',
                'No fills, queue/latency simulation, capital recycling, settlement calibration or portfolio replay.',
                'Uses recorded eligibility, not current thresholds or re-estimated past forecasts; no automatic promotion.']}
