# Automatic recovery in the terminal

The `watch` command supervises one normal bot process. It is a restart mechanism, not a reset: positions, orders, FIFO lots, spending limits, trend samples, credentials and STOP stay intact.

Stop the existing bot with Ctrl+C and wait for the prompt before running:

```sh
python3 -m election_bot watch --live
```

Use `watch` without `--live` for paper trading. An explicit configuration works with `python3 -m election_bot --config /absolute/path/config.json watch --live`. The child uses the same Python executable and an argument list without a shell; paths containing spaces are supported. Output continues in the terminal. The parent does not read credentials or call a trading API.

## What recovers

Temporary GET outages normally retry inside the worker using existing backoff and Retry-After handling. Slow/expired SIG clock evidence also pauses for fresh data inside the worker; the clock thresholds are not relaxed. The watcher also restarts an escaped transient GET failure without a Retry-After constraint, an otherwise unclassified Python crash, or a worker killed by SIGKILL/SIGABRT/SIGSEGV. Delays are 5, 10 and 20 seconds. A maximum of three restarts is allowed in a rolling 15-minute window. If another crash occurs before that budget clears, the watcher writes STOP and exits for review.

Each new child loads configuration, acquires the normal exclusive bot lock, opens the existing journal and reconciles pending orders before trading. Unknown submissions are not automatically replayed; normal reconciliation halts for explicit recovery when necessary. No order is resubmitted merely because the process restarted.

## What stays stopped

- Ctrl+C, SIGTERM, SIGHUP, a normal exit, or an existing STOP signal.
- Known operational errors: unknown/inconsistent orders, inventory mismatch, malformed configuration/data, database errors, file permission failures, hard clock-check failures and risk/accounting halts.
- Non-retryable API errors, mutation failures, and escaped rate-limit/Retry-After errors. Normal in-process rate-limit handling remains unchanged; the watcher does not shorten a server-requested wait.

These exits use a non-restarting status. Review the visible error before another manual launch. The watcher does not bypass a safety check, change time, clear STOP, repair/delete a database, or run `recover` or `resume` automatically. If the crash limit set STOP, `resume` clears it only when explicitly run; it does not launch the bot.

A separate supervisor lock prevents duplicate watchers. An existing standalone bot causes the watcher to refuse startup; the existing process is not interrupted. A competing launch is also rejected by the child's normal runtime lock.

## Shutdown and visibility

Ctrl+C stops the watcher and sends one SIGINT to its child, giving normal reconciliation up to 30 seconds. If cleanup hangs, it escalates to termination, then kill after another five seconds. It never starts a replacement during shutdown; reservations remain for subsequent reconciliation. Terminating the parent forcibly with SIGKILL prevents its cleanup handler from running and may leave the child alive; the normal worker lock still prevents a second trading process.

The latest supervisor state is atomically stored in `.runtime/supervisor/status.json`, mode 0600. `python3 -m election_bot status` displays that last record alongside journal status. A stale record is not evidence that either process is still alive. The watcher catches worker crashes, not failure of the watcher itself or shutdown of the operating system.

Keep the terminal session running and the Mac awake. This implementation does not register a login agent, run after a reboot, prevent sleep, or restart after deliberately closing the terminal. No live bot is started or restarted by installing the code.
