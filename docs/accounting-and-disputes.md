# Fill-history accounting and disputed-position flags

## Complete order fill history

After an order is confirmed closed, SIG fill history is read with `limit=100` and followed through `nextCursor` until `hasMore` is false. The returned lifecycle quantity and average price still drive accounting. Every page must agree on order, exchange, tournament, total quantity and average price, and report complete projection coverage. The global projection sequence may advance between pages without invalidating an unchanged order.

Fill IDs must be unique, quantities must have consistent outcome signs, timestamps must include timezones, and the accumulated row quantity must equal the lifecycle total. Missing/repeated cursors, empty intermediate pages, contradictory data or a 100-page limit breach halt completion. A transient GET failure on a later page propagates to the existing retry/reconciliation handling. No partial history releases a reservation, and no retry blindly resubmits an order.

The execution timestamp used for new measurements is the latest fill time across the complete history, regardless of row order. Nonzero executions require timestamped fill rows. Existing executions and markouts are not rewritten; this change improves new imports and completions. It does not reinterpret earlier measurements as corrected historical evidence.

The trading ledger and read-only performance report now share the pure `inventory_from_executions` FIFO reconstruction function. Its input remains execution-time/rowid order, and its lot costs, realized P&L and oversell checks are unchanged. The report no longer constructs a partial Ledger object. The duplicate reservation calculation in the entry-only engine has also been removed.

## Persistent disputed-result review

A fresh, matching `disputed_result` story from an allowlisted source with `pause_on_risk: true` creates a persistent flag keyed by article and mapping. The exchange ID is saved with the flag so renaming a mapping cannot bypass it, and a held position remains visible in reports even if its mapping is subsequently disabled.

This is a heuristic headline classification, **not a verified result or valuation**. The flag retains the source, title, article link and detection time for review. It blocks new entries and automatic exits in the affected race while the news gate is enabled. It does not liquidate holdings, alter prices or change coin/share limits. Requests already in flight can still fill. Ordinary withdrawal and ballot-change stories keep their existing timed-pause behavior.

Flags survive expiration of the normal 15-minute pause, a process restart, a feed recovery and `resume`. Existing active legacy dispute pauses are migrated when the updated news gate starts or the news CLI runs. Expired legacy stories are not blindly revived. Clearing a particular flag is an explicit local review action; feed text cannot issue the command or clear it. A duplicate of the same reviewed article does not recreate that flag, but a new disputed-result article creates a new flag requiring its own review.

Inspect unresolved flags:

```bash
python3 -m election_bot news --status
python3 -m election_bot diagnostics
```

`news --status` lists all `unresolved_disputes`. Both `diagnostics` and `performance` include `position_news_risk`, showing flags on the journal's current holdings with side, quantity, entry cost and source link. These reports do not contact trading APIs. The news and trading journals are separate snapshots; the report states when the news database or new schema is unavailable rather than treating unknown status as no flags.

After reviewing the actual article and resolution rules, use its article ID and exact mapping name. For example, replacing the sample values:

```bash
python3 -m election_bot news --clear-dispute 123 \
  --mapping nh-senate-democratic \
  --note "Reviewed the correction and applicable settlement rules"
```

This records a timestamp and review note, and removes only the timed pause belonging to that same article/mapping. Other disputes, newer pauses, feed-health checks and every existing execution guard remain in force. It does not submit orders or start the bot. The next scan can trade only if all remaining checks pass. Review state is shared by paper and live news readers.

## Remaining P1 work

Per-office directional limits and zero-cost recovery of unfilled paper reservations were completed in the preceding batch. Region-level limits remain a separate change requiring an explicit region grouping and limits; this update does not choose or enable them. P2 strategy analysis and threshold/sizing experiments are also separate.
