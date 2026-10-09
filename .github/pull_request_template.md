## Plan

<!-- 5–15 bullets, written before the code. -->

## Changes

## Tests

<!-- What runs, what each failure branch covers; `make check` result. -->

## Live findings

<!-- Observations against IBKR or a data source, tagged with route and date, or "none". -->

## Risks

## Money-path checklist

- [ ] Fails closed: every ambiguity on a money path cancels, refuses or alerts
- [ ] Failure-branch tests for every money path touched
- [ ] No secrets, account ids, tokens or personal data in code, fixtures, logs or this PR
- [ ] No hardcoded values: thresholds, venues, schedules and budgets live in `config/`
- [ ] Every new config key has a consumer
- [ ] Point-in-time reads: every store read is bounded by `as_of`
- [ ] Eligibility respected: nothing ineligible under the active jurisdiction can be bought
- [ ] Paper guard intact: no broker write without a paper-prefixed account under profile `paper`

## Follow-ups

## VERIFY-LIVE

<!-- Checks that need live credentials, also queued in docs/ROADMAP.md, or "none". -->
