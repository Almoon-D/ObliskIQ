# ObliskIQ

An autonomous, risk-first portfolio manager for one Interactive Brokers account.

- **Core** — documented premia held for the long run, in the variant the owner picks from a
  tested after-tax frontier: from global equities bought and held to a risk balance across
  growth and inflation environments, with or without a slow, tax-aware trend tilt.
- **Satellite** — the Asymmetry Hunter: rare, corroborated, defined-risk bets on
  dislocations, sized by evidence.
- **Discipline** — a hard decision, risk and execution layer; a scoreboard that grades every
  proposal; capital that follows tested evidence. No leverage, no shorting, no written
  options, limit-capped orders only.

**Status: pre-alpha. It does not trade yet.** Built so far: configuration, the store and
calendars, the IBKR read and order paths with the kill switch and the pre-trade rails (tested
against fixtures; no order has reached IBKR yet), and the research harness with its first
verdicts in [`research/reports/`](research/reports/). The build proceeds session by session,
paper trading first.

## Start here

| | |
|---|---|
| The design | [`docs/MASTER_PLAN.md`](docs/MASTER_PLAN.md) |
| How work is done here | [`AGENTS.md`](AGENTS.md) |
| Progress and what is open | [`docs/ROADMAP.md`](docs/ROADMAP.md) |
| Why things are as they are | [`docs/DECISIONS.md`](docs/DECISIONS.md) |

```
make setup                       # uv-managed Python 3.14, locked dependencies, hooks
make check                       # format, lint, types, import contracts, tests, coverage
uv run obliskiq config validate  # check config/owner.yaml and the expert defaults
uv run obliskiq settings show    # every setting in force and where it comes from
```

Your settings live in [`config/owner.yaml`](config/owner.yaml); credentials in the
environment ([`.env.example`](.env.example) names them).

Not investment or tax advice. Licensed under the
[GNU Affero General Public License v3.0 or later](LICENSE).
