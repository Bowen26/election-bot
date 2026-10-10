"""Bounded terminal supervision with verified recovery before resuming trades."""
from collections import deque
from contextlib import contextmanager
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import time

from .clients import APIError
from .clock_guard import ClockSampleUnavailable
from .runner import transient_read
from .state import exclusive_lock
from .recovery_wait import UnresolvedOrderError

HALT = 78
RETRY = 75
RECOVER = 76
WINDOW_SECONDS = 900
MAX_RESTARTS = 3


def failure_code(error):
    """Separate uncertain mutations from read retries and hard safety halts."""
    if isinstance(error, UnresolvedOrderError):
        return RECOVER
    if isinstance(error, ClockSampleUnavailable):
        return RETRY
    if isinstance(error, APIError):
        if (error.venue == 'SIG' and error.method in ('POST', 'DELETE')
                and error.status in (None, 408, 500, 502, 503, 504) and not error.retry_after):
            return RECOVER
        # In-process retries honor Retry-After. An escaped rate-limit error
        # cannot carry that delay through an exit code, so do not bypass it.
        return RETRY if transient_read(error) and error.status != 429 and not error.retry_after else HALT
    if isinstance(error, (RuntimeError, ValueError, KeyError, TypeError,
                          sqlite3.Error, OSError, ArithmeticError, AssertionError)):
        return HALT
    return 1


def restartable(code):
    return code in (1, RETRY, -signal.SIGKILL, -signal.SIGABRT, -signal.SIGSEGV)


@contextmanager
def shutdown_signals():
    previous = {}
    def interrupt(signum, frame):
        raise KeyboardInterrupt
    for name in ('SIGTERM', 'SIGHUP'):
        sig = getattr(signal, name, None)
        if sig is not None:
            previous[sig] = signal.signal(sig, interrupt)
    try:
        yield
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def stop_child(child):
    if child is None or child.poll() is not None:
        return
    # A private process session means only the parent receives terminal Ctrl+C.
    # Send it once to the child so normal journal reconciliation can run.
    try:
        child.send_signal(signal.SIGINT)
    except ProcessLookupError:
        child.wait()
        return
    try:
        child.wait(timeout=30)
    except (subprocess.TimeoutExpired, KeyboardInterrupt):
        child.terminate()
        try:
            child.wait(timeout=5)
        except (subprocess.TimeoutExpired, KeyboardInterrupt):
            child.kill()
            child.wait()


def watch(root, runtime, config, live=False, report=print):
    root, runtime = Path(root).resolve(), Path(runtime).resolve()
    directory = runtime / 'supervisor'
    command = [sys.executable, '-u', '-m', 'election_bot', '--config', str(Path(config).resolve()), 'run']
    if live:
        command.append('--live')
    recovery_command = [sys.executable, '-u', '-m', 'election_bot', '--config',
                        str(Path(config).resolve()), 'recover', '--wait']
    mode = 'run'
    stop = runtime / 'STOP'
    child = None
    restarts = deque()
    total_restarts = 0

    def state(status, **extra):
        payload = {'event': 'supervisor', 'status': status, 'at': time.time(),
                   'supervisor_pid': os.getpid(), 'child_pid': child.pid if child else None,
                   'live': live, 'restarts': total_restarts, 'mode': mode, **extra}
        temporary = directory / 'status.json.tmp'
        with temporary.open('w') as handle:
            os.chmod(temporary, 0o600)
            json.dump(payload, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(directory / 'status.json')
        report(json.dumps(payload))

    with exclusive_lock(directory), shutdown_signals():
        try:
            if stop.exists():
                state('stopped', reason='STOP is set; use resume only after reviewing the halt')
                return 0
            # Refuse to run alongside an already running standalone bot.
            with exclusive_lock(runtime):
                pass
            while not stop.exists():
                started = time.monotonic()
                child = subprocess.Popen(recovery_command if mode == 'recover' else command,
                                         cwd=str(root), start_new_session=True)
                state('recovering' if mode == 'recover' else 'running')
                while child.poll() is None and not stop.exists():
                    time.sleep(.25)
                if stop.exists():
                    stop_child(child)
                    state('stopped', reason='STOP is set')
                    return 0
                code = child.returncode
                if mode == 'recover' and code == 0:
                    state('recovered', reason='Reconciliation succeeded; restarting normal trading')
                    mode = 'run'
                    continue
                needs_recovery = live and code == RECOVER
                if not restartable(code) and not needs_recovery:
                    intentional = code in (0,130,-signal.SIGINT,-signal.SIGTERM,-signal.SIGHUP)
                    state('stopped' if intentional else 'halted', exit_code=code,
                          reason='Worker exited; no automatic restart for this exit code')
                    return (code if code and code > 0 else 0) if intentional else (code if code > 0 else HALT)
                now = time.monotonic()
                while restarts and now-restarts[0] >= WINDOW_SECONDS:
                    restarts.popleft()
                if len(restarts) >= MAX_RESTARTS:
                    # Persistent STOP prevents another automatic launch from looping.
                    stop.touch(mode=0o600, exist_ok=True)
                    state('halted', exit_code=code, reason='Restart limit reached; STOP set for review')
                    return HALT
                delay = min(60,5*2**len(restarts))
                if needs_recovery:
                    mode = 'recover'
                state('restart_wait', exit_code=code, retry_in_seconds=delay,
                      worker_uptime_seconds=round(now-started,1))
                deadline = time.monotonic()+delay
                while time.monotonic() < deadline:
                    if stop.exists():
                        state('stopped', reason='STOP set during restart delay')
                        return 0
                    time.sleep(min(.25,max(0,deadline-time.monotonic())))
                if stop.exists():
                    break
                restarts.append(time.monotonic())
                total_restarts += 1
            state('stopped', reason='STOP is set')
            return 0
        except KeyboardInterrupt:
            stop_child(child)
            state('stopped', reason='User interrupted supervisor')
            return 130
        finally:
            stop_child(child)
