"""Exercise migration and cached reads on a COPY of live state; forbid all writes to SIG."""
import json
from pathlib import Path
import sqlite3
import sys
import time
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from election_bot.__main__ import key, save_json
from election_bot.active_engine import ActiveEngine
from election_bot.clients import Sig, References
from election_bot.state import Journal

class ReadOnlySig(Sig):
    def place(self, payload):
        raise RuntimeError('Validation cannot submit orders')

    def cancel(self, oid):
        raise RuntimeError('Validation cannot cancel orders')


def main():
    config = json.loads((ROOT / 'config.json').read_text())
    config['execution'] = {'enabled': True, 'sell_enabled': True, 'max_orders_per_cycle': 4,
        'metadata_cache_seconds': 900, 'batch_pause_seconds': 1,
        'exit_edge': '0.02', 'take_profit_min': '0.02'}
    source = sqlite3.connect((ROOT / '.runtime/live.sqlite3').as_uri() + '?mode=ro', uri=True)
    dest_path = ROOT / '.runtime/v2-validation.sqlite3'
    dest = sqlite3.connect(dest_path)
    source.backup(dest)
    source.close()
    binding = dest.execute('SELECT binding FROM identity').fetchone()[0]
    dest.close()
    journal = Journal(dest_path, binding)
    refs_client = References()
    try:
        sig = ReadOnlySig(key(), config['tournament_slug'])
        engine = ActiveEngine(config, sig, refs_client, journal, ROOT / '.runtime/v2-validation', live=True)
        engine.sync_history()
        engine.sync_history()  # Idempotent import of confirmed old trades.
        account, positions = sig.account(), sig.positions()
        engine.check_account(account)
        mismatches = []
        for p in positions:
            try:
                engine.held(str(p['exchangeId']), positions)
            except ValueError as error:
                mismatches.append({'exchange': p['exchangeId'], 'reason': str(error)})
        samples = [m for m in config['markets'] if m['name'] in (
            'nh-senate-democratic', 'me-senate-democratic', 'tx-senate-democratic',
            'nh-01-house-democratic', 'ri-governor-democratic', 'me-02-house-democratic')]
        reads = []
        for phase in ('cold', 'cached'):
            start = time.monotonic()
            for m in samples:
                try:
                    metadata = engine.metadata(m)
                    refs = engine.references.books(m, metadata)
                    book = sig.book(m['sig_exchange_id'])
                    engine.ledger.observe(m['sig_exchange_id'], book, config['strategy']['max_age_seconds'])
                    signal = engine.decide(m, account, positions, book, refs)
                    reads.append({'phase': phase, 'name': m['name'], 'signal': vars(signal) if signal else None})
                except ValueError as error:
                    reads.append({'phase': phase, 'name': m['name'], 'skip': str(error)})
            reads.append({'phase': phase, 'elapsed_seconds': round(time.monotonic()-start, 2)})
        result = {'read_only': True, 'journal_is_copy': True, 'summary': engine.ledger.summary(),
                  'inventory_mismatches': mismatches, 'checks': reads}
        result = json.loads(json.dumps(result, default=str))
        save_json(ROOT / '.runtime/v2-validation.json', result)
        print(json.dumps(result, indent=2))
    finally:
        try:
            refs_client.close()
        finally:
            journal.close()


if __name__ == '__main__':
    main()
