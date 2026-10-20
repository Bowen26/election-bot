"""Per-cycle descriptive gate counts, without evaluating or changing any guard."""
from collections import Counter, defaultdict


class GateMetrics:
    def __init__(self):
        self.seen=set()
        self.counts=Counter()
        self.phases=defaultdict(Counter)

    def blocked(self, gate, phase, exchange=None):
        identity=(gate,phase,exchange)
        if identity in self.seen:return
        self.seen.add(identity)
        self.counts[gate]+=1
        self.phases[phase][gate]+=1

    def summary(self):
        return {'gate_counts':dict(self.counts),
                'gate_counts_by_phase':{phase:dict(counts) for phase,counts in self.phases.items()},
                'gate_count_basis':'Unique contract/gate/phase per cycle (global gates once per phase). '
                    'Overlapping route blocks, not independent rejected orders; later gates may not be reached.'}
