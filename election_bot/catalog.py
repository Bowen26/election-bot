"""Strict identity checks for the reviewed 2026 party-contract catalog."""
from .news import STATE_NAMES

STATES = dict(zip(STATE_NAMES, ('AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA '
    'ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT '
    'VA WA WV WI WY').split()))


def verify_candidate(review, party="Democratic Party"):
    if party not in ("Democratic Party", "Republican Party"):
        raise ValueError("Independent/candidate contracts require individual attribution review")
    adjective = party.removesuffix(" Party")
    plural = "Democrats" if adjective == "Democratic" else "Republicans"
    suffix = "D" if adjective == "Democratic" else "R"
    mapping, record = review['mapping'], review['contract']
    year, office, place = mapping['race_key'].split(':')
    code = place.split('-')[0]
    state = next(name for name, value in STATES.items() if value == code)
    if year != '2026' or office not in ('senate', 'governor', 'house'):
        raise ValueError('Unsupported election year or office')
    root = record['sig']['resolution_tree']
    details = root['contract_details']
    if office == 'house':
        district = int(place.split('-')[1])
        race_name = 'U.S. House ' + state + ' District ' + str(district)
        sig_race = place + ' House race'
        ticker = 'HOUSE' + code + str(district) + '-26-' + suffix
        question = 'Will the ' + party + ' win the ' + place + ' House seat?'
        rule = ('If the House member sworn in for ' + place + ' for the term beginning in 2027 '
                'is a member of the ' + adjective + ' party, then the market resolves to Yes.')
        description_start = ('This market will resolve according to the party of the candidate who wins the '
            + place + ' congressional district seat in the U.S. House of Representatives in the 2026 midterm elections.')
    elif office == 'senate':
        race_name = 'U.S. Senate ' + state
        sig_race = state + ' Senate'
        ticker = 'SENATE' + code + '-26-' + suffix
        question = 'Will the ' + plural + ' win the ' + state + ' Senate race in 2026?'
        rule = ('If a representative of the ' + adjective + ' party is sworn in as a Senator of ' + state +
                ' for the term beginning in 2027, then the market resolves to Yes.')
        description_start = 'This market will resolve according to the winner of the 2026 midterm ' + state + ' U.S. Senate election'
    else:
        race_name = 'Governor of ' + state
        sig_race = state + ' Governor'
        ticker = 'GOVPARTY' + code + '-26-' + suffix
        question = 'Will the ' + plural + ' win the ' + state + ' governor race in 2026?'
        rule = ('If a representative of the ' + adjective + ' party is inaugurated as the governor of ' + state +
                ' pursuant to the 2026 election, then the market resolves to Yes.')
        description_start = 'This market will resolve according to the winner of the 2026 ' + state + ' gubernatorial election.'
    expected = {'usState': code, 'raceName': race_name, 'raceStage': 'General',
        'winnerName': party, 'electionDate': '2026-11-03',
        'resolutionType': 'Party Winner', 'contractType': 'Election Outcome',
        'officeLevel': 'State' if office == 'governor' else 'Federal'}
    if root['node_type'] != 'contract' or any(details.get(k) != v for k, v in expected.items()):
        raise ValueError('SIG structured resolution identity mismatch')
    if record['sig']['title'] != 'Will the ' + party + ' win the ' + sig_race + '?':
        raise ValueError('SIG title mismatch')
    if (str(record['sig']['id']) != mapping['sig_market_id'] or
            record['sig']['exchange'] != mapping['sig_exchange_id'] or record['sig']['option'].upper() != 'YES'):
        raise ValueError('SIG exchange mismatch')
    tickers = {ticker}
    if office == 'house':
        tickers.add('KXHOUSERACE-' + place.replace('-', '') + '-26-' + suffix)
    if mapping['kalshi_ticker'] not in tickers or record['kalshi']['ticker'] != mapping['kalshi_ticker']:
        raise ValueError('Kalshi ticker mismatch')
    rules = {rule.casefold()}
    if code == 'MN' and adjective == 'Democratic':
        rules.add(rule.replace('Democratic party', 'Democratic (DFL) party').casefold())
    if record['kalshi']['rules_primary'].casefold() not in rules:
        raise ValueError('Kalshi settlement rule needs individual review')
    if (record['polymarket']['question'] != question or
            not record['polymarket']['description'].startswith(description_start)):
        raise ValueError('Polymarket election identity mismatch')
    if (record['orientations'] != [True, True] or
            mapping['kalshi_yes_matches_sig_yes'] is not True or
            mapping['polymarket_yes_matches_sig_yes'] is not True):
        raise ValueError('Expected direct matching party outcome on all three venues')
    return True
