# ObliskIQ

An autonomous wealth manager for one Interactive Brokers account.

- **The thesis book** — the return engine: a few concentrated positions on written ideas about
  companies, sectors and the economy from an LLM analyst and, when he has one, the owner, held
  as shares or long options. Every idea states its target, horizon and what would prove it
  wrong before it trades, and is graded; the book may hold all investable capital while that
  record allows.
- **The reserve** — where capital without a thesis waits: global equities with a slow,
  tax-aware trend brake. It sells to fund an approved thesis.
- **The systematic stock book** — a rule-based book of stocks, designed and not built: no test
  of it can yet pass.
- **Discipline** — a hard risk and execution layer; capital that moves by tested evidence,
  with no human in the loop. No leverage, no shorting, no written options, limit-capped
  orders only.

**Status: 1.0.0, built and not yet proven live. It has not filled an order.** The build is
complete: configuration, the store and calendars, the IBKR read and order paths with the kill
switch and the pre-trade rails, the collectors, the reserve's engine, the thesis book and its
analyst, the scoreboard and promotion gate, the cockpit (Claude and Telegram), the deployment
image with updates, backups and rollback, and the research harness with its verdicts in
[`research/reports/`](research/reports/). The failure-mode audit is
[`docs/AUDIT.md`](docs/AUDIT.md). What it has not done: no order has filled on paper, no signal
has passed its test (the Hunter holds nothing), and no idea has been graded yet. What remains
is the owner's, on the machine, with the paper account: the ordered checklist in
[`docs/ROADMAP.md`](docs/ROADMAP.md), then at least eight weeks of autonomous paper before any
decision about real money.

## Start here

| | |
|---|---|
| The design | [`docs/MASTER_PLAN.md`](docs/MASTER_PLAN.md) |
| How work is done here | [`AGENTS.md`](AGENTS.md) |
| Progress and what is open | [`docs/ROADMAP.md`](docs/ROADMAP.md) |
| Running it | [`docs/DEPLOY.md`](docs/DEPLOY.md), [`docs/RUNBOOK.md`](docs/RUNBOOK.md) |
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
