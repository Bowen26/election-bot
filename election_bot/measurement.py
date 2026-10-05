"""Forward price observation policy, shared by collection and read-only reports."""

# Horizon -> maximum delay after the target time. These are sampled windows,
# not claims that an exact historical quote was available at the target second.
WINDOWS = {60: 60, 300: 120, 900: 180, 3600: 900, 86400: 21600}
SHORT_HORIZONS = (60, 300, 900)
MAX_OBSERVATION_BOOKS = 4


def tracking_start(db):
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if 'performance_settings' not in tables:
        return None
    row = db.execute("SELECT value FROM performance_settings WHERE name='short_horizons_started_at'").fetchone()
    return float(row[0]) if row else None


def eligible(at, horizon, started):
    return horizon not in SHORT_HORIZONS or (started is not None and at >= started)
