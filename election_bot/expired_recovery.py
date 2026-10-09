"""Explicit recovery of an expired request rejected by SIG's input validator.

An expiry rejection alone cannot prove the original submission was unfilled.
Require complete history containing only already-accounted orders and matching
account inventory twice. Unknown orders are never guessed into a local request.
"""
import json
import time

from .clients import iso_time
from .risk import verify_account_inventory
from .strategy import D


def accounted_history(engine, payload, history):
    known = {}
    for row in engine.journal.db.execute('''SELECT o.payload,o.response,e.quantity
            FROM orders o LEFT JOIN executions e ON e.key=o.key
            WHERE o.market=? AND o.state='closed' ''', (payload['exchangeId'],)):
        response = json.loads(row['response']) if row['response'] else {}
        oid = response.get('orderId')
        if oid is None:
            continue
        oid = str(oid)
        if oid in known or row['quantity'] is None:
            raise RuntimeError('Local order history not fully accounted; reservation retained')
        known[oid] = (json.loads(row['payload']), D(row['quantity']))
    seen = set()
    for order in history:
        oid = str(order['id'])
        if oid in seen or oid not in known:
            raise RuntimeError('Unrecognized or duplicate SIG order in recovery history; reservation retained')
        seen.add(oid)
        original, filled = known[oid]
        if (str(order['exchangeId']) != payload['exchangeId'] or order['tournamentId'] != engine.sig.tid
                or order['open'] is not False or order['action'] != original['action']
                or order['side'] != original['side'] or D(order['priceLimit']) != D(original['price'])
                or D(order['quantity']) != D(original['quantity']) or D(order['quantityFilled']) != filled
                or abs(iso_time(order['expirationDate']) - iso_time(original['expirationDate'])) > .001):
            raise RuntimeError('SIG history differs from accounted orders; reservation retained')
    if seen != set(known):
        raise RuntimeError('Accounted orders missing from recovery history; reservation retained')
    return sorted(seen)


def recover_rejected_expiration(engine, row, payload):
    if time.time() < max(iso_time(payload['expirationDate']), row['created']) + 90:
        raise RuntimeError('Expired-request recovery requires expiration plus 90 seconds; reservation retained')
    pending = engine.journal.pending()
    if len(pending) != 1 or pending[0]['key'] != row['key'] or row['response'] is not None:
        raise RuntimeError('Recovery requires exactly one unacknowledged request; reservation retained')
    if engine.ledger.missing():
        raise RuntimeError('Historical fills must be imported before expired recovery; reservation retained')
    prior, evidence = None, []
    for attempt in range(2):
        if attempt:
            time.sleep(1)
        history = engine.sig.recovery_history(payload['exchangeId'])
        ids = accounted_history(engine, payload, history['orders'])
        # Confirm history reads bracket a complete portfolio comparison.
        verify_account_inventory(engine.ledger, engine.sig.positions())
        sequence = history['sequence']
        if type(sequence) is not int or sequence < 0:
            raise RuntimeError('Recovery history checkpoint missing; reservation retained')
        if prior is not None and (sequence < prior['sequence'] or ids != prior['order_ids']):
            raise RuntimeError('Recovery history changed or regressed; reservation retained')
        prior = {'sequence': sequence, 'order_ids': ids}
        evidence.append(prior)
    # Record evidence and zero execution atomically; a crash never loses the
    # reservation without also retaining why it was released.
    with engine.journal.db:
        engine.journal.db.execute('INSERT INTO events VALUES (?,?,?)', (time.time(),
            'expired_request_verified_unplaced', json.dumps({'exchange': payload['exchangeId'],
                'order_key': row['key'], 'expiration': payload['expirationDate'],
                'checks': evidence, 'portfolio_matches': True})))
        engine.ledger.record(payload, 0, 0, engine.config['strategy']['cost_buffer_per_share'])
    engine.report('recovery_complete', {'exchange': payload['exchangeId'],
        'outcome': 'expired_request_verified_unplaced', 'released_reservation': row['amount']})
