"""Read-only quote smoke check for the audited catalog; never submits orders."""
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from election_bot.__main__ import key, save_json
from election_bot.catalog import verify_candidate
from election_bot.clients import APIError, References, Sig
from election_bot.strategy import choose


def main():
    config = json.loads((ROOT / 'config.json').read_text())
    audit = json.loads((ROOT / '.runtime/catalog-review.json').read_text())
    sig = Sig(key(), config['tournament_slug'])
    if sig.tid != config['tournament_id']:
        raise ValueError('Tournament mismatch')
    results = []
    with References() as refs:
        started = time.time()
        for review in audit['reviews']:
            if review['status'] != 'candidate':
                continue
            try:
                verify_candidate(review)
            except ValueError:
                continue
            mapping = review['mapping']
            result = {'name': mapping['name'], 'at': time.time(), 'status': 'unavailable'}
            try:
                metadata = refs.metadata(mapping)
                books = refs.books(mapping, metadata)
                target = sig.book(mapping['sig_exchange_id'])
                result['status'] = 'feeds_ok'
                try:
                    signal = choose(target, books, config['strategy'], '50')
                    result.update(status='signal' if signal else 'no_gap',
                                  signal=json.loads(json.dumps(vars(signal), default=str)) if signal else None)
                except ValueError as error:
                    result['skip_reason'] = str(error)
            except (APIError, ValueError, KeyError) as error:
                result['skip_reason'] = str(error)
            results.append(result)
            save_json(ROOT / '.runtime/catalog-books.json',
                      {'started': started, 'elapsed_seconds': time.time()-started, 'results': results})
            print(json.dumps(result, default=str), flush=True)


if __name__ == '__main__':
    main()
