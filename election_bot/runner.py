"""Pause continuous execution on transient read failures, without replaying writes."""
import time

from .clients import APIError


def transient_read(error):
    # Method is structured provenance from HTTP, never inferred from error text.
    return (isinstance(error, APIError) and error.method == 'GET'
            and error.status in (None, 408, 429, 500, 502, 503, 504))


def wait(engine, seconds, news=None):
    return wait_until(engine.stopped, seconds, news)


def wait_until(stopped, seconds, news=None):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if stopped():
            return False
        if news and news.wake.is_set():
            news.wake.clear()
            return True
        time.sleep(min(.25, max(0, deadline-time.monotonic())))
    return not stopped()


def retry_delay(failures, error):
    return max(min(60, 5 * 2**min(failures-1, 4)), error.retry_after or 0)


def connect(factory, stopped, report):
    """Retry read-only initialization before any journal or execution is opened."""
    failures = 0
    while not stopped():
        try:
            client = factory()
        except APIError as error:
            if not transient_read(error):
                raise
            failures += 1
            delay = retry_delay(failures, error)
            report({'event': 'startup_connection_pause', 'reason': str(error),
                    'retry_in_seconds': delay, 'action': 'Waiting for SIG; trading has not started'})
            if not wait_until(stopped, delay):
                return None
        else:
            if failures:
                report({'event': 'startup_connection_restored', 'read_failures_before_recovery': failures})
            return client
    return None


def run_loop(engine, once=False, news=None):
    failures = 0
    while not engine.stopped():
        try:
            running = engine.cycle()
        except APIError as error:
            if once or not transient_read(error):
                raise
            failures += 1
            delay = retry_delay(failures, error)
            engine.report('connection_pause', {'reason': str(error), 'retry_in_seconds': delay,
                'consecutive_read_failures': failures,
                'pending_orders': len(engine.journal.pending()),
                'action': 'No new orders; next cycle reconciles pending orders first'})
            # News cannot shorten a server-requested outage/rate-limit delay.
            if not wait(engine, delay):
                return
            continue
        if failures:
            engine.report('connection_restored', {'read_failures_before_recovery': failures})
            failures = 0
        if not running or once:
            return
        config = engine.config
        pause = (config['execution']['batch_pause_seconds']
                 if config.get('execution', {}).get('enabled') else config['poll_seconds'])
        if not wait(engine, pause, news):
            return
