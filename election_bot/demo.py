"""A synthetic market and broker. Never accesses the network."""
import copy
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import time

from .clients import contract_record, fingerprint
from .engine import Engine
from .state import Journal
from .strategy import Book


class DemoSig:
    tid = 'offline-demo-tournament'

    def __init__(self):
        self.placed = []
        self.cancelled = []
        self.closed = False

    def account(self):
        return {'id': self.tid, 'status': 'active', 'isPendingEnrolment': False,
                'startDate': '2026-01-01T00:00:00+00:00',
                'endDate': datetime.fromtimestamp(time.time()+86400, timezone.utc).isoformat(),
                'myBalance': 100000}

    def open_orders(self):
        return []

    def positions(self):
        return []

    def market(self, mid):
        return {'id': '1', 'title': 'SYNTHETIC Democratic election victory', 'status': 'open',
                'exchanges': [{'id': '2', 'option': 'Yes'}],
                'resolution_tree': {'type': 'demo-only'}}

    def book(self, exchange):
        return Book.make([('0.55', 100)], [('0.60', 100)])

    def place(self, payload):
        self.placed.append(copy.deepcopy(payload))
        return {'orderId': 123, 'open': True, 'quantityTraded': 10, 'totalCost': 6.0}

    def order(self, oid):
        return {'exchangeId': '2', 'tournamentId': self.tid, 'open': not self.closed,
                'quantityFilled': 10 if self.closed else None}

    def cancel(self, oid):
        self.cancelled.append(oid)
        self.closed = True


class DemoReferences:
    def metadata(self, mapping):
        return ({'ticker': 'DEMO-D', 'rules_primary': 'Synthetic demo rule'},
                {'id': 'demo', 'description': 'Synthetic demo rule'}, 'demo-token')

    def books(self, mapping, metadata):
        return [Book.make([('0.73', 200)], [('0.75', 200)]),
                Book.make([('0.74', 200)], [('0.76', 200)])]


def fixture():
    config = json.loads((Path(__file__).resolve().parent.parent / 'config.example.json').read_text())
    config['news']['enabled'] = False
    config['tournament_id'] = DemoSig.tid
    mapping = config['markets'][0]
    mapping.update(enabled=True, sig_market_id='1', sig_exchange_id='2')
    sig, references = DemoSig(), DemoReferences()
    mapping['contract_fingerprint'] = fingerprint(contract_record(sig.market('1'), mapping,
                                                                  references.metadata(mapping)))
    return config, sig, references


def run_demo():
    config, sig, references = fixture()
    with tempfile.TemporaryDirectory(prefix='sig-bot-demo-') as directory:
        journal = Journal(Path(directory) / 'demo.sqlite3', 'offline-demo')
        try:
            print('OFFLINE SYNTHETIC DEMO — no account, network, or real trades')
            engine = Engine(config, sig, references, journal, directory, live=True)
            engine.cycle()
            print(json.dumps({'demo_complete': True, 'submitted_to_fake_broker': len(sig.placed),
                              'cancelled_fake_remainder': sig.cancelled,
                              'journal': journal.summary()}))
        finally:
            journal.close()
