"""Fair bounded scanning, alternating news priority with ordinary coverage."""
import json
from pathlib import Path


class Scanner:
    def __init__(self, markets, runtime):
        self.markets = markets
        self.path = Path(runtime) / 'scan_cursor.json'
        self.index = 0
        self.priority_turn = True
        if self.path.exists():
            try:
                saved = json.loads(self.path.read_text())
                self.index = next((i for i, m in enumerate(markets)
                                   if m['name'] == saved.get('next_market')), 0)
                self.priority_turn = saved.get('priority_turn') is True
            except (ValueError, TypeError, AttributeError):
                pass  # Losing a scan cursor cannot lose order/accounting state.

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix('.tmp')
        temp.write_text(json.dumps({'next_market': self.markets[self.index]['name'],
                                   'priority_turn': self.priority_turn}))
        temp.replace(self.path)

    def batch(self, priority, size):
        seen = set()
        for _ in range(min(size, len(self.markets))):
            urgent = next((m for m in self.markets
                           if m['name'] in priority and m['name'] not in seen), None)
            if urgent is not None and self.priority_turn:
                mapping = urgent
                self.priority_turn = False
            else:
                while self.markets[self.index]['name'] in seen:
                    self.index = (self.index + 1) % len(self.markets)
                mapping = self.markets[self.index]
                self.index = (self.index + 1) % len(self.markets)
                self.priority_turn = True
            seen.add(mapping['name'])
            # Persist before yielding: an order/early return cannot reset fairness.
            self.save()
            yield mapping
