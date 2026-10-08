"""Explicit, fingerprint-bound basis-risk reviews for optional sibling routing."""
from collections import defaultdict
import time
from urllib.parse import urlsplit

from .clients import fingerprint, iso_time
from .strategy import D

PARTIES = ('Democratic Party', 'Republican Party')


def review_fields(mapping, settings, now=None):
    review = mapping.get('settlement_review')
    if not isinstance(review, dict) or review.get('decision') != 'accept_basis_risk':
        raise ValueError('Sibling contract needs an explicit accept_basis_risk settlement review')
    if review.get('version') != 1 or type(review.get('version')) is not int:
        raise ValueError('Unsupported settlement review version')
    if (review.get('contract_fingerprint') != mapping.get('contract_fingerprint') or
            review.get('race_key') != mapping.get('race_key') or review.get('party') not in PARTIES):
        raise ValueError('Settlement review does not match the pinned contract/race/party')
    if review.get('full_rules_reviewed') is not True:
        raise ValueError('Full rules review is required; summary fields alone are insufficient')
    stamp = iso_time(review.get('reviewed_at'))
    if not 0 < stamp <= (time.time() if now is None else now):
        raise ValueError('Settlement review date is invalid or in the future')
    for key in ('rationale', 'entry_edge_rationale'):
        if not isinstance(review.get(key), str) or not review[key].strip():
            raise ValueError('Settlement review requires ' + key)
    sources = review.get('rule_sources')
    if not isinstance(sources, dict) or set(sources) != {'SIG', 'Kalshi', 'Polymarket'}:
        raise ValueError('Settlement review needs rule sources for all three venues')
    for url in sources.values():
        if not isinstance(url, str):
            raise ValueError('Rule sources must be HTTPS URLs')
        parsed = urlsplit(url)
        if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError('Rule sources must be HTTPS URLs without credentials')
    flags = review.get('acknowledged_flags')
    if (not isinstance(flags, list) or any(not isinstance(f, str) or not f for f in flags)
            or len(flags) != len(set(flags))):
        raise ValueError('Settlement review requires distinct acknowledged flag codes')
    value = review.get('additional_entry_edge')
    if isinstance(value, bool) or value is None:
        raise ValueError('An explicit additional_entry_edge is required')
    try:
        extra = D(value)
    except (ValueError, TypeError, ArithmeticError):
        raise ValueError('Invalid additional_entry_edge') from None
    if extra < 0 or D(settings['minimum_edge']) + extra >= 1:
        raise ValueError('Additional entry edge must be nonnegative and keep total edge below one')
    return review


def multi_races(config):
    groups = defaultdict(list)
    for mapping in config['markets']:
        if mapping.get('race_key'):
            groups[mapping['race_key']].append(mapping)
    return {race: rows for race, rows in groups.items() if len(rows) > 1}


def validate_multicontract_config(config):
    mode = config.get('execution', {}).get('multi_contract_races', False)
    if type(mode) is not bool:
        raise ValueError('multi_contract_races must be boolean')
    reviewed = [m for m in config['markets'] if m.get('enabled') and m.get('exposure_mode') == 'gross']
    if not mode and not reviewed:
        return
    if not config['execution'].get('enabled'):
        raise ValueError('Multi-contract races require ActiveEngine')
    for field in ('net_shares_total', 'net_shares_per_office', 'net_shares_per_region', 'realized_loss_stop_fraction'):
        if field not in config['limits']:
            raise ValueError('Multi-contract races require ' + field)
    for mapping in reviewed:
        review_fields(mapping, config['strategy'])
    # Every enabled row needs an explicit race key; otherwise grouping is ambiguous.
    for mapping in config['markets']:
        if mapping.get('enabled') and not mapping.get('race_key'):
            raise ValueError('Multi-contract mode requires explicit race_key on enabled mappings')
    for race, rows in multi_races(config).items():
        parts = race.split(':')
        if len(parts) != 3 or parts[0] != '2026' or parts[1] not in ('house', 'senate', 'governor'):
            raise ValueError('Unsupported multi-contract race_key')
        if any(m.get('office') != parts[1] for m in rows):
            raise ValueError('Sibling office must match race_key')
        geography = {(m.get('office'), m.get('state_code'), m.get('region')) for m in rows}
        if len(geography) != 1:
            raise ValueError('Sibling contracts must share office/state/region')
        parties = set()
        for mapping in rows:
            if mapping.get('exposure_mode') != 'gross' or 'exposure_sign' in mapping:
                raise ValueError('All sibling mappings, including disabled ones, require gross exposure without exposure_sign')
            if mapping.get('enabled'):
                review = review_fields(mapping, config['strategy'])
                if review['party'] in parties:
                    raise ValueError('Duplicate party contract in a race')
                parties.add(review['party'])
                if config.get('news', {}).get('enabled') and mapping.get('news_match') != rows[0].get('news_match'):
                    raise ValueError('Sibling mappings must share their news matching rules')


def verify_review(mapping, record, settings):
    review = review_fields(mapping, settings)
    if fingerprint(record) != review['contract_fingerprint']:
        raise ValueError('Settlement evidence changed; a new review is required')
    # Imported lazily to avoid the report/risk dependency cycle.
    from .contract_review import assess
    assessment = assess(record, mapping, review['party'])
    if not assessment['structural_identity_verified']:
        raise ValueError('Settlement review cannot override failed structural identity')
    flags = {f['code'] for f in assessment['flags']}
    if flags != set(review['acknowledged_flags']):
        raise ValueError('Settlement review flags differ from current evidence')
    return assessment


def entry_settings(mapping, settings):
    result = dict(settings)
    review = review_fields(mapping, settings)
    result['minimum_edge'] = str(D(settings['minimum_edge']) + D(review['additional_entry_edge']))
    return result
