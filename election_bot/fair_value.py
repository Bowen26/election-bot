"""Independent, uncalibrated polling research. Never returns an executable order."""
from collections import Counter
from math import erf, sqrt
import time

from .polling import digest, timestamp
from .strategy import D, choose

VERSION = 'poll_margin_v1'
POLICY = dict(max_poll_age_days=45, fresh_poll_days=21, half_life_days=14,
              min_pollsters=2, max_sample_size=2000, design_effect=1.5,
              pollster_error_pp=3, common_error_pp=4, future_error_pp_per_sqrt_day=.15,
              sensitivity_shift_pp=2, neutral_prior_sd_pp=12, min_edge=.05,
              max_other_pct=5, rv_weight=.75, unknown_sponsorship_weight=.5, moe_z=1.96)


def cdf(value):
    return (1+erf(value/sqrt(2)))/2


def estimate(race, polls, asof=None):
    asof = time.time() if asof is None else asof
    base = {'version':VERSION,'calibrated':False,'as_of':asof,'race_key':race['race_key'],
            'policy':POLICY,'status':'unavailable','excluded':{},
            'scope':'General plurality, two leading parties; independent polling research, not a settlement guarantee.'}
    if race['_received_at']>asof:
        return dict(base,reason='race_not_known_at_prediction_time')
    election=timestamp(race['election_at'])
    if election<=asof:
        return dict(base,reason='election_started_or_passed')
    prior=race.get('prior')
    if prior and timestamp(prior['published_at'])>asof:
        return dict(base,reason='prior_not_published')
    exclusions=Counter(); by_pollster={}
    for p in polls:
        if p['race_key']!=race['race_key']:continue
        reason=None
        age=(asof-timestamp(p['field_end']))/86400
        if p['_received_at']>asof or timestamp(p['published_at'])>asof:reason='not_available_at_prediction_time'
        elif p.get('withdrawn'):reason='withdrawn'
        elif any(p[k].casefold()!=race[k].casefold() for k in ('dem_candidate','rep_candidate')):reason='candidate_mismatch'
        elif not 0<=age<=POLICY['max_poll_age_days']:reason='stale_or_future_fieldwork'
        elif p['other_pct']>POLICY['max_other_pct']:reason='material_other_candidate_share'
        if reason:exclusions[reason]+=1;continue
        # One latest release per pollster avoids treating tracking waves/subsamples as independent.
        rank=(timestamp(p['field_end']),p['population']=='lv',timestamp(p['published_at']),p['_received_at'],p['_hash'])
        old=by_pollster.get(p['pollster'])
        if old is None or rank>old[0]:by_pollster[p['pollster']]=(rank,p)
    selected=[x[1] for x in by_pollster.values()]
    base.update(excluded=dict(exclusions),pollster_count=len(selected),poll_ids=[p['poll_id'] for p in selected])
    if len(selected)<POLICY['min_pollsters']:
        return dict(base,reason='insufficient_independent_pollsters')
    youngest=min((asof-timestamp(p['field_end']))/86400 for p in selected)
    if youngest>POLICY['fresh_poll_days']:
        return dict(base,reason='no_recent_poll')
    mean0=prior['margin_pp'] if prior else 0
    sd0=prior['sd_pp'] if prior else POLICY['neutral_prior_sd_pp']
    precision=1/sd0**2; weighted=mean0*precision; weights=[]
    for p in selected:
        age=(asof-timestamp(p['field_end']))/86400
        dem,rep=p['dem_pct']/100,p['rep_pct']/100
        sampling=10000*(dem+rep-(dem-rep)**2)/min(p['sample_size'],POLICY['max_sample_size'])
        sampling=POLICY['design_effect']*sampling
        if p.get('reported_moe_pp') is not None:
            sampling=max(sampling,(2*p['reported_moe_pp']/POLICY['moe_z'])**2)
        variance=sampling+POLICY['pollster_error_pp']**2
        weight=2**(-age/POLICY['half_life_days'])/variance
        if p['population']=='rv':weight*=POLICY['rv_weight']
        if p['partisan'] is None:weight*=POLICY['unknown_sponsorship_weight']
        precision+=weight;weighted+=weight*(p['dem_pct']-p['rep_pct'])
        weights.append({'poll_id':p['poll_id'],'hash':p['_hash'],'weight':weight,'age_days':age,'sponsorship_unknown':p['partisan'] is None,
                        'source_url':p['source_url'],'received_at':p['_received_at'],
                        'published_at':p['published_at'],'field_end':p['field_end']})
    margin=weighted/precision
    days=(election-asof)/86400
    sd=sqrt(1/precision+POLICY['common_error_pp']**2+days*POLICY['future_error_pp_per_sqrt_day']**2)
    sign=1 if race['yes_party']=='DEM' else -1
    mean=sign*margin
    probability=cdf(mean/sd)
    low=cdf((mean-POLICY['sensitivity_shift_pp'])/sd)
    high=cdf((mean+POLICY['sensitivity_shift_pp'])/sd)
    evidence={'version':VERSION,'policy':POLICY,'race_hash':race['_hash'],
              'polls':sorted(p['_hash'] for p in selected),'as_of':asof}
    return dict(base,status='estimated',estimate_id=digest(evidence),race_hash=race['_hash'],
                yes_probability=probability,yes_low=low,yes_high=high,
                range_kind='plus/minus two margin-point sensitivity; NOT a confidence interval',
                dem_margin_pp=margin,predictive_sd_pp=sd,weights=weights,
                prior_basis=prior or {'method':'neutral weak prior; fundamentals not yet supplied',
                                       'margin_pp':0,'sd_pp':sd0})


def compare(forecast, book, refs, strategy):
    result={'model':[],'market':None,'market_reason':None,'research_only':True,
            'portfolio_simulation':False,'sizing':'One-share price ideas; ignores account/budget/news/cooldown eligibility'}
    book.check(strategy['max_age_seconds'],venue='SIG')
    # Market comparison can fail independently; that must not erase the model estimate.
    try:
        signal=choose(book,refs,strategy,1000000)
        result['market']={'side':signal.side,'price':str(signal.price),'edge':str(signal.edge)} if signal else None
        result['market_reason']='eligible_price_gap' if signal else 'no_price_gap'
    except ValueError as error:result['market_reason']=str(error)
    if forecast['status']!='estimated':return result
    fee=D(strategy['cost_buffer_per_share']); threshold=max(D(POLICY['min_edge']),D(strategy['minimum_edge']))
    for side,target,value,conservative in (
            ('yes',book,forecast['yes_probability'],forecast['yes_low']),
            ('no',book.complement(),1-forecast['yes_probability'],1-forecast['yes_high'])):
        ask,depth=target.asks[0]
        edge=D(value)-ask-fee; robust=D(conservative)-ask-fee
        eligible=robust>=threshold and depth>=1 and ask%D('.005')==0
        result['model'].append({'side':side,'ask':str(ask),'depth':str(depth),'fair_value':value,
            'edge':str(edge),'sensitivity_edge':str(robust),'eligible':eligible,
            'reason':'research_candidate' if eligible else 'insufficient_edge_or_depth_or_tick'})
    return result
