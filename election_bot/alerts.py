"""Best-effort operator alerts for watcher status changes. Never raises.

Off unless alerts.enabled is true; then macOS notifications are on by default. An optional HTTPS push URL (for example an
ntfy.sh topic you created) receives a short plain-text line: status, reason and
exit code only, never balances, positions or credentials.
"""
import subprocess
import urllib.parse
import urllib.request

DEFAULTS = dict(enabled=False, macos=True, push_url=None,
                statuses=['halted', 'restart_wait', 'recovering', 'stopped'])


def settings(config):
    raw = config.get('alerts', {})
    if not isinstance(raw, dict) or set(raw)-set(DEFAULTS):
        raise ValueError('Invalid alerts configuration')
    p = dict(DEFAULTS, **raw)
    for key in ('enabled', 'macos'):
        if type(p[key]) is not bool:
            raise ValueError('alerts %s must be boolean' % key)
    if p['push_url'] is not None:
        url = urllib.parse.urlsplit(p['push_url']) if isinstance(p['push_url'], str) else None
        if url is None or url.scheme != 'https' or not url.hostname or url.username or url.password:
            raise ValueError('alerts push_url must be an https URL without credentials')
    if not isinstance(p['statuses'], list) or not all(isinstance(s, str) for s in p['statuses']):
        raise ValueError('alerts statuses must be a list of status names')
    return p


def message(payload):
    parts = [str(payload.get('status'))]
    if payload.get('exit_code') is not None:
        parts.append('exit %s' % payload['exit_code'])
    if payload.get('reason'):
        parts.append(str(payload['reason'])[:160])
    return 'Election bot: ' + '; '.join(parts)


def notify(policy, payload, run=subprocess.run, opener=urllib.request.urlopen):
    """Send one alert for a supervisor payload if its status is selected; returns what was attempted."""
    if not policy['enabled'] or payload.get('status') not in policy['statuses']:
        return []
    text, sent = message(payload), []
    if policy['macos']:
        script = 'display notification %s with title "Election bot" sound name "Glass"' % _quote(text)
        try:
            run(['osascript', '-e', script], timeout=5, check=False, capture_output=True)
            sent.append('macos')
        except (OSError, subprocess.SubprocessError):
            pass
    if policy['push_url']:
        try:
            request = urllib.request.Request(policy['push_url'], data=text.encode(), method='POST',
                                             headers={'Content-Type': 'text/plain', 'Title': 'Election bot'})
            with opener(request, timeout=5):
                pass
            sent.append('push')
        except Exception:
            pass  # Alerts must never interfere with supervision.
    return sent


def _quote(text):
    return '"' + text.replace('\\', '\\\\').replace('"', '\\"') + '"'
