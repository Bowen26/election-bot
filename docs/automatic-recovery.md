# Automatic recovery in the terminal

The `watch` command supervises one normal bot process. It is a restart mechanism, not a reset: positions, orders, FIFO lots, spending limits, trend samples, credentials and STOP stay intact.

Stop the existing bot with Ctrl+C and wait for the prompt before running:

```sh
python3 -m election_bot watch --live
```

Use `watch` without `--live` for paper trading. An explicit configuration works with `python3 -m election_bot --config /absolute/path/config.json watch --live`. The child uses the same Python executable and an argument list without a shell; paths containing spaces are supported. Output continues in the terminal. The parent does not read credentials or call a trading API.

## What recovers

Temporary GET outages normally retry inside the worker using existing backoff and Retry-After handling. Slow/expired or sleep/wake-discontinuous SIG clock evidence also pauses for fresh data inside the worker; the clock thresholds are not relaxed. The watcher also restarts an escaped transient GET failure without a Retry-After constraint, an otherwise unclassified Python crash, or a worker killed by SIGKILL/SIGABRT/SIGSEGV. Delays are 5, 10 and 20 seconds. A maximum of three restart/recovery attempts is allowed in a rolling 15-minute window. If another crash occurs before that budget clears, the watcher writes STOP and exits for review.

Each new child loads configuration, acquires the normal exclusive bot lock, opens the existing journal and reconciles pending orders before trading.

### Recovery before restarting after a write timeout

In live mode, SIG POST/DELETE network failures and HTTP 408/500/502/503/504 responses without a Retry-After constraint return a separate recovery exit code. Finding an unknown submission at worker startup also enters recovery. The watcher launches a separate `recover --wait` child using the same interpreter, configuration and runtime lock. It does not launch another trading worker until recovery exits successfully. Paper mode never launches a live recovery command.

Unknown requests wait until both their original creation and expiration are at least 90 seconds old, covering SIG's documented in-flight lease and the expired-request fallback. The wait honors STOP and Ctrl+C, rejects timestamps more than 180 seconds ahead of the allowed recovery point, and has a 180-second monotonic ceiling so a clock adjustment cannot trap it indefinitely. After waiting, recovery reads fresh SIG account data and rechecks the clock before replaying the exact original payload and idempotency key. It never extends an expiration or makes a replacement order. Known orders are reconciled without that expiration wait.

A completed idempotent replay is verified against the order and fill endpoints. If SIG rejects an expired replay, the existing strict history-and-inventory fallback must verify it was unplaced before its reservation is released. Unrecognized orders or accounting differences halt recovery. A recovery network failure retries recovery, not trading; all such attempts share the existing three-attempt rolling budget. `recovering`, `recovered`, and `mode` in supervisor status expose this transition. Recovery completion is followed by normal startup reconciliation and all normal trading checks.

## What stays stopped

- Ctrl+C, SIGTERM, SIGHUP, a normal exit, or an existing STOP signal.
- Known operational errors: inconsistent/unverifiable orders after recovery, inventory mismatch, malformed configuration/data, database errors, file permission failures, hard clock-check failures and risk/accounting halts.
- Non-retryable API errors and escaped rate-limit/Retry-After errors. Normal in-process rate-limit handling remains unchanged; the watcher does not shorten a server-requested wait.

These exits use a non-restarting status. Review the visible error before another manual launch. The watcher does not bypass a safety check, change time, clear STOP, repair/delete a database, or run `resume` automatically. It invokes `recover --wait` only for the recovery path described above. If the crash limit set STOP, `resume` clears it only when explicitly run; it does not launch the bot.

A separate supervisor lock prevents duplicate watchers. An existing standalone bot causes the watcher to refuse startup; the existing process is not interrupted. A competing launch is also rejected by the child's normal runtime lock.

## Shutdown and visibility

Ctrl+C stops the watcher and sends one SIGINT to its child, giving normal reconciliation up to 30 seconds. If cleanup hangs, it escalates to termination, then kill after another five seconds. It never starts a replacement during shutdown; reservations remain for subsequent reconciliation. Terminating the parent forcibly with SIGKILL prevents its cleanup handler from running and may leave the child alive; the normal worker lock still prevents a second trading process.

The latest supervisor state is atomically stored in `.runtime/supervisor/status.json`, mode 0600. `python3 -m election_bot status` displays that last record alongside journal status. A stale record is not evidence that either process is still alive. The watcher catches worker crashes, not failure of the watcher itself or shutdown of the operating system.

Keep the terminal session running and the Mac awake. This implementation does not register a login agent, run after a reboot, prevent sleep, or restart after deliberately closing the terminal. No live bot is started or restarted by installing the code.
