# Clock checks and reference workers

## Clock verification

`python3 -m election_bot clock-check` makes a SIG tournament GET using the locally stored key and reports the clock check. It places/cancels no orders and opens no trading journal. It can be used while the trading process is stopped to inspect timing before restart. A timeout is a connectivity failure, not a measured clock offset.

Both execution engines verify SIG timing after the account read at startup and again before orders. ActiveEngine repeats the check during final account/risk preflight and inside the local reservation guard. `Sig.place` checks once more before submitting, including an explicit recovery replay. Initial success logs `clock_check`; failed execution checks log `clock_halt` and halt new orders. Reads and cancellation remain available for reconciliation. The guard does not set or clear STOP, modify the Mac's clock, rewrite source timestamps or relax quote freshness rules.

The check uses SIG's HTTPS response Date, request/receipt wall times and matching monotonic timestamps. With whole-second Date `D`, local send time `S`, and receipt time `R`, the possible server-minus-local offset is `[D-R, D+1-S]`. The whole interval must lie within **±5 seconds**. An interval entirely outside that bound reports definite disagreement; one overlapping the boundary is inconclusive rather than healthy. This allows for header precision and network delay without assuming equal outbound/inbound latency.

Evidence must have zero cache Age, a roundtrip of at most four seconds, and a monotonic age of at most fifteen seconds. Missing/invalid Date or timing, cached responses, slow/expired samples and wall-clock movements differing from monotonic elapsed time by more than one second fail verification. These thresholds are fixed implementation constants, not trading-budget settings. A slow request or an inconclusive interval does not establish that the Mac's clock is wrong. Obtain a fresh check after connectivity improves; for a definite mismatch, check the Mac's automatic date/time settings and retry.

HTTP Date is an upstream server/proxy assertion, not independent NTP authentication. Clock verification supplements the existing source-age, cross-venue and tournament-close checks. It cannot make remote order submission and clock changes atomic. A failure after local reservation keeps the existing conservative reconciliation behavior; reservations are not silently discarded.

## Persistent public-feed workers

Each `References` object now lazily creates one two-worker `ThreadPoolExecutor`. Kalshi and Polymarket book GETs still run concurrently, but subsequent scans reuse those workers. Metadata-only uses create no threads. This reduces thread creation overhead; it does not increase concurrency, reduce SIG pacing or claim a measured latency/edge improvement.

A per-client lock serializes metadata/books calls and shutdown, protecting mutable HTTP response timing. If either venue fails or the caller is interrupted, both submitted futures are drained before another call can reuse client state. The existing HTTP timeout remains in force. The failed request is not converted into an empty book or a stale successful result.

The client supports `with References() as refs:` and an idempotent `close()`. The CLI and audit scripts close workers on normal return, error or interruption; trading shutdown reconciles known orders and closes news/journal resources before disposing of the reference client. Calls after close fail explicitly. Third-party scripts using the class should use its context manager or call `close()` in `finally`.

Per-response receipt times and original source timestamps remain separate. Reusing a worker does not reuse a book or refresh its timestamp. All external venues remain read-only, and SIG writes remain serial.
