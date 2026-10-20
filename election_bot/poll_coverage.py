"""Read-only polling coverage diagnosis per enabled Senate contract; no network or writes."""
from collections import Counter, defaultdict
import json
from pathlib import Path

from .fair_value import estimate
from .polling import digest
from .votehub import CATALOG, EXCLUDED, adapt

STATES = dict(AL='Alabama', AK='Alaska', AR='Arkansas', CO='Colorado', DE='Delaware', GA='Georgia',
    IA='Iowa', ID='Idaho', IL='Illinois', KS='Kansas', KY='Kentucky', LA='Louisiana', MA='Massachusetts',
    ME='Maine', MI='Michigan', MN='Minnesota', MS='Mississippi', MT='Montana', NC='North Carolina',
    NE='Nebraska', NH='New Hampshire', NJ='New Jersey', NM='New Mexico', OH='Ohio', OK='Oklahoma',
    OR='Oregon', RI='Rhode Island', SC='South Carolina', SD='South Dakota', TN='Tennessee', TX='Texas',
    VA='Virginia', WV='West Virginia', WY='Wyoming', FL='Florida')


def replay(raw_rows, config, received):
    """Run the current adapter on staged rows and estimate both tiers, as of receipt."""
    evidence, rejected = adapt(raw_rows, config, {}, received)
    polls = [dict(p, _received_at=received, _hash=digest(p)) for p in evidence['polls']]
    races = {r['race_key']: dict(r, _received_at=received, _hash=digest(r)) for r in evidence['races']}
    return races, polls, rejected


def report(runtime, config):
    runtime = Path(runtime)
    status_path = runtime/'poll-collector'/'status.json'
    result = {'status': 'not_collected', 'research_only': True, 'contracts': {}}
    if not status_path.exists():
        return result
    status = json.loads(status_path.read_text())
    raw_file = status.get('raw_file')
    if status.get('status') != 'ok' or not raw_file or not Path(raw_file).exists():
        result['status'] = 'no_successful_collection'
        return result
    staged = json.loads(Path(raw_file).read_text())
    received = staged['received_at']
    rows = staged['raw']['us-senator']
    races, polls, rejected = replay(rows, config, received)
    by_id = {('votehub:'+r['id']) if isinstance(r, dict) and isinstance(r.get('id'), str) else None: r
             for r in rows}
    subjects = {spec['state']: name for name, spec in CATALOG.items()}
    reasons = defaultdict(Counter)
    for item in rejected:
        row = by_id.get(item['poll_id']) or {}
        reasons[row.get('subject', 'unknown')][item['reason']] += 1
    rows_by_subject = Counter(r.get('subject') for r in rows if isinstance(r, dict))
    for m in config['markets']:
        if m.get('enabled') is not True or m.get('office') != 'senate':
            continue
        key, state = m.get('race_key'), m.get('state_code') or str(m.get('race_key', '')).rsplit(':', 1)[-1]
        subject = subjects.get(state, '2026 ' + STATES.get(state, state))
        entry = {'name': m['name'], 'provider_rows': rows_by_subject.get(subject, 0), 'binding': 'unbound'}
        if key in races:
            race = races[key]
            entry['binding'] = 'research_only' if race.get('live_model_eligible') is False else 'live_and_research'
            mine = [p for p in polls if p['race_key'] == key and not p.get('withdrawn')]
            live = [p for p in mine if p.get('live_model_eligible', True)]
            entry.update(accepted_polls=len(mine), live_tier_polls=len(live),
                         research_reasons=dict(Counter(r for p in mine for r in p.get('research_reasons', []))),
                         rejections=dict(reasons.get(subject, {})))
            research = estimate(race, mine, received)
            entry['research_estimate'] = {k: research.get(k) for k in
                ('status', 'reason', 'pollster_count', 'excluded', 'yes_probability')}
            if entry['binding'] == 'live_and_research':
                live_result = estimate(race, live, received)
                entry['live_estimate'] = {k: live_result.get(k) for k in
                    ('status', 'reason', 'pollster_count', 'excluded', 'yes_probability')}
        elif state in EXCLUDED:
            entry['binding'] = 'excluded:' + EXCLUDED[state]
        elif entry['provider_rows']:
            entry.update(binding='unbound:needs_candidate_review', rejections=dict(reasons.get(subject, {})))
        result['contracts'][key] = entry
    result.update(status='ok', staged_received_at=received, raw_file=raw_file,
                  senate_rows=len(rows), note=('Replays the current adapter on the latest staged feed. '
                  'A running watcher uses the code it started with until it is restarted.'))
    return result


def format_report(result):
    lines = ['POLLING COVERAGE — research diagnosis; no orders']
    if result['status'] != 'ok':
        return '\n'.join(lines+[result['status']])
    lines.append(f"Staged Senate rows: {result['senate_rows']}. {result['note']}")
    lines.append('Contract   Binding                               Rows  Accepted(live)  Research estimate            Live estimate')
    for key, e in sorted(result['contracts'].items()):
        def show(est):
            if not est:
                return '—'
            if est['status'] == 'estimated':
                return f"estimated p={est['yes_probability']:.3f} ({est['pollster_count']} pollsters)"
            return f"{est['reason']} ({est.get('pollster_count', 0)})"
        accepted = f"{e.get('accepted_polls', '—')}({e.get('live_tier_polls', '—')})"
        lines.append(f"{key.rsplit(':', 1)[-1]:10} {e['binding']:37} {e['provider_rows']:4}  {accepted:14}  "
                     f"{show(e.get('research_estimate')):28} {show(e.get('live_estimate'))}")
    lines.append('material_other_candidate_share = residual undecided/other above the unchanged 5-point cap.')
    lines.append('Use --json for rejection reasons and per-race exclusions.')
    return '\n'.join(lines)
