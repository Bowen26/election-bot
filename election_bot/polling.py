"""Versioned, locally received election evidence. No trading API access."""
from datetime import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import time
from urllib.parse import urlsplit

MAX_BYTES = 4_000_000


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def timestamp(value):
    if not isinstance(value, str):
        raise ValueError('Dates require ISO timestamps with an explicit timezone')
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('Dates require an explicit timezone')
    return parsed.timestamp()


def number(value, low, high):
    if isinstance(value, bool):
        raise ValueError('Boolean is not a numeric measurement')
    result = float(value)
    if not math.isfinite(result) or not low <= result <= high:
        raise ValueError('Numeric measurement outside supported bounds')
    return result


def text(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 1000 or 'REPLACE_' in value:
        raise ValueError('Missing or placeholder identity/source')
    return value.strip()


def source_url(value):
    value = text(value)
    url = urlsplit(value)
    if url.scheme != 'https' or not url.hostname or url.username or url.password:
        raise ValueError('Evidence requires an HTTPS source URL without credentials')
    return value


def validate_race(raw, mappings):
    r = dict(raw)
    for key in ('sig_exchange_id', 'contract_fingerprint', 'race_key', 'dem_candidate', 'rep_candidate'):
        r[key] = text(r[key])
    match = mappings.get(r['sig_exchange_id'])
    if not match or any(r[k] != match.get(k) for k in ('contract_fingerprint','race_key')):
        raise ValueError('Fair-value race must match the configured contract fingerprint and race')
    if r['yes_party'] not in ('DEM','REP') or r['contest_type'] != 'general_plurality':
        raise ValueError('Version 1 supports reviewed Democratic/Republican general plurality races only')
    if r['dem_candidate'].casefold() == r['rep_candidate'].casefold():
        raise ValueError('Candidates must be different')
    if match.get('exposure_sign') != (1 if r['yes_party']=='DEM' else -1):
        raise ValueError('Party orientation must match the configured signed contract')
    timestamp(r['election_at'])
    r['source_url'] = source_url(r['source_url'])
    if r.get('prior') is not None:
        prior = dict(r['prior'])
        prior['margin_pp'] = number(prior['margin_pp'], -80, 80)
        prior['sd_pp'] = number(prior['sd_pp'], 8, 30)
        prior['source_url'] = source_url(prior['source_url'])
        timestamp(prior['published_at'])
        prior['method'] = text(prior['method'])
        r['prior'] = prior
    return r


def validate_poll(raw):
    p = dict(raw)
    for key in ('poll_id','survey_id','pollster','race_key','dem_candidate','rep_candidate'):
        p[key] = text(p[key])
    p['pollster'] = p['pollster'].casefold()
    if p['stage'] != 'general' or p['population'] not in ('lv','rv'):
        raise ValueError('Only general-election likely/registered voter samples are supported')
    if 'partisan' not in p or (p['partisan'] is not None and p['partisan'] is not False):
        raise ValueError('Campaign/party polls are excluded; unknown sponsorship must be null')
    p['source_url'] = source_url(p['source_url'])
    start, end, published = (timestamp(p[k]) for k in ('field_start','field_end','published_at'))
    if not start <= end <= published or end-start > 31*86400:
        raise ValueError('Invalid fieldwork/publication chronology')
    p['sample_size'] = number(p['sample_size'], 100, 100000)
    if int(p['sample_size']) != p['sample_size']:
        raise ValueError('Sample size must be an integer')
    for key in ('dem_pct','rep_pct','other_pct'):
        p[key] = number(p[key],0,100)
    if not 50 <= p['dem_pct']+p['rep_pct'] <= 100 or sum(p[k] for k in ('dem_pct','rep_pct','other_pct')) > 100.5:
        raise ValueError('Invalid vote shares; use raw shares, not a win probability')
    if p.get('reported_moe_pp') is not None:
        p['reported_moe_pp'] = number(p['reported_moe_pp'], .1, 30)
    p['methodology'] = text(p['methodology'])
    if type(p.get('withdrawn',False)) is not bool:
        raise ValueError('withdrawn must be boolean')
    return p


class PollStore:
    def __init__(self, path, readonly=False):
        path = Path(path).resolve()
        if readonly:
            self.db = sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=.2)
        else:
            path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
            self.db = sqlite3.connect(path,timeout=2)
            os.chmod(path,0o600)
            self.db.execute('PRAGMA journal_mode=WAL')
            self.db.execute('PRAGMA synchronous=FULL')
            self.db.execute('''CREATE TABLE IF NOT EXISTS evidence (
                seq INTEGER PRIMARY KEY, kind TEXT NOT NULL, identity TEXT NOT NULL,
                received REAL NOT NULL, hash TEXT NOT NULL, body TEXT NOT NULL)''')
            self.db.execute('CREATE INDEX IF NOT EXISTS evidence_asof ON evidence(kind,received,identity)')
            self.db.commit()

    def close(self):
        self.db.close()

    def ingest(self, data, config, now=None):
        now = time.time() if now is None else now
        if not isinstance(data,dict) or data.get('schema_version') != 1 or data.get('synthetic'):
            raise ValueError('Expected non-synthetic schema_version 1 evidence')
        races, polls = data.get('races',[]), data.get('polls',[])
        if not isinstance(races,list) or not isinstance(polls,list) or len(races)+len(polls)>5000:
            raise ValueError('Evidence batch must contain at most 5000 records')
        mappings = {m['sig_exchange_id']:m for m in config['markets'] if m.get('sig_exchange_id')}
        validated = [('race',validate_race(r,mappings)) for r in races]+[('poll',validate_poll(p)) for p in polls]
        count = 0
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            for kind, row in validated:
                identity = row['sig_exchange_id'] if kind=='race' else row['poll_id']
                previous = self.db.execute('SELECT body FROM evidence WHERE kind=? AND identity=? ORDER BY seq DESC LIMIT 1',
                                           (kind,identity)).fetchone()
                if kind=='poll' and previous:
                    old = json.loads(previous[0])
                    if any(old[k]!=row[k] for k in ('pollster','survey_id','race_key')):
                        raise ValueError('Poll revisions cannot change survey identity')
                if previous and digest(json.loads(previous[0])) == digest(row):
                    continue
                count += self.db.execute('INSERT INTO evidence(kind,identity,received,hash,body) VALUES(?,?,?,?,?)',
                    (kind,identity,now,digest(row),canonical(row))).rowcount
        return {'inserted_versions':count,'unchanged':len(validated)-count,'received_at':now}

    def latest(self, kind, asof):
        # Latest locally known revision. A withdrawn/corrected poll must not revive its old value.
        rows = self.db.execute('''SELECT e.received,e.hash,e.body FROM evidence e JOIN
            (SELECT identity,MAX(seq) AS seq FROM evidence WHERE kind=? AND received<=? GROUP BY identity) x
            ON e.seq=x.seq''',(kind,asof))
        return [dict(json.loads(body),_received_at=received,_hash=sha) for received,sha,body in rows]


def read_input(path):
    with Path(path).open('rb') as stream:
        body=stream.read(MAX_BYTES+1)
    if len(body)>MAX_BYTES:
        raise ValueError('Polling input exceeds 4 MB')
    return json.loads(body)
