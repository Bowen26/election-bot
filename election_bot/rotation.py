"""Prospective, non-executing capital-rotation comparisons from normal scans."""
from collections import Counter
import copy
import json
from pathlib import Path
import sqlite3
import time
from types import SimpleNamespace
import uuid

from .strategy import D
from .risk import Exposure, LOSS_FIELD
from .race_controls import configured_races, committed_by_race

DEFAULTS = dict(enabled=False, partial_fraction='.25', depth_fraction='.25',
                minimum_improvement_fraction='.03', cooldown_seconds=1800)
HORIZONS = {900: 180, 3600: 900}
MAX_PAIR_DELAY = 120


def settings(config):
    raw = config.get('rotation_study', {})
    if not isinstance(raw, dict) or set(raw)-set(DEFAULTS):
        raise ValueError('Invalid rotation_study settings')
    p = dict(DEFAULTS, **raw)
    if type(p['enabled']) is not bool: raise ValueError('Rotation study enabled must be boolean')
    for k in ('partial_fraction', 'depth_fraction', 'minimum_improvement_fraction'): p[k] = D(p[k])
    if not 0 < p['partial_fraction'] <= D('.25') or not 0 < p['depth_fraction'] <= D('.25'):
        raise ValueError('Rotation study fractions must be positive and at most .25')
    if not 0 < p['minimum_improvement_fraction'] <= 1:
        raise ValueError('Invalid rotation improvement threshold')
    if type(p['cooldown_seconds']) is not int or not 1800 <= p['cooldown_seconds'] <= 86400:
        raise ValueError('Invalid rotation study cooldown')
    if p['enabled'] and not (config.get('execution', {}).get('enabled') and config['execution'].get('sell_enabled')):
        raise ValueError('Rotation study requires active execution with selling')
    return p


def reference(book, refs, side, strategy):
    book.check(strategy['max_age_seconds'], venue='SIG')
    if len(refs) != 2: raise ValueError('Two reference books required')
    for ref in refs: ref.check(strategy['max_age_seconds'], strategy['max_reference_spread'])
    mids = [(r.bids[0][0]+r.asks[0][0])/2 for r in refs]
    if abs(mids[0]-mids[1]) > D(strategy['max_reference_disagreement']):
        raise ValueError('Reference disagreement')
    target = book if side == 'yes' else book.complement()
    aligned = refs if side == 'yes' else [r.complement() for r in refs]
    if any(r.bids[0][1] < D(strategy['min_reference_depth']) for r in aligned):
        raise ValueError('Insufficient reference depth')
    if any(p % D('.005') for p in (target.bids[0][0], target.asks[0][0])):
        raise ValueError('Invalid SIG tick')
    return target, min(r.bids[0][0] for r in aligned)


def simulate_sale(ledger, exchange, side, quantity, price, fee):
    """Copy FIFO inventory for limit checks; never write executions/reservations."""
    holdings, realized = copy.deepcopy(ledger.inventory())
    value = holdings[(exchange, side)]
    remaining, basis, lots = D(quantity), D(0), []
    for count, cost in value['lots']:
        take = min(remaining, count); basis += take*cost; remaining -= take
        if count > take: lots.append([count-take, cost])
    if remaining: raise ValueError('Rotation sale exceeds holdings')
    if lots:
        holdings[(exchange, side)] = dict(quantity=sum(q for q, _ in lots),
                                         cost=sum(q*p for q, p in lots), lots=lots)
    else: holdings.pop((exchange, side))
    pnl = quantity*(price-fee)-basis
    realized[exchange] = realized.get(exchange, D(0))+pnl
    simulated = SimpleNamespace(inventory=lambda: (holdings, realized), journal=ledger.journal)
    return simulated, basis, pnl


class RotationStudy:
    def __init__(self, ledger, config):
        self.ledger, self.db, self.config = ledger, ledger.db, config
        self.policy = settings(config); self.cache = {}; self.epoch = None
        if not self.policy['enabled']: return
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS rotation_proposals (
                id TEXT PRIMARY KEY, at REAL NOT NULL, source TEXT NOT NULL,
                target TEXT NOT NULL, detail TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS rotation_outcomes (
                proposal TEXT NOT NULL, horizon INTEGER NOT NULL, leg TEXT NOT NULL,
                at REAL NOT NULL, status TEXT NOT NULL, value TEXT,
                PRIMARY KEY(proposal,horizon,leg));
            CREATE TABLE IF NOT EXISTS rotation_activity (
                reason TEXT PRIMARY KEY, count INTEGER NOT NULL, last_at REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS rotation_proposals_at ON rotation_proposals(at);
        ''')

    def note(self, reason, now):
        with self.db:
            self.db.execute('INSERT INTO rotation_activity VALUES (?,1,?) ON CONFLICT(reason) DO UPDATE SET count=count+1,last_at=excluded.last_at', (reason, now))

    def observe_outcomes(self, exchange, book, now=None):
        if not self.policy['enabled']: return
        now = time.time() if now is None else now
        book.check(self.config['strategy']['max_age_seconds'], venue='SIG')
        # At most 30 minutes of proposal spacing per involved race keeps this bounded.
        rows = self.db.execute('SELECT * FROM rotation_proposals WHERE at>=? AND at<=? AND (source=? OR target=?)',
                              (now-max(h+w for h,w in HORIZONS.items()), now-min(HORIZONS), exchange, exchange)).fetchall()
        with self.db:
            for row in rows:
                d = json.loads(row['detail']); leg = 'source' if row['source'] == exchange else 'target'
                for horizon, window in HORIZONS.items():
                    due = row['at']+horizon
                    if not due <= now <= due+window: continue
                    if min(book.observed_at, book.source_at) < due: continue
                    if self.db.execute('SELECT 1 FROM rotation_outcomes WHERE proposal=? AND horizon=? AND leg=?', (row['id'], horizon, leg)).fetchone(): continue
                    target = book if d[leg]['side'] == 'yes' else book.complement()
                    price, depth = target.bids[0]; q = D(d[leg]['quantity'])
                    # First valid observation is final, including insufficient depth.
                    status = 'measured' if depth >= q else 'insufficient_depth'
                    value = str(q*(price-D(d['buffer']))) if status == 'measured' else None
                    self.db.execute('INSERT INTO rotation_outcomes VALUES (?,?,?,?,?,?)', (row['id'], horizon, leg, now, status, value))

    def observe(self, mapping, book, refs, snapshot_id, buy_allowed, available, cash,
                blocked=lambda m: False, now=None):
        if not self.policy['enabled']: return
        now = time.time() if now is None else now
        ex = mapping['sig_exchange_id']; strategy = self.config['strategy']; p = self.policy
        epoch = self.db.execute('SELECT MAX(rowid) FROM executions').fetchone()[0]
        if epoch != self.epoch: self.cache.clear(); self.epoch = epoch
        self.cache.pop(ex, None)
        if self.ledger.journal.pending(): self.note('unresolved_order', now); return
        if not buy_allowed: self.note('buy_gate', now); return
        if mapping.get('exposure_mode') == 'gross': self.note('gross_contract_excluded', now); return
        if not mapping.get('enabled') or blocked(mapping): self.note('mapping_or_news', now); return
        self.cache[ex] = dict(mapping=dict(mapping), book=book, refs=list(refs), snapshot=snapshot_id,
                              available=D(available), cash=D(cash), at=now)
        # Old or invalid quotes cannot enter a comparison, even if another race just refreshed.
        valid = {}
        for key, item in self.cache.items():
            if not 0 <= now-item['at'] <= strategy['max_age_seconds'] or blocked(item['mapping']): continue
            try:
                reference(item['book'], item['refs'], 'yes', strategy)
            except ValueError: continue
            valid[key] = item
        self.cache = valid
        if len(valid) < 2: self.note('needs_two_fresh_contracts', now); return
        races = configured_races(self.config)
        recent = self.db.execute('SELECT source,target FROM rotation_proposals WHERE at>?', (now-p['cooldown_seconds'],)).fetchall()
        cooling = {races[e] for row in recent for e in row if e in races}
        candidates = []
        for source, src in valid.items():
            held, _ = self.ledger.held(source)
            if not held or races[source] in cooling: continue
            for target, dst in valid.items():
                if ex not in (source, target): continue
                if races[source] == races[target] or races[target] in cooling: continue
                # This first study rotates into an unowned race, avoiding position-addition guard inference.
                if any(self.ledger.held(k)[0] for k in races if races[k] == races[target]): continue
                for side in ('yes', 'no'):
                    proposal = self.compare(source, src, target, dst, held, side, races)
                    if proposal is not None: candidates.append(proposal)
        if not candidates: self.note('no_qualifying_rotation', now); return
        best = max(candidates, key=lambda d: D(d['proxy_improvement_coins']))
        pid = str(uuid.uuid4()); best.update(id=pid, at=now, version=1, simulation_only=True,
            policy=p, valuation_basis='minimum_external_bid_proxy', execution_epoch=epoch)
        with self.db:
            self.db.execute('INSERT INTO rotation_proposals VALUES (?,?,?,?,?)',
                (pid, now, best['source']['exchange'], best['target']['exchange'], json.dumps(best, default=str)))
        self.note('proposal', now)

    def compare(self, source, src, target, dst, held, side, races, value_provider=None):
        c, p = self.config, self.policy; st = c['strategy']; fee = D(st['cost_buffer_per_share']); limits = c['limits']
        source_side = 'yes' if held > 0 else 'no'
        try:
            if value_provider is None:
                sell, source_ref = reference(src['book'], src['refs'], source_side, st)
                buy, target_ref = reference(dst['book'], dst['refs'], side, st)
            else:
                sell, source_ref = value_provider(src, source_side, 'hold')
                buy, target_ref = value_provider(dst, side, 'buy')
        except ValueError: return None
        bid, depth = sell.bids[0]; ask, ask_depth = buy.asks[0]
        if bid <= fee or target_ref-ask-2*fee < D(st['minimum_edge']): return None
        exposure = Exposure(self.ledger, c)
        sale_cap = exposure.headroom(source, 'sell', source_side) if exposure.enabled else abs(held)
        q = int(min(abs(held)*p['partial_fraction'], depth*p['depth_fraction'],
                    D(limits['per_order'])/bid, D(st['max_shares_per_order']), sale_cap))
        if q < 1: return None
        simulated, basis, pnl = simulate_sale(self.ledger, source, source_side, q, bid, fee)
        holdings, realized = simulated.inventory(); net_loss = max(D(0), -sum(realized.values(), D(0)))
        if LOSS_FIELD in limits and net_loss >= D(limits[LOSS_FIELD])*D(limits['total']): return None
        committed = sum((v['cost'] for v in holdings.values()), D(0))+net_loss
        race_cost = committed_by_race(holdings, realized, [], races)[races[target]]
        proceeds = q*(bid-fee)
        budget = min(proceeds, max(D(0), dst['cash']+proceeds), D(limits['per_order']), D(limits['daily'])-self.ledger.journal.used(today=True),
                     D(limits['total'])-committed, D(limits['per_market'])-race_cost)
        after = Exposure(simulated, c)
        cap = after.headroom(target, 'buy', side) if after.enabled else D(st['max_shares_per_order'])
        buy_q = int(min(max(D(0), budget)/(ask+fee), ask_depth*p['depth_fraction'],
                        buy.bids[0][1]*p['depth_fraction'], D(st['max_shares_per_order']), cap))
        if buy_q < 1: return None
        spend = buy_q*(ask+fee); residual = proceeds-spend
        hold_proxy = q*(source_ref-fee); rotation_proxy = buy_q*(target_ref-fee)+residual
        improvement = rotation_proxy-hold_proxy
        if improvement < proceeds*p['minimum_improvement_fraction']: return None
        return dict(source=dict(exchange=source, race=races[source], side=source_side, quantity=q, bid=bid,
                                proxy=source_ref, bid_depth=depth, snapshot_id=src['snapshot'], captured_at=src['at']),
                    target=dict(exchange=target, race=races[target], side=side, quantity=buy_q, ask=ask,
                                proxy=target_ref, ask_depth=ask_depth, bid_depth=buy.bids[0][1],
                                snapshot_id=dst['snapshot'], captured_at=dst['at']),
                    limits=dict(limits), strategy=dict(st),
                    post_sale_committed=committed, daily_used=self.ledger.journal.used(today=True),
                    buffer=fee, sale_proceeds=proceeds, fifo_basis=basis, sale_realized_pnl=pnl,
                    purchase_cost=spend, residual_cash=residual, proxy_improvement_coins=improvement,
                    proxy_improvement_fraction=improvement/proceeds,
                    currently_fundable_without_sale=spend <= dst['available'])


def report(path, hours=24, now=None):
    now = time.time() if now is None else now
    hours = float(hours)
    if not 0 < hours <= 168: raise ValueError('Rotation lookback must be between 0 and 168 hours')
    result = dict(simulation_only=True, hours=hours, initialized=False, proposals=0, outcomes={}, recent=[], activity=[])
    path = Path(path)
    from .model_rotation import report as model_report
    result['independent_model'] = model_report(path, hours, now)
    if not path.exists(): return result
    db = sqlite3.connect(path.resolve().as_uri()+'?mode=ro', uri=True); db.row_factory = sqlite3.Row
    try:
        db.execute('BEGIN')
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='rotation_proposals'").fetchone(): return result
        result['initialized'] = True
        rows = db.execute('SELECT * FROM rotation_proposals WHERE at>=? AND at<=? ORDER BY at', (now-hours*3600, now)).fetchall()
        result['proposals'] = len(rows)
        result['fundable_without_sale'] = sum(json.loads(r['detail'])['currently_fundable_without_sale'] for r in rows)
        result['activity'] = [dict(r) for r in db.execute('SELECT * FROM rotation_activity ORDER BY last_at DESC')]
        for horizon, window in HORIZONS.items():
            counts = Counter(); deltas = []
            for row in rows:
                d = json.loads(row['detail'])
                legs = {r['leg']: r for r in db.execute('SELECT * FROM rotation_outcomes WHERE proposal=? AND horizon=? AND at<=?', (row['id'], horizon, now))}
                if now < row['at']+horizon: counts['not_due'] += 1; continue
                if len(legs) != 2:
                    counts['missing' if now > row['at']+horizon+window else 'pending'] += 1; continue
                if any(r['status'] != 'measured' for r in legs.values()): counts['insufficient_depth'] += 1; continue
                if abs(legs['source']['at']-legs['target']['at']) > MAX_PAIR_DELAY:
                    counts['unaligned'] += 1; continue
                delta = D(legs['target']['value'])+D(d['residual_cash'])-D(legs['source']['value'])
                deltas.append(delta); counts['measured'] += 1
                if delta > 0: counts['rotation_better'] += 1
                elif delta < 0: counts['holding_better'] += 1
                else: counts['equal'] += 1
            result['outcomes'][str(horizon)] = dict(counts, mean_incremental_coins=str(sum(deltas)/len(deltas)) if deltas else None)
        result['recent'] = [json.loads(r['detail']) for r in rows[-10:]]
        return result
    finally: db.close()


def format_report(r):
    lines = ['Capital rotation study — simulated comparisons only',
             'Initialized: '+str(r['initialized'])+' | Proposals in window: '+str(r['proposals'])]
    for h, values in r['outcomes'].items():
        lines.append(str(int(h)//60)+' min: '+json.dumps(values))
    if r.get('initialized'): lines.append('Already fundable without sale: '+str(r.get('fundable_without_sale', 0)))
    if r['activity']: lines.append('Latest collection result: '+r['activity'][0]['reason'])
    lines.append('Means compare rotation with holding the same source shares; they are not realized profits or a portfolio backtest.')
    from .model_rotation import format_report as format_model_report
    if 'independent_model' in r:lines.append(format_model_report(r['independent_model']))
    return '\n'.join(lines)
