"""Source-backed historical-lean experiment, without a broker or live signal."""
from datetime import datetime, timezone
from copy import deepcopy
from pathlib import Path
import time

from .polling import PollStore, digest, read_input, timestamp
from .state import exclusive_lock

VERSION = 'historical_lean_v1'
POLICY = {'years': [2020, 2024], 'cycle_weights': [.5, .5],
          'lean_coefficient': .5, 'prior_sd_pp': 12,
          'national_environment_pp': 0, 'incumbency_adjustment_pp': 0,
          'calibrated': False}
DATA_PATH = Path(__file__).resolve().parent.parent/'data/research/fec-presidential-lean.json'
EXPANDED_DATA_PATH = DATA_PATH.with_name('fec-presidential-lean-expanded.json')
EXPANDED_STATES = ('MI', 'OH', 'TX')


def reviewed_data(state):
    # Keep the original dataset bytes/hash for previously imported NH/IA/NC priors.
    # Additive coverage must not invalidate stored forecasts or refresh availability.
    return read_input(EXPANDED_DATA_PATH if state in EXPANDED_STATES else DATA_PATH)


def margin(votes):
    if (set(votes) != {'dem', 'rep'} or
            any(type(n) is not int or not 0 < n < 500_000_000 for n in votes.values())):
        raise ValueError('Historical counts must be positive integer D/R votes')
    return 100*(votes['dem']-votes['rep'])/(votes['dem']+votes['rep'])


def build_prior(state, available_at, data=None):
    """A deliberately weak, uncalibrated transfer from presidential to Senate lean."""
    data = reviewed_data(state) if data is None else data
    timestamp(available_at)
    if data.get('schema_version') != 1 or state not in data['states']:
        raise ValueError('Unsupported historical state data')
    elections = data['elections']
    if [e['year'] for e in elections] != POLICY['years']:
        raise ValueError('Expected exactly the reviewed 2020 and 2024 cycles')
    components = []
    for election in elections:
        year = election['year']
        url = f'https://www.fec.gov/resources/cms-content/documents/{year}presgeresults.pdf'
        if election['source_url'] != url:
            raise ValueError('Unexpected historical source')
        state_votes, national_votes = election['states'][state], election['national']
        state_margin, national_margin = margin(state_votes), margin(national_votes)
        if any(state_votes[k] > national_votes[k] for k in ('dem', 'rep')):
            raise ValueError('State votes exceed national votes')
        components.append(dict(year=year, state_votes=state_votes, national_votes=national_votes,
            state_two_party_margin_pp=state_margin, national_two_party_margin_pp=national_margin,
            relative_lean_pp=state_margin-national_margin, source_url=url,
            dem_page=election['dem_page'], rep_page=election['rep_page']))
    lean = sum(w*row['relative_lean_pp'] for w,row in zip(POLICY['cycle_weights'],components))
    return dict(version=VERSION, state=state, dataset_hash=digest(data), policy=deepcopy(POLICY),
        margin_pp=POLICY['lean_coefficient']*lean, sd_pp=POLICY['prior_sd_pp'],
        published_at=available_at, publication_time_basis='first_local_import_of_this_historical_prior',
        source_url=components[-1]['source_url'], components=components,
        average_relative_lean_pp=lean,
        method='Half the equally weighted 2020/2024 state-minus-national two-party D-R margin; '
               '12-point prior SD. Transfer coefficient is an assumption, not fitted. '
               'National environment and incumbency are neutral placeholders, not observations.')


def validate_prior(prior, race_key):
    parts = race_key.split(':')
    if len(parts) != 3 or parts[:2] != ['2026', 'senate']:
        raise ValueError('Historical-lean experiment currently supports 2026 Senate bindings only')
    expected = build_prior(parts[2], prior['published_at'])
    if prior != expected:
        raise ValueError('Historical prior does not match its reviewed inputs and fixed policy')
    return prior


def seed(runtime, config, now=None):
    """Add the alternative prior only; preserve configured primary/custom priors."""
    from .votehub import bindings
    now = time.time() if now is None else now
    with exclusive_lock(Path(runtime)/'poll-collector'):
        store = PollStore(Path(runtime)/'fair-value.sqlite3')
        try:
            last = store.db.execute('SELECT MAX(received) FROM evidence').fetchone()[0]
            if last is not None and last > now:
                raise ValueError('Evidence clock is ahead of import; no backdating allowed')
            old = {r['sig_exchange_id']: r for r in store.latest('race', now)}
            races, skipped = [], []
            for bound in bindings(config):
                state = bound['race_key'].split(':')[2]
                historical_data = reviewed_data(state)
                if state not in historical_data['states']:
                    skipped.append(dict(exchange=bound['sig_exchange_id'],
                                        reason='historical_state_not_reviewed'))
                    continue
                prior = old.get(bound['sig_exchange_id'])
                identity = ('race_key', 'contract_fingerprint', 'yes_party', 'dem_candidate', 'rep_candidate', 'election_at')
                if prior and any(prior[k] != bound[k] for k in identity):
                    skipped.append(dict(exchange=bound['sig_exchange_id'], reason='binding_changed_requires_review'))
                    continue
                race = {k:v for k,v in (prior or bound).items() if not k.startswith('_')}
                candidate = build_prior(state, datetime.fromtimestamp(now,timezone.utc).isoformat(), historical_data)
                previous = race.get('fundamentals_prior')
                if previous:
                    expected = dict(candidate, published_at=previous.get('published_at'))
                    if previous == expected:
                        candidate = previous  # Duplicate import cannot refresh availability.
                race['fundamentals_prior'] = candidate
                races.append(race)
            result = store.ingest(dict(schema_version=1,races=races,polls=[]),config,now)
            return dict(result, bound_contracts=len(races), skipped=skipped,
                        historical_states=sorted({r['fundamentals_prior']['state'] for r in races}),
                        research_only=True, live_model_orders=False, policy=deepcopy(POLICY))
        finally:
            store.close()


def variants(race, polls, asof):
    """Paired counterfactuals on identical polling inputs, known at prediction time."""
    from .fair_value import estimate
    prior = race.get('fundamentals_prior')
    if not prior:
        return {}
    validate_prior(prior, race['race_key'])
    if race['_received_at'] > asof or timestamp(prior['published_at']) > asof:
        return {}  # Newly installed history must not manufacture past forecasts.
    result = {}
    for name, value in (('neutral_prior', None), ('historical_lean', prior)):
        alternate = dict(race, prior=value)
        alternate['_hash'] = digest(dict(binding_hash=race['_hash'], prior_variant=name, prior=value))
        forecast = estimate(alternate, polls, asof)
        forecast.update(prior_variant=name, experiment=VERSION, base_race_hash=race['_hash'])
        result[name] = forecast
    return result
