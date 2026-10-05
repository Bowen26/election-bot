"""Read-only SIG execution schema check. Never submits or cancels an order."""
import json
from pathlib import Path
import sqlite3
import sys
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from election_bot.__main__ import key
from election_bot.clients import Sig

config = json.loads((ROOT / 'config.json').read_text())
sig = Sig(key(), config['tournament_slug'])
assert sig.tid == config['tournament_id']
print(json.dumps({'positions': sig.positions()}, default=str))
db = sqlite3.connect((ROOT / '.runtime/live.sqlite3').as_uri() + '?mode=ro', uri=True)
for (raw,) in db.execute("SELECT response FROM orders WHERE response IS NOT NULL ORDER BY created DESC LIMIT 2"):
    response = json.loads(raw)
    oid = response.get('orderId')
    if oid:
        order, fills = sig.order(oid), sig.fills(oid)
        print(json.dumps({'order': order, 'fills': fills, 'ack': response}))
db.close()
