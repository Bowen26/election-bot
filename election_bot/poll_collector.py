"""Hourly public polling collection isolated from the trading thread and journal."""
from collections import Counter
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import urllib.parse
import urllib.request

from .clients import NoRedirect
from .polling import MAX_BYTES, PollStore, digest
from .state import exclusive_lock
from .votehub import ATTRIBUTION, adapt

OFFICES = ('us-senator', 'governor', 'us-representative')
INTERVAL = 3600


class CollectionStopped(Exception):
    pass


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix=path.name+'.', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream, allow_nan=False, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def fetch(office, now, stopped):
    start = datetime.fromtimestamp(now-45*86400, timezone.utc).date().isoformat()
    url = 'https://api.votehub.com/polls?' + urllib.parse.urlencode(dict(poll_type=office, from_date=start))
    request = urllib.request.Request(url, headers={'Accept': 'application/json',
        'User-Agent': 'ElectionBot-Research/1.0 (+https://github.com/Bowen26/election-bot)'})
    began = time.monotonic()
    chunks, size = [], 0
    with urllib.request.build_opener(NoRedirect()).open(request, timeout=10) as response:
        while True:
            if stopped():
                raise CollectionStopped()
            if time.monotonic()-began > 30:
                raise TimeoutError('Public poll download exceeded deadline')
            chunk = response.read(min(65536, MAX_BYTES+1-size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > MAX_BYTES:
                raise ValueError('Public poll feed exceeds 4 MB')
    data = json.loads(b''.join(chunks))
    # The documented wrapper and current bare list are supported. Unknown pagination is not.
    if isinstance(data, dict) and set(data) == {'polls'}:
        data = data['polls']
    if not isinstance(data, list) or len(data) > 5000:
        raise ValueError('Unsupported public poll feed envelope or row count')
    return url, data


def status(runtime, now=None):
    now = time.time() if now is None else now
    path = Path(runtime)/'poll-collector'/'status.json'
    result = json.loads(path.read_text()) if path.exists() else {'status': 'not_collected'}
    last = result.get('last_success_at')
    age = now-last if last is not None else None
    return dict(result, success_age_seconds=age,
                stale=age is None or not 0 <= age <= 2*INTERVAL,
                research_only=True, live_model_orders=False)


def collect(runtime, config, *, force=False, now=None, stopped=lambda: False, fetcher=fetch):
    runtime = Path(runtime)
    directory = runtime/'poll-collector'
    with exclusive_lock(directory):
        began = time.time() if now is None else now
        old = status(runtime, began)
        if not force and 0 < old.get('next_attempt_at', 0)-began <= INTERVAL:
            return dict(old, deferred=True)
        if stopped():
            raise CollectionStopped()
        result = dict(status='collecting', attempted_at=began, next_attempt_at=began+INTERVAL,
                      last_success_at=old.get('last_success_at'), attribution=ATTRIBUTION,
                      research_only=True, live_model_orders=False)
        # Persist cadence before networking so process restarts do not hammer the source.
        save(directory/'status.json', result)
        store = None
        try:
            batches, sources = {}, []
            for office in OFFICES:
                if stopped():
                    raise CollectionStopped()
                url, rows = fetcher(office, began, stopped)
                if not isinstance(rows, list) or len(rows) > 5000:
                    raise ValueError('Invalid provider rows')
                batches[office] = rows
                sources.append(dict(url=url, office=office, rows=len(rows)))
            received = time.time() if now is None else now
            if received < began:
                raise ValueError('Clock moved backwards while collecting')
            raw_hash = digest(batches)
            raw_path = runtime/'poll-staging'/('votehub-'+raw_hash+'.json')
            if not raw_path.exists():
                save(raw_path, dict(received_at=received, attribution=ATTRIBUTION, sources=sources, raw=batches))
            if stopped():
                raise CollectionStopped()
            store = PollStore(runtime/'fair-value.sqlite3')
            latest_time = store.db.execute('SELECT MAX(received) FROM evidence').fetchone()[0]
            if latest_time is not None and latest_time > received:
                raise ValueError('Research evidence clock is ahead of collector')
            previous = {p['poll_id']: p for p in store.latest('poll', received)}
            # Only reviewed Senate bindings enter the model. The other offices stay staged.
            evidence, quarantine = adapt(batches['us-senator'], config, previous, received)
            existing = {r['sig_exchange_id']: r for r in store.latest('race', received)}
            for i, race in enumerate(evidence['races']):
                prior = existing.get(race['sig_exchange_id'])
                keys = ('contract_fingerprint', 'race_key', 'yes_party', 'dem_candidate', 'rep_candidate', 'election_at')
                if prior and all(race[k] == prior[k] for k in keys):
                    # Collection must never overwrite a manually sourced fundamentals prior.
                    evidence['races'][i] = {k: v for k, v in prior.items() if not k.startswith('_')}
            audit = dict(received_at=received, raw_file=str(raw_path), attribution=ATTRIBUTION,
                         rejected=quarantine,
                         accepted_poll_ids=[p['poll_id'] for p in evidence['polls'] if not p.get('withdrawn')],
                         withdrawn_poll_ids=[p['poll_id'] for p in evidence['polls'] if p.get('withdrawn')])
            audit_path = directory/'audits'/(str(int(received*1000))+'-'+digest(audit)+'.json')
            save(audit_path, audit)
            if stopped():
                raise CollectionStopped()
            ingested = store.ingest(evidence, config, received)
            result.update(status='ok', last_success_at=received, sources=sources,
                bound_races=len({r['race_key'] for r in evidence['races']}),
                bound_contracts=len(evidence['races']), accepted_polls=len(audit['accepted_poll_ids']),
                withdrawn_polls=len(audit['withdrawn_poll_ids']), rejected_rows=len(quarantine),
                rejection_reasons=dict(Counter(r['reason'] for r in quarantine)),
                staged_only_offices=['governor', 'us-representative'], ingestion=ingested,
                audit_file=str(audit_path), raw_file=str(raw_path))
        except CollectionStopped:
            result.update(status='stopped')
        except Exception as error:
            result.update(status='error', error_type=type(error).__name__)
        finally:
            if store is not None:
                store.close()
            save(directory/'status.json', result)
        return result


class PollCollector:
    """Owns its SQLite connections; no network or database work on the trading thread."""
    def __init__(self, runtime, config):
        self.runtime, self.config = Path(runtime), config
        self.halt = threading.Event()
        self.thread = threading.Thread(target=self._work, name='research-polls', daemon=True)

    def start(self):
        self.thread.start()

    def stopped(self):
        return self.halt.is_set() or (self.runtime/'STOP').exists()

    def _work(self):
        while not self.stopped():
            try:
                collect(self.runtime, self.config, stopped=self.stopped)
            except Exception:
                # Filesystem trouble/another collector cannot halt trading or bypass its controls.
                # Existing collector status remains available; no successful refresh is fabricated.
                pass
            if self.halt.wait(60):
                break

    def close(self):
        self.halt.set()
        self.thread.join(timeout=.2)
