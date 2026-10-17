"""Improved-price entry pilot. Never chases the ask; an unfilled order expires.

The route does not rest an order across scan cycles. It reuses the existing
order lifetime, so a non-fill is the already-supported zero-quantity execution
and consumes no budget. Assignment between the patient and control arms is
derived from committed ledger state alone, so the reservation guard can
re-derive it independently instead of trusting a carried decision.
"""
import hashlib
import json

from .strategy import D, Signal

REASON = 'patient_entry'
TICK = D('.005')
DEFAULTS = dict(enabled=False, allocation='500', improvement_ticks=1,
                min_sig_spread='.01', control_fraction='.5')


def settings(config):
    raw = config.get('patient_entry', {})
    if not isinstance(raw, dict) or set(raw)-set(DEFAULTS):
        raise ValueError('Invalid patient_entry configuration')
    p = dict(DEFAULTS, **raw)
    if type(p['enabled']) is not bool:
        raise ValueError('patient_entry enabled must be boolean')
    if type(p['improvement_ticks']) is not int or not 1 <= p['improvement_ticks'] <= 10:
        raise ValueError('patient_entry improvement_ticks must be an integer from 1 to 10')
    for key in ('allocation', 'min_sig_spread', 'control_fraction'):
        if isinstance(p[key], bool):
            raise ValueError('patient_entry ' + key + ' must be numeric')
        p[key] = D(p[key])
    if p['allocation'] <= 0:
        raise ValueError('patient_entry allocation must be positive')
    if not TICK <= p['min_sig_spread'] < 1:
        raise ValueError('patient_entry min_sig_spread must be at least one tick and below one')
    if not 0 <= p['control_fraction'] <= 1:
        raise ValueError('patient_entry control_fraction must be between zero and one')
    if p['enabled'] and config.get('execution', {}).get('enabled') is not True:
        raise ValueError('Patient entry requires active execution')
    return p


def initialize(db):
    # Separate from event logs, which may be archived.
    db.execute('''CREATE TABLE IF NOT EXISTS patient_orders (
        key TEXT PRIMARY KEY, exchange TEXT NOT NULL, arm TEXT NOT NULL,
        priced TEXT NOT NULL DEFAULT 'improved', improvable INTEGER,
        detail TEXT NOT NULL)''')
    present = {row[1] for row in db.execute('PRAGMA table_info(patient_orders)')}
    for name, spec in (('priced', "TEXT NOT NULL DEFAULT 'improved'"), ('improvable', 'INTEGER')):
        if name not in present:
            db.execute('ALTER TABLE patient_orders ADD COLUMN %s %s' % (name, spec))
    db.commit()


def prior_buys(db, exchange):
    """Committed buy count for this contract; the reservation guard re-reads it."""
    row = db.execute('''SELECT COUNT(*) FROM executions
        WHERE exchange=? AND action='buy' AND CAST(quantity AS REAL)>0''', (exchange,)).fetchone()
    return int(row[0])


def arm(db, exchange, p):
    """Deterministic split from committed state, not from a clock or a counter in memory."""
    if p['control_fraction'] >= 1:
        return 'control'
    if p['control_fraction'] <= 0:
        return 'patient'
    seed = '%s:%d' % (exchange, prior_buys(db, exchange))
    draw = int(hashlib.sha256(seed.encode()).hexdigest()[:8], 16) % 10000
    return 'control' if D(draw) < p['control_fraction']*10000 else 'patient'


def allowance(db, p):
    """Lifetime gross cost already spent on the patient arm; never recycled."""
    spent = D(0)
    # Charged on the price actually sent, not the arm assigned: a patient-arm order
    # that fell back to the control price spends no pilot allocation.
    for row in db.execute('''SELECT o.amount FROM patient_orders t JOIN orders o ON o.key=t.key
                             WHERE t.priced='improved' '''):
        amount = D(row['amount'])
        if amount < 0:
            raise ValueError('Invalid patient reservation accounting')
        spent += amount
    return max(D(0), p['allocation']-spent), spent


def inside(book, side, control_price, p):
    """The improved limit price for this book, or None with the reason it is unavailable.

    Pure arithmetic on an already-validated book. Shared by the live route and by
    the control arm's counterfactual, so both arms answer 'was this improvable?'
    with the same rule rather than two implementations that can drift apart.
    """
    target = book if side == 'yes' else book.complement()
    if not target.bids or not target.asks:
        return None, 'two_sided_book_required'
    bid, ask = target.bids[0][0], target.asks[0][0]
    if ask != control_price:
        return None, 'ask_moved_since_selection'
    if ask-bid < p['min_sig_spread']:
        return None, 'spread_too_narrow_to_improve'
    price = max(bid, ask-p['improvement_ticks']*TICK)
    if price >= ask:
        return None, 'no_price_improvement_available'
    return price, 'improved'


def counterfactual(book, side, control_price, p):
    """Would this control order have been improvable? Measurement only; never raises.

    Without this the pilot compares improved fills against every control fill,
    including the narrow-spread markets the patient arm never competes in. That
    difference in eligibility, not patience, would drive the result.
    """
    try:
        price, reason = inside(book, side, D(control_price), p)
    except Exception as error:                      # a measurement must not reject an order
        return {'improvable': None, 'reason': 'counterfactual_failed: '+str(error)}
    return {'improvable': price is not None, 'reason': reason,
            'would_be_price': str(price) if price is not None else None}


def improve(book, signal, p, fee):
    """Price inside the spread without changing the intended size.

    Quantity is deliberately held at the control size. Improving the price
    alone keeps the arms comparable; re-sizing would confound price
    improvement with a different exposure.
    """
    if signal.action != 'buy' or signal.reason != 'entry_gap':
        return None, 'not_a_plain_entry'
    price, reason = inside(book, signal.side, signal.price, p)
    if price is None:
        return None, reason
    if price % TICK or not TICK <= price <= 1-TICK:
        raise ValueError('Improved patient price is not a valid limit-order tick')
    # Same reference and fee as the control signal, so a lower price is a wider edge.
    improved = Signal(signal.side, price, signal.quantity, signal.reference,
                      signal.reference-price-fee, 'buy', REASON)
    if improved.edge <= signal.edge:
        raise ValueError('Patient price failed to widen the entry edge')
    return improved, 'improved'


def propose(db, mapping, config, book, entry, now=None):
    """Return the patient replacement for an eligible entry, or None to keep it."""
    p = settings(config)
    exchange = mapping['sig_exchange_id']
    detail = {'status': 'blocked', 'route': REASON, 'exchange': exchange, 'at': now,
              'control_price': str(entry.price) if entry else None,
              'control_quantity': entry.quantity if entry else None}
    if not p['enabled']:
        return None, dict(detail, reason='disabled')
    if entry is None:
        return None, dict(detail, reason='no_control_entry')
    assigned = arm(db, exchange, p)
    detail['arm'] = assigned
    if assigned == 'control':
        return None, dict(detail, status='control', reason='assigned_control')
    remaining, spent = allowance(db, p)
    detail.update(allocation_spent=str(spent), allocation_remaining=str(remaining))
    if remaining <= 0:
        return None, dict(detail, reason='patient_allocation_exhausted')
    fee = D(config['strategy']['cost_buffer_per_share'])
    improved, why = improve(book, entry, p, fee)
    if improved is None:
        return None, dict(detail, reason=why)
    reserve = improved.quantity*(improved.price+fee)
    if reserve > remaining:
        return None, dict(detail, reason='patient_allocation_too_small', required=str(reserve))
    detail.update(status='eligible', reason=REASON, price=str(improved.price),
                  quantity=improved.quantity, improvement=str(entry.price-improved.price),
                  settings={k: str(v) if not isinstance(v, bool) else v for k, v in p.items()})
    return improved, detail


def attach(db, payload, detail, priced='improved', improvable=True):
    if not db.in_transaction:
        raise RuntimeError('Patient attribution requires the reservation transaction')
    db.execute('INSERT INTO patient_orders VALUES (?,?,?,?,?,?)',
               (payload['idempotencyKey'], payload['exchangeId'], detail['arm'], priced,
                None if improvable is None else int(improvable),
                json.dumps(detail, default=str, allow_nan=False)))


def attach_control(db, payload, config, entry_context):
    """Record a control-arm buy so the pilot has a denominator. Never rejects an order.

    The patient route is the only one that can refuse to trade on its own
    assertions. Attribution is measurement: if anything here goes wrong the
    order proceeds and the row records why it could not be classified.
    """
    try:
        p = settings(config)
        if not p['enabled'] or payload['action'] != 'buy':
            return
        exchange = payload['exchangeId']
        assigned = arm(db, exchange, p)
        detail = {'status': 'control', 'route': 'control', 'arm': assigned, 'exchange': exchange,
                  'control_price': str(payload['price']), 'control_quantity': payload['quantity']}
        if entry_context is not None and entry_context[0].get('sig_exchange_id') == exchange:
            detail.update(counterfactual(entry_context[1], payload['side'], payload['price'], p))
        else:
            detail.update(improvable=None, reason='no_book_for_counterfactual')
        attach(db, payload, detail, priced='control', improvable=detail['improvable'])
    except Exception as error:
        try:
            db.execute('INSERT INTO patient_orders VALUES (?,?,?,?,?,?)',
                       (payload['idempotencyKey'], payload['exchangeId'], 'unknown', 'control',
                        None, json.dumps({'status': 'attribution_failed', 'error': str(error)})))
        except Exception:
            pass                                    # measurement never blocks a trade
