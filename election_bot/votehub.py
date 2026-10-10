"""Conservative VoteHub adapter. Produces research evidence, never orders.

Attribution: VoteHub (https://votehub.com/polls/api/), CC BY 4.0.
Candidate bindings are reviewed; individual provider figures are not source-audited.
"""
from collections import defaultdict
from datetime import datetime, time as day_time, timezone
import re
from zoneinfo import ZoneInfo

from .polling import digest, number, source_url, text, validate_poll

ATTRIBUTION = 'VoteHub (https://votehub.com/polls/api/), CC BY 4.0; normalized for research'
CATALOG = {
    '2026 New Hampshire': dict(state='NH', dem='Chris Pappas', rep='John E. Sununu',
        dem_aliases=('Chris Pappas',), rep_aliases=('John Sununu', 'John E. Sununu'),
        zone='America/New_York', source='https://scholars.unh.edu/survey_center_polls/1005/'),
    '2026 Iowa': dict(state='IA', dem='Josh Turek', rep='Ashley Hinson',
        dem_aliases=('Josh Turek',), rep_aliases=('Ashley Hinson',), zone='America/Chicago',
        source='https://emersoncollegepolling.com/iowa-2026-poll-hinson-leads-turek/'),
    '2026 North Carolina': dict(state='NC', dem='Roy Cooper', rep='Michael Whatley',
        dem_aliases=('Roy Cooper',), rep_aliases=('Michael Whatley',), zone='America/New_York',
        source='https://surveyresearch-ecu.reportablenews.com/pr/ecu-poll-cooper-s-lead-holds-at-7-points'),
}
# New labels require panel-family review instead of manufacturing independence.
POLLSTERS = {name.casefold(): family for name, family in (
    ('YouGov', 'yougov'), ('Emerson College', 'emerson college'),
    ('The New York Times/Siena University', 'siena university'),
    ('University of New Hampshire', 'university of new hampshire'),
    ('East Carolina University', 'east carolina university'),
    ('Harper Polling', 'harper polling'), ('InsiderAdvantage', 'insideradvantage'),
    ('High Point University Survey Research Center', 'high point university'),
    ('Big Data Poll', 'big data poll'), ('Marist University', 'marist'),
    ('Beacon Research/Shaw & Co. Research', 'beacon research/shaw & co. research'),
    ('CNN/SSRS', 'ssrs'), ('co/efficient', 'co/efficient'))}


def iso(at):
    return datetime.fromtimestamp(at, timezone.utc).isoformat()


def date_only(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
        raise ValueError('invalid_calendar_date')
    return datetime.strptime(value, '%Y-%m-%d').date()


def bindings(config):
    result = []
    for spec in CATALOG.values():
        for m in config['markets']:
            if (m.get('enabled') is not True or m.get('office') != 'senate'
                    or m.get('race_key') != '2026:senate:' + spec['state']
                    or type(m.get('exposure_sign')) is not int or m['exposure_sign'] not in (-1, 1)):
                continue
            result.append(dict(sig_exchange_id=m['sig_exchange_id'],
                contract_fingerprint=m['contract_fingerprint'], race_key=m['race_key'],
                yes_party='DEM' if m['exposure_sign'] == 1 else 'REP',
                dem_candidate=spec['dem'], rep_candidate=spec['rep'],
                election_at=datetime(2026, 11, 3, tzinfo=ZoneInfo(spec['zone'])).isoformat(),
                contest_type='general_plurality', source_url=spec['source'], prior=None,
                notes='Research candidate binding only; not a live settlement attestation.'))
    return result


def normalize(row, available_races, now):
    spec = CATALOG.get(row.get('subject'))
    if not spec or row.get('poll_type') != 'us-senator' or row.get('seat_name') is not None:
        raise ValueError('unreviewed_race_or_stage')
    race = '2026:senate:' + spec['state']
    if race not in available_races:
        raise ValueError('no_enabled_bound_contract')
    if row.get('internal') is not False or 'partisan' not in row or row['partisan'] is not None:
        raise ValueError('internal_partisan_or_unknown_flags')
    pollster = POLLSTERS.get(text(row['pollster']).casefold())
    if not pollster:
        raise ValueError('unreviewed_pollster_family')
    zone = ZoneInfo(spec['zone'])
    start, end, created = (date_only(row[k]) for k in ('start_date', 'end_date', 'created_at'))
    if created > datetime.fromtimestamp(now, zone).date() or created < end:
        raise ValueError('invalid_provider_creation_date')
    start = datetime.combine(start, day_time.min, zone)
    end = datetime.combine(end, day_time(23, 59, 59), zone)
    if end.timestamp() > now or start > end or (now-end.timestamp()) > 46*86400:
        raise ValueError('future_stale_or_reversed_fieldwork')
    answers = row['answers']
    if not isinstance(answers, list) or not 2 <= len(answers) <= 20:
        raise ValueError('invalid_answers')
    values = {}
    for answer in answers:
        choice = text(answer['choice']).casefold()
        if choice in values:
            raise ValueError('duplicate_answer')
        values[choice] = number(answer['pct'], 0, 100)
    if sum(values.values()) > 100.5:
        raise ValueError('answer_total_exceeds_100_5')
    shares = []
    for aliases in (spec['dem_aliases'], spec['rep_aliases']):
        found = [values[a.casefold()] for a in aliases if a.casefold() in values]
        if len(found) != 1:
            raise ValueError('candidate_identity_missing_or_ambiguous')
        shares.append(found[0])
    poll_id = 'votehub:' + text(row['id'])
    return validate_poll(dict(poll_id=poll_id, survey_id=poll_id, pollster=pollster,
        race_key=race, dem_candidate=spec['dem'], rep_candidate=spec['rep'], stage='general',
        population=row['population'], partisan=None, sample_size=row['sample_size'],
        field_start=start.isoformat(), field_end=end.isoformat(), published_at=iso(now),
        publication_time_basis='first_observed_provider_version; not original publication time',
        dem_pct=shares[0], rep_pct=shares[1], other_pct=max(0, 100-sum(shares)),
        other_pct_basis='residual_upper_bound_including_undecided; not measured third-party support',
        methodology='VoteHub structured LV/RV figures; poll mode not independently verified. '
                    'Raw shares retained. Missing residual is conservatively treated as other/undecided.',
        source_url=source_url(row['url']), provider='votehub', provider_created_date=row['created_at'],
        provider_row_hash=digest(row), attribution=ATTRIBUTION, withdrawn=False))


def adapt(rows, config, previous, now):
    """Quarantine ambiguity, favor LV over RV, withdraw newly invalid revisions."""
    races = bindings(config)
    available = {r['race_key'] for r in races}
    candidates, rejected, seen, duplicate_ids = {}, [], set(), set()

    def reject(pid, reason):
        rejected.append(dict(poll_id=pid, reason=reason))

    for row in rows:
        pid = 'votehub:' + row['id'] if isinstance(row, dict) and isinstance(row.get('id'), str) else None
        try:
            if not isinstance(row, dict):
                raise ValueError('invalid_poll_row')
            if pid in seen:
                # Identical repeats can collapse; conflicting versions of one ID cannot.
                if pid in candidates and candidates[pid]['provider_row_hash'] == digest(row):
                    continue
                duplicate_ids.add(pid)
                raise ValueError('conflicting_provider_id')
            seen.add(pid)
            p = normalize(row, available, now)
            old = previous.get(pid)
            if old and any(old[k] != p[k] for k in ('pollster', 'survey_id', 'race_key')):
                raise ValueError('provider_changed_immutable_identity')
            if old and not old.get('withdrawn') and old.get('provider_row_hash') == p['provider_row_hash']:
                p['published_at'] = old['published_at']
            candidates[pid] = p
        except (ValueError, KeyError, TypeError, OverflowError) as error:
            reject(pid, str(error)[:200])
    for pid in duplicate_ids:
        candidates.pop(pid, None)
    # Repeated releases/subsamples from the same pollster and fieldwork are one survey.
    groups = defaultdict(list)
    for p in candidates.values():
        groups[(p['race_key'], p['pollster'], p['field_start'], p['field_end'])].append(p)
    for group in groups.values():
        preferred = [p for p in group if p['population'] == 'lv'] or group
        signatures = {tuple(p[k] for k in ('population', 'sample_size', 'dem_pct', 'rep_pct', 'other_pct')) for p in preferred}
        winner = min(p['poll_id'] for p in preferred) if len(signatures) == 1 else None
        for p in group:
            if p['poll_id'] != winner:
                candidates.pop(p['poll_id'], None)
                reject(p['poll_id'], 'conflicting_survey_variants' if winner is None else 'duplicate_or_superseded_subsample')
    # An invalid revision must not leave a previously accepted number active.
    withdrawals = []
    for pid in {r['poll_id'] for r in rejected} - candidates.keys():
        old = previous.get(pid)
        if old and old.get('provider') == 'votehub' and not old.get('withdrawn'):
            p = {k: v for k, v in old.items() if not k.startswith('_')}
            p.update(withdrawn=True, withdrawal_reason='Provider revision or survey conflict quarantined')
            withdrawals.append(p)
    return dict(schema_version=1, races=races, polls=list(candidates.values())+withdrawals), rejected
