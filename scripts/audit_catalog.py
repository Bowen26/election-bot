"""Read-only full competition audit. Writes evidence, never changes config/orders."""
import json
from pathlib import Path
import re
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from election_bot.regions import region_for_state
from election_bot.__main__ import key, save_json
from election_bot.clients import References, Sig, contract_record, fingerprint, APIError
from election_bot.catalog import STATES


def main():
    config = json.loads((ROOT / 'config.json').read_text())
    sig = Sig(key(), config['tournament_slug'])
    if sig.tid != config['tournament_id']:
        raise ValueError('Tournament mismatch')
    with References() as refs:
        markets = sig.markets()
        resume = '--retry-unmatched' in sys.argv
        prior = {}
        if resume:
            saved = json.loads((ROOT / '.runtime/catalog-review.json').read_text())
            prior = {r['sig_market_id']: r for r in saved['reviews'] if r['status'] == 'candidate'}
            events = json.loads((ROOT / '.runtime/poly-catalog.json').read_text())
        else:
            events = []
            for offset in range(0, 3000, 20):
                page = refs.gamma.request('/events', params={'tag_id': 102289, 'active': 'true',
                    'closed': 'false', 'limit': 20, 'offset': offset, 'order': 'id', 'ascending': 'true'})
                events.extend(page)
                if len(page) < 20:
                    break
            else:
                raise ValueError('Incomplete Polymarket pagination')
            save_json(ROOT / '.runtime/poly-catalog.json', events)
        reviews = []
        for market in markets:
            title = market['title']
            if not title.startswith('Will the Democratic Party win the '):
                continue
            if str(market['id']) in prior:
                reviews.append(prior[str(market['id'])])
                continue
            race = title.removeprefix('Will the Democratic Party win the ').removesuffix('?')
            district = re.fullmatch(r'([A-Z]{2})-(\d{2}) House race', race)
            state_race = re.fullmatch(r'(.+) (Senate|Governor)', race)
            if district:
                code, number = district.groups()
                state = next((s for s, c in STATES.items() if c == code), None)
                office = 'House'
                label = code + '-' + number
                ticker = 'HOUSE' + code + str(int(number)) + '-26-D'
                event_title = label + ' House Election Winner'
            elif state_race and state_race[1] in STATES:
                state, office = state_race.groups()
                code = STATES[state]
                label = state
                ticker = ('SENATE' if office == 'Senate' else 'GOVPARTY') + code + '-26-D'
                event_title = state + ' ' + office + ' Election Winner'
            else:
                reviews.append({'sig_market_id': market['id'], 'title': title,
                                'status': 'unsupported', 'reason': 'Requires separate settlement review'})
                continue
            review = {'sig_market_id': market['id'], 'title': title, 'status': 'unmatched'}
            try:
                matches = [e for e in events if e['title'].strip() == event_title]
                if len(matches) != 1:
                    raise ValueError('Polymarket event absent or ambiguous: ' + event_title)
                event = matches[0]
                question = 'Will the Democrats win the ' + label + ' ' + ('governor' if office == 'Governor' else office) + ' race in 2026?'
                if district:
                    question = 'Will the Democratic Party win the ' + label + ' House seat?'
                pm = [p for p in event['markets'] if p['question'] == question]
                if len(pm) != 1:
                    raise ValueError('Polymarket party question absent or ambiguous: ' + question)
                news = {'all': [[state], [office, 'Senator' if office == 'Senate' else office]],
                        'exclude': ['state senate'] if office == 'Senate' else []}
                if district:
                    news = {'all': [[label, label.replace('-0', '-'),
                                     state + "'s " + str(int(number)) + ' congressional district']], 'exclude': []}
                mapping = {'name': (code + '-' + (number + '-' if district else '') + office + '-democratic').lower(),
                    'race_key': '2026:' + office.lower() + ':' + (label if district else code),
                    'office': office.lower(), 'exposure_sign': 1,
                    'state_code': code, 'region': region_for_state(code),
                    'enabled': False, 'news_match': news, 'sig_market_id': str(market['id']),
                    'sig_exchange_id': str(market['exchanges'][0]['id']),
                    'kalshi_ticker': ticker, 'polymarket_event': event['slug'], 'polymarket_market': pm[0]['slug'],
                    'kalshi_yes_matches_sig_yes': True, 'polymarket_yes_matches_sig_yes': True}
                try:
                    metadata = refs.metadata(mapping)
                except APIError as error:
                    if error.status != 404 or not district:
                        raise
                    mapping['kalshi_ticker'] = 'KXHOUSERACE-' + code + number + '-26-D'
                    metadata = refs.metadata(mapping)
                sm = sig.market(mapping['sig_market_id'])
                record = contract_record(sm, mapping, metadata)
                mapping['contract_fingerprint'] = fingerprint(record)
                review.update(status='candidate', mapping=mapping, contract=record,
                              kalshi_status=metadata[0]['status'], poly_active=metadata[1].get('acceptingOrders'))
            except (ValueError, KeyError, APIError) as error:
                review['reason'] = str(error)
            reviews.append(review)
            save_json(ROOT / '.runtime/catalog-review.json', {'at': time.time(), 'markets': markets,
                      'polymarket_event_count': len(events), 'reviews': reviews})
            print(json.dumps({k: review[k] for k in ('title', 'status', 'reason') if k in review}), flush=True)
        save_json(ROOT / '.runtime/catalog-review.json', {'at': time.time(), 'markets': markets,
                  'polymarket_event_count': len(events), 'reviews': reviews})


if __name__ == '__main__':
    main()
