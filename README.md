# ObliskIQ

An autonomous portfolio manager for one Interactive Brokers account.

- **The Hunter** — the return engine: a systematic, diversified book of stocks of any size,
  selected by signals that passed a pre-registered test after costs and tax, each tested by
  size tier; long options when their own test allows. It holds nothing until a test passes.
- **The thesis book** — a few concentrated positions on written ideas from an LLM analyst and,
  when he has one, the owner. Every idea states its target, horizon and what would prove it
  wrong before it trades, and is graded; the book's capital follows that record.
- **The reserve** — everything the Hunter does not hold: global equities with a slow,
  tax-aware trend brake.
- **Discipline** — a hard risk and execution layer; capital that moves by tested evidence,
  with no human in the loop. No leverage, no shorting, no written options, limit-capped
  orders only.

**Status: pre-alpha. It does not trade yet.** Built so far: configuration, the store and
calendars, the IBKR read and order paths with the kill switch and the pre-trade rails, the
collectors, the core engine and the research harness with its first verdicts in
[`research/reports/`](research/reports/). No order has filled on paper yet and no signal has
passed its test; [`docs/ROADMAP.md`](docs/ROADMAP.md) says what comes next.

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
