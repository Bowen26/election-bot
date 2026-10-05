import copy
import json
from pathlib import Path
import unittest

from election_bot.catalog import verify_candidate


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.examples = json.loads((Path(__file__).parent / 'data/catalog_examples.json').read_text())

    def test_three_offices_and_two_house_ticker_formats(self):
        for review in self.examples:
            self.assertTrue(verify_candidate(review))

    def test_wrong_state_in_settlement_rule_rejected(self):
        review = copy.deepcopy(self.examples[0])
        review['contract']['kalshi']['rules_primary'] = 'If a Senator of Kentucky is sworn in, resolves Yes.'
        with self.assertRaisesRegex(ValueError, 'settlement rule'):
            verify_candidate(review)

    def test_wrong_year_party_and_primary_election_rejected(self):
        for key, wrong in [('electionDate', '2028-11-07'), ('winnerName', 'Republican Party'),
                           ('raceStage', 'Primary'), ('resolutionType', 'Candidate Winner')]:
            review = copy.deepcopy(self.examples[0])
            review['contract']['sig']['resolution_tree']['contract_details'][key] = wrong
            with self.assertRaisesRegex(ValueError, 'resolution identity'):
                verify_candidate(review)

    def test_wrong_house_district_and_candidate_question_rejected(self):
        review = copy.deepcopy(next(r for r in self.examples if ':house:' in r['mapping']['race_key']))
        review['contract']['polymarket']['question'] = 'Will a named candidate win?'
        with self.assertRaisesRegex(ValueError, 'Polymarket election identity'):
            verify_candidate(review)
        review = copy.deepcopy(next(r for r in self.examples if ':house:' in r['mapping']['race_key']))
        review['contract']['sig']['resolution_tree']['contract_details']['raceName'] += '2'
        with self.assertRaisesRegex(ValueError, 'resolution identity'):
            verify_candidate(review)

    def test_reversed_orientation_rejected(self):
        review = copy.deepcopy(self.examples[0])
        review['mapping']['polymarket_yes_matches_sig_yes'] = False
        with self.assertRaisesRegex(ValueError, 'outcome'):
            verify_candidate(review)
