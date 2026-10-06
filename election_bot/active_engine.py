"""Inventory-aware execution with reusable capital and measured decisions."""
from datetime import datetime, timezone
import json
import math
import time
import uuid

from .clients import APIError, contract_record, fingerprint, iso_time
from .engine import Engine
from .ledger import Ledger
from .scanner import ScanInterest
from .shadow import decisions as shadow_decisions
from .risk import Exposure, LOSS_FIELD, verify_account_inventory
from .strategy import BookValidationError, D, choose, choose_exit


class InventoryValidationError(ValueError):
    def __init__(self, reason, message):
        super().__init__(message)
        self.reason = reason


class ActiveEngine(Engine):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.ledger = Ledger(self.journal)
        self.cache = {}
        self.execution = self.config['execution']
        self.scan_interest = ScanInterest()
        self.scan_policy = 'priority_v1' if self.execution.get('priority_scanning', True) else 'news_round_robin'
        self.scanner.set_policy(self.scan_policy)

    def check_account(self, account):
        now = time.time()
        if (account['id'] != self.config['tournament_id'] or account['id'] != self.sig.tid or
                account['status'] != 'active' or account.get('isPendingEnrolment') or
                now < iso_time(account['startDate']) or
                now + self.config['order_lifetime_seconds'] >= iso_time(account['endDate'])):
            raise ValueError('Tournament identity, enrollment or trading dates invalid')

    def finish(self, key, payload, response, reserved):
        if not isinstance(response.get('open'), bool):
            raise RuntimeError('Malformed acknowledgement; reservation retained')
        oid = response.get('orderId')
        if oid is None:
            if response['open'] or D(response['quantityTraded']):
                raise RuntimeError('Missing order ID; reservation retained')
            self.ledger.record(payload, 0, 0, self.config['strategy']['cost_buffer_per_share'])
            return
        order = self.sig.order(oid)
        if (str(order['exchangeId']) != payload['exchangeId'] or order['tournamentId'] != self.sig.tid
                or str(order['id']) != str(oid)):
            raise RuntimeError('Order identity mismatch; reservation retained')
        mismatch = (order['action'] != payload['action'] or order['side'] != payload['side']
                    or D(order['priceLimit']) != D(payload['price']))
        if response.get('action') is not None:
            mismatch = mismatch or response['action'] != payload['action'] or response['side'] != payload['side']
        if order['open']:
            self.sig.cancel(oid)
            for _ in range(4):
                order = self.sig.order(oid)
                if not order['open']:
                    break
                time.sleep(1)
        if (str(order['exchangeId']) != payload['exchangeId'] or order['tournamentId'] != self.sig.tid
                or str(order['id']) != str(oid)):
            raise RuntimeError('Final order identity mismatch; reservation retained')
        mismatch = mismatch or order['action'] != payload['action'] or order['side'] != payload['side']
        if mismatch:
            raise RuntimeError('Order was canonicalized unexpectedly; own remainder cancelled, trading halted')
        if order['open'] or order.get('quantityFilled') is None:
            raise RuntimeError('Closure/fill quantity unconfirmed; reservation retained')
        fills = self.sig.fills(oid)
        if (str(fills['orderId']) != str(oid) or str(fills['exchangeId']) != payload['exchangeId']
                or fills['tournamentId'] != self.sig.tid or fills.get('coverage', {}).get('complete') is not True):
            raise RuntimeError('Fill reporting identity/coverage mismatch; reservation retained')
        signed = D(fills['totalQuantityFilled'])
        if signed and (signed < 0) != (payload['side'] == 'no'):
            raise RuntimeError('Fill outcome mismatch; reservation retained')
        quantity = abs(signed)
        if quantity != D(order['quantityFilled']) or quantity < D(response['quantityTraded']):
            raise RuntimeError('Fill totals disagree; reservation retained')
        price = D(fills['avgFillPrice']) if quantity else D(0)
        if quantity and ((payload['action'] == 'buy' and price > D(payload['price'])) or
                         (payload['action'] == 'sell' and price < D(payload['price']))):
            raise RuntimeError('Fill outside limit; reservation retained')
        if quantity and not fills['data']:
            raise RuntimeError('Nonzero fill has no timestamped rows; reservation retained')
        at = max((iso_time(r['filledAt']) for r in fills['data']), default=iso_time(order['createdAt']))
        self.ledger.record(payload, quantity, price, self.config['strategy']['cost_buffer_per_share'], at)
        self.report('order_closed', {'order_id': oid, 'exchange': payload['exchangeId'],
                    'action': payload['action'], 'side': payload['side'], 'filled': quantity,
                    'average_fill_price': price, **self.ledger.summary()})
        self.check_loss_stop()

    def sync_history(self):
        for row in self.ledger.missing():
            payload = json.loads(row['payload'])
            if self.live:
                if not row['response']:
                    if D(row['amount']) == 0:
                        self.ledger.record(payload, 0, 0, self.config['strategy']['cost_buffer_per_share'], row['created'])
                        continue
                    raise RuntimeError('Legacy closed order lacks response; cannot import inventory')
                self.finish(row['key'], payload, json.loads(row['response']), D(row['amount']))
            else:
                # Legacy paper fills charged limit plus buffer; never query real fills.
                price, fee = D(payload['price']), D(self.config['strategy']['cost_buffer_per_share'])
                quantity = D(row['amount']) / (price + fee)
                self.ledger.record(payload, quantity, price, fee, row['created'])

    def reconcile(self, replay_unknown=False):
        if not self.live:
            # A crash between a paper reservation and its atomic fill has no execution.
            for row in self.journal.pending():
                self.ledger.record(json.loads(row['payload']), 0, 0,
                                   self.config['strategy']['cost_buffer_per_share'])
        else:
            super().reconcile(replay_unknown)
        self.sync_history()

    def metadata(self, mapping, force=False):
        key = mapping['name']
        cached = self.cache.get(key)
        if not force and cached and time.monotonic()-cached[0] < self.execution['metadata_cache_seconds']:
            return cached[1]
        # Never cache quotes. Every actionable order forces a new contract check.
        self.cache.pop(key, None)
        market = self.sig.market(mapping['sig_market_id'])
        if market['status'] != 'open':
            raise ValueError('SIG market closed')
        metadata = self.references.metadata(mapping)
        if fingerprint(contract_record(market, mapping, metadata)) != mapping.get('contract_fingerprint'):
            raise ValueError('Contract changed or unreviewed; inspect and pin')
        self.cache[key] = (time.monotonic(), metadata)
        return metadata

    def held(self, exchange, positions):
        owned, cost = self.ledger.held(exchange)
        if self.live:
            matching = [p for p in positions if str(p['exchangeId']) == exchange]
            actual = sum((D(p['quantity']) for p in matching), D(0))
            if any(p.get('settled') and D(p['quantity']) for p in matching):
                raise InventoryValidationError('settled_position', 'Settled position needs settlement accounting review')
            if actual != owned:
                raise InventoryValidationError('inventory_mismatch', 'Account inventory differs from bot ledger; race paused')
        return owned, cost

    def available(self, exchange, account):
        limits = self.config['limits']
        cash = D(account['myBalance'])
        if not self.live:
            # Paper inventory must consume paper cash without altering the real account.
            cash -= self.ledger.committed()
        return max(D(0), min(D(limits['per_order']), cash,
            D(limits['per_market']) - self.ledger.committed(exchange),
            D(limits['total']) - self.ledger.committed(),
            D(limits['daily']) - self.journal.used(today=True)))

    def loss_status(self):
        _, realized = self.ledger.inventory()
        net = sum(realized.values(), D(0))
        fraction = self.config['limits'].get(LOSS_FIELD)
        threshold = D(fraction) * D(self.config['limits']['total']) if fraction is not None else None
        return {'realized_pnl_after_buffers': net, 'net_realized_loss': max(D(0), -net),
                'realized_loss_stop_coins': threshold,
                'includes_unrealized_losses': False}

    def check_loss_stop(self):
        status = self.loss_status()
        threshold = status['realized_loss_stop_coins']
        if threshold is None or status['net_realized_loss'] < threshold:
            return False
        # A persistent STOP survives restart; resume rechecks the same loss threshold.
        already_stopped = self.stopped()
        self.runtime.mkdir(parents=True, exist_ok=True)
        (self.runtime / 'STOP').touch(mode=0o600)
        if not already_stopped:
            self.report('risk_stop', {'reason': 'net_realized_loss_limit', **status})
        return True

    def record_quote_snapshot(self, mapping, book, refs, phase):
        now = time.time()
        self.latest_snapshot_id = str(uuid.uuid4())
        quotes = []
        for venue, quote in zip(('SIG', 'Kalshi', 'Polymarket'), [book] + list(refs)):
            bid = quote.bids[0] if quote.bids else None
            ask = quote.asks[0] if quote.asks else None
            quotes.append({'venue': venue, 'bid': bid, 'ask': ask,
                'midpoint': (bid[0]+ask[0])/2 if bid and ask else None,
                'observed_at': quote.observed_at if math.isfinite(quote.observed_at) else None,
                'source_at': quote.source_at if math.isfinite(quote.source_at) else None,
                'timestamp_basis': quote.timestamp_basis,
                'quality': quote.diagnostic(self.config['strategy']['max_age_seconds'],
                    self.config['strategy']['max_reference_spread'] if venue != 'SIG' else None,
                    venue=venue, now=now)})
        # Journal only: avoid flooding the trading console with every quote.
        self.journal.event('quote_snapshot', {'version': 1, 'snapshot_id': self.latest_snapshot_id,
            'exchange': mapping['sig_exchange_id'], 'phase': phase, 'captured_at': now,
            'orientation': 'SIG YES (references already aligned)', 'quotes': quotes})

    def decide(self, mapping, account, positions, book, refs, phase='scan'):
        exchange = mapping['sig_exchange_id']
        self.record_quote_snapshot(mapping, book, refs, phase)
        exposure = Exposure(self.ledger, self.config)
        if exposure.enabled and self.live:
            verify_account_inventory(self.ledger, positions)
        held, cost = self.held(exchange, positions)
        side_limits = ({side: exposure.headroom(exchange, 'buy', side) for side in ('yes', 'no')}
                       if exposure.enabled else None)
        exit_limit = (exposure.headroom(exchange, 'sell', 'yes' if held > 0 else 'no')
                      if exposure.enabled and held else None)
        diagnostics = []
        exit_check = {'status': 'blocked' if held else 'not_applicable',
                      'reason': 'selling_disabled' if held else 'no_position'}
        entry = choose(book, refs, self.config['strategy'], self.available(exchange, account), diagnostics, side_limits)
        exit_signal = choose_exit(book, refs, self.config['strategy'], self.execution, held, cost,
                                  self.config['limits']['per_order'], exit_check, exit_limit) if self.execution['sell_enabled'] else None
        if exit_signal and exit_signal.reason == 'convergence_take_profit':
            basis = self.ledger.sale_basis(exchange, exit_signal.side, exit_signal.quantity)
            net = exit_signal.price - D(self.config['strategy']['cost_buffer_per_share'])
            route = exit_check['routes']['convergence_take_profit']
            route.update(fifo_cost_per_share=str(basis/exit_signal.quantity),
                         net_profit_per_share_fifo=str(net-basis/exit_signal.quantity),
                         fifo_profit_passed=net-basis/exit_signal.quantity >= D(self.execution['take_profit_min']))
            if net - basis / exit_signal.quantity < D(self.execution['take_profit_min']):
                exit_check.update(status='blocked', reason='fifo_profit_below_minimum')
                exit_signal = None  # An average-cost gain need not be a gain on the FIFO shares sold.
        if entry and held and ((held < 0) != (entry.side == 'no')):
            entry = None  # Never use a complement buy to close or flip inventory.
            diagnostics.append({'reason': 'opposite_inventory'})
        signal = exit_signal or entry
        self.scan_interest.observe(mapping['name'], diagnostics, refs, self.config['strategy'], held)
        self.report('decision', {'exchange': exchange, 'held': held, 'available': self.available(exchange, account),
            'checks': diagnostics, 'exit_check': exit_check, 'phase': phase,
            'snapshot_id': self.latest_snapshot_id, 'exposure': exposure.summary(),
            'action': signal.action if signal else None,
            'reason': signal.reason if signal else 'no_eligible_signal'})
        if phase == 'scan':
            try:
                shadow = shadow_decisions(book, refs, self.config['strategy'],
                    self.available(exchange, account), held, side_limits, exit_signal is not None)
            except ValueError as error:
                # A quote can age out during this extra calculation. Measurement
                # failure must not change the already selected live decision.
                shadow = {'version': 1, 'experiment': 'reference_bid_v1',
                          'status': 'unavailable', 'reason': str(error)}
            self.journal.event('shadow_decision', {**shadow, 'exchange': exchange,
                'snapshot_id': self.latest_snapshot_id, 'phase': phase})
        return signal

    def quote_diagnostics(self, exchange, book, refs, phase, error=None):
        settings, now = self.config['strategy'], time.time()
        details = []
        if book is not None:
            details.append(book.diagnostic(settings['max_age_seconds'], venue='SIG', now=now))
        for venue, ref in zip(('Kalshi', 'Polymarket'), refs or []):
            details.append(ref.diagnostic(settings['max_age_seconds'], settings['max_reference_spread'], venue, now))
        if isinstance(error, BookValidationError) and not any(d['issues'] for d in details):
            details.append(error.detail)
        if any(d['issues'] for d in details):
            self.report('quote_diagnostics', {'exchange': exchange, 'phase': phase, 'books': details})
        if isinstance(error, APIError):
            self.report('feed_error', {'exchange': exchange, 'phase': phase,
                'venue': error.venue or 'unknown', 'status': error.status, 'reason': str(error)})

    def exit_blocked(self, exchange, reason, phase, detail=None):
        held, _ = self.ledger.held(exchange)
        if held:
            self.report('exit_blocked', {'exchange': exchange, 'phase': phase,
                'held': held, 'status': 'not_evaluated', 'reason': reason, 'detail': detail})

    def observe_due(self):
        """Bounded SIG-only reads; missing external quotes cannot block measurement."""
        for exchange in self.ledger.due_exchanges():
            if self.stopped():
                return
            book = None
            try:
                book = self.sig.book(exchange)
                self.ledger.observe(exchange, book, self.config['strategy']['max_age_seconds'])
            except APIError as error:
                self.quote_diagnostics(exchange, book, None, 'performance_observation', error)
                if error.status in (401, 403, 429):
                    raise
                self.report('performance_observation_skip', {'exchange': exchange, 'reason': str(error)})
            except (ValueError, KeyError) as error:
                self.quote_diagnostics(exchange, book, None, 'performance_observation', error)
                self.report('performance_observation_skip', {'exchange': exchange, 'reason': str(error)})

    def scan_hints(self):
        if not self.execution.get('priority_scanning', True):
            return None
        holdings, _ = self.ledger.inventory()
        exchanges = {ex for (ex, side), values in holdings.items() if values['quantity']}
        owned = {m['name'] for m in self.markets if m['sig_exchange_id'] in exchanges}
        last_orders = {r['market']: r['last'] for r in self.journal.db.execute(
            'SELECT market,MAX(created) AS last FROM orders GROUP BY market')}
        not_before = {m['name']: (last_orders.get(m['sig_exchange_id']) or 0) + self.config['cooldown_seconds']
                      for m in self.markets}
        return self.scan_interest.hints(owned, not_before)

    def cycle(self):
        self.reconcile()
        self.check_loss_stop()
        if self.stopped():
            return False
        if not self.markets:
            raise ValueError('No enabled mappings')
        if self.config.get('news', {}).get('enabled') and self.news is None:
            raise ValueError('News monitor is required')
        account = self.sig.account()
        self.check_account(account)
        self.report('portfolio_risk', {**Exposure(self.ledger, self.config).summary(), **self.loss_status()})
        self.observe_due()
        if self.sig.open_orders():
            self.report('skip', {'reason': 'Existing open orders; trading paused'})
            holdings, _ = self.ledger.inventory()
            for exchange in {ex for ex, side in holdings}:
                self.exit_blocked(exchange, 'open_orders', 'scan')
            return True
        positions = self.sig.positions()
        for name, events in (self.news.drain() if self.news else {}).items():
            self.pending_news[name] = (self.pending_news.get(name, []) + events)[-100:]
        orders, started, checked = 0, time.monotonic(), 0
        self.report('scan_batch', {'enabled_races': len(self.markets),
                    'max_orders': self.execution['max_orders_per_cycle'], 'scan_policy': self.scan_policy,
                    **self.ledger.summary()})
        for mapping in self.scanner.batch(self.pending_news, self.config.get('scan_batch_size', 8), self.scan_hints()):
            if self.stopped():
                return False
            exchange = mapping['sig_exchange_id']
            self.report('scan_visit', {'exchange': exchange, 'market': mapping['name'],
                        'scan_policy': self.scan_policy, **self.scanner.selection})
            book, refs, phase = None, None, 'scan'
            try:
                if time.time()-self.journal.last_order(exchange) < self.config['cooldown_seconds']:
                    self.report('skip', {'exchange': exchange, 'reason': 'cooldown'})
                    self.exit_blocked(exchange, 'cooldown', phase)
                    continue
                context = self.pending_news.pop(mapping['name'], [])
                metadata = self.metadata(mapping, bool(context))
                refs = self.references.books(mapping, metadata)
                book = self.sig.book(exchange)
                checked += 1
                self.ledger.observe(exchange, book, self.config['strategy']['max_age_seconds'])
                block = self.news.block_reason(mapping) if self.news else None
                if block:
                    self.scan_interest.invalidate(mapping['name'])
                    self.report('skip', {'exchange': exchange, 'reason': block})
                    self.exit_blocked(exchange, 'news_pause', phase, block)
                    continue
                signal = self.decide(mapping, account, positions, book, refs)
                self.report('scan_quote', {'exchange': exchange, 'market': mapping['name'],
                    'scan_policy': self.scan_policy, **self.scanner.selection,
                    'quote_revisit_seconds': self.scanner.record_quote(mapping['name'])})
                if context:
                    self.report('news_review', {'exchange': exchange, 'market': mapping['name'],
                        'paper_only': True, 'news': context,
                        'proposed_price_signal': vars(signal) if signal else None,
                        'method': 'Price and inventory rules only; news never sets valuation'})
                if signal is None:
                    continue
                # Serial final checks: cached rules/old positions never authorize an order.
                phase, book, refs = 'preflight', None, None
                metadata = self.metadata(mapping, force=True)
                account = self.sig.account()
                self.check_account(account)
                if self.sig.open_orders():
                    self.report('skip', {'exchange': exchange, 'reason': 'Open order appeared during scan'})
                    self.exit_blocked(exchange, 'open_orders', phase)
                    return True
                positions = self.sig.positions()
                refs = self.references.books(mapping, metadata)
                book = self.sig.book(exchange)
                signal = self.decide(mapping, account, positions, book, refs, phase=phase)
                if signal is None:
                    continue
            except (ValueError, KeyError, APIError) as error:
                self.cache.pop(mapping['name'], None)
                self.scan_interest.invalidate(mapping['name'])
                self.quote_diagnostics(exchange, book, refs, phase, error)
                exit_reason = (error.reason if isinstance(error, InventoryValidationError) else
                               'quote_validation' if isinstance(error, BookValidationError) else
                               'feed_error' if isinstance(error, APIError) else 'validation_error')
                self.exit_blocked(exchange, exit_reason, phase, str(error))
                if isinstance(error, APIError) and error.status in (401, 403, 429):
                    raise
                self.report('skip', {'exchange': exchange, 'reason': str(error)})
                continue
            if self.stopped():
                return False
            if self.news and self.news.block_reason(mapping):
                self.report('skip', {'exchange': exchange, 'reason': 'News pause during preflight'})
                self.exit_blocked(exchange, 'news_pause', 'pre_submit')
                continue
            try:
                book.check(self.config['strategy']['max_age_seconds'], venue='SIG')
                for venue, ref in zip(('Kalshi', 'Polymarket'), refs):
                    ref.check(self.config['strategy']['max_age_seconds'], venue=venue)
            except BookValidationError as error:
                self.quote_diagnostics(exchange, book, refs, 'pre_submit', error)
                self.exit_blocked(exchange, 'quote_validation', 'pre_submit', str(error))
                raise
            self.check_account(account)  # Recheck the close cutoff after quote requests.
            if self.check_loss_stop():
                return False
            exposure = Exposure(self.ledger, self.config)
            if exposure.enabled:
                if self.live:
                    verify_account_inventory(self.ledger, positions)
                if signal.quantity > exposure.headroom(exchange, signal.action, signal.side):
                    self.report('skip', {'exchange': exchange, 'reason': 'Exposure changed before submission'})
                    continue
            payload = {'exchangeId': exchange, 'side': signal.side, 'action': signal.action,
                'quantity': signal.quantity, 'price': float(signal.price), 'tournamentId': self.sig.tid,
                'idempotencyKey': str(uuid.uuid4()), 'expirationDate': datetime.fromtimestamp(
                    time.time()+self.config['order_lifetime_seconds'], timezone.utc).isoformat()}
            fee = D(self.config['strategy']['cost_buffer_per_share'])
            reserve = signal.quantity * (signal.price + fee)
            self.journal.reserve(payload, reserve)
            if self.stopped() or (self.news and self.news.block_reason(mapping)):
                self.ledger.record(payload, 0, 0, fee)
                return not self.stopped()
            self.report('signal', {'exchange': exchange, 'order_key': payload['idempotencyKey'],
                                   'snapshot_id': self.latest_snapshot_id, **vars(signal)})
            if self.live:
                response = self.sig.place(payload)
                self.journal.response(payload['idempotencyKey'], response)
                self.finish(payload['idempotencyKey'], payload, response, reserve)
            else:
                self.ledger.record(payload, signal.quantity, signal.price, fee)
                self.report('paper_fill', {'exchange': exchange, **vars(signal), 'simulation': True})
            orders += 1
            self.check_loss_stop()
            if self.stopped():
                return False
            if orders >= self.execution['max_orders_per_cycle']:
                break
            account = self.sig.account()
            self.check_account(account)
            positions = self.sig.positions()
        self.report('scan_complete', {'quotes_checked': checked, 'orders': orders,
                    'seconds': round(time.monotonic()-started, 2), 'scan_policy': self.scan_policy,
                    **self.ledger.summary()})
        return True
