"""Documented public venue feeds and authenticated SIG competition API."""
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
from email.utils import parsedate_to_datetime
import hashlib
import json
import socket
import ssl
import time
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
    def __init__(self, message, status=None, retry_after=None, method=None, venue=None):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after
        self.method = method
        self.venue = venue


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

    def request(self, path, method='GET', params=None, payload=None):
        if not path.startswith('/') or '://' in path or '..' in path:
            raise ValueError("Invalid API path")
        if method != 'GET' and self.base != 'https://sig.thesuper.market/api/v1':
            raise ValueError("External venues are read-only")
        if self.key:
            attr, interval = ('last_read', 0.8) if method == 'GET' else ('last_write', 2.2)
            time.sleep(max(0, getattr(self, attr) + interval - time.monotonic()))
            setattr(self, attr, time.monotonic())
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
        try:
            with self.opener.open(request, timeout=8) as response:
                body = response.read(4_000_001)
                if len(body) > 4_000_000:
                    raise APIError("API response too large", venue=self.venue)
                date = response.headers.get('Date')
                age = float(response.headers.get('Age', '0'))
                self.server_at = parsedate_to_datetime(date).timestamp() - age if date else started
                return json.loads(body)
        except urllib.error.HTTPError as error:
            # Response bodies can contain user data; never log them or the key.
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
                           status=error.code, retry_after=retry_after, method=method, venue=self.venue) from None
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

    def positions(self):
        return self.read('/tournaments/' + self.slug + '/portfolio/positions')['positions']

    def place(self, payload):
        if payload['tournamentId'] != self.tid:
            raise ValueError("Wrong tournament")
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
                if error.status not in (502, 503, 504) or attempt == 2 or delay > 5:
                    raise
                print('SIG read {} temporarily unavailable (HTTP {}); retrying in {}s'.format(
                    path, error.status, delay), flush=True)
                time.sleep(delay)


class References:
    def __init__(self):
        self.kalshi = HTTP('https://external-api.kalshi.com/trade-api/v2')
        self.gamma = HTTP('https://gamma-api.polymarket.com')
        self.clob = HTTP('https://clob.polymarket.com')

    def metadata(self, mapping):
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

    def books(self, mapping, metadata):
        kalshi, poly, token = metadata
        if kalshi['status'] != 'active' or kalshi.get('result'):
            raise ValueError("Kalshi market is not active")
        if not poly.get('active') or poly.get('closed') or not poly.get('acceptingOrders'):
            raise ValueError("Polymarket market is not accepting orders")
        # Independent public hosts, separate HTTP clients; SIG writes stay serial.
        with ThreadPoolExecutor(max_workers=2) as pool:
            kalshi_future = pool.submit(self.kalshi.request,
                '/markets/' + urllib.parse.quote(mapping['kalshi_ticker'], safe='') + '/orderbook',
                params={'depth': 100})
            poly_future = pool.submit(self.clob.request, '/book', params={'token_id': token})
            data = kalshi_future.result()['orderbook_fp']
            poly_data = poly_future.result()
        from .strategy import D
        kb = Book.make(data['yes_dollars'], [(1-D(p), q) for p, q in data['no_dollars']],
                       self.kalshi.server_at, venue='Kalshi', timestamp_basis='http_date_minus_cache_age')
        data = poly_data
        if str(data['asset_id']) != token or data['market'] != poly['conditionId']:
            raise ValueError("Polymarket token/condition mismatch")
        pb = Book.make([(r['price'], r['size']) for r in data['bids']],
                       [(r['price'], r['size']) for r in data['asks']],
                       float(data['timestamp']) / 1000, venue='Polymarket', timestamp_basis='exchange_book_timestamp')
        books = [kb, pb]
        for i, field in enumerate(('kalshi_yes_matches_sig_yes', 'polymarket_yes_matches_sig_yes')):
            if not isinstance(mapping[field], bool):
                raise ValueError("Explicit outcome orientation required")
            if not mapping[field]:
                books[i] = books[i].complement()
        return books


def contract_record(sig_market, mapping, metadata):
    kalshi, poly, token = metadata
    exchanges = [x for x in sig_market['exchanges'] if str(x['id']) == mapping['sig_exchange_id']]
    if len(exchanges) != 1:
        raise ValueError("Exchange does not belong to SIG market")
    return {'sig': {'id': sig_market['id'], 'title': sig_market['title'],
                    'exchange': mapping['sig_exchange_id'], 'option': exchanges[0]['option'],
                    'settlementDate': sig_market.get('settlementDate'),
                    'resolution_tree': sig_market['resolution_tree']},
            'kalshi': {k: kalshi.get(k) for k in ('ticker', 'title', 'rules_primary', 'rules_secondary')},
            'polymarket': {k: poly.get(k) for k in ('id', 'question', 'description', 'conditionId')},
            'orientations': [mapping['kalshi_yes_matches_sig_yes'], mapping['polymarket_yes_matches_sig_yes']],
            'poly_yes_token': token}
