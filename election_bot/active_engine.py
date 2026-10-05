"""Inventory-aware execution with reusable capital and measured decisions."""
from datetime import datetime, timezone
import json
import time
import uuid

from .clients import APIError, contract_record, fingerprint, iso_time
from .engine import Engine
from .ledger import Ledger
from .strategy import D, choose, choose_exit


class ActiveEngine(Engine):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.ledger = Ledger(self.journal)
        self.cache = {}
        self.execution = self.config['execution']

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
        at = max((iso_time(r['filledAt']) for r in fills['data']), default=iso_time(order['createdAt']))
        self.ledger.record(payload, quantity, price, self.config['strategy']['cost_buffer_per_share'], at)
        self.report('order_closed', {'order_id': oid, 'exchange': payload['exchangeId'],
                    'action': payload['action'], 'side': payload['side'], 'filled': quantity,
                    'average_fill_price': price, **self.ledger.summary()})

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
                raise ValueError('Settled position needs settlement accounting review')
            if actual != owned:
                raise ValueError('Account inventory differs from bot ledger; race paused')
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

    def decide(self, mapping, account, positions, book, refs):
        exchange = mapping['sig_exchange_id']
        held, cost = self.held(exchange, positions)
        diagnostics = []
        entry = choose(book, refs, self.config['strategy'], self.available(exchange, account), diagnostics)
        exit_signal = choose_exit(book, refs, self.config['strategy'], self.execution, held, cost,
                                  self.config['limits']['per_order']) if self.execution['sell_enabled'] else None
        if exit_signal and exit_signal.reason == 'convergence_take_profit':
            basis = self.ledger.sale_basis(exchange, exit_signal.side, exit_signal.quantity)
            net = exit_signal.price - D(self.config['strategy']['cost_buffer_per_share'])
            if net - basis / exit_signal.quantity < D(self.execution['take_profit_min']):
                exit_signal = None  # An average-cost gain need not be a gain on the FIFO shares sold.
        if entry and held and ((held < 0) != (entry.side == 'no')):
            entry = None  # Never use a complement buy to close or flip inventory.
            diagnostics.append({'reason': 'opposite_inventory'})
        signal = exit_signal or entry
        self.report('decision', {'exchange': exchange, 'held': held, 'available': self.available(exchange, account),
            'checks': diagnostics, 'action': signal.action if signal else None,
            'reason': signal.reason if signal else 'no_eligible_signal'})
        return signal

    def cycle(self):
        self.reconcile()
        if self.stopped():
            return False
        if not self.markets:
            raise ValueError('No enabled mappings')
        if self.config.get('news', {}).get('enabled') and self.news is None:
            raise ValueError('News monitor is required')
        account = self.sig.account()
        self.check_account(account)
        if self.sig.open_orders():
            self.report('skip', {'reason': 'Existing open orders; trading paused'})
            return True
        positions = self.sig.positions()
        for name, events in (self.news.drain() if self.news else {}).items():
            self.pending_news[name] = (self.pending_news.get(name, []) + events)[-100:]
        orders, started, checked = 0, time.monotonic(), 0
        self.report('scan_batch', {'enabled_races': len(self.markets),
                    'max_orders': self.execution['max_orders_per_cycle'], **self.ledger.summary()})
        for mapping in self.scanner.batch(self.pending_news, self.config.get('scan_batch_size', 8)):
            if self.stopped():
                return False
            exchange = mapping['sig_exchange_id']
            context = self.pending_news.pop(mapping['name'], [])
            try:
                if time.time()-self.journal.last_order(exchange) < self.config['cooldown_seconds']:
                    self.report('skip', {'exchange': exchange, 'reason': 'cooldown'})
                    continue
                metadata = self.metadata(mapping, bool(context))
                refs = self.references.books(mapping, metadata)
                book = self.sig.book(exchange)
                checked += 1
                self.ledger.observe(exchange, book, self.config['strategy']['max_age_seconds'])
                block = self.news.block_reason(mapping) if self.news else None
                if block:
                    self.report('skip', {'exchange': exchange, 'reason': block})
                    continue
                signal = self.decide(mapping, account, positions, book, refs)
                if context:
                    self.report('news_review', {'exchange': exchange, 'market': mapping['name'],
                        'paper_only': True, 'news': context,
                        'proposed_price_signal': vars(signal) if signal else None,
                        'method': 'Price and inventory rules only; news never sets valuation'})
                if signal is None:
                    continue
                # Serial final checks: cached rules/old positions never authorize an order.
                metadata = self.metadata(mapping, force=True)
                account = self.sig.account()
                self.check_account(account)
                if self.sig.open_orders():
                    self.report('skip', {'exchange': exchange, 'reason': 'Open order appeared during scan'})
                    return True
                positions = self.sig.positions()
                refs = self.references.books(mapping, metadata)
                book = self.sig.book(exchange)
                signal = self.decide(mapping, account, positions, book, refs)
                if signal is None:
                    continue
            except (ValueError, KeyError, APIError) as error:
                self.cache.pop(mapping['name'], None)
                if isinstance(error, APIError) and error.status in (401, 403, 429):
                    raise
                self.report('skip', {'exchange': exchange, 'reason': str(error)})
                continue
            if self.stopped():
                return False
            if self.news and self.news.block_reason(mapping):
                self.report('skip', {'exchange': exchange, 'reason': 'News pause during preflight'})
                continue
            book.check(self.config['strategy']['max_age_seconds'])
            for ref in refs:
                ref.check(self.config['strategy']['max_age_seconds'])
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
            self.report('signal', {'exchange': exchange, **vars(signal)})
            if self.live:
                response = self.sig.place(payload)
                self.journal.response(payload['idempotencyKey'], response)
                self.finish(payload['idempotencyKey'], payload, response, reserve)
            else:
                self.ledger.record(payload, signal.quantity, signal.price, fee)
                self.report('paper_fill', {'exchange': exchange, **vars(signal), 'simulation': True})
            orders += 1
            if orders >= self.execution['max_orders_per_cycle']:
                break
            account = self.sig.account()
            self.check_account(account)
            positions = self.sig.positions()
        self.report('scan_complete', {'quotes_checked': checked, 'orders': orders,
                    'seconds': round(time.monotonic()-started, 2), **self.ledger.summary()})
        return True
