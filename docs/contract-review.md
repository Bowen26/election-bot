# Settlement basis and alternative-party contract review

This read-only review is the first stage of broader party-contract coverage. It does not add mappings to the trading configuration, enable Republican/Independent contracts, change entry thresholds or route orders. Existing live mappings continue under their existing pinned rules and risk limits.

```sh
python3 -m election_bot contract-review
python3 -m election_bot contract-review --race 2026:senate:NE --json
python3 -m election_bot contract-review --race 2026:senate:NE --refresh --json
```

Default reporting reads the saved catalog, any later per-race evidence and the local execution journal. It needs no API key and writes nothing. `--refresh` requires one existing enabled `race_key`; it uses the local SIG credential for SIG GET requests only and public reference endpoints for external data. Evidence is saved under `.runtime/contract-reviews/`, which is gitignored. This is a one-off inspection, not another trading process or background feed. It shares the API account's capacity, so avoid repeated concurrent refresh commands while trading. A report does not need a bot restart.

The saved October 4 catalog contains 115 individual races with 115 Democratic contracts, 115 Republican contracts and three Independent Party contracts. Four chamber-control contracts require a separate review and are excluded from the race grouping. Titles only provide discovery hints. Historical catalog save time does not prove every cached rule was fetched at that time, especially when earlier audit runs resumed from cache. Fresh review evidence records its own timing; other races remain historical.

## Identity and settlement are different checks

The strict identity verifier now supports Democratic and Republican party contracts for Senate, governor and House races. It checks SIG's structured state, race, stage, election date, winner label, office and resolution type, exact external question/rule templates, ticker identity and direct outcome orientation. The default verifier remains Democratic for existing audit scripts. A candidate-only market is not substituted for a party contract. Independent Party contracts require individual attribution review.

Fresh discovery reads actual Kalshi sibling markets using the anchor contract's returned event ticker and cursor pagination, rather than inventing a Republican ticker. It rejects foreign event identities, duplicate tickers and broken pagination. See the official [Kalshi market-list API](https://docs.kalshi.com/api-reference/market/get-markets). Polymarket candidates must come from the configured event and pass exact party-question checks. Each SIG market's full structured resolution is fetched before evaluating it.

Structural identity can pass while settlement equivalence remains unresolved. The basis assessment flags taking office versus election victory, nomination versus party membership, caucus language, runoff scope and missing attribution details in SIG's structured record. Kalshi's accelerated-determination secondary language is retained as a fact; it does not erase the primary taking-office rule. Full contract evidence and its fingerprint remain in the JSON for review. A changed current contract pin is reported explicitly.

`review_score` is an ordinal triage score: identity unverified 50, missing external rules 40, unverified SIG resolution 40, taking-office/winner difference 25, unspecified SIG attribution 20, nominee/membership difference 15, caucus language 15 and runoff scope 10, capped at 100. Forty or more is labelled high priority. These chosen weights are not fitted probabilities, expected losses, cents of edge or evidence that one contract is profitable. `additional_edge_required` stays unknown. New trading authorization and settlement-equivalence verification remain false for every row.

The initial fresh Nebraska Senate review found a useful discrepancy: the SIG title says Independent Party while its structured winner field says Nonpartisan. The report flags that identity mismatch instead of assuming those labels or an external candidate contract are interchangeable.

## Quote comparisons

The report displays each native SIG contract's YES and NO ask and displayed size. NO is derived from that same binary contract's YES bid; it is not silently relabelled as another party's YES. Democratic NO can include Republican, independent and other outcomes under its own rules. All these contracts appear under one race for review, but cross-contract equivalence remains unverified and no automatic cheapest-contract ranking is produced.

JSON includes SIG, Kalshi and Polymarket quote evidence. A diagnostic reference gap is shown only when the party identity is verified, all books were valid, their source/local ages remain within the configured limit, cross-venue source/receipt skew is at most five seconds, and the external midpoints meet the existing agreement limit. It is the lower external outcome bid minus the SIG outcome ask and entry buffer. It is not a trade signal, proof of equivalence, liquidity-adjusted execution result or arbitrage claim. It does not include exit cost. Old or invalid quotes keep their historical prices but their gap is unavailable and freshness is false. Sequential one-off requests can expire before the review finishes; this command never resets timestamps to make a comparison pass.

## One budget per race

The displayed race allowance combines all configured sibling contracts, including disabled mappings with holdings, using open FIFO cost plus net realized losses plus pending reservations. A second party contract does not receive another 250-coin allowance. Missing journals, unimported orders or unassigned inventory produce unknown allowance, not a fresh budget. This is a consistent local journal snapshot, not live account reconciliation; total/daily/directional controls are additional constraints.

The report and ActiveEngine now share race-level commitment accounting. ActiveEngine also applies a shared cooldown, retains historical race assignments and checks budgets during an atomic local reservation. See [shared race controls](shared-race-controls.md). Candidate mappings remain disabled and omit an inferred exposure sign. There is no apply/approve/enable command in this report. The optional [multi-contract mode](multi-contract-routing.md) separately validates explicit settlement reviews and gross exposure before independent serial routing; it remains off in the installed configuration. Do not copy candidate rows into config to bypass this review.

Remaining activation work is to review unresolved full-rule relationships, establish a justified acceptance/exclusion and entry-edge policy for a specific race, then validate that reviewed configuration in paper mode. This report provides inspectable evidence for those steps without expanding positions during discovery.
