"""Configuration-level exit coverage; never claims a current executable price."""
from collections import Counter


def exit_coverage(holdings, config):
    mappings={str(m.get('sig_exchange_id')):m for m in config['markets']}
    execution=config.get('execution',{})
    positions=[]
    for (exchange,side),value in sorted(holdings.items()):
        mapping=mappings.get(exchange)
        if not execution.get('enabled') or not execution.get('sell_enabled'):
            reason='selling_disabled'
        elif mapping is None:
            reason='unmapped_contract'
        elif not mapping.get('enabled'):
            reason='mapping_disabled'
        else:
            reason='configured_for_exit'
        positions.append({'exchange':exchange,'side':side,'quantity':str(value['quantity']),
                          'reason':reason})
    reasons=Counter(p['reason'] for p in positions)
    return {'owned_positions':len(positions),
            'without_automatic_exit':sum(v for k,v in reasons.items() if k!='configured_for_exit'),
            'counts':dict(reasons),'positions':positions,
            'scope':'Configuration only; quotes, news, current account inventory and execution limits may still block a sale.'}
