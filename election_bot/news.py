"""Read-only RSS/Atom collection. Feed text is data, never executable instructions.

Headlines can request a price refresh or temporarily block entries. They cannot
set prices, increase budgets, enable markets, or call any trading API.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import hashlib
from html import unescape
import json
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

from .clients import NoRedirect
from .state import exclusive_lock

MAX_BYTES = 2_000_000
RISK_CATEGORIES = {'withdrawal', 'ballot_change', 'disputed_result'}
ELECTION_WORDS = re.compile(r'\b(midterms?|elections?|senate|senators?|congress|congressional|'
                            r'governors?|ballots?|candidates?|polls?|polling|voters?|voting|'
                            r'house race|house control)\b', re.I)
STATE_NAMES = ('Alabama|Alaska|Arizona|Arkansas|California|Colorado|Connecticut|Delaware|'
               'Florida|Georgia|Hawaii|Idaho|Illinois|Indiana|Iowa|Kansas|Kentucky|Louisiana|'
               'Maine|Maryland|Massachusetts|Michigan|Minnesota|Mississippi|Missouri|Montana|'
               'Nebraska|Nevada|New Hampshire|New Jersey|New Mexico|New York|North Carolina|'
               'North Dakota|Ohio|Oklahoma|Oregon|Pennsylvania|Rhode Island|South Carolina|'
               'South Dakota|Tennessee|Texas|Utah|Vermont|Virginia|Washington|West Virginia|'
               'Wisconsin|Wyoming').split('|')


def clean(value, limit=1000):
    value = unescape(re.sub(r'<[^>]*>', ' ', value or ''))
    return ' '.join(value.split())[:limit]


def normalized(value):
    return ' '.join(re.findall(r'[a-z0-9]+', value.lower()))


def contains(text, phrase):
    return ' ' + normalized(phrase) + ' ' in ' ' + normalized(text) + ' '


def timestamp(value):
    if not value:
        return None
    try:
        result = parsedate_to_datetime(value)
    except (ValueError, TypeError):
        try:
            result = datetime.fromisoformat(value.replace('Z', '+00:00'))
        except (ValueError, TypeError):
            return None
    # Do not invent a timezone for an ambiguous publication time.
    return result.timestamp() if result.tzinfo is not None else None


def canonical_url(value):
    parts = urllib.parse.urlsplit(value.strip())
    if parts.scheme not in ('http', 'https') or not parts.hostname or parts.username or parts.password:
        raise ValueError('Expected a public article URL')
    query = [(k, v) for k, v in urllib.parse.parse_qsl(parts.query)
             if not k.lower().startswith('utm_') and k.lower() not in ('fbclid', 'gclid')]
    return urllib.parse.urlunsplit((parts.scheme.lower(), parts.netloc.lower(),
                                   parts.path or '/', urllib.parse.urlencode(sorted(query)), ''))


@dataclass
class Article:
    title: str
    url: str
    published: object
    summary: str = ''


def parse_feed(body):
    if len(body) > MAX_BYTES:
        raise ValueError('Oversized feed')
    # Decode first, so UTF-16 cannot bypass declaration checks using NUL bytes.
    text = body.decode('utf-8-sig')
    if '<!doctype' in text.lower() or '<!entity' in text.lower():
        raise ValueError('Oversized feed or unsupported XML declarations')
    root = ET.fromstring(text)
    local = lambda node: node.tag.rsplit('}', 1)[-1]
    if local(root) not in ('rss', 'feed', 'RDF'):
        raise ValueError('Response is not an RSS/Atom feed')
    articles = []
    for node in (n for n in root.iter() if local(n) in ('item', 'entry')):
        values = {}
        links = []
        for child in node:
            name = local(child)
            values[name] = ''.join(child.itertext()).strip()
            if name == 'link' and child.get('rel', 'alternate') == 'alternate':
                links.append(child.get('href') or values[name])
        title = clean(values.get('title'), 400)
        if not title or not links:
            continue
        try:
            url = canonical_url(links[0])
        except ValueError:
            continue
        # An Atom update time alone is not evidence of original publication.
        published = timestamp(values.get('pubDate') or values.get('published') or values.get('date'))
        articles.append(Article(title, url, published,
                                clean(values.get('description') or values.get('summary'), 800)))
        if len(articles) >= 100:
            break
    return articles


def classify(article):
    text = (article.title + ' ' + article.summary).lower()
    headline = article.title.lower()
    # Precautionary flags, not claims that the report has been independently verified.
    speculative = re.search(r'\b(could|might|may|rumou?r|denies|not|won\W?t|fact.check)\b', headline)
    if not speculative:
        if re.search(r'\b(withdraws?|withdrawal|drops? out|suspends? (?:his |her |their )?campaign)\b', headline):
            return 'withdrawal'
        if (re.search(r'\b(ballot|candidate)\b', headline) and
                re.search(r'\b(disqualif\w*|ineligible|removed|removes|barred|bars|ruling|rules)\b', headline)):
            return 'ballot_change'
        if re.search(r'\b(recount|contested results?|disputed results?|retracts? (?:its |a )?race call)\b', headline):
            return 'disputed_result'
    if re.search(r'\b(poll|polls|polling|survey)\b', text):
        return 'poll'
    if re.search(r'\b(projected winner|projects? .{0,80}win|race called|wins? .{0,60}(?:race|election))\b', headline):
        return 'race_call'
    if re.search(r'\b(results|vote counts?|votes counted|turnout)\b', headline):
        return 'results'
    return 'election_news'


def matching_markets(article, markets):
    text = article.title + ' ' + article.summary
    matches = []
    for mapping in markets:
        rule = mapping.get('news_match', {})
        groups = rule.get('all', [])
        if (groups and all(any(contains(text, phrase) for phrase in group) for group in groups)
                and not any(contains(text, phrase) for phrase in rule.get('exclude', []))):
            matches.append(mapping['name'])
    return matches


def validate_news(config):
    settings = config.get('news', {})
    if not settings.get('enabled'):
        return
    for field, lower, upper in [('poll_seconds', 30, 3600), ('max_article_age_seconds', 60, 86400),
                                ('pause_seconds', 60, 3600), ('max_feed_silence_seconds', 60, 7200)]:
        value = settings.get(field)
        if not isinstance(value, int) or not lower <= value <= upper:
            raise ValueError('Invalid news.' + field)
    if settings['max_feed_silence_seconds'] < 2 * settings['poll_seconds']:
        raise ValueError('News feed silence limit must cover at least two polling intervals')
    sources = settings.get('sources', [])
    if not 1 <= len(sources) <= 10 or len({s['name'] for s in sources}) != len(sources):
        raise ValueError('Configure 1–10 uniquely named news sources')
    for source in sources:
        url = urllib.parse.urlsplit(source['url'])
        if (url.scheme != 'https' or not url.hostname or url.username or url.password or
                url.port not in (None, 443) or not source.get('article_hosts') or
                not isinstance(source.get('pause_on_risk'), bool)):
            raise ValueError('News sources require HTTPS, article_hosts and an explicit pause_on_risk setting')
    names = [m['name'] for m in config['markets']]
    if len(names) != len(set(names)):
        raise ValueError('News requires unique market names')
    for mapping in config['markets']:
        if mapping.get('enabled'):
            groups = mapping.get('news_match', {}).get('all', [])
            if not groups or any(not g or not all(isinstance(v, str) and normalized(v) for v in g) for g in groups):
                raise ValueError('Enabled markets need explicit news_match.all phrase groups')


class FeedClient:
    """Only fetches configured RSS URLs. Never follows article links or sends credentials."""
    def __init__(self):
        self.opener = urllib.request.build_opener(NoRedirect())

    def fetch(self, source, cache):
        headers = {'User-Agent': 'SIG-Election-Bot-News/0.1',
                   'Accept': 'application/rss+xml, application/atom+xml, application/xml, text/xml'}
        if cache.get('etag'):
            headers['If-None-Match'] = cache['etag']
        if cache.get('modified'):
            headers['If-Modified-Since'] = cache['modified']
        request = urllib.request.Request(source['url'], headers=headers)
        try:
            with self.opener.open(request, timeout=6) as response:
                data = response.read(MAX_BYTES + 1)
                return parse_feed(data), response.headers.get('ETag'), response.headers.get('Last-Modified')
        except urllib.error.HTTPError as error:
            if error.code == 304:
                return [], cache.get('etag'), cache.get('modified')
            raise RuntimeError('Feed HTTP ' + str(error.code)) from None


class NewsStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(str(path), timeout=5)
        os.chmod(path, 0o600)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS articles (
            id INTEGER PRIMARY KEY, title_key TEXT UNIQUE NOT NULL, title TEXT NOT NULL,
            url TEXT NOT NULL, source TEXT NOT NULL, published REAL, first_seen REAL NOT NULL,
            category TEXT NOT NULL, states TEXT NOT NULL, eligibility TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS sightings (
            article INTEGER NOT NULL, source TEXT NOT NULL, seen REAL NOT NULL,
            PRIMARY KEY (article,source));
          CREATE TABLE IF NOT EXISTS news_events (
            id INTEGER PRIMARY KEY, article INTEGER NOT NULL, market TEXT NOT NULL,
            created REAL NOT NULL, UNIQUE(article,market));
          CREATE TABLE IF NOT EXISTS pauses (
            market TEXT PRIMARY KEY, until REAL NOT NULL, article INTEGER NOT NULL);
          CREATE TABLE IF NOT EXISTS risk_flags (
            article INTEGER NOT NULL, market TEXT NOT NULL, PRIMARY KEY(article,market));
          CREATE TABLE IF NOT EXISTS feeds (
            name TEXT PRIMARY KEY, url TEXT NOT NULL, last_attempt REAL, last_success REAL,
            last_error TEXT, etag TEXT, modified TEXT, items INTEGER);
          CREATE TABLE IF NOT EXISTS cursors (consumer TEXT PRIMARY KEY, last_id INTEGER NOT NULL);
        ''')

    def close(self):
        self.db.close()

    def cache(self, source):
        row = self.db.execute('SELECT * FROM feeds WHERE name=? AND url=?',
                              (source['name'], source['url'])).fetchone()
        return dict(row) if row else {}

    def health(self, source, now, error=None, etag=None, modified=None, count=0):
        previous = self.cache(source)
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO feeds VALUES (?,?,?,?,?,?,?,?)',
                (source['name'], source['url'], now, previous.get('last_success') if error else now,
                 error, previous.get('etag') if error else etag,
                 previous.get('modified') if error else modified, count))

    def ingest(self, article, source, config, now=None):
        now = time.time() if now is None else now
        settings = config['news']
        if not ELECTION_WORDS.search(article.title + ' ' + article.summary):
            return 0
        host = urllib.parse.urlsplit(article.url).hostname
        if host not in source['article_hosts']:
            return 0
        eligibility = 'fresh'
        if article.published is None:
            eligibility = 'missing_timestamp'
        elif article.published > now + 60:
            eligibility = 'future_timestamp'
        elif now - article.published > settings['max_article_age_seconds']:
            eligibility = 'stale'
        title_key = hashlib.sha256(normalized(article.title).encode()).hexdigest()
        category = classify(article)
        states = [name for name in STATE_NAMES if contains(article.title + ' ' + article.summary, name)]
        count = 0
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO articles '
                '(title_key,title,url,source,published,first_seen,category,states,eligibility) VALUES (?,?,?,?,?,?,?,?,?)',
                (title_key, article.title, article.url, source['name'], article.published, now,
                 category, json.dumps(states), eligibility))
            stored = self.db.execute('SELECT * FROM articles WHERE title_key=?', (title_key,)).fetchone()
            aid = stored['id']
            self.db.execute('INSERT OR IGNORE INTO sightings VALUES (?,?,?)', (aid, source['name'], now))
            # Reposted/retimestamped headlines cannot turn old news into a fresh alert.
            if eligibility != 'fresh' or stored['eligibility'] != 'fresh' or now-stored['published'] > settings['max_article_age_seconds']:
                return 0
            for market in matching_markets(article, config['markets']):
                inserted = self.db.execute('INSERT OR IGNORE INTO news_events(article,market,created) VALUES (?,?,?)',
                                          (aid, market, now)).rowcount
                count += inserted
                if source['pause_on_risk'] and category in RISK_CATEGORIES:
                    new_risk = self.db.execute('INSERT OR IGNORE INTO risk_flags VALUES (?,?)', (aid, market)).rowcount
                    if new_risk:
                        # A later permitted source can flag a story first seen on an alert-only source.
                        count += 0 if inserted else 1
                        self.db.execute('INSERT INTO pauses VALUES (?,?,?) ON CONFLICT(market) DO UPDATE SET '
                                        'until=MAX(pauses.until,excluded.until),article=excluded.article',
                                        (market, now+settings['pause_seconds'], aid))
        return count

    def drain(self, consumer, max_age):
        # Watermark commits atomically. A restart does not replay every news trigger.
        with self.db:
            row = self.db.execute('SELECT last_id FROM cursors WHERE consumer=?', (consumer,)).fetchone()
            last = row['last_id'] if row else 0
            rows = self.db.execute('SELECT e.id AS event_id,e.market,e.created,a.title,a.url,a.category,a.source,'
                                  'a.published,a.first_seen FROM news_events e JOIN articles a ON a.id=e.article '
                                  'WHERE e.id>? ORDER BY e.id LIMIT 1000', (last,)).fetchall()
            if rows:
                self.db.execute('INSERT OR REPLACE INTO cursors VALUES (?,?)', (consumer, rows[-1]['event_id']))
        return [dict(r) for r in rows if time.time()-r['published'] <= max_age]

    def block_reason(self, market, settings):
        now = time.time()
        pause = self.db.execute('SELECT until FROM pauses WHERE market=?', (market,)).fetchone()
        if pause and pause['until'] > now:
            return 'News uncertainty pause until ' + datetime.fromtimestamp(pause['until'], timezone.utc).isoformat()
        healthy = [self.cache(source).get('last_success') for source in settings['sources']]
        if not any(t is not None and now-t <= settings['max_feed_silence_seconds'] for t in healthy):
            return 'News monitor has no recently healthy feed; new entries paused'
        return None

    def status(self):
        return {'feeds': [dict(r) for r in self.db.execute('SELECT name,last_attempt,last_success,last_error,items FROM feeds')],
                'active_pauses': [dict(r) for r in self.db.execute('SELECT * FROM pauses WHERE until>?', (time.time(),))],
                'article_count': self.db.execute('SELECT COUNT(*) FROM articles').fetchone()[0],
                'recent': [dict(r) for r in self.db.execute('SELECT title,url,source,published,first_seen,category,states,eligibility '
                                                         'FROM articles ORDER BY COALESCE(published,first_seen) DESC,id DESC LIMIT 10')]}


class NewsCollector:
    def __init__(self, config, store, client=None):
        validate_news(config)
        self.config, self.store = config, store
        self.client = client or FeedClient()

    def poll(self, stop=None):
        added, outcomes = 0, []
        for source in self.config['news']['sources']:
            if stop is not None and stop.is_set():
                break
            try:
                items, etag, modified = self.client.fetch(source, self.store.cache(source))
                count = sum(self.store.ingest(a, source, self.config) for a in items)
                added += count
                self.store.health(source, time.time(), etag=etag, modified=modified, count=len(items))
                outcomes.append({'source': source['name'], 'ok': True, 'items': len(items), 'new_race_events': count})
            except Exception as error:
                # Never output response bodies, credentials or control text from a feed.
                message = 'HTTP/feed failure: ' + type(error).__name__
                self.store.health(source, time.time(), error=message)
                outcomes.append({'source': source['name'], 'ok': False, 'error': message})
        return {'new_race_events': added, 'sources': outcomes}


class NewsGate:
    """Main trading thread's reader plus a separate collector thread/connection."""
    def __init__(self, config, runtime, consumer):
        validate_news(config)
        self.config, self.runtime, self.consumer = config, Path(runtime), consumer
        self.store = NewsStore(self.runtime / 'news.sqlite3')
        self.wake = threading.Event()
        self.stop_event = threading.Event()
        self.thread = None

    def start(self):
        self.thread = threading.Thread(target=self._work, name='news-feeds', daemon=True)
        self.thread.start()

    def _work(self):
        try:
            with exclusive_lock(self.runtime / 'news-collector'):
                store = NewsStore(self.runtime / 'news.sqlite3')
                try:
                    collector = NewsCollector(self.config, store)
                    first = True
                    while not self.stop_event.is_set():
                        result = collector.poll(self.stop_event)
                        if first or result['new_race_events'] or any(not s['ok'] for s in result['sources']):
                            print(json.dumps({'event': 'news_poll', **result}), flush=True)
                        if first or result['new_race_events']:
                            self.wake.set()
                        first = False
                        self.stop_event.wait(self.config['news']['poll_seconds'])
                finally:
                    store.close()
        except Exception as error:
            print(json.dumps({'event': 'news_monitor_error', 'reason': type(error).__name__,
                              'detail': 'Monitor unavailable; entries pause once stored feed health expires'}), flush=True)

    def drain(self):
        grouped = {}
        for event in self.store.drain(self.consumer, self.config['news']['max_article_age_seconds']):
            grouped.setdefault(event['market'], []).append(event)
        return grouped

    def block_reason(self, mapping):
        return self.store.block_reason(mapping['name'], self.config['news'])

    def close(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=7)
        self.store.close()
