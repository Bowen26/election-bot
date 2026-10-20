import argparse
import getpass
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import time

from .clients import HTTP, References, Sig, contract_record, fingerprint
from .engine import Engine, validate_config
from .state import Journal, exclusive_lock
from .news import NewsCollector, NewsGate, NewsStore

ROOT = Path(__file__).resolve().parent.parent
RUNTIME = ROOT / '.runtime'


def output(value):
    print(json.dumps(value, indent=2, default=str))


def key():
    value = os.environ.get('SIG_API_KEY')
    if value:
        return value
    env = ROOT / '.env'
    if env.exists():
        for line in env.read_text().splitlines():
            if line.startswith('SIG_API_KEY='):
                return line.split('=', 1)[1].strip()
    raise ValueError('SIG_API_KEY missing. Run python3 -m election_bot setup locally.')


def save_json(path, data):
    temp = path.with_suffix(path.suffix + '.tmp')
    with temp.open('w') as handle:
        json.dump(data, handle, indent=2)
        handle.write('\n')
        handle.flush()
        os.fsync(handle.fileno())
    temp.replace(path)


def recent_news_reviews(runtime):
    reviews = []
    for mode in ('paper', 'live'):
        path = (runtime / (mode + '.sqlite3')).resolve()
        if not path.exists():
            continue
        db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
        try:
            for at, detail in db.execute("SELECT at,detail FROM events WHERE kind='news_review' ORDER BY at DESC LIMIT 5"):
                reviews.append({'trading_mode': mode, 'at': at, **json.loads(detail)})
        finally:
            db.close()
    return sorted(reviews, key=lambda r: r['at'], reverse=True)


def main():
    parser = argparse.ArgumentParser(description='Autonomous SIG competition bot; external venues are read-only.')
    parser.add_argument('--config', type=Path, default=ROOT / 'config.json')
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('demo', help='Run a synthetic offline order and cancellation')
    sub.add_parser('setup', help='Store SIG key locally, and create a configuration template')
    discover = sub.add_parser('discover', help='List SIG tournaments or market/exchange IDs')
    discover.add_argument('--tournaments', action='store_true')
    discover.add_argument('--search', default='')
    for name in ('inspect', 'pin'):
        command = sub.add_parser(name, help='Inspect mapping rules' if name == 'inspect' else 'Save a reviewed mapping and enable it')
        command.add_argument('--mapping', required=True)
    sub.add_parser('feeds', help='Check public reference APIs without a SIG key')
    news_command = sub.add_parser('news', help='Read news status or collect RSS feeds once; no SIG key needed')
    news_action = news_command.add_mutually_exclusive_group()
    news_action.add_argument('--once', action='store_true', help='Fetch configured news feeds once')
    news_action.add_argument('--status', action='store_true', help='Show local news, source health and active pauses')
    news_action.add_argument('--clear-dispute', type=int, metavar='ARTICLE_ID', help='Clear one reviewed dispute flag locally')
    news_command.add_argument('--mapping', help='Exact mapping name for a dispute review')
    news_command.add_argument('--note', help='Required explanation for clearing a dispute flag')
    run = sub.add_parser('run', help='Run continuously; paper mode unless --live is supplied')
    run.add_argument('--live', action='store_true', help='Automatically submit SIG competition orders with no per-trade prompts')
    run.add_argument('--once', action='store_true')
    watch = sub.add_parser('watch', help='Run with bounded automatic crash recovery; honors STOP')
    watch.add_argument('--live', action='store_true', help='Supervise live SIG competition trading')
    recover = sub.add_parser('recover', help='Reconcile live pending orders; replay unknown requests only after expiration')
    recover.add_argument('--wait', action='store_true',
                         help='Wait past expiration and refresh SIG timing; honors STOP (used by watch --live)')
    sub.add_parser('stop', help='Write the stop signal; does not liquidate holdings')
    sub.add_parser('resume', help='Clear the stop signal; does not launch the bot')
    sub.add_parser('status', help='Read local spending and pending-order status')
    sub.add_parser('coverage', help='Show enabled race counts, limits and catalog exclusions; no network')
    performance = sub.add_parser('performance', help='Read local execution, markout and skip diagnostics')
    performance.add_argument('--paper', action='store_true')
    drawdown = sub.add_parser('drawdown-status', help='Read portfolio drawdown and buy-pause status; no orders or state reset')
    drawdown.add_argument('--paper', action='store_true')
    strategy_performance = sub.add_parser('strategy-performance', help='Read FIFO profits, model allowance and blockers by entry route; no network')
    strategy_performance.add_argument('--paper', action='store_true')
    strategy_performance.add_argument('--json', action='store_true')
    diagnostics = sub.add_parser('diagnostics', help='Read venue freshness and exit blockers from the local journal')
    diagnostics.add_argument('--paper', action='store_true')
    analysis = sub.add_parser('analysis', help='Read entry-price and entry-gap markout reports; no network')
    analysis.add_argument('--paper', action='store_true')
    analysis.add_argument('--json', action='store_true', help='Include full statistics and uncertainty details')
    leadership = sub.add_parser('leadership', help='Read reference leadership and prospective shadow comparisons; no network')
    leadership.add_argument('--paper', action='store_true')
    leadership.add_argument('--json', action='store_true')
    leadership.add_argument('--hours', type=float, default=48, help='Lookback in hours, up to 720 (default: 48)')
    timing = sub.add_parser('entry-timing-study', help='Simulate immediate vs confirmed-bid entry timing; no orders or network')
    timing.add_argument('--paper', action='store_true')
    timing.add_argument('--json', action='store_true')
    timing.add_argument('--hours', type=float, default=48, help='Lookback in hours, up to 720 (default: 48)')
    timing.add_argument('--include-retrospective', action='store_true',
                        help='Also replay proposals recorded before the rule was pre-registered (exploratory)')
    followups = sub.add_parser('research-followups', help='Read research follow-up quote coverage; no network')
    followups.add_argument('--paper', action='store_true')
    followups.add_argument('--json', action='store_true')
    followups.add_argument('--hours', type=float, default=48)
    review = sub.add_parser('settlement-review', help='Replay holdings, sales and drawdown at settlement value; no network')
    review.add_argument('--paper', action='store_true')
    review.add_argument('--json', action='store_true')
    review.add_argument('--hours', type=float, default=72)
    night = sub.add_parser('election-night-rehearsal', help='Show what the election-night route would buy on recorded quotes; no orders')
    night.add_argument('--paper', action='store_true')
    night.add_argument('--json', action='store_true')
    night.add_argument('--hours', type=float, default=24)
    dash = sub.add_parser('dashboard', help='One-screen status: watcher, P&L, baskets, orders, errors; local files only')
    dash.add_argument('--json', action='store_true')
    check = sub.add_parser('config-check', help='Validate config.json and list every feature switch and limit; no network')
    check.add_argument('--json', action='store_true')
    swing = sub.add_parser('swing-risk', help='Stress holdings against a uniform national swing; no network')
    swing.add_argument('--paper', action='store_true')
    swing.add_argument('--json', action='store_true')
    pairs = sub.add_parser('pair-arbitrage', help='Scan SIG party-pair markets for mispricing; paced reads, no orders')
    pairs.add_argument('--json', action='store_true')
    pairs.add_argument('--interval', type=float, default=2.5,
                       help='Seconds between SIG reads (default 2.5, safe beside the live bot; minimum 0.8)')
    pairs.add_argument('--limit', type=int, help='Scan only the first N races (alphabetical)')
    pairs.add_argument('--threshold', default='0.01', help='Minimum edge per set to list (default 0.01)')
    legs = sub.add_parser('pair-legs', help='Discover verified Republican basket legs (GET-only); --write adds them disabled to config')
    legs.add_argument('--write', action='store_true', help='Back up config.json and append the discovered legs (disabled)')
    legs.add_argument('--race', action='append', help='Limit to a race_key, e.g. 2026:house:FL-22 (repeatable)')
    legs.add_argument('--limit', type=int)
    legs.add_argument('--interval', type=float, default=3.0, help='Seconds per SIG read (default 3.0, about 20 reads/min beside the live bot)')
    legs.add_argument('--json', action='store_true')
    coverage_polls = sub.add_parser('polling-coverage', help='Explain polling bindings and inputs per Senate contract; no network')
    coverage_polls.add_argument('--json', action='store_true')
    throttle = sub.add_parser('performance-throttle', help='Read route sizing evidence and state; no orders or network')
    throttle.add_argument('--paper', action='store_true')
    throttle.add_argument('--json', action='store_true')
    patient = sub.add_parser('patient-study', help='Compare the patient entry pilot against the current entry method; no orders or network')
    patient.add_argument('--paper', action='store_true')
    patient.add_argument('--json', action='store_true')
    exits = sub.add_parser('exit-study', help='Read prospective exit-depth comparisons; no network')
    exits.add_argument('--paper', action='store_true')
    exits.add_argument('--json', action='store_true')
    exits.add_argument('--hours', type=float, default=24)
    rotation = sub.add_parser('rotation-study', help='Read prospective capital-rotation comparisons; no orders or network')
    rotation.add_argument('--paper', action='store_true')
    rotation.add_argument('--json', action='store_true')
    rotation.add_argument('--hours', type=float, default=24)
    low = sub.add_parser('low-price-study', help='Replay low-price target/trailing exits from recorded quotes; no orders')
    low.add_argument('--paper', action='store_true')
    low.add_argument('--json', action='store_true')
    low.add_argument('--hours', type=float, default=24)
    low.add_argument('--entry-mode', choices=('external_gap','sig_momentum','compare'), default='external_gap',
                     help='Entry hypothesis to replay; compare runs both on the same recorded window')
    fair = sub.add_parser('fair-value', help='Independent polling research; cannot submit model trades')
    action = fair.add_mutually_exclusive_group()
    action.add_argument('--import-json', type=Path, help='Import verified polling evidence with local receipt timestamps')
    action.add_argument('--seed-public', action='store_true', help='Import the dated, source-checked NH Senate pilot; no model trades')
    action.add_argument('--fetch-public', action='store_true', help='Stage public polling feed for review; never auto-import')
    action.add_argument('--seed-fundamentals', action='store_true', help='Add reviewed historical-lean priors for paired research; no trades')
    action.add_argument('--collect-public', action='store_true', help='Collect VoteHub research inputs once; no orders')
    action.add_argument('--collector-status', action='store_true', help='Show public polling collection health')
    action.add_argument('--template', type=Path, help='Write a draft for up to ten configured Senate contracts')
    action.add_argument('--demo', action='store_true', help='Run a synthetic offline fair-value comparison')
    fair.add_argument('--paper', action='store_true')
    fair.add_argument('--json', action='store_true')
    fair.add_argument('--hours', type=float, default=24)
    sub.add_parser('clock-check', help='Read SIG response timing and verify local clock; no orders')
    contracts = sub.add_parser('contract-review', help='Read settlement and alternative-contract evidence; never enables trading')
    contracts.add_argument('--race', help='Exact race_key, e.g. 2026:senate:NE')
    contracts.add_argument('--json', action='store_true')
    contracts.add_argument('--refresh', action='store_true', help='GET fresh rules and books for one --race and save review evidence')
    args = parser.parse_args()
    # One per-account SIG request budget shared by the bot and every side command.
    import election_bot.clients as clients_module
    clients_module.SHARED_RATE_FILE = RUNTIME / 'sig-rate.json'
    if args.command == 'rotation-study':
        from .rotation import report, format_report
        result = report(RUNTIME / ('paper.sqlite3' if args.paper else 'live.sqlite3'), args.hours)
        output(result) if args.json else print(format_report(result))
        return
    if args.command == 'drawdown-status':
        from .drawdown import report
        config = json.loads(args.config.read_text())
        output(report(RUNTIME / ('paper.sqlite3' if args.paper else 'live.sqlite3'), config))
        return
    if args.command == 'performance-throttle':
        from .performance_throttle import report, format_report
        config = json.loads(args.config.read_text()) if args.config.exists() else {}
        result = report(RUNTIME / ('paper.sqlite3' if args.paper else 'live.sqlite3'),config)
        output(result) if args.json else print(format_report(result))
        return
    if args.command == 'strategy-performance':
        from .strategy_performance import report, format_report
        config = json.loads(args.config.read_text()) if args.config.exists() else None
        result = report(RUNTIME / ('paper.sqlite3' if args.paper else 'live.sqlite3'), config)
        output(result) if args.json else print(format_report(result))
        return
    if args.command == 'demo':
        from .demo import run_demo
        run_demo()
        return
    if args.command == 'fair-value':
        from .fair_cli import run
        path = args.config if args.config.exists() else ROOT / 'config.example.json'
        result = run(args, RUNTIME, json.loads(path.read_text()))
        print(result) if isinstance(result, str) else output(result)
        return
    if args.command == 'setup':
        if not args.config.exists():
            save_json(args.config, json.loads((ROOT / 'config.example.json').read_text()))
        env = ROOT / '.env'
        if not env.exists() and not os.environ.get('SIG_API_KEY'):
            value = getpass.getpass('SIG API key (hidden, read + trade scopes): ').strip()
            if not value or any(c.isspace() for c in value):
                raise ValueError('Expected a nonempty API token with no whitespace')
            fd = os.open(str(env), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'w') as handle:
                handle.write('SIG_API_KEY=' + value + '\n')
                handle.flush()
                os.fsync(handle.fileno())
        print('Local setup saved. Next: discover --tournaments, then edit ' + str(args.config))
        return
    if args.command == 'contract-review':
        from .contract_review import report, format_report, refresh_race
        if args.refresh and not args.race:
            parser.error('contract-review --refresh requires --race')
        config = json.loads(args.config.read_text())
        if args.refresh:
            sig = Sig(key(), config['tournament_slug'])
            with References() as refs:
                evidence = refresh_race(config, args.race, sig, refs)
            directory = RUNTIME / 'contract-reviews'
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            save_json(directory / (fingerprint(args.race)+'.json'), evidence)
        result = report(config, RUNTIME, args.race)
        output(result) if args.json else print(format_report(result))
        return
    if args.command in ('entry-timing-study', 'research-followups'):
        path = RUNTIME / ('paper.sqlite3' if args.paper else 'live.sqlite3')
        if args.command == 'entry-timing-study':
            from .entry_timing import report, format_report
            result = report(path, hours=args.hours, include_retrospective=args.include_retrospective)
        else:
            from .research_followups import report, format_report
            result = report(path, hours=args.hours)
        output(result) if args.json else print(format_report(result))
        return
    if args.command == 'config-check':
        from .config_check import report, format_report
        result = report(args.config)
        output(result) if args.json else print(format_report(result))
        if not result['valid']:
            sys.exit(2)
        return
    if args.command == 'dashboard':
        from .dashboard import report, format_report
        result = report(RUNTIME, json.loads(args.config.read_text()))
        output(result) if args.json else print(format_report(result))
        return
    if args.command == 'swing-risk':
        from .swing_risk import report, format_report
        result = report(RUNTIME / ('paper.sqlite3' if args.paper else 'live.sqlite3'),
                        json.loads(args.config.read_text()))
        output(result) if args.json else print(format_report(result))
        return
    if args.command == 'pair-legs':
        from .pair_baskets import discover
        if args.interval < 0.8:
            parser.error('--interval must be at least 0.8 seconds')
        config = json.loads(args.config.read_text())
        with References() as refs:
            found, skipped = discover(config, Sig(key(), config['tournament_slug']), refs,
                                      races=set(args.race) if args.race else None, limit=args.limit, interval=args.interval)
        result = {'discovered': [{'race_key': l['race_key'], 'name': l['name'], 'sig_exchange_id': l['sig_exchange_id'],
                                  'kalshi_ticker': l['kalshi_ticker'], 'polymarket_market': l['polymarket_market']} for l in found],
                  'skipped': skipped, 'written': False}
        if args.write and found:
            backup = RUNTIME / ('config-before-pair-legs-%d.json' % int(time.time()))
            backup.write_text(args.config.read_text())
            os.chmod(backup, 0o600)
            config['markets'].extend(found)
            validate_config(config)
            save_json(args.config, config)
            result.update(written=True, backup=str(backup))
        if args.json:
            output(result)
        else:
            print('Discovered %d verified legs; skipped %d races.%s' % (len(found), len(skipped),
                  ' Written to config (disabled); backup ' + result['backup'] if result['written'] else ' Preview only; rerun with --write.'))
            for leg in result['discovered']:
                print('  ' + leg['race_key'] + '  ' + leg['name'])
            for race, reason in sorted(skipped.items()):
                print('  skip ' + race + ': ' + reason)
        return
    if args.command == 'pair-arbitrage':
        from .pair_arbitrage import scan, report, format_report
        if args.interval < 0.8:
            parser.error('--interval must be at least 0.8 seconds')
        config = json.loads(args.config.read_text())
        result = report(scan(Sig(key(), config['tournament_slug']), args.interval, args.limit), args.threshold)
        output(result) if args.json else print(format_report(result))
        return
    if args.command == 'election-night-rehearsal':
        from .election_night import rehearse, format_rehearsal
        result = rehearse(RUNTIME / ('paper.sqlite3' if args.paper else 'live.sqlite3'),
                          json.loads(args.config.read_text()), hours=args.hours)
        output(result) if args.json else print(format_rehearsal(result))
        return
    if args.command == 'settlement-review':
        from .settlement_review import report, format_report
        result = report(RUNTIME / ('paper.sqlite3' if args.paper else 'live.sqlite3'), hours=args.hours)
        output(result) if args.json else print(format_report(result))
        return
    if args.command == 'polling-coverage':
        from .poll_coverage import report, format_report
        result = report(RUNTIME, json.loads(args.config.read_text()))
        output(result) if args.json else print(format_report(result))
        return
    if args.command in ('leadership', 'exit-study', 'low-price-study'):
        if args.command == 'low-price-study':
            from .low_price import report, format_report
        elif args.command == 'exit-study':
            from .exit_study import report, format_report
        else:
            from .leadership import report, format_report
        options = {'entry_mode': args.entry_mode} if args.command == 'low-price-study' else {}
        result = report(RUNTIME / ('paper.sqlite3' if args.paper else 'live.sqlite3'), hours=args.hours, **options)
        if args.json:
            output(result)
        else:
            print(format_report(result))
        return
    RUNTIME.mkdir(exist_ok=True, mode=0o700)
    if args.command == 'patient-study':
        from .patient_report import report, format_report
        result = report(RUNTIME / ('paper.sqlite3' if args.paper else 'live.sqlite3'))
        output(result) if args.json else print(format_report(result))
        return
    if args.command == 'analysis':
        from .entry_analysis import report, format_report
        result = report(RUNTIME / ('paper.sqlite3' if args.paper else 'live.sqlite3'))
        if args.json:
            output(result)
        else:
            print(format_report(result))
        return
    if args.command in ('performance', 'diagnostics'):
        from .performance import report
        result = report(RUNTIME / ('paper.sqlite3' if args.paper else 'live.sqlite3'))
        if args.command == 'diagnostics':
            fields = ('as_of', 'status', 'unresolved_orders', 'quote_diagnostics_last_24h',
                      'exit_diagnostics_last_24h', 'position_news_risk')
            result = {k: result[k] for k in fields if k in result}
        output(result)
        return
    if args.command == 'stop':
        (RUNTIME / 'STOP').touch(mode=0o600)
        print('Stop requested. A running bot will finish cancelling its known order, then exit. Holdings remain.')
        return
    if args.command == 'resume':
        (RUNTIME / 'STOP').unlink(missing_ok=True)
        print('Stop signal cleared. Use run to launch the bot.')
        return
    if args.command == 'status':
        output({'stop_requested': (RUNTIME / 'STOP').exists()})
        supervisor_status = RUNTIME / 'supervisor' / 'status.json'
        if supervisor_status.exists():
            output({'supervisor_last_recorded': json.loads(supervisor_status.read_text()),
                    'note': 'Last recorded state, not a process-liveness check.'})
        for mode in ('paper', 'live'):
            path = RUNTIME / (mode + '.sqlite3')
            if path.exists():
                db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
                try:
                    output({'mode': mode, 'orders_by_state': db.execute(
                        'SELECT state,COUNT(*),SUM(CAST(amount AS REAL)) FROM orders GROUP BY state').fetchall()})
                finally:
                    db.close()
        return
    if args.command == 'discover' and args.tournaments:
        http, rows, offset = HTTP('https://sig.thesuper.market/api/v1', key()), [], 0
        for _ in range(100):
            page = http.request('/tournaments', params={'limit': 100, 'offset': offset})
            rows.extend(page['data'])
            if not page['pagination']['hasMore']:
                output(rows)
                return
            offset += 100
        raise ValueError('Tournament pagination exceeded limit')
    path = args.config if args.config.exists() else ROOT / 'config.example.json'
    config = json.loads(path.read_text())
    validate_config(config)
    if args.command == 'watch':
        from .supervisor import watch
        raise SystemExit(watch(ROOT, RUNTIME, path, live=args.live))
    if args.command == 'coverage':
        from collections import Counter
        enabled = [m for m in config['markets'] if m.get('enabled')]
        report_path = RUNTIME / 'coverage.json'
        report = json.loads(report_path.read_text()) if report_path.exists() else {}
        output({'configured_races': len(enabled),
                'by_office': dict(Counter(m.get('race_key', 'unknown:unknown').split(':')[1] for m in enabled)),
                'by_region': dict(Counter(m.get('region', 'unassigned') for m in enabled)),
                'limits': config['limits'], 'scan_batch_size': config.get('scan_batch_size', 8),
                'execution': config.get('execution', {'enabled': False}),
                'budget_mode': 'open_cost_plus_realized_losses' if config.get('execution', {}).get('enabled') else 'cumulative_purchases',
                'catalog_audited_at': report.get('audited_at'),
                'excluded_contests': report.get('excluded_contests', []),
                'note': 'Saved configuration; a running process loads changes only after restart.'})
        return
    if args.command == 'news':
        if not config.get('news', {}).get('enabled'):
            raise ValueError('News is not enabled in this configuration')
        if args.clear_dispute is not None and (not args.mapping or not args.note):
            raise ValueError('--clear-dispute requires --mapping and --note')
        if args.clear_dispute is None and (args.mapping or args.note):
            raise ValueError('--mapping and --note apply only to --clear-dispute')
        store = NewsStore(RUNTIME / 'news.sqlite3')
        try:
            store.migrate_active_disputes(config)
            if args.clear_dispute is not None:
                output(store.clear_dispute(args.clear_dispute, args.mapping, args.note))
            if args.once:
                with exclusive_lock(RUNTIME / 'news-collector'):
                    output(NewsCollector(config, store).poll())
            output({**store.status(), 'recent_price_reviews': recent_news_reviews(RUNTIME)})
        finally:
            store.close()
        return
    with References() as refs:
        if args.command == 'feeds':
            for mapping in config['markets']:
                metadata = refs.metadata(mapping)
                books = refs.books(mapping, metadata)
                output({'mapping': mapping['name'], 'venues': [
                    {'venue': venue, 'bid': book.bids[0] if book.bids else None,
                     'ask': book.asks[0] if book.asks else None,
                     'source_age_seconds': round(time.time()-book.source_at, 2)}
                    for venue, book in zip(('Kalshi', 'Polymarket'), books)]})
            return
        token = key()
        if args.command == 'run' and not args.once:
            from .runner import connect
            sig = connect(lambda: Sig(token, config['tournament_slug']),
                          lambda: (RUNTIME / 'STOP').exists(), output)
            if sig is None:
                print('Stop signal is set. Use resume before run.')
                return
        else:
            sig = Sig(token, config['tournament_slug'])
        if args.command == 'clock-check':
            output(sig.check_clock())
            return
        if args.command == 'discover':
            output({'tournament': sig.tournament, 'markets': sig.markets(args.search)})
            return
        if args.command in ('inspect', 'pin'):
            matches = [m for m in config['markets'] if m['name'] == args.mapping]
            if len(matches) != 1:
                raise ValueError('Mapping name missing or duplicated')
            mapping = matches[0]
            record = contract_record(sig.market(mapping['sig_market_id']), mapping, refs.metadata(mapping))
            output({'tournament': sig.tournament, 'contract': record, 'fingerprint': fingerprint(record)})
            if args.command == 'pin':
                mapping['contract_fingerprint'] = fingerprint(record)
                mapping['enabled'] = True
                config['tournament_id'] = sig.tid
                save_json(args.config, config)
                print('Mapping pinned and enabled. Run remains paper mode unless --live is specified.')
            return
        live = args.command == 'recover' or args.live
        if (RUNTIME / 'STOP').exists() and args.command != 'recover':
            raise ValueError('Stop signal is set. Use resume before run.')
        with exclusive_lock(RUNTIME):
            binding = fingerprint({'tournament': sig.tid, 'key_hash': hashlib.sha256(token.encode()).hexdigest()})
            journal = Journal(RUNTIME / ('live.sqlite3' if live else 'paper.sqlite3'), binding)
            engine = None
            news = None
            polls = None
            try:
                if args.command != 'recover' and config.get('news', {}).get('enabled'):
                    news = NewsGate(config, RUNTIME, ('live:' if live else 'paper:') + sig.tid)
                    news.start()
                engine_class = Engine
                if config.get('execution', {}).get('enabled'):
                    from .active_engine import ActiveEngine
                    engine_class = ActiveEngine
                engine = engine_class(config, sig, refs, journal, RUNTIME, live=live, news=news)
                if args.command == 'recover':
                    if args.wait:
                        from .recovery_wait import prepare_recovery
                        if not prepare_recovery(engine):
                            raise KeyboardInterrupt
                        engine.recovery_stop_guard = True
                    engine.reconcile(replay_unknown=True)
                    output(journal.summary())
                    return
                if not args.once and config.get('research_polling', {}).get('enabled') is True:
                    try:
                        from .poll_collector import PollCollector
                        polls = PollCollector(RUNTIME, config)
                        polls.start()
                    except Exception as poll_error:
                        polls = None
                        print('Polling research worker unavailable: ' + type(poll_error).__name__, file=sys.stderr)
                print('LIVE SIG COMPETITION EXECUTION — automatic orders' if live else 'PAPER SIMULATION — no orders submitted', flush=True)
                from .runner import run_loop
                run_loop(engine, once=args.once, news=news)
            finally:
                original_error = sys.exc_info()[0] is not None
                if polls:
                    try:
                        polls.close()
                    except Exception as poll_error:
                        print('Polling research shutdown error: ' + type(poll_error).__name__, file=sys.stderr)
                try:
                    if engine is not None:
                        try:
                            engine.reconcile()
                        except Exception as cleanup_error:
                            if not original_error:
                                raise
                            print('Shutdown reconciliation incomplete; reservations retained: ' +
                                  str(cleanup_error), file=sys.stderr)
                finally:
                    try:
                        if news:
                            news.close()
                    finally:
                        journal.close()


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('\nStopped. Run status to check for unresolved orders.', file=sys.stderr)
        sys.exit(130)
    except Exception as error:
        print('HALTED: ' + str(error), file=sys.stderr)
        from .supervisor import failure_code, record_halt
        code = failure_code(error)
        record_halt(RUNTIME, error, code)  # Lets the watcher decide whether auto-recovery is safe.
        sys.exit(code)
