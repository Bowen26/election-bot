"""Inventory-aware execution with reusable capital and measured decisions."""
from datetime import datetime, timezone
import json
import math
import sqlite3
import time
import uuid

from .clients import APIError, contract_record, fingerprint, iso_time
from .engine import Engine
from .clock_guard import SubmissionClockUnavailable
from .ledger import Ledger
from .scanner import ScanInterest
from .shadow import decisions as shadow_decisions
from .exit_study import checked_exit, experiment as exit_experiment
from .risk import Exposure, LOSS_FIELD, verify_account_inventory, bind_exposure_modes
from .settlement import multi_races, verify_review, entry_settings
from .race_controls import RaceGroups, committed_by_race
from .profit_exit import profit_exit_mode
from .exit_coverage import exit_coverage
from .exit_trend import ExitTrend, assess as assess_exit_trend
from .strategy import BookValidationError, D, choose
from .gate_metrics import GateMetrics
from .settlement_value import exit_settings, gate_exit

RADAR_REUSE_SECONDS = 5  # Election radar and gap radar share one bulk reference read per cycle.


class ReservationRejected(ValueError):
    def __init__(self, message, gate='reservation_guard'):
        super().__init__(message)
        self.gate = gate


class OrderUnresolved(Exception):
    """An order was sent (or may have been); its original error must reach recovery."""
    def __init__(self, error):
        super().__init__(str(error))
        self.original = error


class InventoryValidationError(ValueError):
    def __init__(self, reason, message):
        super().__init__(message)
        self.reason = reason


class ActiveEngine(Engine):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.ledger = Ledger(self.journal)
        from .drawdown import Drawdown
        self.drawdown = Drawdown(self.ledger, self.config)
        from .position_guard import PositionGuard
        self.position_guard = PositionGuard(self.journal.db, self.config)
        from .risk_exit import RiskExit
        self.risk_exit = RiskExit(self.ledger, self.config)
        self.latest_risk_context = None
        self.gate_metrics = GateMetrics()
        from .rotation import RotationStudy
        self.rotation_study = RotationStudy(self.ledger, self.config)
        from .model_rotation import ModelRotationStudy
        self.model_rotation_study = ModelRotationStudy(self.ledger, self.config, self.runtime)
        self.latest_entry_context = None
        from . import patient_entry
        patient_entry.initialize(self.journal.db)
        self.latest_patient_context = None
        from .performance_throttle import PerformanceThrottle
        self.performance_throttle = PerformanceThrottle(self.journal.db,self.config)
        self.latest_throttle_context = None
        from . import model_live
        model_live.initialize(self.journal.db)
        self.latest_model_context = None
        self.races = RaceGroups(self.config, self.journal.db)
        self.races.bind(self.journal.db)
        bind_exposure_modes(self.ledger, self.config)
        Exposure(self.ledger, self.config)  # Validate existing holdings/pending metadata at startup.
        self.sibling_races = multi_races(self.config)
        self.cache = {}
        self.execution = self.config['execution']
        self.exit_trend = ExitTrend(self.journal.db) if profit_exit_mode(self.execution) == 'trend' else None
        self.latest_exit_epoch = None
        self.last_exit_coverage = None
        self.scan_interest = ScanInterest()
        from .research_followups import ResearchFollowups
        self.research_followups = ResearchFollowups(self.journal.db, self.config)
        self.settlement_exits = exit_settings(self.config)
        from .election_night import ElectionNight
        self.election_night = ElectionNight(self.config)
        from .election_night import Radar
        self.radar = Radar(self.election_night)
        from .gap_radar import GapRadar
        self.gap_radar = GapRadar(self.config)
        self._radar_books = None  # (fetched_at, books, skipped, watched): one bulk read serves both radars
        from . import pair_baskets
        pair_baskets.initialize(self.journal.db)
        self.pair_policy = pair_baskets.settings(self.config)
        self.pair_list = pair_baskets.pairs(self.config)
        self.pair_cursor, self.pair_verified, self.pair_edges = 0, {}, {}
        self._basket_budget_reported = None
        from .cash_reserve import settings as cash_settings
        self.cash_policy = cash_settings(self.config)
        from .capital_rotation import Rotation
        self.rotation = Rotation(self.config)
        self.scan_policy = 'priority_v1' if self.execution.get('priority_scanning', True) else 'news_round_robin'
        self.scanner.set_policy(self.scan_policy)

    def check_account(self, account):
        self.check_clock()
        now = time.time()
        if (account['id'] != self.config['tournament_id'] or account['id'] != self.sig.tid or
                account['status'] != 'active' or account.get('isPendingEnrolment') or
                now < iso_time(account['startDate']) or
                now + self.config['order_lifetime_seconds'] >= iso_time(account['endDate'])):
            raise ValueError('Tournament identity, enrollment or trading dates invalid')

    def finish(self, key, payload, response, reserved, patient_mapping=None):
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
        patience_error = None
        if order['open'] and not mismatch and patient_mapping is not None:
            from .patient_entry import settings as patient_settings
            from .patient_wait import observe
            tagged = self.journal.db.execute("SELECT priced FROM patient_orders WHERE key=?", (key,)).fetchone()
            if tagged and tagged['priced'] == 'improved':
                try:
                    order, reason = observe(payload, order, patient_settings(self.config)['wait_seconds'],
                        self.sig.order, lambda: self.patient_still_eligible(patient_mapping, payload, reserved),
                        self.stopped)
                    self.report('patient_wait', {'order_key':key,'reason':reason})
                except BaseException as error:
                    # A known order must be cancelled and its fills reconciled even
                    # when monitoring fails or the user interrupts the wait.
                    patience_error = error
        if order['open'] or patience_error is not None:
            self.sig.cancel(oid)
            for _ in range(4):
                order = self.sig.order(oid)
                if not order['open']:
                    break
                time.sleep(1)
        if (str(order['exchangeId']) != payload['exchangeId'] or order['tournamentId'] != self.sig.tid
                or str(order['id']) != str(oid)):
            raise RuntimeError('Final order identity mismatch; reservation retained')
        mismatch = mismatch or order['action'] != payload['action'] or order['side'] != payload['side'] or D(order['priceLimit']) != D(payload['price'])
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
        if quantity > D(payload['quantity']) or quantity != D(order['quantityFilled']) or quantity < D(response['quantityTraded']):
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
                    'average_fill_price': price, **self.portfolio_summary()})
        self.check_loss_stop()
        if patience_error is not None:
            raise patience_error

    def patient_still_eligible(self, mapping, payload, reserve):
        if self.stopped() or self.news_block(mapping) or not self.drawdown.status(persist=False)['buy_allowed']:
            return False
        self.check_account(self.sig.account())
        metadata = self.metadata(mapping)
        refs = self.references.books(mapping, metadata)
        book = self.sig.book(mapping['sig_exchange_id'])
        held, _ = self.ledger.held(mapping['sig_exchange_id'])
        if not self.position_guard.check(mapping,book,held,phase='preflight',persist=False)['buy_allowed']:
            return False
        # Conservative: cancel if the original ask-taking signal disappears,
        # even if the resting price might still be attractive. Never chase it.
        signal = choose(book,refs,self.entry_settings(mapping),reserve)
        return (signal is not None and signal.side==payload['side'] and
                D(payload['price']) <= signal.price and not self.reentry_wait(mapping['sig_exchange_id']))

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

    def recover_expired_rejection(self, row, payload):
        from .expired_recovery import recover_rejected_expiration
        recover_rejected_expiration(self, row, payload)

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
        record = contract_record(market, mapping, metadata)
        if fingerprint(record) != mapping.get('contract_fingerprint'):
            raise ValueError('Contract changed or unreviewed; inspect and pin')
        if mapping.get('exposure_mode') == 'gross':
            verify_review(mapping, record, self.config['strategy'])
        self.risk_exit.bind(mapping, record)
        self.cache[key] = (time.monotonic(), metadata)
        return metadata

    def news_block(self, mapping):
        if not self.news:
            return None
        siblings = self.sibling_races.get(mapping.get('race_key'), [mapping])
        for sibling in siblings:
            reason = self.news.block_reason(sibling)
            if reason:
                return reason
        return None

    def send_order(self, payload, reserve, patient_mapping=None):
        """The one live placement path for every route: place, record the response, reconcile.

        A clock failure before the POST records a zero fill. Any error once the order was
        (or may have been) sent is wrapped in OrderUnresolved so no caller can skip past it;
        callers re-raise the original error for the supervisor's recovery handling.
        """
        fee = D(self.config['strategy']['cost_buffer_per_share'])
        try:
            response = self.sig.place(payload)
        except SubmissionClockUnavailable:
            self.gate_block('submission_clock', 'pre_submit', payload['exchangeId'])
            # No POST was attempted; record zero fill atomically, never a guessed fill.
            self.ledger.record(payload, 0, 0, fee)
            self.report('order_not_submitted', {'exchange': payload['exchangeId'],
                'order_key': payload['idempotencyKey'], 'reason': 'clock_sample_unavailable'})
            raise
        except BaseException as error:
            raise OrderUnresolved(error) from error
        try:
            self.journal.response(payload['idempotencyKey'], response)
            self.finish(payload['idempotencyKey'], payload, response, reserve, patient_mapping=patient_mapping)
        except BaseException as error:
            raise OrderUnresolved(error) from error

    def submit_basket_leg(self, mapping, role, price, quantity, account, positions):
        """One NO buy through the normal reservation/placement/reconciliation path. Returns shares filled."""
        from .pair_baskets import REASON
        exchange = mapping['sig_exchange_id']
        payload = {'exchangeId': exchange, 'side': 'no', 'action': 'buy', 'quantity': int(quantity),
            'price': float(price), 'tournamentId': self.sig.tid, 'idempotencyKey': str(uuid.uuid4()),
            'expirationDate': datetime.fromtimestamp(time.time()+self.config['order_lifetime_seconds'],
                                                     timezone.utc).isoformat()}
        fee = D(self.config['strategy']['cost_buffer_per_share'])
        reserve = int(quantity) * (D(price) + fee)
        self.reserve_order(payload, reserve, account, positions, exit_reason=REASON,
                           basket_context=(self.races.race(exchange), role))
        if self.stopped():
            self.ledger.record(payload, 0, 0, fee)
            return 0
        self.report('signal', {'exchange': exchange, 'order_key': payload['idempotencyKey'], 'side': 'no',
                               'price': str(price), 'quantity': int(quantity), 'action': 'buy',
                               'reason': REASON, 'basket_role': role})
        if self.live:
            self.send_order(payload, reserve)
        else:
            self.ledger.record(payload, int(quantity), D(price), fee)
            self.report('paper_fill', {'exchange': exchange, 'side': 'no', 'price': str(price),
                                       'filled': int(quantity), 'simulation': True, 'reason': REASON})
        row = self.journal.db.execute('SELECT quantity FROM executions WHERE key=?',
                                      (payload['idempotencyKey'],)).fetchone()
        return int(D(row[0])) if row else 0

    def mark_holding(self, mapping):
        """Fresh drawdown mark for one contract (with references in fair-value mode)."""
        if not self.drawdown.policy['enabled']:
            return
        refs = self.references.books(mapping, self.metadata(mapping)) if self.drawdown.fair else None
        self.drawdown.observe(mapping['sig_exchange_id'], self.sig.book(mapping['sig_exchange_id']), refs)

    def pair_basket_cycle(self, account, positions):
        """Check a few pairs per cycle; buy NO on both parties when the bids sum above 1 + min_edge."""
        from . import pair_baskets as pb
        policy = self.pair_policy
        if not policy['enabled'] or not self.pair_list:
            return
        candidates = [p for p in self.pair_list if policy['races'] is None or p[0]['race_key'] in policy['races']]
        if not candidates:
            return
        unmatched = [p for p in candidates if pb.unmatched(self.journal.db, *p)]
        if policy['total_coins']-pb.basket_cost(self.journal.db) < 1:
            # Budget spent (a set costs about one coin): only repairs can trade, so skip
            # the price screen and book reads for new baskets.
            if self._basket_budget_reported != len(unmatched):
                self.report('pair_basket_budget_spent', {'repairs_pending': len(unmatched)})
                self._basket_budget_reported = len(unmatched)
            candidates = unmatched
            if not candidates:
                return
        else:
            self._basket_budget_reported = None
        try:
            # One bulk read screens every pair; full books are read only where an edge shows.
            quotes = self.sig.prices([m['sig_exchange_id'] for p in candidates for m in p])
            for anchor, leg in candidates:
                a, l = quotes.get(anchor['sig_exchange_id']), quotes.get(leg['sig_exchange_id'])
                if a and l and a[0] is not None and l[0] is not None:
                    self.pair_edges[anchor['race_key']] = D(str(a[0]))+D(str(l[0]))-1
            screened = sorted((p for p in candidates if p not in unmatched
                               and self.pair_edges.get(p[0]['race_key'], -1) >= policy['min_edge']),
                              key=lambda p: -self.pair_edges[p[0]['race_key']])
            batch = (unmatched+screened)[:policy['pairs_per_cycle']]
        except (APIError, ValueError, KeyError, TypeError, ArithmeticError) as error:
            if isinstance(error, APIError) and error.status in (401, 403, 429):
                raise
            self.report('pair_basket_screen_skip', {'reason': str(error)[:160]})
            # Fallback: unmatched legs and recent edges first, the rest in rotation.
            hot = unmatched+[p for p in candidates if p not in unmatched
                             and self.pair_edges.get(p[0]['race_key'], -1) >= policy['min_edge']-D('.005')]
            hot = hot[:max(1, policy['pairs_per_cycle']//2)]
            start = self.pair_cursor % len(candidates)
            rotation = [p for p in candidates[start:]+candidates[:start] if p not in hot]
            batch = hot+rotation[:policy['pairs_per_cycle']-len(hot)]
            self.pair_cursor = start+policy['pairs_per_cycle']-len(hot)
        for anchor, leg in batch:
            if self.stopped():
                return
            race = anchor['race_key']
            detail = {'race': race}
            try:
                block = self.news_block(anchor)
                if block and not block.startswith('News uncertainty pause'):
                    detail['skip'] = 'news_or_dispute'
                    continue
                for mapping in (anchor, leg):
                    held, _ = self.held(mapping['sig_exchange_id'], positions)
                    if held > 0:
                        raise ValueError('holds YES on a basket contract')
                def verify():
                    # Identity reads only when a trade is possible; cached for verify_seconds.
                    if time.time()-self.pair_verified.get(race, 0) > policy['verify_seconds']:
                        self.metadata(anchor, force=True)   # Existing full pin for the anchor.
                        pb.verify_pair(self.sig.market(anchor['sig_market_id']),
                                       self.sig.market(leg['sig_market_id']), anchor, leg)
                        self.pair_verified[race] = time.time()
                books = {'anchor': self.sig.book(anchor['sig_exchange_id']), 'leg': self.sig.book(leg['sig_exchange_id'])}
                for book in books.values():
                    book.check(self.config['strategy']['max_age_seconds'], venue='SIG')
                if books['anchor'].bids and books['leg'].bids:
                    self.pair_edges[race] = books['anchor'].bids[0][0]+books['leg'].bids[0][0]-1
                legs = {'anchor': anchor, 'leg': leg}
                repair = pb.unmatched(self.journal.db, anchor, leg)
                if repair:
                    role, quantity, cap = repair
                    bids = books[role].bids
                    price = 1-bids[0][0] if bids else None
                    detail.update(repair_role=role, repair_quantity=quantity, break_even=str(cap))
                    if price is not None and price <= cap:
                        size = int(min(quantity, bids[0][1]*policy['depth_fraction']))
                        if size >= 1:
                            verify()
                            detail['repaired'] = self.submit_basket_leg(legs[role], role, price, size, account, positions)
                            if detail['repaired']:
                                self.mark_holding(legs[role])
                    continue
                fee = D(self.config['strategy']['cost_buffer_per_share'])
                # Both legs share the race budget; size so the pair fits within coin limits.
                coins = min(policy['total_coins']-pb.basket_cost(self.journal.db),
                            min(self.available(anchor['sig_exchange_id'], account),
                                self.available(leg['sig_exchange_id'], account)))
                plan, reason = pb.plan(books['anchor'], books['leg'], policy, coins, fee)
                if plan is None:
                    detail['skip'] = reason
                    continue
                detail.update(edge=str(plan['edge']), sets=plan['sets'], order=plan['order'])
                verify()
                first, second = plan['order']
                filled = self.submit_basket_leg(legs[first], first, plan['prices'][first], plan['sets'], account, positions)
                detail['first_filled'] = filled
                if filled < 1:
                    continue
                # The new holding needs a drawdown mark, or the second leg is paused as incomplete.
                self.mark_holding(legs[first])
                account = self.sig.account()
                self.check_account(account)
                positions = self.sig.positions()
                fresh = self.sig.book(legs[second]['sig_exchange_id'])
                price = pb.second_leg_price(plan['prices'][first], 1-fresh.bids[0][0]) if fresh.bids else None
                if price is None:
                    detail['second'] = 'above_break_even; repair later'
                    continue
                size = int(min(filled, fresh.bids[0][1]*policy['depth_fraction']))
                detail['second_filled'] = self.submit_basket_leg(legs[second], second, price, size, account, positions) if size >= 1 else 0
            except OrderUnresolved as error:
                # Same handling as an unresolved ordinary order: recovery, retry or halt.
                detail['halt'] = 'basket order unresolved: '+str(error)[:160]
                raise error.original
            except ReservationRejected as error:
                detail['skip'] = 'reservation: '+str(error)
                self.gate_block(error.gate, 'pair_basket', anchor['sig_exchange_id'])
            except APIError as error:
                if error.status in (401, 403, 429):
                    raise
                detail['skip'] = 'api: '+str(error)[:120]
            except (ValueError, KeyError, BookValidationError) as error:
                detail['skip'] = str(error)[:160]
            finally:
                self.report('pair_basket', detail)

    def cap_basket_sale(self, signal, exchange, held, detail):
        """Shares bought for a pair basket are held to settlement; sell only the rest."""
        if signal is None or signal.action != 'sell':
            return signal
        from .pair_baskets import locked
        reserved = locked(self.journal.db, exchange)
        if not reserved:
            return signal
        free = int(abs(D(held))-reserved)
        if free < 1:
            detail.update(status='blocked', reason='basket_shares_held_to_settlement', basket_shares=str(reserved))
            return None
        if signal.quantity > free:
            from dataclasses import replace
            detail['basket_capped_quantity'] = free
            return replace(signal, quantity=free)
        return signal

    def night_exempt(self, action, reason, key):
        """True for an election-night buy that may bypass the named control right now."""
        from .election_night import REASON
        return action == 'buy' and reason == REASON and self.election_night.exempt(key)

    def news_gate(self, mapping, signal):
        """News block for this signal; election-night buys may pass a generic uncertainty pause."""
        from .election_night import REASON, RECYCLE
        block = self.news_block(mapping)
        if (block and signal is not None and signal.reason in (REASON, RECYCLE)
                and self.election_night.overrides(block)):
            return None
        return block

    def entry_settings(self, mapping):
        return (entry_settings(mapping, self.config['strategy'])
                if mapping.get('exposure_mode') == 'gross' else self.config['strategy'])

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

    def coin_rooms(self, exchange, account, reserve_floor=True):
        """Each coin limit's remaining room; a sale relaxes only cash and total."""
        limits = self.config['limits']
        cash = D(account['myBalance'])
        if not self.live:
            # Paper inventory must consume paper cash without altering the real account.
            cash -= self.ledger.committed()
        if reserve_floor:
            from .cash_reserve import floor
            cash -= floor(self.cash_policy)  # Election-night reserve; zero once the window opens.
        return {'per_order': D(limits['per_order']), 'cash': cash,
                'per_market': D(limits['per_market']) - self.races.committed(self.ledger, exchange),
                'total': D(limits['total']) - self.ledger.committed(),
                'daily': D(limits['daily']) - self.journal.used(today=True)}

    def available(self, exchange, account, reserve_floor=True):
        return max(D(0), min(self.coin_rooms(exchange, account, reserve_floor).values()))

    def rotation_step(self, mapping, account, book, refs, settings, held, blocked_by_coins, signal,
                      side_limits, buys_allowed, phase):
        """Before election night: note an entry blocked only by coins, or sell part of a
        nearly converged holding to fund one. Returns the signal to use."""
        exchange = mapping['sig_exchange_id']
        race = self.races.race(exchange)
        hypothetical = None
        if blocked_by_coins and signal is None and buys_allowed:
            rooms = self.coin_rooms(exchange, account)
            other = min(rooms['per_order'], rooms['per_market'], rooms['daily'])
            if other > 0:
                hypothetical = choose(book, refs, settings, other, None, side_limits)
            if hypothetical is not None and held and ((held < 0) != (hypothetical.side == 'no')):
                hypothetical = None  # Never fund a buy that would close or flip inventory.
        if hypothetical is None:
            self.rotation.clear(exchange)
        else:
            unit = hypothetical.price+D(self.config['strategy']['cost_buffer_per_share'])
            desired = hypothetical.quantity*unit
            # The sale must also refill any cash-floor shortfall before the buy can use it.
            need = max(desired-rooms['cash'], desired-rooms['total'])
            if self.rotation.note(exchange, race, hypothetical, need):
                self.journal.event('capital_rotation_pending', {'exchange': exchange, 'race': race,
                    'side': hypothetical.side, 'ask': str(hypothetical.price), 'edge': str(hypothetical.edge),
                    'need_coins': str(need), 'phase': phase})
        if signal is not None or not held:
            return signal
        detail = {}
        sale = self.rotation.propose(book, refs, held, exchange, race, self.config['limits']['per_order'], detail)
        sale = self.cap_basket_sale(sale, exchange, held, detail)
        if detail.get('reason') not in ('no_pending_entry', 'disabled_or_no_position'):
            self.journal.event('capital_rotation_decision', {**detail, 'exchange': exchange, 'race': race,
                               'snapshot_id': self.latest_snapshot_id, 'phase': phase})
        return sale

    def portfolio_summary(self):
        return {**self.ledger.summary(), **self.races.summary(self.ledger)}

    def reentry_wait(self, exchange):
        seconds = self.execution.get('reentry_cooldown_seconds', 0)
        if not seconds:
            return 0
        last = self.races.last_sale(self.ledger, exchange)
        return max(0, last+seconds-time.time()) if last else 0

    def gate_block(self, gate, phase, exchange=None):
        self.gate_metrics.blocked(gate, phase, exchange)

    def reserve_order(self, payload, reserve, account, positions, exit_reason=None, exit_epoch=None, model_context=None, entry_context=None, risk_context=None, patient_context=None, throttle_context=None, basket_context=None):
        """Recheck local limits under the same write lock as the reservation."""
        drawdown_rejected = False
        position_rejected = None
        from .pair_baskets import REASON as PAIR, SWAP
        basket = exit_reason == PAIR
        swap = exit_reason == SWAP
        if basket and (basket_context is None or payload['action'] != 'buy' or payload['side'] != 'no'):
            raise ReservationRejected('Basket legs are NO buys with basket context', gate='pair_basket')
        if swap and (basket_context is None or payload['action'] != 'sell' or payload['side'] != 'yes'):
            raise ReservationRejected('Swap sales are YES sells with basket context', gate='pair_basket')
        from .election_night import REASON as NIGHT
        def guard():
            nonlocal drawdown_rejected, position_rejected
            self.ledger.inventory(refresh=True)  # Include any newly committed execution.
            self.check_account(account)
            if self.ledger.missing():
                raise RuntimeError('Unimported executions block reservation')
            exchange = payload['exchangeId']
            # Both legs of a basket/swap are in one race; the second must not wait out the race cooldown.
            if not (basket or swap) and time.time()-self.races.last_order(self.journal, exchange) < self.config['cooldown_seconds']:
                raise ReservationRejected('Race cooldown changed before reservation', gate='race_cooldown')
            expected = D(payload['quantity']) * (D(payload['price']) + D(self.config['strategy']['cost_buffer_per_share']))
            if D(reserve) != expected:
                raise RuntimeError('Reservation amount disagrees with order')
            if payload['action'] == 'buy':
                drawdown = self.drawdown.status(persist=False)
                if not drawdown['buy_allowed'] and not self.night_exempt(payload['action'], exit_reason, 'ignore_drawdown_pause'):
                    drawdown_rejected = True
                    raise ReservationRejected('New buys paused: '+drawdown['reason'], gate='portfolio_drawdown')
                if basket:
                    held, _ = self.held(exchange, positions)
                    if held > 0:
                        raise ReservationRejected('Basket NO leg would oppose held YES', gate='pair_basket')
                elif self.night_exempt(payload['action'], exit_reason, 'ignore_position_guard'):
                    held, _ = self.held(exchange, positions)
                    if held and payload['side'] != ('yes' if held > 0 else 'no'):
                        raise ReservationRejected('Additional buy would oppose existing inventory', gate='position_entry_guard')
                elif self.position_guard.policy['enabled']:
                    held, _ = self.held(exchange, positions)
                    if held:
                        if entry_context is None or entry_context[0]['sig_exchange_id'] != exchange:
                            raise ReservationRejected('Missing position entry context', gate='position_entry_guard')
                        mapping, book = entry_context
                        if payload['side'] != ('yes' if held > 0 else 'no'):
                            raise ReservationRejected('Additional buy would oppose existing inventory', gate='position_entry_guard')
                        try:
                            checked = self.position_guard.check(mapping, book, held, phase='preflight', persist=False)
                        except BookValidationError as error:
                            raise ReservationRejected('Position entry quote invalid: '+str(error), gate='position_entry_guard') from error
                        if not checked['buy_allowed']:
                            position_rejected = (mapping, book, held)
                            raise ReservationRejected('Additional buys paused: '+checked['reason'], gate='position_entry_guard')
                night_exempt = (exit_reason == NIGHT and self.election_night.active()
                                and self.election_night.policy['ignore_reentry_cooldown'])
                if self.reentry_wait(exchange) and not (night_exempt or basket):
                    raise ReservationRejected('Race is in post-sale reentry cooldown', gate='reentry_cooldown')
                # The cash floor is for election night; basket legs were sized against it before
                # the first leg, so a completing leg never strands an unmatched position.
                if reserve > self.available(exchange, account, reserve_floor=not (basket or exit_reason == NIGHT)):
                    raise ReservationRejected('Coin limits changed before reservation', gate='coin_limits')
                if basket:
                    race, role = basket_context
                    if race != self.races.race(exchange):
                        raise ReservationRejected('Basket race changed before reservation', gate='pair_basket')
                    # Tag in the reservation transaction: these shares are never sold by other routes.
                    self.journal.db.execute('INSERT INTO basket_orders VALUES (?,?,?,?)',
                                            (payload['idempotencyKey'], race, exchange, role))
            if payload['action'] == 'sell':
                basis = self.ledger.sale_basis(exchange, payload['side'], payload['quantity'])
                if exit_reason == 'trend_profit_target':
                    if self.exit_trend is None or exit_epoch != self.exit_trend.epoch(exchange):
                        raise ReservationRejected('Position changed since trend evaluation', gate='exit_inventory_recheck')
                if exit_reason in ('profit_target', 'convergence_take_profit', 'trend_profit_target'):
                    fee = D(self.config['strategy']['cost_buffer_per_share'])
                    profit = D(payload['price'])-fee-basis/D(payload['quantity'])
                    if profit < D(self.execution['take_profit_min']):
                        raise ReservationRejected('FIFO profit changed before reservation', gate='exit_profit_recheck')
            exposure = Exposure(self.ledger, self.config)
            if exposure.enabled:
                if self.live:
                    verify_account_inventory(self.ledger, positions)
                if (not self.night_exempt(payload['action'], exit_reason, 'ignore_exposure_caps')
                        and D(payload['quantity']) > exposure.headroom(exchange, payload['action'], payload['side'])):
                    raise ReservationRejected('Exposure changed before reservation', gate='exposure_headroom')
            if exit_reason == 'model_fair_value':
                from . import model_live
                if model_context is None or payload['action'] != 'buy':
                    raise ReservationRejected('Missing model decision context', gate='model_recheck')
                mapping, book, refs, strategy = model_context
                if mapping['sig_exchange_id'] != exchange:
                    raise ReservationRejected('Model exchange changed before reservation', gate='model_recheck')
                held, _ = self.held(exchange, positions)
                limits = ({side: exposure.headroom(exchange, 'buy', side) for side in ('yes','no')}
                          if exposure.enabled else None)
                fresh, detail = model_live.propose(self.runtime, mapping, self.config, self.journal.db,
                    book, refs, strategy, self.available(exchange, account), held, limits)
                if (fresh is None or fresh.side != payload['side'] or fresh.price != D(payload['price'])
                        or fresh.quantity < D(payload['quantity'])):
                    raise ReservationRejected('Model changed or unavailable at reservation: '+detail.get('reason',''), gate='model_recheck')
                model_live.attach(self.journal.db, payload, mapping['race_key'], detail)
            if exit_reason == 'risk_reduction_exit':
                if risk_context is None or payload['action'] != 'sell':
                    raise ReservationRejected('Missing risk exit preflight context', gate='risk_exit_recheck')
                mapping, book, verified_at = risk_context
                if (mapping['sig_exchange_id'] != exchange or
                        not 0 <= time.time()-verified_at <= self.config['strategy']['max_age_seconds']):
                    raise ReservationRejected('Risk exit contract check expired', gate='risk_exit_recheck')
                held, cost = self.held(exchange, positions)
                cap = exposure.headroom(exchange, 'sell', payload['side']) if exposure.enabled else None
                try:
                    fresh, detail = self.risk_exit.propose(mapping, book, held, cost, cap,
                                                          phase='preflight', persist=False)
                except BookValidationError as error:
                    raise ReservationRejected(str(error), gate='risk_exit_recheck') from error
                if (fresh is None or fresh.side != payload['side'] or fresh.price != D(payload['price']) or
                        fresh.quantity < D(payload['quantity'])):
                    raise ReservationRejected('Risk exit changed before reservation: '+detail['reason'], gate='risk_exit_recheck')
                self.risk_exit.attach(payload, detail)
            if exit_reason == 'patient_entry':
                from . import patient_entry
                if patient_context is None or payload['action'] != 'buy':
                    raise ReservationRejected('Missing patient entry context', gate='patient_entry_recheck')
                mapping, book, refs, settings = patient_context
                if mapping['sig_exchange_id'] != exchange:
                    raise ReservationRejected('Patient exchange changed before reservation', gate='patient_entry_recheck')
                # Re-derive the arm and price from committed state and the quotes already
                # validated in pre-submit. No network call may run under the write lock.
                limits = ({side: exposure.headroom(exchange, 'buy', side) for side in ('yes', 'no')}
                          if exposure.enabled else None)
                held, _ = self.held(exchange, positions)
                control = choose(book, refs, settings, self.available(exchange, account),
                                 None, limits)
                if control is None:
                    raise ReservationRejected('Control entry no longer eligible before reservation', gate='patient_entry_recheck')
                if held and ((held < 0) != (control.side == 'no')):
                    raise ReservationRejected('Patient entry would oppose existing inventory', gate='patient_entry_recheck')
                fresh, detail = patient_entry.propose(self.journal.db, mapping, self.config,
                                                      book, control, now=time.time())
                if (fresh is None or fresh.side != payload['side'] or fresh.price != D(payload['price'])
                        or fresh.quantity < D(payload['quantity'])):
                    raise ReservationRejected('Patient entry changed before reservation: '+detail['reason'], gate='patient_entry_recheck')
                patient_entry.attach(self.journal.db, payload, detail)
            elif exit_reason == 'entry_gap' and payload['action'] == 'buy':
                # Measurement only: the control arm needs a denominator, and each
                # control buy needs to record whether it could have been improved.
                from . import patient_entry
                patient_entry.attach_control(self.journal.db, payload, self.config,
                                             self.latest_entry_context)
            if payload['action']=='buy' and not basket:  # Riskless baskets are not a priced route.
                from .performance_throttle import enabled as throttle_enabled
                if throttle_context is None:
                    if throttle_enabled(self.config):
                        raise ReservationRejected('Missing route throttle context',gate='performance_throttle')
                else:
                    mapping, book, raw = throttle_context
                    if (mapping['sig_exchange_id']!=exchange or raw.action!='buy' or raw.side!=payload['side']
                            or raw.price!=D(payload['price']) or raw.reason!=exit_reason):
                        raise ReservationRejected('Route throttle context changed',gate='performance_throttle')
                    try:
                        allowed=self.performance_throttle.scale(raw)
                        if payload['quantity']>allowed.quantity:
                            raise ValueError('Route size reduction changed before reservation')
                        self.performance_throttle.attach(payload,mapping,book,raw)
                    except ValueError as error:
                        raise ReservationRejected(str(error),gate='performance_throttle') from error
            loss = self.loss_status()
            if loss['realized_loss_stop_coins'] is not None and loss['net_realized_loss'] >= loss['realized_loss_stop_coins']:
                raise RuntimeError('Realized loss limit reached before reservation')
        try:
            self.journal.reserve(payload, reserve, guard=guard)
        except ReservationRejected as error:
            self.gate_block(error.gate, 'reservation', payload['exchangeId'])
            # The failed reservation rolled back. Persist a newly detected pause separately.
            if drawdown_rejected:
                self.drawdown.status()
            if position_rejected:
                self.position_guard.check(*position_rejected, phase='preflight')
            raise

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

    def snapshot_quotes(self, book, refs, now):
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
        return quotes

    def record_quote_snapshot(self, mapping, book, refs, phase):
        try:
            self.model_rotation_study.observe_outcomes(mapping, book)
        except (ValueError, KeyError, TypeError, ArithmeticError, sqlite3.Error, OSError) as error:
            self.journal.event('model_rotation_observation_skip', {'reason':str(error)})
        try:
            self.rotation_study.observe_outcomes(mapping['sig_exchange_id'], book)
        except ValueError as error:
            self.journal.event('rotation_observation_skip', {'reason': str(error)})
        now = time.time()
        self.latest_snapshot_id = str(uuid.uuid4())
        quotes = self.snapshot_quotes(book, refs, now)
        # Journal only: avoid flooding the trading console with every quote.
        self.journal.event('quote_snapshot', {'version': 1, 'snapshot_id': self.latest_snapshot_id,
            'exchange': mapping['sig_exchange_id'], 'phase': phase, 'captured_at': now,
            'orientation': 'SIG YES (references already aligned)', 'quotes': quotes})
        try:
            self.research_followups.observed(mapping['sig_exchange_id'], book, now)
            if phase == 'scan':
                self.research_followups.schedule(mapping['sig_exchange_id'], self.latest_snapshot_id, now)
        except (ValueError, TypeError, sqlite3.Error) as error:
            self.journal.event('research_followup_skip', {'reason': str(error)})
        if phase == 'scan':
            from .fair_research import observe
            research = observe(self.runtime, mapping, book, refs, self.entry_settings(mapping),
                               self.latest_snapshot_id, now=now)
            if research is not None:
                self.journal.event('fair_value_shadow', research)
                from .near_miss import record as record_near_miss
                try:
                    record_near_miss(self.runtime, mapping, book, research,
                                     self.entry_settings(mapping)['cost_buffer_per_share'], now,
                                     paper=not self.live)
                except (ValueError, KeyError, TypeError, ArithmeticError, sqlite3.Error, OSError) as error:
                    self.journal.event('near_miss_skip', {'error_type':type(error).__name__,
                        'reason':str(error), 'snapshot_id':self.latest_snapshot_id})

    def decide(self, mapping, account, positions, book, refs, phase='scan', news_restricted=False):
        exchange = mapping['sig_exchange_id']
        self.record_quote_snapshot(mapping, book, refs, phase)
        self.drawdown.observe(exchange, book, refs)
        drawdown = self.drawdown.status()
        exposure = Exposure(self.ledger, self.config)
        if exposure.enabled and self.live:
            verify_account_inventory(self.ledger, positions)
        held, cost = self.held(exchange, positions)
        self.latest_entry_context = (mapping, book)
        position_guard = self.position_guard.check(mapping, book, held, phase)
        side_limits = ({side: exposure.headroom(exchange, 'buy', side) for side in ('yes', 'no')}
                       if exposure.enabled else None)
        exit_limit = (exposure.headroom(exchange, 'sell', 'yes' if held > 0 else 'no')
                      if exposure.enabled and held else None)
        diagnostics = []
        exit_check = {'status': 'blocked' if held else 'not_applicable',
                      'reason': 'selling_disabled' if held else 'no_position'}
        settings = self.entry_settings(mapping)
        entry = choose(book, refs, settings, self.available(exchange, account), diagnostics, side_limits)
        blocked_by_coins = entry is None and any(c.get('reason') == 'budget_or_target_depth' for c in diagnostics)
        reason_gates = {'reference_depth':'reference_depth', 'price_gap':'reference_price_gap',
                        'exposure_limit':'exposure_headroom', 'budget_or_target_depth':'coin_or_target_depth'}
        for check in diagnostics:
            if check.get('reason') in reason_gates:
                check['gate'] = reason_gates[check['reason']]
                if entry is None:self.gate_block(check['gate'], phase, exchange)
        if self.available(exchange, account) <= 0:self.gate_block('coin_limits', phase, exchange)
        if side_limits is not None and all(v < 1 for v in side_limits.values()):
            self.gate_block('exposure_headroom', phase, exchange)
        if not drawdown['buy_allowed']:
            self.gate_block('portfolio_drawdown', phase, exchange)
            entry = None
            diagnostics.append({'reason': drawdown['reason'], 'gate': 'portfolio_drawdown'})
        if not position_guard['buy_allowed']:
            self.gate_block('position_entry_guard', phase, exchange)
            entry = None
            diagnostics.append({'reason': position_guard['reason'], 'gate': 'position_entry_guard'})
        reentry_wait = self.reentry_wait(exchange)
        if reentry_wait:
            self.gate_block('reentry_cooldown', phase, exchange)
            diagnostics.append({'reason':'reentry_cooldown','gate':'reentry_cooldown'})
            entry = None
            for check in diagnostics:
                if check.get('reason') == 'eligible':
                    check.update(reason='reentry_cooldown', retry_in_seconds=round(reentry_wait, 1))
        self.latest_exit_epoch = None
        trend_check = None
        if self.exit_trend is not None and held and self.execution['sell_enabled']:
            trend_context = self.exit_trend.context(mapping, book, refs, held,
                self.config['strategy'], self.execution, phase)
            self.latest_exit_epoch = trend_context['epoch']
            trend_check = lambda quantity: assess_exit_trend(trend_context, quantity)
        sale_basis = lambda side, quantity: self.ledger.sale_basis(exchange, side, quantity)
        exit_signal = checked_exit(book, refs, self.config['strategy'], self.execution, held, cost,
                                  self.config['limits']['per_order'], sale_basis, exit_check, exit_limit,
                                  trend_check=trend_check) if self.execution['sell_enabled'] else None
        # Optional: sell only above reference settlement value (the competition scores settlement).
        exit_signal = gate_exit(exit_signal, refs, self.config['strategy'], self.settlement_exits, exit_check)
        exit_signal = self.cap_basket_sale(exit_signal, exchange, held, exit_check)
        if entry and held and ((held < 0) != (entry.side == 'no')):
            entry = None  # Never use a complement buy to close or flip inventory.
            self.gate_block('opposite_inventory', phase, exchange)
            diagnostics.append({'reason': 'opposite_inventory', 'gate':'opposite_inventory'})
        self.latest_patient_context = None
        from .patient_entry import sized_control
        entry = sized_control(entry, self.config, self.journal.db, exchange)
        if entry is not None and exit_signal is None:
            from .patient_entry import propose as patient_propose
            patient, patient_detail = patient_propose(self.journal.db, mapping, self.config,
                                                      book, entry, now=time.time())
            self.journal.event('patient_entry_decision', {**patient_detail,
                               'snapshot_id': self.latest_snapshot_id, 'phase': phase})
            if patient is not None:
                entry = patient
                self.latest_patient_context = (mapping, book, refs, settings)
        signal = exit_signal or entry
        if news_restricted:
            # A news pause overridden for election night admits only that route.
            signal = None
            self.gate_block('news_pause', phase, exchange)
        if self.election_night.active():
            night = {}
            controls = (('drawdown_pause', drawdown['buy_allowed'], 'ignore_drawdown_pause'),
                        ('position_guard', position_guard['buy_allowed'], 'ignore_position_guard'))
            blocking = [name for name, ok, key in controls if not ok and not self.election_night.exempt(key)]
            bypassed = [name for name, ok, key in controls if not ok and self.election_night.exempt(key)]
            if bypassed:
                night['bypassed'] = bypassed
            if signal is not None:
                night.update(status='not_evaluated', reason='other_signal_selected')
            elif blocking:
                night.update(status='blocked', reason='drawdown_or_position_guard', blocking=blocking)
            elif reentry_wait and not self.election_night.policy['ignore_reentry_cooldown']:
                night.update(status='blocked', reason='reentry_cooldown')
            else:
                cash = D(account['myBalance']) - (D(0) if self.live else self.ledger.committed())
                # A called race no longer moves with the others; per-race and total coin limits still apply.
                limits = None if self.election_night.exempt('ignore_exposure_caps') else side_limits
                if side_limits is not None and limits is None:
                    night['exposure_caps'] = 'not_applied_to_called_race'
                signal = self.election_night.select(book, refs, self.available(exchange, account, reserve_floor=False),
                                                    held, limits, night, exchange=exchange, cash=cash)
            if signal is None and held:
                # Optional: free coins from a called, nearly settled holding for a better pending buy.
                from .election_night import recycle
                recycled = {}
                signal = recycle(self.election_night, book, refs, held, exchange,
                                 self.config['limits']['per_order'], recycled)
                signal = self.cap_basket_sale(signal, exchange, held, recycled)
                night['recycle'] = recycled
            # Stay on the priority lane only while SIG still lags a called race.
            if night.get('status') == 'eligible' or night.get('reason') == 'size_below_one_share':
                self.scan_interest.flag(mapping['name'], 'election_night')
            self.journal.event('election_night_decision', {**night, 'exchange': exchange,
                               'news_restricted': news_restricted,
                               'snapshot_id': self.latest_snapshot_id, 'phase': phase})
        if self.rotation.enabled and not self.election_night.active():
            signal = self.rotation_step(mapping, account, book, refs, settings, held, blocked_by_coins, signal,
                side_limits, drawdown['buy_allowed'] and position_guard['buy_allowed'] and not reentry_wait
                and not news_restricted, phase)
        self.latest_model_context = None
        if signal is None and not news_restricted and drawdown['buy_allowed'] and position_guard['buy_allowed'] and not reentry_wait and self.config.get('model_trading',{}).get('enabled'):
            from . import model_live
            signal, model_detail = model_live.propose(self.runtime, mapping, self.config, self.journal.db,
                book, refs, settings, self.available(exchange, account), held, side_limits)
            self.journal.event('model_decision', {**model_detail, 'exchange': exchange,
                               'snapshot_id': self.latest_snapshot_id, 'phase': phase})
            if not signal:
                gate = ('model_budget' if model_detail.get('reason')=='model_or_portfolio_budget' else
                        'model_price_depth_inventory' if model_detail.get('reason')=='model_price_depth_or_inventory' else 'model_inputs')
                self.gate_block(gate, phase, exchange)
                diagnostics.append({'gate':gate,'reason':model_detail.get('reason')})
            if signal:
                self.latest_model_context = (mapping, book, refs, settings)
        self.latest_throttle_context = None
        if signal is not None and signal.action=='buy':
            raw=signal
            signal=self.performance_throttle.scale(raw)
            self.latest_throttle_context=(mapping,book,raw)
            if signal.quantity<raw.quantity:
                self.report('performance_throttle_size',{'exchange':exchange,'route':raw.reason,
                    'phase':phase,'ordinary_quantity':raw.quantity,'quantity':signal.quantity})
        if signal is None:self.gate_block('no_eligible_signal', phase, exchange)
        elif signal.action == 'sell':self.gate_block('exit_priority', phase, exchange)
        self.scan_interest.observe(mapping['name'], diagnostics, refs, settings, held)
        self.report('decision', {'exchange': exchange, 'race_key': self.races.race(exchange), 'held': held, 'available': self.available(exchange, account),
            'checks': diagnostics, 'exit_check': exit_check, 'position_entry_guard': position_guard, 'phase': phase,
            'entry_minimum_edge': settings['minimum_edge'],
            'reentry_wait_seconds': round(reentry_wait, 1),
            'routing_policy': 'independent_contract_serial' if mapping.get('exposure_mode') == 'gross' else 'single_contract',
            'snapshot_id': self.latest_snapshot_id, 'exposure': exposure.summary(),
            'action': signal.action if signal else None,
            'reason': signal.reason if signal else 'no_eligible_signal'})
        if phase == 'scan' and held:
            try:
                study = exit_experiment(book, refs, self.config['strategy'], self.execution, held, cost,
                    self.config['limits']['per_order'], sale_basis, exit_signal, exit_check, exit_limit)
            except ValueError as error:
                study = {'version': 1, 'experiment': 'exit_depth_v1', 'simulation_only': True,
                         'status': 'unavailable', 'reason': str(error)}
            self.journal.event('exit_shadow', {**study, 'exchange': exchange,
                'snapshot_id': self.latest_snapshot_id, 'phase': phase})
        if phase == 'scan':
            try:
                shadow = shadow_decisions(book, refs, settings,
                    self.available(exchange, account), held, side_limits, exit_signal is not None)
            except ValueError as error:
                # A quote can age out during this extra calculation. Measurement
                # failure must not change the already selected live decision.
                shadow = {'version': 1, 'experiment': 'reference_bid_v1',
                          'status': 'unavailable', 'reason': str(error)}
            self.journal.event('shadow_decision', {**shadow, 'exchange': exchange,
                'snapshot_id': self.latest_snapshot_id, 'phase': phase})
            # Simulated entries and held positions get priority research follow-ups.
            if held or any(d.get('candidate') for d in shadow.get('decisions', [])):
                try:
                    self.research_followups.schedule(exchange, self.latest_snapshot_id, time.time(), 'position')
                except (ValueError, TypeError, sqlite3.Error) as error:
                    self.journal.event('research_followup_skip', {'reason': str(error)})
        if phase == 'scan' and self.rotation_study.policy['enabled']:
            try:
                self.rotation_study.observe(mapping, book, refs, self.latest_snapshot_id,
                    drawdown['buy_allowed'], self.available(exchange, account), account['myBalance'],
                    blocked=lambda m: bool(self.news_block(m) or self.reentry_wait(m['sig_exchange_id']) or
                        time.time()-self.races.last_order(self.journal, m['sig_exchange_id']) < self.config['cooldown_seconds']))
            except ValueError as error:
                self.journal.event('rotation_observation_skip', {'reason': str(error)})
        if phase == 'scan' and self.model_rotation_study.enabled:
            try:
                self.model_rotation_study.observe(mapping, book, self.latest_snapshot_id,
                    drawdown['buy_allowed'], self.available(exchange, account), account['myBalance'],
                    blocked=lambda m: bool(self.news_block(m) or self.reentry_wait(m['sig_exchange_id']) or
                        time.time()-self.races.last_order(self.journal, m['sig_exchange_id']) < self.config['cooldown_seconds']))
            except (ValueError, KeyError, TypeError, ArithmeticError, sqlite3.Error, OSError) as error:
                self.journal.event('model_rotation_observation_skip', {'reason':str(error)})
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
                self.drawdown.observe(exchange, book)
            except APIError as error:
                self.quote_diagnostics(exchange, book, None, 'performance_observation', error)
                if error.status in (401, 403, 429):
                    raise
                self.report('performance_observation_skip', {'exchange': exchange, 'reason': str(error)})
            except (ValueError, KeyError) as error:
                self.quote_diagnostics(exchange, book, None, 'performance_observation', error)
                self.report('performance_observation_skip', {'exchange': exchange, 'reason': str(error)})

    def observe_research_due(self):
        """Bounded SIG-only research reads; never marks the ledger or drawdown estimate."""
        followups = self.research_followups
        try:
            exchanges = followups.due()
        except sqlite3.Error as error:
            self.report('research_followup_skip', {'reason': str(error)})
            return
        for exchange in exchanges:
            if self.stopped():
                return
            try:
                followups.attempted(exchange)
                book = self.sig.book(exchange)
                now = time.time()
                quotes = self.snapshot_quotes(book, [], now)
                self.journal.event('quote_snapshot', {'version': 1, 'snapshot_id': str(uuid.uuid4()),
                    'exchange': exchange, 'phase': 'research_followup', 'captured_at': now,
                    'orientation': 'SIG YES; SIG-only research follow-up', 'quotes': quotes})
                followups.observed(exchange, book, now, status='fetched')
            except APIError as error:
                if error.status == 429:
                    followups.back_off()
                if error.status in (401, 403, 429):
                    raise
                self.report('research_followup_skip', {'exchange': exchange, 'reason': str(error)})
            except (ValueError, KeyError, TypeError, sqlite3.Error) as error:
                self.report('research_followup_skip', {'exchange': exchange, 'reason': str(error)})

    def radar_books(self, now):
        """Bulk public top-of-book for every enabled race (no SIG reads); shared by both radars."""
        if self._radar_books is not None and 0 <= now-self._radar_books[0] <= RADAR_REUSE_SECONDS:
            return self._radar_books[1:]
        for mapping in self.radar.missing(self.markets):
            try:
                self.radar.metadata[mapping['name']] = self.references.metadata(mapping)
            except (ValueError, KeyError, TypeError, APIError) as error:
                self.radar.metadata[mapping['name']] = None
                self.report('election_radar_skip', {'market': mapping['name'], 'reason': str(error)[:120]})
        items = [(m, self.radar.metadata[m['name']]) for m in self.markets
                 if self.radar.metadata.get(m['name'])]
        books, skipped = self.references.radar(items)
        self._radar_books = (now, books, skipped, len(items))
        return books, skipped, len(items)

    def election_radar(self):
        """Election night: bulk public reference reads flag newly called races for a SIG check."""
        now = time.time()
        if not self.radar.due(now):
            return
        try:
            books, skipped, watched = self.radar_books(now)
            names = {m['sig_exchange_id']: m['name'] for m in self.markets}
            fresh = self.radar.update({names[ex]: refs for ex, refs in books.items()}, now)
        except Exception as error:
            # Detection is optional: any radar failure must leave ordinary scanning running.
            self.radar.last_run = now
            self.report('election_radar_skip', {'error_type': type(error).__name__, 'reason': str(error)[:200]})
            return
        for name in fresh:
            self.scan_interest.flag(name, 'election_night')
        if fresh or skipped:
            self.report('election_radar', {'called': len(self.radar.called), 'newly_called': fresh,
                        'watched': watched, 'skipped': len(skipped)})

    def funding_targets(self, now, night):
        """Pending coin-blocked buys a sale could fund, with the funding rule in force, or None."""
        if night:
            p = self.election_night.policy
            if not p['recycle_enabled']:
                return None
            pending = [dict(v, race=self.races.race(ex)) for ex, v in self.election_night.pending.items()
                       if now-v['at'] <= p['recycle_pending_seconds']]
            rule = dict(max_giveup=p['recycle_max_giveup'], min_gain=p['recycle_margin'], called_only=True)
        else:
            if not self.rotation.enabled:
                return None
            p = self.rotation.policy
            pending = [v for v in self.rotation.pending.values() if now-v['at'] <= p['pending_seconds']]
            rule = dict(max_giveup=p['max_giveup'], min_gain=p['min_gain'], called_only=False)
        return dict(rule, targets=[{k: v[k] for k in ('race', 'ask', 'edge', 'need_coins')}
                                   for v in pending if v['need_coins'] > 0])

    def gap_radar_step(self):
        """Bulk-screen every enabled race and put the most valuable at the front of the priority lane.

        Costs one SIG read per 100 contracts plus the shared public reference reads. A flag
        only schedules a check; the visit applies every ordinary rule to fresh books.
        """
        from .gap_radar import Rules, rank, screen
        from .pair_baskets import locked
        now = time.time()
        night = self.election_night.active(now)
        if not self.execution.get('priority_scanning', True) or not self.gap_radar.due(now, night):
            return  # Flags only order the priority lane.
        self.gap_radar.last_run = now
        try:
            books, skipped, watched = self.radar_books(now)
            quotes = self.sig.prices([m['sig_exchange_id'] for m in self.markets])
            holdings, realized = self.ledger.inventory()
            held = {ex: v['quantity']*(1 if side == 'yes' else -1) for (ex, side), v in holdings.items()
                    if v['quantity']}
            committed = committed_by_race(holdings, realized, self.journal.pending(), self.races.assignments)
            exposure = Exposure(self.ledger, self.config)
            rules = Rules(self.gap_radar.policy, self.config, self.election_night if night else None,
                          self.funding_targets(now, night))
            screened = []
            for mapping in self.markets:
                exchange = mapping['sig_exchange_id']
                if quotes.get(exchange) is None or books.get(exchange) is None:
                    continue
                race, own = self.races.race(exchange), held.get(exchange, D(0))
                headroom = ({side: exposure.headroom(exchange, 'buy', side) for side in ('yes', 'no')}
                            if exposure.enabled else None)
                screened.append((mapping['name'], screen(
                    quotes[exchange], books[exchange], own, locked(self.journal.db, exchange) if own else D(0),
                    D(self.config['limits']['per_market'])-committed[race], headroom, rules, race)))
        except APIError as error:
            if error.status in (401, 403, 429):
                raise
            self.report('gap_radar_skip', {'error_type': 'APIError', 'reason': str(error)[:200]})
            return
        except Exception as error:
            # Screening is optional: any failure must leave ordinary scanning running.
            self.report('gap_radar_skip', {'error_type': type(error).__name__, 'reason': str(error)[:200]})
            return
        flagged = rank(screened, self.gap_radar.policy)
        self.scan_interest.set_gaps({name: c['value'] for name, c in flagged}, now+self.gap_radar.ttl(night))
        exchanges = {m['name']: m['sig_exchange_id'] for m in self.markets}
        self.report('gap_radar', {'phase': 'election_night' if night else 'before_night', 'watched': watched,
            'screened': len(screened), 'skipped': len(skipped),
            'candidates': sum(c is not None for _, c in screened),
            'flagged': [{'exchange': exchanges[name], 'market': name, 'route': c['route'], 'side': c['side'],
                         'edge': str(round(c['edge'], 4)), 'shares': c['shares'], 'value': str(round(c['value'], 2))}
                        for name, c in flagged]})

    def refresh_drawdown(self):
        """Bounded SIG-only refresh, including holdings outside normal entry scanning."""
        due = self.drawdown.due()
        if self.election_night.active():
            due = due[:self.election_night.policy['drawdown_refresh_per_cycle']]  # Reads go to trading.
        for exchange in due:
            if self.stopped():
                return
            self.drawdown.attempted(exchange)
            try:
                refs = None
                if self.drawdown.fair:
                    # Settlement-value marks need both reference books, not just SIG.
                    # Basket legs are disabled mappings but still carry pinned reference identities.
                    mapping = next((m for m in self.config['markets'] if m.get('sig_exchange_id') == exchange
                                    and (m.get('enabled') or m.get('basket_leg'))), None)
                    try:
                        if mapping is None:
                            raise ValueError('Held contract has no enabled mapping for fair-value marks')
                        refs = self.references.books(mapping, self.metadata(mapping))
                    except (ValueError, KeyError, APIError) as error:
                        if isinstance(error, APIError) and error.venue == 'SIG' and error.status in (401, 403, 429):
                            raise
                        # Still record the SIG bid mark: the drawdown falls back to it.
                        self.report('drawdown_fair_mark_skip', {'exchange': exchange, 'reason': str(error)[:200]})
                self.drawdown.observe(exchange, self.sig.book(exchange), refs)
            except APIError as error:
                if error.status in (401, 403, 429):
                    raise
                self.report('drawdown_mark_skip', {'exchange': exchange, 'reason': str(error)})
            except (ValueError, KeyError) as error:
                self.report('drawdown_mark_skip', {'exchange': exchange, 'reason': str(error)})
        if self.drawdown.policy['enabled']:
            self.report('drawdown_control', self.drawdown.status())

    def risk_exit_decision(self, mapping, positions, phase='scan', force=False):
        self.latest_risk_context = None
        if not self.risk_exit.policy['enabled']:
            return None, None
        if self.settlement_exits['enabled'] and self.settlement_exits['block_risk_exits']:
            return None, None  # SIG-only loss sales ignore settlement value.
        if self.live:
            verify_account_inventory(self.ledger, positions)
        held, cost = self.held(mapping['sig_exchange_id'], positions)
        if not held or self.news_block(mapping):
            return None, None
        # Bootstrap only from a full successful pin verification, never from an unreviewed SIG read.
        if not self.risk_exit.bound(mapping):
            self.metadata(mapping, force=True)
        verified_at = None
        if force:
            self.risk_exit.verify_market(mapping, self.sig.market(mapping['sig_market_id']))
            verified_at = time.time()
        book = self.sig.book(mapping['sig_exchange_id'])
        self.ledger.observe(mapping['sig_exchange_id'], book, self.config['strategy']['max_age_seconds'])
        self.drawdown.observe(mapping['sig_exchange_id'], book)
        exposure = Exposure(self.ledger, self.config)
        cap = exposure.headroom(mapping['sig_exchange_id'], 'sell', 'yes' if held > 0 else 'no') if exposure.enabled else None
        signal, detail = self.risk_exit.propose(mapping, book, held, cost, cap, phase=phase)
        signal = self.cap_basket_sale(signal, mapping['sig_exchange_id'], held, detail)
        self.record_quote_snapshot(mapping, book, [], 'risk_'+phase)
        self.report('risk_exit_check', {**detail, 'phase': phase, 'snapshot_id': self.latest_snapshot_id})
        if signal and force:
            self.latest_risk_context = (mapping, book, verified_at)
        return signal, book

    def scan_hints(self):
        if not self.execution.get('priority_scanning', True):
            return None
        holdings, _ = self.ledger.inventory()
        exchanges = {ex for (ex, side), values in holdings.items() if values['quantity']}
        owned = {m['name'] for m in self.markets if m['sig_exchange_id'] in exchanges}
        last_orders = self.races.last_orders(self.journal)
        not_before = {m['name']: last_orders.get(self.races.race(m['sig_exchange_id']), 0) + self.config['cooldown_seconds']
                      for m in self.markets}
        return self.scan_interest.hints(owned, not_before)

    def cycle(self):
        self.gate_metrics = GateMetrics()
        self._scan_metrics = {'quotes_checked':0, 'orders':0, 'contracts_visited':0}
        self._scan_status = 'complete'
        started = time.monotonic()
        failure = None
        try:
            result = self._cycle()
            if not result:self._scan_status = 'stopped'
            return result
        except BaseException as error:
            failure = error
            self._scan_status = 'halted'
            raise
        finally:
            # A diagnostic write failure must not replace the original recovery/STOP exception.
            try:
                self.report('scan_complete', {**self._scan_metrics, 'status':self._scan_status,
                    'error_type':type(failure).__name__ if failure else None,
                    'seconds':round(time.monotonic()-started,2), 'scan_policy':self.scan_policy,
                    'sig_reads_last_minute':getattr(getattr(self.sig,'http',None),'reads_last_minute',lambda:None)(),
                    **self.gate_metrics.summary()})
            except Exception:
                if failure is None:raise

    def _cycle(self):
        try:self.reconcile()
        except Exception:
            self.gate_block('order_recovery', 'cycle')
            raise
        try:
            assessment=self.performance_throttle.refresh()
            self.report('performance_throttle',{'routes':{k:{'suggested_multiplier':d['suggested_multiplier'],
                'reason':d['reason']} for k,d in assessment.items()}})
        except (ValueError,TypeError,KeyError,sqlite3.Error) as error:
            self.performance_throttle.error=str(error)
            self.report('performance_throttle_unavailable',{'error_type':type(error).__name__})
        coverage = exit_coverage(self.ledger.inventory()[0], self.config)
        if coverage != self.last_exit_coverage:
            self.report('exit_coverage', coverage)
            self.last_exit_coverage = coverage
        self.check_loss_stop()
        if self.stopped():
            self.gate_block('stop', 'cycle')
            return False
        if not self.markets:
            raise ValueError('No enabled mappings')
        if self.config.get('news', {}).get('enabled') and self.news is None:
            raise ValueError('News monitor is required')
        account = self.sig.account()
        self.check_account(account)
        self.report('portfolio_risk', {**Exposure(self.ledger, self.config).summary(), **self.loss_status()})
        self.observe_due()
        self.refresh_drawdown()
        if self.election_night.active():
            self.election_radar()
            if not self.election_night.policy['pause_research_reads']:
                self.observe_research_due()
        else:
            self.observe_research_due()
        self.gap_radar_step()
        if self.sig.open_orders():
            self.gate_block('open_orders', 'cycle')
            self._scan_status = 'waiting'
            self.report('skip', {'reason': 'Existing open orders; trading paused'})
            holdings, _ = self.ledger.inventory()
            for exchange in {ex for ex, side in holdings}:
                self.exit_blocked(exchange, 'open_orders', 'scan')
            return True
        positions = self.sig.positions()
        if self.pair_policy['enabled'] and self.pair_list:
            self.pair_basket_cycle(account, positions)
            if self.stopped():
                return False
            account = self.sig.account()
            self.check_account(account)
            positions = self.sig.positions()
        for name, events in (self.news.drain() if self.news else {}).items():
            self.pending_news[name] = (self.pending_news.get(name, []) + events)[-100:]
        orders, checked = 0, 0
        self.report('scan_batch', {'enabled_races': len({self.races.race(m['sig_exchange_id']) for m in self.markets}),
                    'enabled_contracts': len(self.markets),
                    'max_orders': self.execution['max_orders_per_cycle'], 'scan_policy': self.scan_policy,
                    **self.portfolio_summary()})
        from .entry_ranking import settings as rank_settings, candidate, ordered, MAX_RECHECKS
        deferred = []
        ranking_enabled = rank_settings(self.config)
        def visits():
            for item in self.scanner.batch(self.pending_news, self.config.get('scan_batch_size', 8), self.scan_hints()):
                yield item, None
            if ranking_enabled:
                now = time.time()
                ranked = ordered(deferred, now)[:MAX_RECHECKS]
                self.report('entry_ranking', {'policy':'net_surplus_v1','candidates':len(deferred),
                    'fresh_candidates':sum(d['expires'] >= now for d in deferred),'rechecked':len(ranked),
                    'scope':'current scan batch only',
                    'order':[{'exchange':d['mapping']['sig_exchange_id'],'surplus_proxy':str(d['surplus'])} for d in ranked]})
                for item in ranked:
                    # Stale ranking quotes are fine here: preflight re-quotes and re-decides.
                    yield item['mapping'], item
        for mapping, ranked in visits():
            if self.stopped():
                return False
            exchange = mapping['sig_exchange_id']
            book, refs, phase = None, None, 'scan'
            risk_route = False
            try:
                if ranked is None:
                    self._scan_metrics['contracts_visited'] += 1
                    self.report('scan_visit', {'exchange':exchange,'market':mapping['name'],
                                'scan_policy':self.scan_policy, **self.scanner.selection})
                    self.scan_interest.checked(mapping['name'])
                    if time.time()-self.races.last_order(self.journal, exchange) < self.config['cooldown_seconds']:
                        self.gate_block('race_cooldown', phase, exchange)
                        self.report('skip', {'exchange': exchange, 'reason': 'cooldown'})
                        self.exit_blocked(exchange, 'cooldown', phase)
                        continue
                    context = self.pending_news.pop(mapping['name'], [])
                    risk_route = False
                    refs = []
                    signal, book = self.risk_exit_decision(mapping, positions)
                    if signal is not None:
                        risk_route = True
                        self.gate_block('risk_exit_preemption', phase, exchange)
                        checked += 1
                        self._scan_metrics['quotes_checked'] = checked
                    else:
                        metadata = self.metadata(mapping, bool(context))
                        refs = self.references.books(mapping, metadata)
                        book = self.sig.book(exchange)
                        checked += 1
                        self._scan_metrics['quotes_checked'] = checked
                        self.ledger.observe(exchange, book, self.config['strategy']['max_age_seconds'])
                        self.drawdown.observe(exchange, book, refs)
                        block = self.news_block(mapping)
                        if block and not self.election_night.overrides(block):
                            self.gate_block('news_pause', phase, exchange)
                            self.scan_interest.invalidate(mapping['name'])
                            self.report('skip', {'exchange': exchange, 'reason': block})
                            self.exit_blocked(exchange, 'news_pause', phase, block)
                            continue
                        signal = self.decide(mapping, account, positions, book, refs,
                                             news_restricted=bool(block))
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
                    if ranking_enabled and signal.action == 'buy':
                        deferred.append(candidate(mapping,signal,book,refs,self.config))
                        continue
                # Serial final checks: cached rules/old positions never authorize an order.
                phase, book, refs = 'preflight', None, None
                if not risk_route:
                    metadata = self.metadata(mapping, force=True)
                account = self.sig.account()
                self.check_account(account)
                if self.sig.open_orders():
                    self.gate_block('open_orders', phase, exchange)
                    self._scan_status = 'waiting'
                    self.report('skip', {'exchange': exchange, 'reason': 'Open order appeared during scan'})
                    self.exit_blocked(exchange, 'open_orders', phase)
                    return True
                positions = self.sig.positions()
                if risk_route:
                    refs = []
                    signal, book = self.risk_exit_decision(mapping, positions, phase=phase, force=True)
                else:
                    refs = self.references.books(mapping, metadata)
                    book = self.sig.book(exchange)
                    # Outside the window this adds no lookup, so preflight is unchanged.
                    block = self.news_block(mapping) if self.election_night.active() else None
                    signal = self.decide(mapping, account, positions, book, refs, phase=phase,
                                         news_restricted=bool(block) and self.election_night.overrides(block))
                if signal is None:
                    self.gate_block('preflight_recheck', phase, exchange)
                    continue
                if ranked is not None and signal.action=='buy' and (
                        signal.side!=ranked['side'] or signal.reason!=ranked['reason']):
                    self.gate_block('ranked_signal_changed',phase,exchange)
                    continue
            except (ValueError, KeyError, APIError) as error:
                self.cache.pop(mapping['name'], None)
                self.scan_interest.invalidate(mapping['name'])
                self.quote_diagnostics(exchange, book, refs, phase, error)
                exit_reason = (error.reason if isinstance(error, InventoryValidationError) else
                               'quote_validation' if isinstance(error, BookValidationError) else
                               'feed_error' if isinstance(error, APIError) else 'validation_error')
                self.gate_block(exit_reason, phase, exchange)
                self.exit_blocked(exchange, exit_reason, phase, str(error))
                if isinstance(error, APIError) and error.status in (401, 403, 429):
                    raise
                self.report('skip', {'exchange': exchange, 'reason': str(error)})
                continue
            if self.stopped():
                return False
            if self.news_gate(mapping, signal):
                self.gate_block('news_pause', 'pre_submit', exchange)
                self.report('skip', {'exchange': exchange, 'reason': 'News pause during preflight'})
                self.exit_blocked(exchange, 'news_pause', 'pre_submit')
                continue
            try:
                book.check(self.config['strategy']['max_age_seconds'], venue='SIG')
                for venue, ref in zip(('Kalshi', 'Polymarket'), refs):
                    ref.check(self.config['strategy']['max_age_seconds'], venue=venue)
            except BookValidationError as error:
                self.gate_block('quote_validation', 'pre_submit', exchange)
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
                if (not self.night_exempt(signal.action, signal.reason, 'ignore_exposure_caps')
                        and signal.quantity > exposure.headroom(exchange, signal.action, signal.side)):
                    self.gate_block('exposure_headroom', 'pre_submit', exchange)
                    self.report('skip', {'exchange': exchange, 'reason': 'Exposure changed before submission'})
                    continue
            payload = {'exchangeId': exchange, 'side': signal.side, 'action': signal.action,
                'quantity': signal.quantity, 'price': float(signal.price), 'tournamentId': self.sig.tid,
                'idempotencyKey': str(uuid.uuid4()), 'expirationDate': datetime.fromtimestamp(
                    time.time()+self.config['order_lifetime_seconds'], timezone.utc).isoformat()}
            fee = D(self.config['strategy']['cost_buffer_per_share'])
            reserve = signal.quantity * (signal.price + fee)
            try:
                self.reserve_order(payload, reserve, account, positions, exit_reason=signal.reason,
                                   exit_epoch=self.latest_exit_epoch, model_context=self.latest_model_context,
                                   entry_context=self.latest_entry_context, risk_context=self.latest_risk_context,
                                   patient_context=self.latest_patient_context,
                                   throttle_context=self.latest_throttle_context)
            except ReservationRejected as error:
                self.report('skip', {'exchange': exchange, 'reason': str(error)})
                continue
            if self.stopped() or self.news_gate(mapping, signal):
                self.gate_block('stop_or_news', 'post_reservation', exchange)
                self.ledger.record(payload, 0, 0, fee)
                return not self.stopped()
            self.report('signal', {'exchange': exchange, 'order_key': payload['idempotencyKey'],
                                   'snapshot_id': self.latest_snapshot_id, **vars(signal)})
            if self.live:
                try:
                    self.send_order(payload, reserve,
                                    patient_mapping=mapping if signal.reason=='patient_entry' else None)
                except OrderUnresolved as error:
                    raise error.original  # Unchanged recovery/retry/halt handling for the supervisor.
            else:
                # A resting limit cannot be assumed to fill just because it was proposed.
                paper_quantity = 0 if signal.reason == 'patient_entry' else signal.quantity
                self.ledger.record(payload, paper_quantity, signal.price if paper_quantity else 0, fee)
                self.report('paper_fill', {'exchange': exchange, **vars(signal),
                    'filled':paper_quantity, 'simulation':True, 'resting_fill_not_modeled':signal.reason=='patient_entry'})
            from .capital_rotation import REASON as ROTATION
            if signal.reason == ROTATION:
                self.rotation.sold(signal.quantity*signal.price)  # Shared cash: every pending need shrinks.
            orders += 1
            self._scan_metrics['orders'] = orders
            self.check_loss_stop()
            if self.stopped():
                return False
            if orders >= self.execution['max_orders_per_cycle']:
                break
            account = self.sig.account()
            self.check_account(account)
            positions = self.sig.positions()
        self._scan_metrics.update(self.portfolio_summary())
        return True
