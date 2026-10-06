"""Read-only audit of proposed race mappings; never enables markets or sends orders."""
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from election_bot.regions import region_for_state
from election_bot.__main__ import key
from election_bot.clients import References, Sig, contract_record, fingerprint
from election_bot.strategy import choose


def main():
    config = json.loads((ROOT / 'config.json').read_text())
    sig = Sig(key(), config['tournament_slug'])
    if sig.tid != config['tournament_id']:
        raise ValueError('Wrong tournament')
    markets = sig.markets('Senate')
    refs = References()
    reviews = []
    for state, code in [('Maine', 'ME'), ('Texas', 'TX'), ('Michigan', 'MI'), ('Iowa', 'IA')]:
        title = 'Will the Democratic Party win the ' + state + ' Senate?'
        matches = [m for m in markets if m['title'] == title and m['status'] == 'open']
        if len(matches) != 1 or len(matches[0]['exchanges']) != 1:
            raise ValueError('Missing or ambiguous SIG race: ' + state)
        event_slug = state.lower() + '-senate-election-winner'
        event = refs.gamma.request('/events/slug/' + event_slug)
        poly = [m for m in event['markets'] if m['question'] ==
                'Will the Democrats win the ' + state + ' Senate race in 2026?']
        kalshi = refs.kalshi.request('/markets', params={'event_ticker': 'SENATE'+code+'-26', 'limit': 100})
        km = [m for m in kalshi['markets'] if m['ticker'] == 'SENATE'+code+'-26-D']
        if len(poly) != 1 or len(km) != 1:
            raise ValueError('Missing or ambiguous external party contract: ' + state)
        mapping = {'name': code.lower()+'-senate-democratic', 'enabled': False,
                   'office': 'senate', 'exposure_sign': 1,
                   'state_code': code, 'region': region_for_state(code),
                   'news_match': {'all': [[state], ['Senate', 'Senator']], 'exclude': ['state senate']},
                   'sig_market_id': str(matches[0]['id']),
                   'sig_exchange_id': str(matches[0]['exchanges'][0]['id']),
                   'kalshi_ticker': km[0]['ticker'], 'polymarket_event': event_slug,
                   'polymarket_market': poly[0]['slug'],
                   'kalshi_yes_matches_sig_yes': True, 'polymarket_yes_matches_sig_yes': True}
        metadata = refs.metadata(mapping)
        record = contract_record(sig.market(mapping['sig_market_id']), mapping, metadata)
        mapping['contract_fingerprint'] = fingerprint(record)
        books = [sig.book(mapping['sig_exchange_id'])] + refs.books(mapping, metadata)
        # Refresh the target book after external data, just as the trading loop does.
        books[0] = sig.book(mapping['sig_exchange_id'])
        signal, reason = None, None
        try:
            signal = choose(books[0], books[1:], config['strategy'], config['limits']['per_order'])
        except ValueError as error:
            reason = str(error)
        review = {'state': state, 'reviewed_at': time.time(), 'mapping': mapping, 'contract': record,
                  'external_active': {'kalshi': metadata[0]['status'], 'polymarket': metadata[1].get('acceptingOrders')},
                  'books': [{'venue': name, 'bid': b.bids[0] if b.bids else None,
                             'ask': b.asks[0] if b.asks else None, 'source_age_seconds': time.time()-b.source_at}
                            for name, b in zip(('SIG', 'Kalshi', 'Polymarket'), books)],
                  'paper_signal': vars(signal) if signal else None, 'skip_reason': reason}
        reviews.append(review)
        # Partial results are reviewable if a later venue request fails.
        output = ROOT / '.runtime' / 'additional-races-review.json'
        output.write_text(json.dumps(reviews, indent=2, default=str)+'\n')
        print(json.dumps(review, default=str), flush=True)


if __name__ == '__main__':
    main()
