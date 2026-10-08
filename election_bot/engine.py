from datetime import datetime, timezone
import json
import re
from pathlib import Path
import time
import uuid

from .clients import APIError, contract_record, fingerprint, iso_time
from .strategy import D, choose
from .news import validate_news
from .scanner import Scanner
from .profit_exit import profit_exit_mode
from .clock_guard import ClockSampleUnavailable, SubmissionClockUnavailable


def validate_config(config):
    execution = config.get('execution', {})
    if execution.get('enabled'):
        if type(execution.get('priority_scanning', True)) is not bool:
            raise ValueError('priority_scanning must be boolean')
        if type(execution.get('sell_enabled')) is not bool:
            raise ValueError('sell_enabled must be boolean')
        if type(execution.get('profit_target_enabled', False)) is not bool:
            raise ValueError('profit_target_enabled must be boolean')
        if profit_exit_mode(execution) not in ('fixed', 'trend'):
            raise ValueError('profit_exit_mode must be fixed or trend')
        from .exit_trend import settings as trend_settings
        trend_settings(execution)
        if execution.get('profit_exit_mode') == 'trend' and not execution.get('profit_target_enabled'):
            raise ValueError('Trend profit exits require profit_target_enabled')
        reentry = execution.get('reentry_cooldown_seconds', 0)
        if type(reentry) is not int or not 0 <= reentry <= 86400:
            raise ValueError('reentry_cooldown_seconds must be an integer from 0 to 86400')
        if execution.get('profit_target_enabled') and reentry < 60:
            raise ValueError('Profit-target exits require at least 60 seconds of reentry cooldown')
        for field, low, high in [('max_orders_per_cycle', 1, 10),
                                  ('metadata_cache_seconds', 0, 1800), ('batch_pause_seconds', 1, 20)]:
            if type(execution.get(field)) is not int or not low <= execution[field] <= high:
                raise ValueError(field + ' is outside the supported range')
        for field in ('exit_edge', 'take_profit_min'):
            if not 0 < D(execution[field]) < 1:
                raise ValueError(field + ' must be between zero and one')
    settings = config['strategy']
    for name in ('max_age_seconds', 'min_reference_depth', 'max_shares_per_order',
                 'minimum_edge', 'max_reference_spread', 'max_reference_disagreement'):
        if D(settings[name]) <= 0:
            raise ValueError(name + ' must be positive')
    if D(settings['cost_buffer_per_share']) < 0:
        raise ValueError('Cost buffer must be nonnegative')
    shares = D(settings['max_shares_per_order'])
    if shares != int(shares) or shares > 2147483647:
        raise ValueError('Maximum shares must be an integer within the SIG order limit')
    for name in ('per_order', 'per_market', 'total', 'daily'):
        if D(config['limits'][name]) <= 0:
            raise ValueError(name + ' budget must be positive')
    if not (10 <= config['order_lifetime_seconds'] <= 60):
        raise ValueError('Order lifetime must be 10–60 seconds')
    if config['poll_seconds'] < 10 or config['cooldown_seconds'] < 10:
        raise ValueError('Poll/cooldown must be at least 10 seconds')
    from .risk import validate_risk_config
    if not isinstance(config.get('markets'), list):
        raise ValueError('markets must be a list')
    names = set()
    for mapping in config['markets']:
        if not isinstance(mapping, dict) or type(mapping.get('enabled')) is not bool:
            raise ValueError('Each mapping requires a boolean enabled field')
        if not mapping['enabled']:
            continue
        label = mapping.get('name', '<unnamed>')
        for field in ('name', 'sig_market_id', 'sig_exchange_id', 'kalshi_ticker',
                      'polymarket_event', 'polymarket_market'):
            value = mapping.get(field)
            if not isinstance(value, str) or not value.strip() or value != value.strip():
                raise ValueError('Mapping %s requires a nonempty %s' % (label, field))
            if value.startswith('REPLACE_'):
                raise ValueError('Mapping %s still has a placeholder %s' % (label, field))
        for field in ('sig_market_id', 'sig_exchange_id'):
            if not re.fullmatch(r'[1-9][0-9]*', mapping[field]):
                raise ValueError('Mapping %s requires a positive integer string %s' % (label, field))
        for field in ('kalshi_yes_matches_sig_yes', 'polymarket_yes_matches_sig_yes'):
            if type(mapping.get(field)) is not bool:
                raise ValueError('Mapping %s requires a boolean %s' % (label, field))
        if not isinstance(mapping.get('contract_fingerprint'), str) or not re.fullmatch(
                r'[0-9a-f]{64}', mapping['contract_fingerprint']):
            raise ValueError('Mapping %s requires a reviewed contract_fingerprint' % label)
        if label in names:
            raise ValueError('Enabled mapping names must be unique: ' + label)
        names.add(label)
    validate_risk_config(config)
    if execution.get("enabled"):
        from .race_controls import configured_races
        configured_races(config)
    validate_news(config)
    exchanges = [m['sig_exchange_id'] for m in config['markets'] if m.get('enabled')]
    if len(exchanges) != len(set(exchanges)):
        raise ValueError('Each SIG exchange may appear only once')
    if len(exchanges) > 600:
        raise ValueError('At most 600 enabled mappings are supported')
    batch = config.get('scan_batch_size', 8)
    if type(batch) is not int or not 1 <= batch <= 20:
        raise ValueError('scan_batch_size must be an integer from 1 to 20')
    enabled = [m for m in config['markets'] if m.get('enabled')]
    races = [m.get('race_key', m['sig_market_id']) for m in enabled]
    from .settlement import validate_multicontract_config
    validate_multicontract_config(config)
    if len(races) != len(set(races)) and not execution.get("multi_contract_races", False):
        raise ValueError('Enable only one contract per race; alternate parties share the same risk')


class Engine:
    def __init__(self, config, sig, references, journal, runtime, live=False, news=None):
        validate_config(config)
        self.config, self.sig, self.references = config, sig, references
        self.journal, self.runtime, self.live = journal, Path(runtime), live
        self.news = news
        self.markets = [m for m in config['markets'] if m.get('enabled')]
        self.scanner = Scanner(self.markets, self.runtime)
        self.pending_news = {}
        self.clock_reported = False
        self.latest_snapshot_id = None
        self.last_maintenance = None

    def check_clock(self):
        checker = getattr(self.sig, 'check_clock', None)
        if checker is None:
            return  # Synthetic offline brokers have no SIG HTTP clock evidence.
        from .clock_guard import ClockCheckError
        try:
            result = checker()
        except ClockSampleUnavailable as error:
            self.clock_reported = False
            self.journal.event('clock_sample_unavailable', {'reason': str(error), **error.detail})
            raise
        except ClockCheckError as error:
            self.journal.event('clock_halt', {'reason': str(error)})
            raise
        if not self.clock_reported:
            self.report('clock_check', result)
            self.clock_reported = True

    def report(self, kind, detail):
        self.journal.event(kind, detail)
        print(json.dumps({'event': kind, **detail}, default=str), flush=True)

    def maintenance(self):
        now = time.monotonic()
        if self.last_maintenance is None or now-self.last_maintenance >= 300:
            from .maintenance import archive_events
            result = archive_events(self.journal, self.runtime)
            self.last_maintenance = now
            if result:
                self.report('event_archive', result)

    def stopped(self):
        return (self.runtime / 'STOP').exists()

    def reconcile(self, replay_unknown=False):
        """Only bot-owned IDs are cancelled. Unknown submissions stay reserved."""
        if not self.live:
            for row in self.journal.pending():
                # A paper crash cannot have placed an exchange order.
                self.journal.complete(row['key'], 0)
            return
        for row in self.journal.pending():
            payload = json.loads(row['payload'])
            response = json.loads(row['response']) if row['response'] else None
            if response is None:
                if not replay_unknown:
                    raise RuntimeError('Unknown order status. Run recover after its expiration; no new trades allowed.')
                if time.time() < iso_time(payload['expirationDate']) + 5:
                    raise RuntimeError('Wait until original order expiration plus five seconds before recover')
                # Identical payload/key only. Never refresh the expiration or create a retry key.
                response = self.sig.place(payload)
                self.journal.response(row['key'], response)
            self.finish(row['key'], payload, response, D(row['amount']))

    def finish(self, key, payload, response, reserved):
        oid = response.get('orderId')
        if not isinstance(response.get('open'), bool):
            raise RuntimeError('Malformed order acknowledgement; reservation retained')
        initial_filled = D(response['quantityTraded'])
        if not (0 <= initial_filled <= payload['quantity']):
            raise RuntimeError('Unexpected fill quantity; reservation retained')
        filled = initial_filled
        if oid is not None:
            order = self.sig.order(oid)
            if str(order['exchangeId']) != payload['exchangeId'] or order['tournamentId'] != self.sig.tid:
                raise RuntimeError('Order identity mismatch; reservation retained')
            if order['open']:
                self.sig.cancel(oid)
                for attempt in range(4):
                    order = self.sig.order(oid)
                    if not order['open']:
                        break
                    time.sleep(1)
            if order['open']:
                raise RuntimeError('Cancellation not confirmed; reservation retained, trading halted')
            if order.get('quantityFilled') is None:
                # Incomplete reporting: retain the entire cost bound permanently.
                self.journal.complete(key, reserved)
                self.report('closed_conservatively', {'order_id': oid, 'cost_bound': reserved})
                return
            filled = max(filled, D(order['quantityFilled']))
        elif response['open'] or initial_filled:
            raise RuntimeError('Missing ID for an open or filled order; reservation retained')
        if not (0 <= filled <= payload['quantity']):
            raise RuntimeError('Unexpected final fill quantity; reservation retained')
        bound = filled * (D(payload['price']) + D(self.config['strategy']['cost_buffer_per_share']))
        # totalCost includes immediate fills; use it if greater than our limit-price bound.
        bound = max(bound, D(response.get('totalCost', 0)))
        self.journal.complete(key, bound)
        self.report('order_closed', {'order_id': oid, 'filled': filled, 'cost_bound': bound})

    def cycle(self):
        self.reconcile()
        if self.stopped():
            return False
        if not self.markets:
            raise ValueError('No enabled market mappings; inspect and pin one first')
        if self.config.get('news', {}).get('enabled') and self.news is None:
            raise ValueError('News is enabled but its monitor has not been initialized')
        account = self.sig.account()
        self.check_clock()
        now = time.time()
        if (account['status'] != 'active' or account.get('isPendingEnrolment') or
                not account.get('startDate') or not account.get('endDate') or
                now < iso_time(account['startDate']) or
                now + self.config['order_lifetime_seconds'] >= iso_time(account['endDate'])):
            raise ValueError('Tournament is not active/enrolled within trading dates')
        if account['id'] != self.config['tournament_id']:
            raise ValueError('Configured tournament ID does not match the API')
        if self.sig.open_orders():
            self.report('skip', {'reason': 'Existing open orders; the bot will not cancel orders it did not create'})
            return True
        positions = self.sig.positions()
        for name, events in (self.news.drain() if self.news else {}).items():
            self.pending_news[name] = (self.pending_news.get(name, []) + events)[-100:]
        markets = self.scanner.batch(self.pending_news, self.config.get('scan_batch_size', 8))
        self.report('scan_batch', {'enabled_races': len(self.markets),
                                 'batch_limit': self.config.get('scan_batch_size', 8),
                                 'news_waiting': len(self.pending_news)})
        for mapping in markets:
            if self.stopped():
                return False
            exchange = mapping['sig_exchange_id']
            context = self.pending_news.pop(mapping['name'], [])
            cooling_down = time.time() - self.journal.last_order(exchange) < self.config['cooldown_seconds']
            block = self.news.block_reason(mapping) if self.news else None
            if block and not context:
                self.report('skip', {'exchange': exchange, 'reason': block})
                continue
            if cooling_down and not context:
                continue
            try:
                sm = self.sig.market(mapping['sig_market_id'])
                if sm['status'] != 'open':
                    raise ValueError('SIG market is closed')
                metadata = self.references.metadata(mapping)
                record = contract_record(sm, mapping, metadata)
                if not mapping.get('contract_fingerprint') or fingerprint(record) != mapping['contract_fingerprint']:
                    raise ValueError('Contract mapping is unreviewed or its text changed; inspect and pin')
                refs = self.references.books(mapping, metadata)
                book = self.sig.book(exchange)
                limits = self.config['limits']
                available = min(D(limits['per_order']), D(account['myBalance']),
                                D(limits['per_market']) - self.journal.used(market=exchange),
                                D(limits['total']) - self.journal.used(),
                                D(limits['daily']) - self.journal.used(today=True))
                snapshot = {'exchange': exchange, 'available': available,
                            'news_event_ids': [n['event_id'] for n in context],
                            'books': [vars(b) for b in [book] + refs]}
                self.journal.event('snapshot', snapshot)
                signal = choose(book, refs, self.config['strategy'], max(D(0), available))
                if context:
                    self.report('news_review', {'exchange': exchange, 'market': mapping['name'],
                        'paper_only': True, 'news': context,
                        'proposed_price_signal': vars(signal) if signal else None,
                        'entry_block': block or ('Order cooldown' if cooling_down else None),
                        'method': 'Existing price-gap strategy; news does not change valuation or size'})
                if block or cooling_down:
                    self.report('skip', {'exchange': exchange, 'reason': block or 'Order cooldown'})
                    continue
                if not signal:
                    self.report('skip', {'exchange': exchange, 'reason': 'No executable price gap within limits'})
                    continue
                held = sum((D(p['quantity']) for p in positions if str(p['exchangeId']) == exchange), D(0))
                if held and ((held < 0) != (signal.side == 'no')):
                    raise ValueError('Opposite-side position exists; entry-only bot will not net it')
            except (ValueError, KeyError, APIError) as error:
                if isinstance(error, APIError) and error.status in (401, 403, 429):
                    raise  # Authentication/rate limits affect the whole scanner.
                if context:
                    self.report('news_review', {'exchange': exchange, 'market': mapping['name'],
                        'paper_only': True, 'news': context, 'proposed_price_signal': None,
                        'entry_block': str(error)})
                self.report('skip', {'exchange': exchange, 'reason': str(error)})
                continue
            self.report('signal', {'exchange': exchange, **vars(signal)})
            if self.stopped():
                return False
            # A large batch can take time. Recheck available cash before reserving.
            fresh_account = self.sig.account()
            self.check_clock()
            if (fresh_account['id'] != self.sig.tid or fresh_account['status'] != 'active'
                    or fresh_account.get('isPendingEnrolment')
                    or time.time() + self.config['order_lifetime_seconds'] >= iso_time(fresh_account['endDate'])):
                raise ValueError('Tournament no longer active/enrolled')
            reserved = signal.quantity * (signal.price + D(self.config['strategy']['cost_buffer_per_share']))
            if D(fresh_account['myBalance']) < reserved:
                self.report('skip', {'exchange': exchange, 'reason': 'Account balance changed'})
                continue
            # Freshness is checked again immediately before durable submission.
            book.check(self.config['strategy']['max_age_seconds'])
            for reference in refs:
                reference.check(self.config['strategy']['max_age_seconds'])
            payload = {'exchangeId': exchange, 'side': signal.side, 'action': 'buy',
                       'quantity': signal.quantity, 'price': float(signal.price),
                       'tournamentId': self.sig.tid, 'idempotencyKey': str(uuid.uuid4()),
                       'expirationDate': datetime.fromtimestamp(
                           time.time() + self.config['order_lifetime_seconds'], timezone.utc).isoformat()}
            self.journal.reserve(payload, reserved)
            if self.stopped():
                self.journal.complete(payload['idempotencyKey'], 0)
                return False
            # The background monitor may flag a development during price fetching.
            if self.news:
                block = self.news.block_reason(mapping)
                if block:
                    self.journal.complete(payload['idempotencyKey'], 0)
                    self.report('skip', {'exchange': exchange, 'reason': block})
                    continue
            if self.live:
                try:
                    response = self.sig.place(payload)
                except SubmissionClockUnavailable:
                    # Only this pre-POST exception proves the new reservation is unused.
                    self.journal.complete(payload['idempotencyKey'], 0)
                    self.report('order_not_submitted', {'exchange': exchange,
                        'order_key': payload['idempotencyKey'], 'reason': 'clock_sample_unavailable'})
                    raise
                self.journal.response(payload['idempotencyKey'], response)
                self.finish(payload['idempotencyKey'], payload, response, reserved)
            else:
                self.journal.complete(payload['idempotencyKey'], reserved)
                self.report('paper_fill', {'exchange': exchange, 'side': signal.side,
                                          'quantity': signal.quantity, 'price': signal.price,
                                          'cost_bound': reserved, 'simulation': True})
            # Refresh account and positions next cycle before considering another order.
            return True
        return True
