"""Directional share limits supplement coin limits; they are not loss estimates."""
import json
from decimal import InvalidOperation

from .strategy import D

OFFICES = ('house', 'senate', 'governor')
CAP_FIELDS = ('net_shares_total', 'net_shares_per_office')
LOSS_FIELD = 'realized_loss_stop_fraction'


def validate_risk_config(config):
    limits = config['limits']
    configured = any(name in limits for name in CAP_FIELDS + (LOSS_FIELD,))
    if configured and not config.get('execution', {}).get('enabled'):
        raise ValueError('Portfolio risk controls require execution.enabled=true (ActiveEngine)')
    if any(name in limits for name in CAP_FIELDS):
        for name in CAP_FIELDS:
            if name not in limits:
                raise ValueError('Directional limits require both ' + ' and '.join(CAP_FIELDS))
            try:
                cap = D(limits[name]) if not isinstance(limits[name], bool) else D(0)
            except (InvalidOperation, ValueError, TypeError):
                raise ValueError(name + ' must be a positive integer share count') from None
            if cap <= 0 or cap != int(cap):
                raise ValueError(name + ' must be a positive integer share count')
        if D(limits['net_shares_per_office']) > D(limits['net_shares_total']):
            raise ValueError('net_shares_per_office cannot exceed net_shares_total')
        seen = set()
        for mapping in config.get('markets', []):
            if isinstance(mapping, dict) and mapping.get('sig_exchange_id') is not None:
                exchange = mapping['sig_exchange_id']
                if exchange in seen:
                    raise ValueError('Exposure mappings must have unique exchange IDs, including disabled mappings')
                seen.add(exchange)
            if not isinstance(mapping, dict) or not mapping.get('enabled'):
                continue  # Generic mapping validation reports malformed records.
            _metadata(mapping)
    if LOSS_FIELD in limits:
        try:
            fraction = D(limits[LOSS_FIELD]) if not isinstance(limits[LOSS_FIELD], bool) else D(0)
        except (InvalidOperation, ValueError, TypeError):
            raise ValueError(LOSS_FIELD + ' must be greater than zero and at most one') from None
        if not 0 < fraction <= 1:
            raise ValueError(LOSS_FIELD + ' must be greater than zero and at most one')


def _metadata(mapping):
    if mapping.get('office') not in OFFICES:
        raise ValueError('Explicit office required for exposure mapping: ' + str(mapping.get('name')))
    if type(mapping.get('exposure_sign')) is not int or mapping['exposure_sign'] not in (-1, 1):
        raise ValueError('Explicit exposure_sign (+1 or -1) required for mapping: ' + str(mapping.get('name')))
    return mapping['office'], mapping['exposure_sign']


class Exposure:
    """Conservative net-share interval over all possible pending partial fills.

    Positive is the configured common factor (currently Democratic victory).
    Do not assume a NO in one race hedges a YES in another at settlement.
    """
    def __init__(self, ledger, config):
        self.enabled = 'net_shares_total' in config['limits']
        self.limits = config['limits']
        self.mappings = {m['sig_exchange_id']: m for m in config['markets']
                         if isinstance(m, dict) and 'sig_exchange_id' in m}
        self.net = {'total': D(0), **{office: D(0) for office in OFFICES}}
        self.bounds = {group: [D(0), D(0)] for group in self.net}
        if not self.enabled:
            return
        holdings, _ = ledger.inventory()
        for (exchange, side), value in holdings.items():
            office, sign = self.metadata(exchange)
            delta = value['quantity'] * sign * self.direction('buy', side)
            self.net['total'] += delta
            self.net[office] += delta
        self.bounds = {group: [net, net] for group, net in self.net.items()}
        for row in ledger.journal.pending():
            payload = json.loads(row['payload'])
            office, sign = self.metadata(payload['exchangeId'])
            quantity = D(payload['quantity'])
            if quantity < 0 or quantity != int(quantity):
                raise RuntimeError('Invalid pending exposure quantity; reconcile before trading')
            delta = quantity * sign * self.direction(payload['action'], payload['side'])
            # Reserve the entire unknown fill interval, including pending sales.
            for group in ('total', office):
                self.bounds[group][0] += min(D(0), delta)
                self.bounds[group][1] += max(D(0), delta)

    def metadata(self, exchange):
        try:
            return _metadata(self.mappings[exchange])
        except (KeyError, ValueError) as error:
            raise RuntimeError('Unmapped portfolio exposure for exchange ' + str(exchange)) from error

    @staticmethod
    def direction(action, side):
        if action not in ('buy', 'sell') or side not in ('yes', 'no'):
            raise RuntimeError('Unknown action/side in exposure accounting')
        return (1 if action == 'buy' else -1) * (1 if side == 'yes' else -1)

    def headroom(self, exchange, action, side):
        if not self.enabled:
            return None
        office, sign = self.metadata(exchange)
        direction = sign * self.direction(action, side)
        caps = {'total': D(self.limits['net_shares_total']),
                office: D(self.limits['net_shares_per_office'])}
        rooms = []
        for group, cap in caps.items():
            low, high = self.bounds[group]
            # If already outside a cap, allow only moves toward the allowed band.
            rooms.append(max(D(0), cap-high if direction > 0 else cap+low))
        return min(rooms)

    def summary(self):
        return {'enabled': self.enabled, 'net_shares': self.net if self.enabled else None,
                'possible_net_shares_including_pending': self.bounds if self.enabled else None,
                'total_cap': self.limits.get('net_shares_total'),
                'per_office_cap': self.limits.get('net_shares_per_office')}


def verify_account_inventory(ledger, positions):
    """Unknown/manual positions cannot silently bypass bot portfolio limits."""
    holdings, _ = ledger.inventory()
    expected = {}
    for (exchange, side), value in holdings.items():
        expected[exchange] = expected.get(exchange, D(0)) + value['quantity'] * (1 if side == 'yes' else -1)
    actual = {}
    for position in positions:
        exchange, quantity = str(position['exchangeId']), D(position['quantity'])
        if position.get('settled') and quantity:
            raise RuntimeError('Settled account position requires accounting review before portfolio trading')
        actual[exchange] = actual.get(exchange, D(0)) + quantity
    if {e: q for e, q in expected.items() if q} != {e: q for e, q in actual.items() if q}:
        raise RuntimeError('Account portfolio differs from bot ledger; reconcile before portfolio trading')
