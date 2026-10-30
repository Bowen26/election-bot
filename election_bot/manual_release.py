"""Release of an unacknowledged order that SIG never recorded, by the operator or by recovery.

Normal recovery releases a reservation only when SIG marks the market's order history
complete. If SIG's history for one market stays incomplete, that single reservation
blocks every trade indefinitely. This command lets the operator release it as unplaced,
with the evidence recovery uses plus two independent account-wide checks:

- the request was never acknowledged and expired at least MIN_EXPIRED_SECONDS ago;
- SIG shows no open orders anywhere;
- every order SIG lists for the market matches a locally accounted order, except possibly
  the request itself with nothing filled; local orders SIG omits must have filled nothing
  (SIG's list can omit unfilled orders, which is why it reports itself incomplete);
- SIG positions match the bot ledger in every market;
- SIG cash matches the cash implied by every recorded execution, and a fill of this
  request would have moved cash by more than the tolerance.

Every read is taken twice and must agree. Nothing changes unless every check passes.
The operator names the order key. With `auto_recovery.release_unplaced`, `recover` also
runs these checks itself, but only after SIG's history fails expired-request recovery.
"""
import json
import time

from .clients import iso_time
from .risk import verify_account_inventory
from .strategy import D

MIN_EXPIRED_SECONDS = 600
CASH_TOLERANCE = D('0.01')  # SIG reports cash in cents.
STARTING_CASH = '100000'  # Tournament starting balance; also the release-unplaced --starting-cash default.
AUTO_EVENT = 'auto_released_unplaced'


class ReleaseRefused(RuntimeError):
    """A check failed; the reservation stays in place."""


def market_history(sig, exchange):
    """Every SIG order for one market, recording the coverage flags instead of requiring them."""
    params = {'status': 'all', 'tournamentId': sig.tid, 'exchangeId': str(exchange), 'limit': 100}
    rows, ids, cursors, coverage = [], set(), set(), []
    for _ in range(100):
        page = sig.read('/orders', params=dict(params))
        coverage.append(page.get('coverage'))
        if not isinstance(page.get('data'), list):
            raise ReleaseRefused('Malformed order history page')
        for row in page['data']:
            oid = str(row['id'])
            if oid in ids or str(row['exchangeId']) != str(exchange) or row['tournamentId'] != sig.tid:
                raise ReleaseRefused('Order history identity mismatch')
            ids.add(oid)
            rows.append(row)
        pagination = page.get('pagination') or {}
        if pagination.get('hasMore') is False:
            return {'orders': rows, 'coverage': coverage}
        cursor = pagination.get('nextCursor')
        if pagination.get('hasMore') is not True or not isinstance(cursor, str) or not cursor or cursor in cursors:
            raise ReleaseRefused('Incomplete order history pagination')
        cursors.add(cursor)
        params['cursor'] = cursor
    raise ReleaseRefused('Order history longer than 100 pages')


def compare_history(db, tid, payload, history):
    """Match SIG's listed orders against locally accounted ones for this market.

    Returns (listed ids, local ids SIG omits, id of the request if SIG lists it)."""
    known = {}
    for row in db.execute('''SELECT o.payload, o.response, e.quantity FROM orders o
            LEFT JOIN executions e ON e.key=o.key WHERE o.market=? AND o.state='closed' ''',
                          (payload['exchangeId'],)):
        response = json.loads(row['response']) if row['response'] else {}
        if response.get('orderId') is None:
            continue
        oid = str(response['orderId'])
        if oid in known or row['quantity'] is None:
            raise ReleaseRefused('Local order history is not fully accounted')
        known[oid] = (json.loads(row['payload']), D(row['quantity']))
    def same(order, original):
        return (order['action'] == original['action'] and order['side'] == original['side']
                and D(str(order['priceLimit'])) == D(str(original['price']))
                and D(order['quantity']) == D(original['quantity'])
                and abs(iso_time(order['expirationDate'])-iso_time(original['expirationDate'])) <= .001)
    seen, request = set(), None
    for order in history:
        oid = str(order['id'])
        if oid in seen or str(order['exchangeId']) != payload['exchangeId'] or order['tournamentId'] != tid:
            raise ReleaseRefused('SIG history identity mismatch')
        seen.add(oid)
        if order['open'] is not False:
            raise ReleaseRefused('SIG lists an open order on this market')
        if oid in known:
            original, filled = known[oid]
            if not same(order, original) or D(order['quantityFilled']) != filled:
                raise ReleaseRefused('SIG history differs from accounted order ' + oid)
        elif same(order, payload):
            if D(order['quantityFilled']) != 0:
                raise ReleaseRefused('SIG shows the request filled; use normal recovery')
            request = oid
        else:
            raise ReleaseRefused('Unrecognized SIG order ' + oid)
    omitted = sorted(oid for oid in known if oid not in seen)
    if any(known[oid][1] != 0 for oid in omitted):
        raise ReleaseRefused('SIG history omits a filled local order')
    return sorted(seen), omitted, request


def implied_cash(db, starting_cash):
    """Starting cash plus every recorded sale minus every recorded purchase (no fees on SIG)."""
    cash = D(starting_cash)
    for row in db.execute('SELECT action, quantity, price FROM executions'):
        value = D(row['quantity'])*D(row['price'])
        cash += value if row['action'] == 'sell' else -value
    return cash


def release_unplaced(journal, ledger, sig, order_key, starting_cash, fee, now=None, sleep=time.sleep,
                     dry_run=False, event='operator_released_unplaced'):
    now = time.time() if now is None else now
    pending = journal.pending()
    if len(pending) != 1 or pending[0]['key'] != order_key:
        raise ReleaseRefused('Exactly one pending order, with this key, is required')
    row = pending[0]
    if row['response'] is not None:
        raise ReleaseRefused('SIG acknowledged this request; use normal recovery')
    payload = json.loads(row['payload'])
    if payload.get('idempotencyKey') != order_key or payload.get('tournamentId') != sig.tid:
        raise ReleaseRefused('Pending order does not match this key and tournament')
    expired_for = now-iso_time(payload['expirationDate'])
    if expired_for < MIN_EXPIRED_SECONDS:
        raise ReleaseRefused('The request must have expired at least %d seconds ago' % MIN_EXPIRED_SECONDS)
    if ledger.missing():
        raise ReleaseRefused('Historical fills must be imported first')
    fill_value = D(str(payload['price']))*D(payload['quantity'])
    if fill_value <= 2*CASH_TOLERANCE:
        raise ReleaseRefused('A fill of this size would not be visible in cash')
    expected_cash = implied_cash(journal.db, starting_cash)
    evidence = []
    for attempt in range(2):
        if attempt:
            sleep(1)
        if sig.open_orders():
            raise ReleaseRefused('SIG shows open orders')
        history = market_history(sig, payload['exchangeId'])
        ids, omitted, request = compare_history(journal.db, sig.tid, payload, history['orders'])
        try:
            verify_account_inventory(ledger, sig.positions())
        except RuntimeError as error:
            raise ReleaseRefused(str(error)) from error
        cash = D(str(sig.account()['myBalance']))
        if abs(cash-expected_cash) > CASH_TOLERANCE:
            raise ReleaseRefused('SIG cash %s differs from recorded executions %s' % (cash, expected_cash))
        evidence.append({'order_ids': ids, 'omitted_unfilled': omitted, 'request_on_sig': request,
                         'coverage': history['coverage'], 'cash': str(cash)})
    if any(evidence[0][k] != evidence[1][k] for k in ('order_ids', 'omitted_unfilled', 'request_on_sig', 'cash')):
        raise ReleaseRefused('SIG history or cash changed between checks')
    detail = {'exchange': payload['exchangeId'], 'order_key': order_key,
              'expiration': payload['expirationDate'], 'expired_seconds': round(expired_for),
              'sig_listed_orders': len(evidence[0]['order_ids']), 'coverage': evidence[-1]['coverage'],
              'local_unfilled_orders_sig_omits': evidence[0]['omitted_unfilled'],
              'request_listed_on_sig': evidence[0]['request_on_sig'],
              'sig_cash': evidence[-1]['cash'], 'recorded_cash': str(expected_cash),
              'fill_would_move_cash_by': str(fill_value), 'positions_match': True, 'open_orders': 0}
    if dry_run:
        return dict(detail, released=False, dry_run=True, would_release=row['amount'])
    # Evidence and the zero execution form one transaction: never one without the other.
    with journal.db:
        journal.db.execute('INSERT INTO events VALUES (?,?,?)', (time.time(), event, json.dumps(detail)))
        ledger.record(payload, 0, 0, fee)
    return dict(detail, released=True, released_reservation=row['amount'])


def release_after_incomplete_history(engine, row, payload, starting_cash=STARTING_CASH):
    """Recovery fallback once expired-request recovery found SIG's market history incomplete.

    Waits until the request has been expired MIN_EXPIRED_SECONDS (STOP ends a supervised wait),
    then applies every check above and records AUTO_EVENT. A refusal halts for review with the
    reservation retained; it is never retried as an incomplete-history recovery."""
    ready = iso_time(payload['expirationDate'])+MIN_EXPIRED_SECONDS
    delay = ready-time.time()
    if delay > MIN_EXPIRED_SECONDS:
        raise RuntimeError('Automatic release wait is unexpectedly long; review the clock and journal; '
                           'reservation retained')
    if delay > 0:
        engine.report('auto_release_wait', {'exchange': payload['exchangeId'], 'order_key': row['key'],
            'wait_seconds': round(delay, 2),
            'action': 'No new trades; SIG history is incomplete, so release checks run %d seconds after '
                      'expiration' % MIN_EXPIRED_SECONDS})
        deadline = time.monotonic()+delay+60
        while time.time() < ready:
            if getattr(engine, 'recovery_stop_guard', False) and engine.stopped():
                raise KeyboardInterrupt
            if time.monotonic() >= deadline:
                raise RuntimeError('Automatic release wait exceeded its clock bound; reservation retained')
            time.sleep(min(.25, max(0, ready-time.time())))
    # Fresh SIG timing, so "expired long enough" also holds on SIG's clock.
    engine.sig.account()
    engine.check_clock()
    try:
        result = release_unplaced(engine.journal, engine.ledger, engine.sig, row['key'], D(starting_cash),
                                  D(engine.config['strategy']['cost_buffer_per_share']),
                                  sleep=time.sleep, event=AUTO_EVENT)
    except ReleaseRefused as error:
        raise ReleaseRefused('Automatic release refused: %s; reservation retained' % error) from error
    engine.report('recovery_complete', {'exchange': payload['exchangeId'], 'outcome': AUTO_EVENT,
                                        'released_reservation': result['released_reservation']})
    return result
