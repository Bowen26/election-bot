"""Preparation for supervised recovery; never edits/replaces an order payload."""
import json
import time

from .clients import iso_time


class UnresolvedOrderError(RuntimeError):
    """An unknown submission requires recovery before another trading cycle."""


def prepare_recovery(engine):
    """Wait for unknown requests to expire, honoring STOP and a monotonic bound.

    The extra 90 seconds covers SIG's documented in-flight execution lease and
    the expired-request history-verification path. Always refresh HTTP timing
    afterward; a pre-wait sample must never authorize a recovery POST.
    """
    if engine.stopped():
        return False
    pending = engine.journal.pending()
    unknown = [row for row in pending if row['response'] is None]
    if unknown:
        expiry = max(max(iso_time(json.loads(row['payload'])['expirationDate']), row['created'])
                     for row in unknown) + 90
        delay = max(0, expiry-time.time())
        if delay > 180:
            raise RuntimeError('Recovery expiration is unexpectedly far ahead; review the clock and journal')
        engine.report('recovery_wait', {'unknown_orders': len(unknown), 'wait_seconds': round(delay, 2),
            'action': 'No new trades; original keys and expirations remain unchanged'})
        deadline = time.monotonic()+180
        while time.time() < expiry:
            if engine.stopped():
                return False
            if time.monotonic() >= deadline:
                raise RuntimeError('Recovery wait exceeded its clock bound; reservation retained')
            time.sleep(min(.25, max(0, expiry-time.time())))
    if engine.stopped():
        return False
    if pending:
        engine.sig.account()
        engine.check_clock()
    return not engine.stopped()
