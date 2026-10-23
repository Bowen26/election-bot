"""Validate config.json and list every feature switch and key limit in one place. No network."""
import json
from pathlib import Path


def report(config_path):
    path = Path(config_path)
    config = json.loads(path.read_text())
    result = {'file': str(path.resolve()), 'valid': True, 'error': None}
    try:
        from .engine import validate_config
        validate_config(config)
    except (ValueError, KeyError, TypeError) as error:
        result.update(valid=False, error=str(error))
    get = lambda section, key, default=None: config.get(section, {}).get(key, default) if isinstance(config.get(section), dict) else default
    markets = config.get('markets', [])
    result['markets'] = {'enabled': sum(1 for m in markets if m.get('enabled')),
                         'basket_legs': sum(1 for m in markets if m.get('basket_leg'))}
    result['features'] = {
        'execution': get('execution', 'enabled', False), 'selling': get('execution', 'sell_enabled', False),
        'model_trading': get('model_trading', 'enabled', False),
        'drawdown_control': get('drawdown_control', 'enabled', False),
        'drawdown_valuation': get('drawdown_control', 'valuation', 'sig_bid'),
        'settlement_exits': get('settlement_exits', 'enabled', False),
        'risk_exits': get('risk_exit', 'enabled', False),
        'election_night': get('election_night', 'enabled', False),
        'election_night_recycling': get('election_night', 'recycle_enabled', False),
        'night_ignores_drawdown': get('election_night', 'ignore_drawdown_pause', True),
        'night_ignores_exposure_caps': get('election_night', 'ignore_exposure_caps', True),
        'capital_rotation': get('capital_rotation', 'enabled', False),
        'pair_baskets': get('pair_baskets', 'enabled', False),
        'research_followups': get('research_followups', 'enabled', False),
        'alerts': get('alerts', 'enabled', False),
        'patient_entry': get('patient_entry', 'enabled', False),
        'entry_ranking': get('entry_ranking', 'enabled', False),
        'news': get('news', 'enabled', False)}
    limits, strategy = config.get('limits', {}), config.get('strategy', {})
    result['limits'] = {k: limits.get(k) for k in ('per_order', 'per_market', 'total', 'daily', 'net_shares_total',
                                                    'net_shares_per_office', 'net_shares_per_region', 'realized_loss_stop_fraction')}
    result['strategy'] = {k: strategy.get(k) for k in ('minimum_edge', 'cost_buffer_per_share', 'max_shares_per_order',
                                                        'max_age_seconds')}
    result['caps'] = {'pair_baskets_total_coins': get('pair_baskets', 'total_coins'),
                      'pair_baskets_min_edge': get('pair_baskets', 'min_edge'),
                      'model_total': get('model_trading', 'total'),
                      'drawdown_pause_coins': get('drawdown_control', 'pause_coins'),
                      'rotation_max_giveup': get('capital_rotation', 'max_giveup', '0.02') if get('capital_rotation', 'enabled', False) else None,
                      'rotation_min_gain': get('capital_rotation', 'min_gain', '0.03') if get('capital_rotation', 'enabled', False) else None}
    return result


def format_report(r):
    lines = ['CONFIG CHECK — ' + r['file'],
             'VALID' if r['valid'] else 'INVALID: ' + str(r['error']),
             f"Markets: {r['markets']['enabled']} enabled, {r['markets']['basket_legs']} basket legs (disabled by design)",
             'Features:']
    for name, value in r['features'].items():
        lines.append(f'  {name:26} {value}')
    lines.append('Limits: ' + ', '.join(f'{k}={v}' for k, v in r['limits'].items() if v is not None))
    lines.append('Strategy: ' + ', '.join(f'{k}={v}' for k, v in r['strategy'].items()))
    lines.append('Caps: ' + ', '.join(f'{k}={v}' for k, v in r['caps'].items() if v is not None))
    lines.append('Changes take effect when the watcher restarts.')
    return '\n'.join(lines)
