"""Documented public venue feeds and authenticated SIG competition API."""
from collections import deque
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, wait
from email.utils import parsedate_to_datetime
import hashlib
import json
import math
import socket
import ssl
import time
import threading
import urllib.error
import urllib.parse
import urllib.request

from .strategy import Book


def iso_time(value):
    if not isinstance(value, str):
        raise ValueError('ISO timestamp must be a string with an explicit timezone')
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError('ISO timestamp requires an explicit timezone')
    return parsed.timestamp()


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True,
                                    separators=(',', ':')).encode()).hexdigest()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RuntimeError("Unexpected API redirect; refusing to forward credentials")


class APIError(RuntimeError):
    def __init__(self, message, status=None, retry_after=None, method=None, venue=None,
                 rejection=None):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after
        self.method = method
        self.venue = venue
        self.rejection = rejection


class RecoveryHistoryUnavailable(RuntimeError):
    """Retry recovery with fresh history; never release an unverified reservation."""


# SIG allows 100 reads and 30 writes per minute per account, across every process.
# When SHARED_RATE_FILE is set (the CLI sets it inside .runtime), all commands share
# one file-locked rolling budget slightly below those limits. Unset: no shared limit.
SHARED_RATE_FILE = None
SHARED_LIMITS = {'read': 90, 'write': 27}


def shared_slot(kind, sleep=time.sleep, clock=time.time):
    """Block until this process may make one more SIG request of this kind."""
    if SHARED_RATE_FILE is None:
        return
    import fcntl
    from pathlib import Path
    path = Path(SHARED_RATE_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    while True:
        with open(path, 'a+') as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                handle.seek(0)
                try:
                    state = json.loads(handle.read() or '{}')
                except ValueError:
                    state = {}
                now = clock()
                recent = [t for t in state.get(kind, []) if now-60 < t <= now+1]
                if len(recent) < SHARED_LIMITS[kind]:
                    recent.append(now)
                    state[kind] = recent
                    handle.seek(0)
                    handle.truncate()
                    handle.write(json.dumps(state))
                    handle.flush()
                    return
                wait = min(recent)+60-now
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)
        sleep(max(0.05, min(wait, 1.0)))


class HTTP:
    HOSTS = {'https://sig.thesuper.market/api/v1',
             'https://external-api.kalshi.com/trade-api/v2',
             'https://gamma-api.polymarket.com', 'https://clob.polymarket.com'}

    def __init__(self, base, key=None):
        if base not in self.HOSTS:
            raise ValueError("Unapproved API host")
        if key and base != 'https://sig.thesuper.market/api/v1':
            raise ValueError("Credentials may only be sent to SIG")
        self.base, self.key = base, key
        self.venue = ('SIG' if base == 'https://sig.thesuper.market/api/v1' else
                      'Kalshi' if 'kalshi.com' in base else 'Polymarket')
        self.opener = urllib.request.build_opener(NoRedirect())
        self.last_read = self.last_write = 0.0
        self.server_at = 0.0
        self.last_timing = None
        self.read_times = deque(maxlen=600)

    def reads_last_minute(self):
        """Authenticated GETs started in the last 60 seconds (diagnostic only)."""
        cutoff = time.monotonic()-60
        return sum(t >= cutoff for t in self.read_times)

    def request(self, path, method='GET', params=None, payload=None):
        if not path.startswith('/') or '://' in path or '..' in path:
            raise ValueError("Invalid API path")
        # Polymarket's bulk order-book query is a POST but reads data only.
        read_only_post = (method == 'POST' and self.base == 'https://clob.polymarket.com' and path == '/books')
        if method != 'GET' and self.base != 'https://sig.thesuper.market/api/v1' and not read_only_post:
            raise ValueError("External venues are read-only")
        if self.key:
            attr, interval = ('last_read', 0.8) if method == 'GET' else ('last_write', 2.2)
            time.sleep(max(0, getattr(self, attr) + interval - time.monotonic()))
            # Every process using this runtime shares one per-account budget (see shared_slot).
            shared_slot('read' if method == 'GET' else 'write')
            setattr(self, attr, time.monotonic())
            if method == 'GET':
                self.read_times.append(time.monotonic())
        url = self.base + path
        if params:
            url += '?' + urllib.parse.urlencode(params)
        headers = {'Accept': 'application/json', 'User-Agent': 'SIG-Election-Bot/0.1'}
        if self.key:
            headers['Authorization'] = 'Bearer ' + self.key
        data = None
        if payload is not None:
            headers['Content-Type'] = 'application/json'
            data = json.dumps(payload, allow_nan=False).encode()
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        started = time.time()
        started_monotonic = time.monotonic()
        try:
            with self.opener.open(request, timeout=8) as response:
                body = response.read(4_000_001)
                if len(body) > 4_000_000:
                    raise APIError("API response too large", venue=self.venue)
                received = time.time()
                received_monotonic = time.monotonic()
                date = response.headers.get('Date')
                age = float(response.headers.get('Age', '0'))
                server_at = parsedate_to_datetime(date).timestamp() - age if date else started
                def finite(value):
                    return value if value is not None and math.isfinite(value) else None
                timing = {'request_started_at': started, 'received_at': received,
                    'request_seconds': finite(received-started),
                    'request_started_monotonic': started_monotonic,
                    'received_monotonic': received_monotonic,
                    'http_date_at': finite(server_at+age) if date else None,
                    'cache_age_seconds': finite(age),
                    'http_date_minus_cache_age_at': finite(server_at) if date else None}
                try:
                    result = json.loads(body)
                except ValueError:
                    # A cut-off or garbled body is a transport failure: retryable, never a halt.
                    raise APIError("{} {} returned an incomplete or invalid JSON body".format(method, path),
                                   method=method, venue=self.venue) from None
                self.server_at, self.last_timing = server_at, timing
                return result
        except urllib.error.HTTPError as error:
            # Response bodies can contain user data; never log them or the key.
            rejection = None
            if self.venue == 'SIG' and method == 'POST' and path == '/orders' and error.code == 400:
                try:
                    body = error.read(16385)
                    detail = json.loads(body).get('error', {}) if len(body) <= 16384 else {}
                    if (isinstance(detail, dict) and detail.get('code') == 'VALIDATION_ERROR'
                            and detail.get('message') == 'expirationDate must be in the future.'):
                        rejection = 'expired_order'
                except (ValueError, TypeError, AttributeError, OSError):
                    pass
            retry_after = None
            header = error.headers.get('Retry-After') if error.headers else None
            if header:
                try:
                    retry_after = max(0.0, float(header))
                except ValueError:
                    try:
                        retry_after = max(0.0, parsedate_to_datetime(header).timestamp() - time.time())
                    except (ValueError, TypeError, OverflowError):
                        pass
            suffix = '; mutation status must be reconciled' if method != 'GET' else ''
            raise APIError("{} {} returned HTTP {}{}".format(method, path, error.code, suffix),
                           status=error.code, retry_after=retry_after, method=method, venue=self.venue,
                           rejection=rejection) from None
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            reason = getattr(error, 'reason', error)
            # Diagnose common transport failures without logging URLs, headers or credentials.
            if isinstance(reason, ssl.SSLCertVerificationError):
                raise RuntimeError('TLS certificate verification failed; check the Python certificate installation') from None
            category = ('DNS lookup failed' if isinstance(reason, socket.gaierror) else
                        'connection timed out' if isinstance(reason, TimeoutError) else
                        'connection refused' if isinstance(reason, ConnectionRefusedError) else
                        type(error).__name__)
            raise APIError("{} {} failed ({})".format(method, path, category),
                           method=method, venue=self.venue) from None


class Sig:
    def __init__(self, key, slug):
        if not key:
            raise ValueError("SIG_API_KEY missing; run setup locally")
        if not slug or '/' in slug or slug.startswith('REPLACE'):
            raise ValueError("Set tournament_slug in config.json")
        self.http = HTTP('https://sig.thesuper.market/api/v1', key)
        self.slug = urllib.parse.quote(slug, safe='')
        self.tournament = self.read('/tournaments/' + self.slug)
        self.tid = self.tournament['id']

    def account(self):
        self.tournament = self.read('/tournaments/' + self.slug)
        if self.tournament['id'] != self.tid:
            raise ValueError("Tournament identity changed")
        return self.tournament

    def collection(self, path, params=None):
        params, rows = dict(params or {}), []
        params['limit'] = 100
        seen = set()
        for _ in range(100):
            result = self.read(path, params=params)
            if result.get('coverage', {}).get('complete') is False:
                raise ValueError("Incomplete SIG account projection")
            rows.extend(result['data'])
            page = result['pagination']
            if not page['hasMore']:
                return rows
            cursor = page.get('nextCursor')
            if not cursor or cursor in seen:
                raise ValueError("Incomplete pagination")
            seen.add(cursor)
            params['cursor'] = cursor
        raise ValueError("Pagination limit exceeded")

    def markets(self, search=''):
        return self.collection('/tournaments/' + self.slug + '/markets', {'search': search})

    def market(self, mid):
        if not str(mid).isdigit():
            raise ValueError("SIG market ID must be numeric")
        market = self.read('/tournaments/' + self.slug + '/markets/' + str(mid))
        tree = self.read('/markets/' + str(mid) + '/nodes', params={'tournamentId': self.tid})
        if str(tree['market_id']) != str(mid) or not tree.get('root'):
            raise ValueError('Missing SIG resolution tree')
        market['resolution_tree'] = tree['root']
        return market

    def prices(self, exchanges):
        """Best bid/ask for many exchanges, 100 per read. Screening only: cached up to 2 s, no depth."""
        exchanges = [str(e) for e in exchanges]
        if not all(e.isdigit() for e in exchanges):
            raise ValueError("SIG exchange IDs must be numeric")
        result = {}
        for start in range(0, len(exchanges), 100):
            data = self.read('/exchanges/prices', params={'ids': ','.join(exchanges[start:start+100]),
                                                         'tournamentId': self.tid})
            for row in data['data']:
                result[str(row['exchangeId'])] = (row.get('bestBid'), row.get('bestAsk'))
        return result

    def book(self, exchange):
        if not str(exchange).isdigit():
            raise ValueError("SIG exchange ID must be numeric")
        data = self.read('/exchanges/' + str(exchange) + '/orderbook',
                                 params={'tournamentId': self.tid, 'depth': 20})
        if str(data['exchangeId']) != str(exchange) or not data.get('asOf'):
            raise ValueError("Wrong SIG exchange or missing book timestamp")
        return Book.make([(r['price'], r['quantity']) for r in data['bids']],
                         [(r['price'], r['quantity']) for r in data['asks']],
                         iso_time(data['asOf']['at']), venue='SIG', timestamp_basis='engine_capture')

    def open_orders(self):
        return self.collection('/orders', {'status': 'open', 'tournamentId': self.tid})

    def recovery_history(self, exchange):
        """Complete history with non-regressing projection coverage on every page."""
        params = {'status': 'all', 'tournamentId': self.tid, 'exchangeId': str(exchange), 'limit': 100}
        rows, ids, cursors, checkpoint = [], set(), set(), None
        for _ in range(100):
            page = self.read('/orders', params=dict(params))
            coverage = page.get('coverage', {})
            sequence = coverage.get('projectedThroughSequence')
            # SIG documents this as the newest projected event at response time,
            # not the cursor snapshot ID. It may advance while paging. Continue
            # the opaque cursor unchanged; incomplete or regressing reads fail.
            if (coverage.get('complete') is not True or type(sequence) is not int or sequence < 0
                    or (checkpoint is not None and sequence < checkpoint)):
                raise RecoveryHistoryUnavailable('Order recovery history coverage/checkpoint unverified; reservation retained')
            checkpoint = sequence
            if not isinstance(page.get('data'), list):
                raise RuntimeError('Malformed recovery history; reservation retained')
            for row in page['data']:
                oid = str(row['id'])
                if (oid in ids or str(row['exchangeId']) != str(exchange)
                        or row['tournamentId'] != self.tid):
                    raise RuntimeError('Recovery history identity mismatch; reservation retained')
                ids.add(oid)
                rows.append(row)
            pagination = page['pagination']
            if pagination.get('hasMore') is False:
                self.check_clock()
                return {'orders': rows, 'sequence': checkpoint}
            cursor = pagination.get('nextCursor')
            if pagination.get('hasMore') is not True or not isinstance(cursor, str) or not cursor or cursor in cursors:
                raise RuntimeError('Incomplete recovery pagination; reservation retained')
            cursors.add(cursor)
            params['cursor'] = cursor
        raise RuntimeError('Recovery history pagination limit exceeded; reservation retained')

    def positions(self):
        return self.read('/tournaments/' + self.slug + '/portfolio/positions')['positions']

    def check_clock(self):
        from .clock_guard import check_timing
        return check_timing(self.http.last_timing)

    def place(self, payload):
        if payload['tournamentId'] != self.tid:
            raise ValueError("Wrong tournament")
        from .clock_guard import ClockSampleUnavailable, SubmissionClockUnavailable
        try:
            self.check_clock()  # Last backstop, including explicit recovery replays.
        except ClockSampleUnavailable as error:
            # This exception proves no HTTP order request was attempted here.
            raise SubmissionClockUnavailable(str(error), error.detail) from error
        return self.http.request('/orders', 'POST', payload=payload)

    def cancel(self, order_id):
        return self.http.request('/orders/' + str(int(order_id)), 'DELETE')

    def order(self, order_id):
        return self.read('/orders/' + str(int(order_id)))

    def fills(self, order_id):
        """Read the complete closed-order fill history, retaining lifecycle totals.

        Never acknowledge a partial/inconsistent history as complete: a failure
        leaves the engine's reservation intact for reconciliation.
        """
        from .strategy import D
        path = '/orders/' + str(int(order_id)) + '/fills'
        params, rows, seen_ids, seen_cursors = {'limit': 100}, [], set(), set()
        first, identity, total, average = None, None, None, None
        for page_number in range(100):
            page = self.read(path, params=dict(params))
            if (str(page['orderId']) != str(int(order_id)) or page['tournamentId'] != self.tid
                    or page.get('coverage', {}).get('complete') is not True):
                raise RuntimeError('Fill page identity/coverage mismatch; reservation retained')
            page_identity = (str(page['orderId']), str(page['exchangeId']), page['tournamentId'])
            page_total = D(page['totalQuantityFilled'])
            page_average = D(page['avgFillPrice']) if page_total else None
            if first is None:
                first, identity, total, average = page, page_identity, page_total, page_average
            elif (identity, total, average) != (page_identity, page_total, page_average):
                raise RuntimeError('Fill history changed during pagination; reservation retained')
            if not isinstance(page.get('data'), list):
                raise RuntimeError('Malformed fill rows; reservation retained')
            for row in page['data']:
                fill_id = str(row['id'])
                quantity, price = D(row['quantity']), D(row['price'])
                if (not fill_id.isdigit() or int(fill_id) <= 0 or fill_id in seen_ids
                        or quantity == 0 or quantity != int(quantity) or not 0 < price < 1
                        or row['side'] not in ('yes', 'no')
                        or (quantity < 0) != (row['side'] == 'no')
                        or (quantity < 0) != (total < 0)):
                    raise RuntimeError('Invalid or repeated fill row; reservation retained')
                iso_time(row['filledAt'])  # Explicit timezone required on every page.
                seen_ids.add(fill_id)
                rows.append(row)
            pagination = page['pagination']
            if type(pagination.get('hasMore')) is not bool:
                raise RuntimeError('Malformed fill pagination; reservation retained')
            if not pagination['hasMore']:
                if sum((D(row['quantity']) for row in rows), D(0)) != total:
                    raise RuntimeError('Fill rows disagree with lifecycle quantity; reservation retained')
                return {**first, 'data': rows, 'pagination': pagination, 'pages_read': page_number+1}
            cursor = pagination.get('nextCursor')
            if (not page['data'] or not isinstance(cursor, str) or not cursor
                    or cursor in seen_cursors):
                raise RuntimeError('Incomplete fill pagination; reservation retained')
            seen_cursors.add(cursor)
            params['cursor'] = cursor
        raise RuntimeError('Fill pagination limit exceeded; reservation retained')

    def read(self, path, params=None):
        # Retrying a GET cannot resubmit a trade. Persistent errors
        # still leave the journal reservation intact and halt new submissions.
        for attempt in range(3):
            try:
                return self.http.request(path, params=params)
            except APIError as error:
                delay = max(attempt + 1, error.retry_after or 0)
                # status None: the connection failed or timed out before any HTTP response.
                if error.status not in (None, 502, 503, 504) or attempt == 2 or delay > 5:
                    raise
                print('SIG read {} temporarily unavailable ({}); retrying in {}s'.format(
                    path, 'HTTP %s' % error.status if error.status else 'connection failure', delay), flush=True)
                time.sleep(delay)


POLYMARKET_MAX_QUIET_SECONDS = 900


def polymarket_book(data, timing, served_at):
    """Polymarket's book timestamp is its last change, not when it was served.

    A quiet book (same hash) keeps an aging timestamp although the server just
    returned it. Within POLYMARKET_MAX_QUIET_SECONDS of its last change, the book
    is current as of the HTTP response (as for Kalshi). Older books keep the
    last-change time, so normal freshness checks still reject a dead book.
    """
    changed = float(data['timestamp']) / 1000
    served = served_at if served_at and math.isfinite(served_at) else None
    quiet = served - changed if served is not None else None
    transport = dict(timing or {}, book_changed_at=changed,
                     book_quiet_seconds=round(quiet, 3) if quiet is not None else None)
    if quiet is not None and 0 <= quiet <= POLYMARKET_MAX_QUIET_SECONDS:
        source, basis = served, 'http_date_minus_cache_age; book unchanged since book_changed_at'
    else:
        source, basis = changed, 'exchange_book_timestamp'
    return Book.make([(r['price'], r['size']) for r in data['bids']],
                     [(r['price'], r['size']) for r in data['asks']],
                     source, venue='Polymarket', timestamp_basis=basis,
                     observed_at=timing['received_at'] if timing else None, transport=transport)


class References:
    def __init__(self):
        self.kalshi = HTTP('https://external-api.kalshi.com/trade-api/v2')
        self.gamma = HTTP('https://gamma-api.polymarket.com')
        self.clob = HTTP('https://clob.polymarket.com')
        self._pool = None
        self._lock = threading.RLock()
        self._closed = False

    def _ensure_open(self):
        if self._closed:
            raise RuntimeError('Reference client is closed')

    def __enter__(self):
        with self._lock:
            self._ensure_open()
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            if self._pool is not None:
                self._pool.shutdown(wait=True, cancel_futures=True)

    def metadata(self, mapping):
        with self._lock:
            self._ensure_open()
            return self._metadata(mapping)

    def books(self, mapping, metadata):
        with self._lock:
            self._ensure_open()
            return self._books(mapping, metadata)

    def radar(self, items, chunk=50):
        """Top-of-book for many races in a few bulk public requests; no SIG access.

        items: [(mapping, metadata)]. Returns ({exchange: [kalshi, polymarket]}, {exchange: reason}),
        both books oriented to SIG YES. Races with an inactive or closed venue are skipped.
        """
        with self._lock:
            self._ensure_open()
            books, skipped = {}, {}
            for start in range(0, len(items), chunk):
                part = items[start:start+chunk]
                tickers = [m['kalshi_ticker'] for m, _ in part]
                kalshi = {r['ticker']: r for r in self.kalshi.request(
                    '/markets', params={'tickers': ','.join(tickers), 'limit': len(tickers)})['markets']}
                kt, k_at = self.kalshi.last_timing, self.kalshi.server_at
                poly = {str(r['asset_id']): r for r in self.clob.request(
                    '/books', method='POST', payload=[{'token_id': md[2]} for _, md in part])}
                pt = self.clob.last_timing
                for mapping, (_, market, token) in part:
                    exchange = mapping['sig_exchange_id']
                    try:
                        k = kalshi.get(mapping['kalshi_ticker'])
                        p = poly.get(str(token))
                        if k is None or p is None:
                            raise ValueError('missing_from_bulk_response')
                        if k['status'] != 'active' or k.get('result'):
                            raise ValueError('kalshi_not_active')
                        if p.get('market') != market['conditionId']:
                            raise ValueError('polymarket_condition_mismatch')
                        kb = Book.make([(k['yes_bid_dollars'], k['yes_bid_size_fp'])],
                                       [(k['yes_ask_dollars'], k['yes_ask_size_fp'])], k_at, venue='Kalshi',
                                       timestamp_basis='http_date_minus_cache_age',
                                       observed_at=kt['received_at'] if kt else None, transport=kt)
                        pb = polymarket_book(p, pt, self.clob.server_at)
                        pair = [kb, pb]
                        for i, field in enumerate(('kalshi_yes_matches_sig_yes', 'polymarket_yes_matches_sig_yes')):
                            if not isinstance(mapping[field], bool):
                                raise ValueError('orientation_required')
                            if not mapping[field]:
                                pair[i] = pair[i].complement()
                        books[exchange] = pair
                    except (KeyError, TypeError, ValueError, ArithmeticError) as error:
                        skipped[exchange] = str(error)[:80]
            return books, skipped

    def _metadata(self, mapping):
        ticker = urllib.parse.quote(mapping['kalshi_ticker'], safe='')
        kalshi = self.kalshi.request('/markets/' + ticker)['market']
        from .strategy import D
        if (kalshi['ticker'] != mapping['kalshi_ticker'] or
                kalshi.get('market_type') != 'binary' or
                D(kalshi['notional_value_dollars']) != 1):
            raise ValueError('Expected the specified Kalshi binary contract with $1 payout')
        event = self.gamma.request('/events/slug/' + urllib.parse.quote(mapping['polymarket_event'], safe=''))
        markets = [m for m in event['markets'] if m['slug'] == mapping['polymarket_market']]
        if len(markets) != 1:
            raise ValueError("Polymarket market missing or ambiguous")
        poly = markets[0]
        outcomes = json.loads(poly['outcomes']) if isinstance(poly['outcomes'], str) else poly['outcomes']
        tokens = json.loads(poly['clobTokenIds']) if isinstance(poly['clobTokenIds'], str) else poly['clobTokenIds']
        if outcomes != ['Yes', 'No'] or len(tokens) != 2:
            raise ValueError("Unsupported Polymarket outcomes")
        return kalshi, poly, tokens[0]

    def _books(self, mapping, metadata):
        kalshi, poly, token = metadata
        if kalshi['status'] != 'active' or kalshi.get('result'):
            raise ValueError("Kalshi market is not active")
        if not poly.get('active') or poly.get('closed') or not poly.get('acceptingOrders'):
            raise ValueError("Polymarket market is not accepting orders")
        # Independent public hosts, separate HTTP clients; SIG writes stay serial.
        if self._pool is None:
            self._pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix='reference-feed')
        submitted = []
        try:
            kalshi_future = self._pool.submit(self.kalshi.request,
                '/markets/' + urllib.parse.quote(mapping['kalshi_ticker'], safe='') + '/orderbook',
                params={'depth': 100})
            submitted.append(kalshi_future)
            poly_future = self._pool.submit(self.clob.request, '/book', params={'token_id': token})
            submitted.append(poly_future)
            data = kalshi_future.result()['orderbook_fp']
            poly_data = poly_future.result()
        finally:
            # Also drain work if starting the second request itself fails.
            wait(submitted)
        from .strategy import D
        kt, pt = self.kalshi.last_timing, self.clob.last_timing
        kb = Book.make(data['yes_dollars'], [(1-D(p), q) for p, q in data['no_dollars']],
                       self.kalshi.server_at, venue='Kalshi', timestamp_basis='http_date_minus_cache_age',
                       observed_at=kt['received_at'] if kt else None, transport=kt)
        data = poly_data
        if str(data['asset_id']) != token or data['market'] != poly['conditionId']:
            raise ValueError("Polymarket token/condition mismatch")
        pb = polymarket_book(data, pt, self.clob.server_at)
        books = [kb, pb]
        for i, field in enumerate(('kalshi_yes_matches_sig_yes', 'polymarket_yes_matches_sig_yes')):
            if not isinstance(mapping[field], bool):
                raise ValueError("Explicit outcome orientation required")
            if not mapping[field]:
                books[i] = books[i].complement()
        return books


def sig_contract_record(sig_market, mapping):
    exchanges = [x for x in sig_market['exchanges'] if str(x['id']) == mapping['sig_exchange_id']]
    if len(exchanges) != 1:
        raise ValueError("Exchange does not belong to SIG market")
    return {'id': sig_market['id'], 'title': sig_market['title'],
                    'exchange': mapping['sig_exchange_id'], 'option': exchanges[0]['option'],
                    'settlementDate': sig_market.get('settlementDate'),
                    'resolution_tree': sig_market['resolution_tree']}


def contract_record(sig_market, mapping, metadata):
    kalshi, poly, token = metadata
    return {'sig': sig_contract_record(sig_market, mapping),
            'kalshi': {k: kalshi.get(k) for k in ('ticker', 'title', 'rules_primary', 'rules_secondary')},
            'polymarket': {k: poly.get(k) for k in ('id', 'question', 'description', 'conditionId')},
            'orientations': [mapping['kalshi_yes_matches_sig_yes'], mapping['polymarket_yes_matches_sig_yes']],
            'poly_yes_token': token}
