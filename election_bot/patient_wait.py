"""Short, synchronous observation of an acknowledged owned order; never resubmits."""
import time
from .clients import iso_time
from .strategy import D


def validate(order, payload, oid):
    if (str(order['id']) != str(oid) or str(order['exchangeId']) != payload['exchangeId']
            or order['tournamentId'] != payload['tournamentId']
            or order['action'] != payload['action'] or order['side'] != payload['side']
            or D(order['priceLimit']) != D(payload['price']) or type(order.get('open')) is not bool):
        raise RuntimeError('Patient order identity/status changed; reservation retained')


def observe(payload, order, seconds, refresh, eligible, stopped,
            wall=time.time, monotonic=time.monotonic, sleep=time.sleep):
    """Caller must cancel/confirm closure afterwards, even on exceptions/interrupts.

    An exchange expiration remains the hard bound if a network request stalls.
    Local patience is never renewed, and recovery never calls this function.
    """
    oid=order['id']
    remaining=min(seconds, max(0, iso_time(payload['expirationDate'])-wall()-2))
    deadline=monotonic()+remaining
    while True:
        validate(order,payload,oid)
        if not order['open']:return order,'closed'
        if stopped():return order,'stop'
        if monotonic()>=deadline:return order,'patience_expired'
        if not eligible():return order,'signal_invalidated'
        remaining=deadline-monotonic()
        if remaining<=0:return order,'patience_expired'
        sleep(min(1,remaining))
        if stopped():return order,'stop'
        if monotonic()>=deadline:return order,'patience_expired'
        order=refresh(oid)
