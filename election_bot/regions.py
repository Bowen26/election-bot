"""Explicit Census region assignments; geographic groups, not independent risks.

Source: https://www2.census.gov/geo/pdfs/maps-data/maps/reference/us_regdiv.pdf
"""
CENSUS_REGIONS = {
    'northeast': 'CT ME MA NH RI VT NJ NY PA'.split(),
    'midwest': 'IN IL MI OH WI IA KS MN MO NE ND SD'.split(),
    'south': 'DE DC FL GA MD NC SC VA WV AL KY MS TN AR LA OK TX'.split(),
    'west': 'AZ CO ID MT NV NM UT WY AK CA HI OR WA'.split(),
}
STATE_REGIONS = {state: region for region, states in CENSUS_REGIONS.items() for state in states}


def region_for_state(state):
    if not isinstance(state, str) or state not in STATE_REGIONS:
        raise ValueError('Unsupported state_code for Census region: ' + str(state))
    return STATE_REGIONS[state]


def mapping_region(mapping):
    expected = region_for_state(mapping.get('state_code'))
    if mapping.get('region') != expected:
        raise ValueError('Mapping region must match Census region for ' + mapping['state_code'])
    if 'race_key' in mapping:
        if not isinstance(mapping['race_key'], str):
            raise ValueError('Mapping race_key must be a string')
        parts = mapping['race_key'].split(':')
        if len(parts) != 3 or parts[2].split('-')[0] != mapping['state_code']:
            raise ValueError('Mapping state_code must match race_key')
    return expected
