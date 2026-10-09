"""Validate local time against fresh SIG HTTP timing without changing any clock."""
import math
import time

MAX_SKEW_SECONDS = 5.0
MAX_SAMPLE_AGE_SECONDS = 15.0
MAX_REQUEST_SECONDS = 4.0
MAX_CLOCK_JUMP_SECONDS = 1.0


class ClockCheckError(RuntimeError):
    pass


class ClockSampleUnavailable(ClockCheckError):
    """Missing, cached, slow, expired or discontinuous timing; refresh required."""
    def __init__(self, message, detail=None):
        super().__init__(message)
        self.detail = detail or {}


class SubmissionClockUnavailable(ClockSampleUnavailable):
    """Raised by Sig.place only when its clock gate prevented any POST attempt."""
    pass


def check_timing(sample, now=None, monotonic_now=None):
    now = time.time() if now is None else now
    monotonic_now = time.monotonic() if monotonic_now is None else monotonic_now
    fields = ('request_started_at', 'received_at', 'http_date_at', 'cache_age_seconds',
              'request_started_monotonic', 'received_monotonic')
    if sample is None or isinstance(sample, dict) and any(k not in sample for k in fields):
        raise ClockSampleUnavailable('SIG clock check unverified: missing timing sample; waiting for fresh evidence')
    if not isinstance(sample, dict) or any(type(sample[k]) not in (int, float)
            or not math.isfinite(sample[k]) for k in fields if k != 'http_date_at'):
        raise ClockCheckError('SIG clock check unverified: invalid response timing')
    if sample['http_date_at'] is not None and (type(sample['http_date_at']) not in (int,float)
            or not math.isfinite(sample['http_date_at'])):
        raise ClockCheckError('SIG clock check unverified: invalid Date')
    if sample['cache_age_seconds'] < 0:
        raise ClockCheckError('SIG clock check unverified: invalid cache Age')
    elapsed = sample['received_monotonic'] - sample['request_started_monotonic']
    wall_elapsed = sample['received_at'] - sample['request_started_at']
    age = monotonic_now - sample['received_monotonic']
    wall_age = now - sample['received_at']
    if elapsed < 0 or age < 0:
        raise ClockCheckError('SIG clock check unverified: invalid monotonic response timing')
    if (abs(wall_elapsed-elapsed) > MAX_CLOCK_JUMP_SECONDS or
            abs(wall_age-age) > MAX_CLOCK_JUMP_SECONDS):
        # This sample spans a clock adjustment or suspend/resume discontinuity.
        # It cannot establish current skew. Reject it and require a fresh GET;
        # never reset its timestamps or reuse its apparent server offset.
        raise ClockSampleUnavailable(
            'Local clock changed during or after the SIG request; waiting for fresh SIG timing',
            {'reason_code': 'clock_discontinuity', 'request_seconds': elapsed,
             'sample_age_seconds': age, 'wall_request_seconds': wall_elapsed,
             'wall_sample_age_seconds': wall_age,
             'request_clock_difference_seconds': wall_elapsed-elapsed,
             'sample_clock_difference_seconds': wall_age-age})
    if sample['http_date_at'] is None:
        raise ClockSampleUnavailable('SIG clock check unverified: missing Date; waiting for fresh evidence',
            {'request_seconds': elapsed, 'sample_age_seconds': age})
    if sample['cache_age_seconds'] > 0:
        raise ClockSampleUnavailable('SIG clock check unverified: cached response; waiting for uncached evidence',
            {'request_seconds': elapsed, 'sample_age_seconds': age, 'cache_age_seconds': sample['cache_age_seconds']})
    # HTTP Date has whole-second precision. Do not assume symmetric network delay.
    lower = sample['http_date_at'] - sample['received_at']
    upper = sample['http_date_at'] + 1 - sample['request_started_at']
    if lower > MAX_SKEW_SECONDS or upper < -MAX_SKEW_SECONDS:
        raise ClockCheckError('Local clock differs from SIG by more than 5 seconds; check automatic date/time')
    if elapsed > MAX_REQUEST_SECONDS or age > MAX_SAMPLE_AGE_SECONDS:
        reason = 'response too slow' if elapsed > MAX_REQUEST_SECONDS else 'timing sample expired'
        raise ClockSampleUnavailable('SIG clock check unverified: ' + reason + '; waiting for fresh evidence',
            {'request_seconds': elapsed, 'sample_age_seconds': age})
    if lower < -MAX_SKEW_SECONDS or upper > MAX_SKEW_SECONDS:
        raise ClockCheckError('SIG clock check inconclusive near the 5-second bound; new orders halted')
    return {'status': 'verified', 'server_minus_local_seconds': [lower, upper],
            'max_skew_seconds': MAX_SKEW_SECONDS, 'sample_age_seconds': age,
            'request_seconds': elapsed, 'method': 'fresh_SIG_Date_interval',
            'note': 'HTTP timing consistency check, not an independent trusted-time service.'}
