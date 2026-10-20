"""Within-batch entry ordering; cached signals never authorize submission."""
import time
from .strategy import D


def settings(config):
    raw=config.get('entry_ranking',{})
    if not isinstance(raw,dict) or set(raw)-{'enabled'} or type(raw.get('enabled',False)) is not bool:
        raise ValueError('entry_ranking requires a boolean enabled setting')
    return raw.get('enabled',False)


def candidate(mapping,signal,book,refs,config):
    target=book if signal.side=='yes' else book.complement()
    # Score patient and control at the same ask so assignment does not boost priority.
    price=target.asks[0][0] if signal.reason=='patient_entry' else signal.price
    fee=D(config['strategy']['cost_buffer_per_share'])
    edge=signal.reference-price-2*fee
    return {'mapping':mapping,'side':signal.side,'reason':signal.reason,
            'surplus':edge*signal.quantity,'edge_per_coin':edge/(price+fee),
            'expires':min(min(b.source_at,b.observed_at) for b in [book,*refs])+config['strategy']['max_age_seconds']}


# Stale ranking quotes only set the order: every ranked buy is re-quoted and re-decided
# in preflight before submission. A bounded grace and recheck cap keep the queue short.
RECHECK_GRACE_SECONDS=120
MAX_RECHECKS=6


def ordered(candidates,now=None):
    now=time.time() if now is None else now
    return sorted((c for c in candidates if c['expires']+RECHECK_GRACE_SECONDS>=now),
                  key=lambda c:(-c['surplus'],-c['edge_per_coin'],c['mapping']['sig_exchange_id']))
