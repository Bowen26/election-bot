# Guard against adding to declining positions

The example configuration leaves `position_entry_guard` disabled, and the local configuration turned it off on October 26, 2026. The guard reads only SIG prices. Since October 15 it had blocked entries that passed the 5¢ edge check while Kalshi and Polymarket held steady: RI Senate up to 12.7¢, HI Governor up to 9.6¢, ID Governor 7.8¢ and MA Governor 7.0¢. The entry rule already stops buying when the lower reference bid falls. The election-night route bypasses the guard either way. This control blocks additional buys in an existing contract when SIG prices show a confirmed decline. It does not sell holdings or block the first purchase in a flat contract, and it does not replace the portfolio drawdown control.

```json
"position_entry_guard": {
  "enabled": true,
  "window_seconds": 1800,
  "sample_spacing_seconds": 60,
  "max_sample_gap_seconds": 600,
  "minimum_span_seconds": 120,
  "decline": "0.02",
  "recovery_distance": "0.01"
}
```

These are provisional risk-control assumptions, not fitted thresholds or evidence of improved returns.

## Entry decisions

An existing position needs at least three ordinary scan observations, at least 60 seconds apart and spanning at least 120 seconds. Any gap exceeding ten minutes breaks the observation chain. Samples older than 30 minutes do not support a new decline decision. Without sufficient recent history, additional buys wait; first purchases in flat contracts still pass the ordinary entry checks.

A decline is confirmed when the last two spaced samples, and the current quote, have **both bid and ask at least two cents below the same earlier observed quote** in the usable chain. Any qualifying earlier quote can trigger the control; a wide-spread outlier cannot mask a different qualifying decline. Spread widening on only one side does not trigger it. YES uses the SIG book directly; NO uses the complemented book. No external venue price sets this trend signal.

After a confirmed decline, further buys stay paused until two later spaced scan observations show both prices at least as high as the saved anchor minus one cent. They must also pass the normal history freshness and span checks. Preflight and transaction checks never create confirmation samples or release a pause. A single bounce cannot release it.

The anchor and pause survive restarts and remain active after the original samples roll out of the window. Partial sales and additional fills do not reset them. A full close followed by a new position starts a new holding episode and new history; ordinary reentry cooldowns still apply. Changing the policy resets usable samples but retains a pause for the same holding episode. Changing the pinned contract identity while a position is open fails validation rather than silently discarding its state.

## Execution integration

Both existing external-price buys and independent-model additions must pass this control, along with the portfolio drawdown gate and all existing limits. Eligible sells retain priority and do not require the entry guard to clear. Existing sell requirements, including external quote validity, news, reconciliation, account inventory and STOP, still apply.

The final buy reservation rechecks the holding episode, current inventory, current SIG quote age and persisted history under the journal write lock. Missing decision context or a proposed opposite-side buy is rejected. State first detected during a rejected reservation is persisted after rollback. This cannot eliminate market movement after the check or prevent an already submitted order from filling.

It uses best bid/ask price history, not a full-depth liquidation model or a news forecast. It can delay worthwhile additions, miss a decline between scans, or permit an addition before the second declining observation. Losing prices may stabilize below the former anchor and leave the position paused indefinitely; this control deliberately does not invent a loss-cutting sale. It applies per contract, not across separately configured sibling contracts or correlated races.

Samples use normal decisions that reach the SIG quote stage. Missing external feeds or news pauses can prevent collection in the current scanner. Missing history blocks additions; it does not waive the guard. Fresh SIG-only risk valuation remains the separate drawdown control's responsibility.

## Load and inspect

Stop the current supervisor with Control-C and wait for it to exit. From the bot folder, restart one supervisor:

```sh
caffeinate -i python3 -m election_bot watch --live
```

Existing holdings initially wait for enough new observations unless usable persisted history exists. Each decision event includes `position_entry_guard` with `buy_allowed`, `reason`, sample count, holding episode, and any paused anchor. Reasons include `position_history_insufficient`, `position_history_clock_reversal`, `position_decline_paused`, and `position_trend_allowed`. This is a distinct gate from `drawdown_paused` or `valuation_incomplete`.

No risk-reduction sale, portfolio reallocation, budget increase or automatic restart is introduced in this increment. A separate position-risk exit remains follow-up work.
