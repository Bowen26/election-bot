"""Validate local time against fresh SIG HTTP timing without changing any clock."""
import math
import time

MAX_SKEW_SECONDS = 5.0
MAX_SAMPLE_AGE_SECONDS = 15.0
MAX_REQUEST_SECONDS = 4.0
MAX_CLOCK_JUMP_SECONDS = 1.0


class ClockCheckError(RuntimeError):
    pass


def check_timing(sample, now=None, monotonic_now=None):
    now = time.time() if now is None else now
    monotonic_now = time.monotonic() if monotonic_now is None else monotonic_now
    fields = ('request_started_at', 'received_at', 'http_date_at', 'cache_age_seconds',
              'request_started_monotonic', 'received_monotonic')
    if not isinstance(sample, dict) or any(type(sample.get(k)) not in (int, float)
            or not math.isfinite(sample[k]) for k in fields):
        raise ClockCheckError('SIG clock check unverified: missing or invalid response timing/Date')
    if sample['cache_age_seconds'] != 0:
        raise ClockCheckError('SIG clock check unverified: cached response (Age must be zero)')
    elapsed = sample['received_monotonic'] - sample['request_started_monotonic']
    wall_elapsed = sample['received_at'] - sample['request_started_at']
    age = monotonic_now - sample['received_monotonic']
    wall_age = now - sample['received_at']
    if (not 0 <= elapsed <= MAX_REQUEST_SECONDS or not 0 <= age <= MAX_SAMPLE_AGE_SECONDS):
        raise ClockCheckError('SIG clock check unverified: response too slow, expired or invalid')
    if (abs(wall_elapsed-elapsed) > MAX_CLOCK_JUMP_SECONDS or
            abs(wall_age-age) > MAX_CLOCK_JUMP_SECONDS):
        raise ClockCheckError('Local clock changed during or after the SIG request; new orders halted')
    # HTTP Date has whole-second precision. Do not assume symmetric network delay.
    lower = sample['http_date_at'] - sample['received_at']
    upper = sample['http_date_at'] + 1 - sample['request_started_at']
    if lower > MAX_SKEW_SECONDS or upper < -MAX_SKEW_SECONDS:
        raise ClockCheckError('Local clock differs from SIG by more than 5 seconds; check automatic date/time')
    if lower < -MAX_SKEW_SECONDS or upper > MAX_SKEW_SECONDS:
        raise ClockCheckError('SIG clock check inconclusive near the 5-second bound; new orders halted')
    return {'status': 'verified', 'server_minus_local_seconds': [lower, upper],
            'max_skew_seconds': MAX_SKEW_SECONDS, 'sample_age_seconds': age,
            'request_seconds': elapsed, 'method': 'fresh_SIG_Date_interval',
            'note': 'HTTP timing consistency check, not an independent trusted-time service.'}
