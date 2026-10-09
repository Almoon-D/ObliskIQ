# Legacy snapshot — MANIFEST

Read-only material later sessions port from (AGENTS.md: port from `legacy/`, never from memory). Nothing here is built, linted, type-checked, collected by pytest, measured by coverage, scanned by import-linter or packaged, and nothing imports it. Session 20 deletes `legacy/`.

| Tree | Repository | Commit | Licence | Files | Scope |
|---|---|---|---|---|---|
| `legacy/lite/` | `Almoon-D/ObliskIQ-lite-MCP` | `b1ebd490b439ce22e03de0ca25085a81d4ccd840` | AGPL-3.0 | 314 | whole repository except `.git`, `docs/NEXT-SESSIONS.md`, `docs/archive/` |
| `legacy/old/` | `Almoon-D/Inversor-Inteligente` | `68280e13ad8d935600a593976fb8cce1bda53bfe` | Apache-2.0 | 81 | the MASTER_PLAN §6.2 paths plus the tests and fixtures they need |
| `legacy/pulse/` | `Almoon-D/market-pulse-bot` | `900dbf404320917f41beac5ac56865c238aebbbe` | AGPL-3.0 | 31 | whole repository except `.git`, `deploy/` |

Every recorded commit matches the commit MASTER_PLAN names (`b1ebd49`, `68280e1`, `900dbf4`). Each tree keeps its own `LICENSE` beside it. The Apache-2.0 files in `legacy/old/` are used under that licence, which is compatible with this repository's AGPL-3.0-or-later; ported code keeps attribution.

**Renames.** Legacy agent instructions are neutralised so no tool loads them: every `CLAUDE.md` and `AGENTS.md` becomes `*.legacy.md` and every `.claude/` directory becomes `_claude/`. Lite's `ROADMAP.md` is kept as `ROADMAP.legacy.md` (its live measurements). The *Original path* column gives the name in the source repository.

**Actions.** `port` — later sessions port it (with its tests) into *Target*; `reference` — read for behaviour or constants, not ported; `knowledge` — condensed into `docs/knowledge/` or `docs/DECISIONS.md`; `registry` — definitions only, into the pattern registry. Targets are packages under `src/obliskiq/` unless a path says otherwise; MASTER_PLAN §6 is the authority.

## `legacy/lite/` — Almoon-D/ObliskIQ-lite-MCP @ `b1ebd49`

| Legacy path | Repository | SHA | Original path | Licence | Target | Action |
|---|---|---|---|---|---|---|
| `legacy/lite/_claude/skills/obliskiq-lite/SKILL.md` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `.claude/skills/obliskiq-lite/SKILL.md` | AGPL-3.0 | `.claude/skills/obliskiq-cockpit/` | port |
| `legacy/lite/.dockerignore` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `.dockerignore` | AGPL-3.0 | `deploy/` | port |
| `legacy/lite/.env.template` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `.env.template` | AGPL-3.0 | — | reference |
| `legacy/lite/.github/workflows/ci.yml` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `.github/workflows/ci.yml` | AGPL-3.0 | — | reference |
| `legacy/lite/.gitignore` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `.gitignore` | AGPL-3.0 | — | reference |
| `legacy/lite/BUILD-BRIEF.md` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `BUILD-BRIEF.md` | AGPL-3.0 | — | reference |
| `legacy/lite/CLAUDE.legacy.md` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `CLAUDE.md` | AGPL-3.0 | — | reference |
| `legacy/lite/Dockerfile` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `Dockerfile` | AGPL-3.0 | `deploy/` | port |
| `legacy/lite/LICENSE` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `LICENSE` | AGPL-3.0 | — | reference |
| `legacy/lite/README.md` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `README.md` | AGPL-3.0 | — | reference |
| `legacy/lite/ROADMAP.legacy.md` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `ROADMAP.md` | AGPL-3.0 | `docs/knowledge/` (live measurements) | knowledge |
| `legacy/lite/calendars.yaml` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `calendars.yaml` | AGPL-3.0 | `config/` (defaults, owner, universe, jurisdictions) | port |
| `legacy/lite/config.yaml` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `config.yaml` | AGPL-3.0 | `config/` (defaults, owner, universe, jurisdictions) | port |
| `legacy/lite/deploy/collect.env.example` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `deploy/collect.env.example` | AGPL-3.0 | `deploy/` | port |
| `legacy/lite/deploy/compute.env.example` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `deploy/compute.env.example` | AGPL-3.0 | `deploy/` | port |
| `legacy/lite/deploy/crontab.example` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `deploy/crontab.example` | AGPL-3.0 | `deploy/` | port |
| `legacy/lite/deploy/obliskiq-collect@.service` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `deploy/obliskiq-collect@.service` | AGPL-3.0 | `deploy/` | port |
| `legacy/lite/deploy/obliskiq-collect@daily.timer` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `deploy/obliskiq-collect@daily.timer` | AGPL-3.0 | `deploy/` | port |
| `legacy/lite/deploy/obliskiq-collect@macro.timer` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `deploy/obliskiq-collect@macro.timer` | AGPL-3.0 | `deploy/` | port |
| `legacy/lite/deploy/obliskiq-collect@universe.timer` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `deploy/obliskiq-collect@universe.timer` | AGPL-3.0 | `deploy/` | port |
| `legacy/lite/deploy/obliskiq-compute@.service` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `deploy/obliskiq-compute@.service` | AGPL-3.0 | `deploy/` | port |
| `legacy/lite/deploy/obliskiq-compute@all.timer` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `deploy/obliskiq-compute@all.timer` | AGPL-3.0 | `deploy/` | port |
| `legacy/lite/deploy/obliskiq-compute@census.timer` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `deploy/obliskiq-compute@census.timer` | AGPL-3.0 | `deploy/` | port |
| `legacy/lite/deploy/obliskiq-mcp.service` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `deploy/obliskiq-mcp.service` | AGPL-3.0 | `deploy/` | port |
| `legacy/lite/deploy/server.env.example` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `deploy/server.env.example` | AGPL-3.0 | `deploy/` | port |
| `legacy/lite/docs/COLLECTORS.md` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `docs/COLLECTORS.md` | AGPL-3.0 | `docs/DECISIONS.md` (condensed), `docs/knowledge/` | knowledge |
| `legacy/lite/docs/COMPUTE.md` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `docs/COMPUTE.md` | AGPL-3.0 | `docs/DECISIONS.md` (condensed), `docs/knowledge/` | knowledge |
| `legacy/lite/docs/DECISIONS.md` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `docs/DECISIONS.md` | AGPL-3.0 | `docs/DECISIONS.md` (condensed), `docs/knowledge/` | knowledge |
| `legacy/lite/docs/DEPLOY.md` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `docs/DEPLOY.md` | AGPL-3.0 | `docs/DECISIONS.md` (condensed), `docs/knowledge/` | knowledge |
| `legacy/lite/docs/GOOGLE-CLOUD.md` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `docs/GOOGLE-CLOUD.md` | AGPL-3.0 | `docs/DECISIONS.md` (condensed), `docs/knowledge/` | knowledge |
| `legacy/lite/docs/ORACLE-CLOUD.md` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `docs/ORACLE-CLOUD.md` | AGPL-3.0 | `docs/DECISIONS.md` (condensed), `docs/knowledge/` | knowledge |
| `legacy/lite/docs/SERVER.md` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `docs/SERVER.md` | AGPL-3.0 | `docs/DECISIONS.md` (condensed), `docs/knowledge/` | knowledge |
| `legacy/lite/pyproject.toml` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `pyproject.toml` | AGPL-3.0 | — | reference |
| `legacy/lite/references/commodities-universe.md` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `references/commodities-universe.md` | AGPL-3.0 | `docs/knowledge/` | knowledge |
| `legacy/lite/references/ibkr-playbook.md` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `references/ibkr-playbook.md` | AGPL-3.0 | `docs/knowledge/` | knowledge |
| `legacy/lite/references/macro-playbook.md` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `references/macro-playbook.md` | AGPL-3.0 | `docs/knowledge/` | knowledge |
| `legacy/lite/references/marketdata-playbook.md` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `references/marketdata-playbook.md` | AGPL-3.0 | `docs/knowledge/` | knowledge |
| `legacy/lite/requirements.txt` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `requirements.txt` | AGPL-3.0 | — | reference |
| `legacy/lite/scripts/build_mcp_capture.py` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `scripts/build_mcp_capture.py` | AGPL-3.0 | optional | reference |
| `legacy/lite/secrets/.gitignore` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `secrets/.gitignore` | AGPL-3.0 | — | reference |
| `legacy/lite/src/obliskiq/__init__.py` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `src/obliskiq/__init__.py` | AGPL-3.0 | — | reference |
| `legacy/lite/src/obliskiq/collect/__init__.py` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `src/obliskiq/collect/__init__.py` | AGPL-3.0 | `cli/` | reference |
| `legacy/lite/src/obliskiq/collect/backfill.py` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `src/obliskiq/collect/backfill.py` | AGPL-3.0 | `data/market/` | port |
| `legacy/lite/src/obliskiq/collect/chain_collector.py` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `src/obliskiq/collect/chain_collector.py` | AGPL-3.0 | `data/market/` | port |
| `legacy/lite/src/obliskiq/collect/cli.py` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `src/obliskiq/collect/cli.py` | AGPL-3.0 | `cli/` | reference |
| `legacy/lite/src/obliskiq/collect/curve_collector.py` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `src/obliskiq/collect/curve_collector.py` | AGPL-3.0 | `data/market/` | port |
| `legacy/lite/src/obliskiq/collect/macro_pull.py` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `src/obliskiq/collect/macro_pull.py` | AGPL-3.0 | `data/macro/` | port |
| `legacy/lite/src/obliskiq/collect/mcp_capture.py` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `src/obliskiq/collect/mcp_capture.py` | AGPL-3.0 | optional | reference |
| `legacy/lite/src/obliskiq/collect/nav.py` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `src/obliskiq/collect/nav.py` | AGPL-3.0 | `orchestration/` | port |
| `legacy/lite/src/obliskiq/collect/option_vendor.py` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `src/obliskiq/collect/option_vendor.py` | AGPL-3.0 | optional | reference |
| `legacy/lite/src/obliskiq/collect/retry.py` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `src/obliskiq/collect/retry.py` | AGPL-3.0 | `data/market/` | port |
| `legacy/lite/src/obliskiq/collect/runlog.py` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `src/obliskiq/collect/runlog.py` | AGPL-3.0 | `orchestration/` | port |
| `legacy/lite/src/obliskiq/collect/sequence.py` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `src/obliskiq/collect/sequence.py` | AGPL-3.0 | `orchestration/` | port |
| `legacy/lite/src/obliskiq/collect/source.py` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `src/obliskiq/collect/source.py` | AGPL-3.0 | `data/market/` | port |
| `legacy/lite/src/obliskiq/collect/state.py` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `src/obliskiq/collect/state.py` | AGPL-3.0 | `orchestration/` | port |
| `legacy/lite/src/obliskiq/collect/status.py` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `src/obliskiq/collect/status.py` | AGPL-3.0 | `orchestration/` | port |
| `legacy/lite/src/obliskiq/collect/strikes.py` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `src/obliskiq/collect/strikes.py` | AGPL-3.0 | `data/market/` | port |
| `legacy/lite/src/obliskiq/collect/universe.py` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `src/obliskiq/collect/universe.py` | AGPL-3.0 | `data/market/` | port |
| `legacy/lite/src/obliskiq/collect/vol.py` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `src/obliskiq/collect/vol.py` | AGPL-3.0 | `data/market/` | port |
| `legacy/lite/src/obliskiq/compute/__init__.py` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `src/obliskiq/compute/__init__.py` | AGPL-3.0 | `cli/` | reference |
| `legacy/lite/src/obliskiq/compute/build_grid.py` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `src/obliskiq/compute/build_grid.py` | AGPL-3.0 | `grid/` | port |
| `legacy/lite/src/obliskiq/compute/calendar.py` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `src/obliskiq/compute/calendar.py` | AGPL-3.0 | `calendars/` | port |
| `legacy/lite/src/obliskiq/compute/catalysts.py` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `src/obliskiq/compute/catalysts.py` | AGPL-3.0 | `engines/` | port |
| `legacy/lite/src/obliskiq/compute/cli.py` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `src/obliskiq/compute/cli.py` | AGPL-3.0 | `cli/` | reference |
| `legacy/lite/src/obliskiq/compute/commodity_engine.py` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `src/obliskiq/compute/commodity_engine.py` | AGPL-3.0 | `engines/` | port |
| `legacy/lite/src/obliskiq/compute/frames.py` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `src/obliskiq/compute/frames.py` | AGPL-3.0 | `grid/` | port |
| `legacy/lite/src/obliskiq/compute/fx_engine.py` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `src/obliskiq/compute/fx_engine.py` | AGPL-3.0 | `engines/` | port |
| `legacy/lite/src/obliskiq/compute/gates.py` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `src/obliskiq/compute/gates.py` | AGPL-3.0 | `decision/` | port |
| `legacy/lite/src/obliskiq/compute/indicators.py` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `src/obliskiq/compute/indicators.py` | AGPL-3.0 | `grid/` | port |
| `legacy/lite/src/obliskiq/compute/iv_solve.py` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `src/obliskiq/compute/iv_solve.py` | AGPL-3.0 | `quant/` | port |
| `legacy/lite/src/obliskiq/compute/penalty.py` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `src/obliskiq/compute/penalty.py` | AGPL-3.0 | `engines/` | port |
| `legacy/lite/src/obliskiq/compute/rankings.py` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `src/obliskiq/compute/rankings.py` | AGPL-3.0 | `grid/` | port |
| `legacy/lite/src/obliskiq/compute/risk_engine.py` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `src/obliskiq/compute/risk_engine.py` | AGPL-3.0 | `engines/` | port |
| `legacy/lite/src/obliskiq/compute/screen.py` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `src/obliskiq/compute/screen.py` | AGPL-3.0 | `decision/` | port |
| `legacy/lite/src/obliskiq/compute/smile.py` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `src/obliskiq/compute/smile.py` | AGPL-3.0 | `quant/` | port |
| `legacy/lite/src/obliskiq/compute/stats.py` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `src/obliskiq/compute/stats.py` | AGPL-3.0 | `quant/` | port |
| `legacy/lite/src/obliskiq/compute/universe_census.py` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `src/obliskiq/compute/universe_census.py` | AGPL-3.0 | `grid/` | port |
| `legacy/lite/src/obliskiq/config.py` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `src/obliskiq/config.py` | AGPL-3.0 | `config/` (replaced by the Pydantic loader, Session 1) | reference |
| `legacy/lite/src/obliskiq/env.py` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `src/obliskiq/env.py` | AGPL-3.0 | `obliskiq/` root modules (Session 1) | port |
| `legacy/lite/src/obliskiq/errors.py` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `src/obliskiq/errors.py` | AGPL-3.0 | `obliskiq/` root modules (Session 1) | port |
| `legacy/lite/src/obliskiq/ibkr/__init__.py` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `src/obliskiq/ibkr/__init__.py` | AGPL-3.0 | `ibkr/` | reference |
| `legacy/lite/src/obliskiq/ibkr/connector.py` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `src/obliskiq/ibkr/connector.py` | AGPL-3.0 | optional | reference |
| `legacy/lite/src/obliskiq/ibkr/cp_client.py` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `src/obliskiq/ibkr/cp_client.py` | AGPL-3.0 | `ibkr/` (orders added) | port |
| `legacy/lite/src/obliskiq/ibkr/oauth.py` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `src/obliskiq/ibkr/oauth.py` | AGPL-3.0 | `ibkr/` (orders added) | port |
| `legacy/lite/src/obliskiq/ibkr/preflight.py` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `src/obliskiq/ibkr/preflight.py` | AGPL-3.0 | `ibkr/` (orders added) | port |
| `legacy/lite/src/obliskiq/ibkr/session.py` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `src/obliskiq/ibkr/session.py` | AGPL-3.0 | `ibkr/` (orders added) | port |
| `legacy/lite/src/obliskiq/instruments.py` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `src/obliskiq/instruments.py` | AGPL-3.0 | `jurisdiction/`, `execution/` | port |
| `legacy/lite/src/obliskiq/logging_setup.py` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `src/obliskiq/logging_setup.py` | AGPL-3.0 | `obliskiq/` root modules (Session 1) | port |
| `legacy/lite/src/obliskiq/macro/__init__.py` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `src/obliskiq/macro/__init__.py` | AGPL-3.0 | — | reference |
| `legacy/lite/src/obliskiq/macro/fetchers.py` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `src/obliskiq/macro/fetchers.py` | AGPL-3.0 | `data/macro/` | port |
| `legacy/lite/src/obliskiq/macro/rates.py` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `src/obliskiq/macro/rates.py` | AGPL-3.0 | `data/macro/` | port |
| `legacy/lite/src/obliskiq/macro/series.py` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `src/obliskiq/macro/series.py` | AGPL-3.0 | `data/macro/` | port |
| `legacy/lite/src/obliskiq/py.typed` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `src/obliskiq/py.typed` | AGPL-3.0 | — | reference |
| `legacy/lite/src/obliskiq/quant/__init__.py` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `src/obliskiq/quant/__init__.py` | AGPL-3.0 | — | reference |
| `legacy/lite/src/obliskiq/quant/bsm.py` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `src/obliskiq/quant/bsm.py` | AGPL-3.0 | `quant/` | port |
| `legacy/lite/src/obliskiq/server/__init__.py` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `src/obliskiq/server/__init__.py` | AGPL-3.0 | `server/` | port |
| `legacy/lite/src/obliskiq/server/app.py` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `src/obliskiq/server/app.py` | AGPL-3.0 | `server/` | port |
| `legacy/lite/src/obliskiq/server/auth.py` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `src/obliskiq/server/auth.py` | AGPL-3.0 | `server/` | port |
| `legacy/lite/src/obliskiq/server/cli.py` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `src/obliskiq/server/cli.py` | AGPL-3.0 | `server/` | port |
| `legacy/lite/src/obliskiq/server/collectors.py` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `src/obliskiq/server/collectors.py` | AGPL-3.0 | `server/` | port |
| `legacy/lite/src/obliskiq/server/context.py` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `src/obliskiq/server/context.py` | AGPL-3.0 | `server/` | port |
| `legacy/lite/src/obliskiq/server/encode.py` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `src/obliskiq/server/encode.py` | AGPL-3.0 | `server/` | port |
| `legacy/lite/src/obliskiq/server/guards.py` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `src/obliskiq/server/guards.py` | AGPL-3.0 | `server/` | port |
| `legacy/lite/src/obliskiq/server/tools/__init__.py` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `src/obliskiq/server/tools/__init__.py` | AGPL-3.0 | `server/` | port |
| `legacy/lite/src/obliskiq/server/tools/base.py` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `src/obliskiq/server/tools/base.py` | AGPL-3.0 | `server/` | port |
| `legacy/lite/src/obliskiq/server/tools/catalysts.py` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `src/obliskiq/server/tools/catalysts.py` | AGPL-3.0 | `server/` | port |
| `legacy/lite/src/obliskiq/server/tools/commodity.py` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `src/obliskiq/server/tools/commodity.py` | AGPL-3.0 | `server/` | port |
| `legacy/lite/src/obliskiq/server/tools/fx.py` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `src/obliskiq/server/tools/fx.py` | AGPL-3.0 | `server/` | port |
| `legacy/lite/src/obliskiq/server/tools/grid.py` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `src/obliskiq/server/tools/grid.py` | AGPL-3.0 | `server/` | port |
| `legacy/lite/src/obliskiq/server/tools/ledger.py` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `src/obliskiq/server/tools/ledger.py` | AGPL-3.0 | `server/` | port |
| `legacy/lite/src/obliskiq/server/tools/marketdata.py` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `src/obliskiq/server/tools/marketdata.py` | AGPL-3.0 | `server/` | port |
| `legacy/lite/src/obliskiq/server/tools/operations.py` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `src/obliskiq/server/tools/operations.py` | AGPL-3.0 | `server/` | port |
| `legacy/lite/src/obliskiq/server/tools/portfolio.py` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `src/obliskiq/server/tools/portfolio.py` | AGPL-3.0 | `server/` | port |
| `legacy/lite/src/obliskiq/server/tools/risk.py` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `src/obliskiq/server/tools/risk.py` | AGPL-3.0 | `server/` | port |
| `legacy/lite/src/obliskiq/server/tools/screen.py` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `src/obliskiq/server/tools/screen.py` | AGPL-3.0 | `server/` | port |
| `legacy/lite/src/obliskiq/server/tools/sleeve.py` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `src/obliskiq/server/tools/sleeve.py` | AGPL-3.0 | `server/` | port |
| `legacy/lite/src/obliskiq/store/__init__.py` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `src/obliskiq/store/__init__.py` | AGPL-3.0 | `store/` (fresh migration chain) | port |
| `legacy/lite/src/obliskiq/store/db.py` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `src/obliskiq/store/db.py` | AGPL-3.0 | `store/` (fresh migration chain) | port |
| `legacy/lite/src/obliskiq/store/ledger.py` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `src/obliskiq/store/ledger.py` | AGPL-3.0 | `store/` (fresh migration chain) | port |
| `legacy/lite/src/obliskiq/store/migrations/001_initial.sql` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `src/obliskiq/store/migrations/001_initial.sql` | AGPL-3.0 | `store/` (fresh migration chain) | port |
| `legacy/lite/src/obliskiq/store/migrations/002_collectors.sql` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `src/obliskiq/store/migrations/002_collectors.sql` | AGPL-3.0 | `store/` (fresh migration chain) | port |
| `legacy/lite/src/obliskiq/store/migrations/003_compute.sql` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `src/obliskiq/store/migrations/003_compute.sql` | AGPL-3.0 | `store/` (fresh migration chain) | port |
| `legacy/lite/src/obliskiq/store/migrations/004_run_status_skipped.sql` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `src/obliskiq/store/migrations/004_run_status_skipped.sql` | AGPL-3.0 | `store/` (fresh migration chain) | port |
| `legacy/lite/src/obliskiq/store/migrations/005_shared_sleeve_pool.sql` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `src/obliskiq/store/migrations/005_shared_sleeve_pool.sql` | AGPL-3.0 | `store/` (fresh migration chain) | port |
| `legacy/lite/src/obliskiq/store/migrations/006_market_data_status.sql` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `src/obliskiq/store/migrations/006_market_data_status.sql` | AGPL-3.0 | `store/` (fresh migration chain) | port |
| `legacy/lite/src/obliskiq/store/migrations/007_grid_observations.sql` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `src/obliskiq/store/migrations/007_grid_observations.sql` | AGPL-3.0 | `store/` (fresh migration chain) | port |
| `legacy/lite/src/obliskiq/store/migrations/008_option_vendor.sql` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `src/obliskiq/store/migrations/008_option_vendor.sql` | AGPL-3.0 | `store/` (fresh migration chain) | port |
| `legacy/lite/src/obliskiq/store/migrations/009_dividend_and_data_quality.sql` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `src/obliskiq/store/migrations/009_dividend_and_data_quality.sql` | AGPL-3.0 | `store/` (fresh migration chain) | port |
| `legacy/lite/src/obliskiq/store/migrations/010_inapplicable_cells.sql` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `src/obliskiq/store/migrations/010_inapplicable_cells.sql` | AGPL-3.0 | `store/` (fresh migration chain) | port |
| `legacy/lite/src/obliskiq/store/migrations/011_position_terms_and_currency.sql` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `src/obliskiq/store/migrations/011_position_terms_and_currency.sql` | AGPL-3.0 | `store/` (fresh migration chain) | port |
| `legacy/lite/src/obliskiq/store/migrations/012_ranking_provenance.sql` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `src/obliskiq/store/migrations/012_ranking_provenance.sql` | AGPL-3.0 | `store/` (fresh migration chain) | port |
| `legacy/lite/src/obliskiq/store/migrations/013_run_continuity.sql` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `src/obliskiq/store/migrations/013_run_continuity.sql` | AGPL-3.0 | `store/` (fresh migration chain) | port |
| `legacy/lite/src/obliskiq/store/migrations/014_ranking_reach.sql` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `src/obliskiq/store/migrations/014_ranking_reach.sql` | AGPL-3.0 | `store/` (fresh migration chain) | port |
| `legacy/lite/src/obliskiq/store/migrations/015_cot_share_of_open_interest.sql` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `src/obliskiq/store/migrations/015_cot_share_of_open_interest.sql` | AGPL-3.0 | `store/` (fresh migration chain) | port |
| `legacy/lite/src/obliskiq/store/migrations/016_ranking_trajectory.sql` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `src/obliskiq/store/migrations/016_ranking_trajectory.sql` | AGPL-3.0 | `store/` (fresh migration chain) | port |
| `legacy/lite/src/obliskiq/store/migrations/017_dividend_yield_and_solved_iv.sql` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `src/obliskiq/store/migrations/017_dividend_yield_and_solved_iv.sql` | AGPL-3.0 | `store/` (fresh migration chain) | port |
| `legacy/lite/src/obliskiq/store/migrations/018_option_exchange.sql` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `src/obliskiq/store/migrations/018_option_exchange.sql` | AGPL-3.0 | `store/` (fresh migration chain) | port |
| `legacy/lite/src/obliskiq/store/migrations/019_cross_asset_correlation.sql` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `src/obliskiq/store/migrations/019_cross_asset_correlation.sql` | AGPL-3.0 | `store/` (fresh migration chain) | port |
| `legacy/lite/src/obliskiq/store/nav.py` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `src/obliskiq/store/nav.py` | AGPL-3.0 | `store/` (fresh migration chain) | port |
| `legacy/lite/src/obliskiq/util/__init__.py` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `src/obliskiq/util/__init__.py` | AGPL-3.0 | `util/` (Session 1) | port |
| `legacy/lite/src/obliskiq/util/http.py` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `src/obliskiq/util/http.py` | AGPL-3.0 | `util/` (Session 1) | port |
| `legacy/lite/src/obliskiq/util/numeric.py` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `src/obliskiq/util/numeric.py` | AGPL-3.0 | `util/` (Session 1) | port |
| `legacy/lite/src/obliskiq/util/timeutil.py` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `src/obliskiq/util/timeutil.py` | AGPL-3.0 | `util/` (Session 1) | port |
| `legacy/lite/tests/conftest.py` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/conftest.py` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/fixtures/build_fixtures.py` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/fixtures/build_fixtures.py` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/fixtures/ibkr/bars/1d.json` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/fixtures/ibkr/bars/1d.json` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/fixtures/ibkr/bars/1m.json` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/fixtures/ibkr/bars/1m.json` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/fixtures/ibkr/bars/1w.json` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/fixtures/ibkr/bars/1w.json` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/fixtures/ibkr/bars/320227571_1d.json` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/fixtures/ibkr/bars/320227571_1d.json` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/fixtures/ibkr/bars/4046_1d.json` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/fixtures/ibkr/bars/4046_1d.json` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/fixtures/ibkr/bars/756733_1d.json` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/fixtures/ibkr/bars/756733_1d.json` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/fixtures/ibkr/bars/8158_1d.json` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/fixtures/ibkr/bars/8158_1d.json` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/fixtures/ibkr/conids.json` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/fixtures/ibkr/conids.json` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/fixtures/ibkr/contracts/320227571_APR26_C.json` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/fixtures/ibkr/contracts/320227571_APR26_C.json` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/fixtures/ibkr/contracts/320227571_APR26_P.json` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/fixtures/ibkr/contracts/320227571_APR26_P.json` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/fixtures/ibkr/contracts/320227571_DEC26_C.json` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/fixtures/ibkr/contracts/320227571_DEC26_C.json` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/fixtures/ibkr/contracts/320227571_DEC26_P.json` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/fixtures/ibkr/contracts/320227571_DEC26_P.json` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/fixtures/ibkr/contracts/320227571_JAN26_C.json` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/fixtures/ibkr/contracts/320227571_JAN26_C.json` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/fixtures/ibkr/contracts/320227571_JAN26_P.json` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/fixtures/ibkr/contracts/320227571_JAN26_P.json` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/fixtures/ibkr/contracts/320227571_JUN26_C.json` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/fixtures/ibkr/contracts/320227571_JUN26_C.json` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/fixtures/ibkr/contracts/320227571_JUN26_P.json` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/fixtures/ibkr/contracts/320227571_JUN26_P.json` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/fixtures/ibkr/contracts/4046_APR26_C.json` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/fixtures/ibkr/contracts/4046_APR26_C.json` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/fixtures/ibkr/contracts/4046_APR26_P.json` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/fixtures/ibkr/contracts/4046_APR26_P.json` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/fixtures/ibkr/contracts/4046_DEC26_C.json` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/fixtures/ibkr/contracts/4046_DEC26_C.json` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/fixtures/ibkr/contracts/4046_DEC26_P.json` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/fixtures/ibkr/contracts/4046_DEC26_P.json` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/fixtures/ibkr/contracts/4046_JAN26_C.json` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/fixtures/ibkr/contracts/4046_JAN26_C.json` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/fixtures/ibkr/contracts/4046_JAN26_P.json` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/fixtures/ibkr/contracts/4046_JAN26_P.json` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/fixtures/ibkr/contracts/4046_JUN26_C.json` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/fixtures/ibkr/contracts/4046_JUN26_C.json` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/fixtures/ibkr/contracts/4046_JUN26_P.json` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/fixtures/ibkr/contracts/4046_JUN26_P.json` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/fixtures/ibkr/contracts/756733_APR26_C.json` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/fixtures/ibkr/contracts/756733_APR26_C.json` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/fixtures/ibkr/contracts/756733_APR26_P.json` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/fixtures/ibkr/contracts/756733_APR26_P.json` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/fixtures/ibkr/contracts/756733_DEC26_C.json` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/fixtures/ibkr/contracts/756733_DEC26_C.json` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/fixtures/ibkr/contracts/756733_DEC26_P.json` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/fixtures/ibkr/contracts/756733_DEC26_P.json` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/fixtures/ibkr/contracts/756733_JAN26_C.json` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/fixtures/ibkr/contracts/756733_JAN26_C.json` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/fixtures/ibkr/contracts/756733_JAN26_P.json` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/fixtures/ibkr/contracts/756733_JAN26_P.json` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/fixtures/ibkr/contracts/756733_JUN26_C.json` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/fixtures/ibkr/contracts/756733_JUN26_C.json` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/fixtures/ibkr/contracts/756733_JUN26_P.json` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/fixtures/ibkr/contracts/756733_JUN26_P.json` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/fixtures/ibkr/contracts/8158_APR26_C.json` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/fixtures/ibkr/contracts/8158_APR26_C.json` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/fixtures/ibkr/contracts/8158_APR26_P.json` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/fixtures/ibkr/contracts/8158_APR26_P.json` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/fixtures/ibkr/contracts/8158_DEC26_C.json` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/fixtures/ibkr/contracts/8158_DEC26_C.json` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/fixtures/ibkr/contracts/8158_DEC26_P.json` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/fixtures/ibkr/contracts/8158_DEC26_P.json` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/fixtures/ibkr/contracts/8158_JAN26_C.json` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/fixtures/ibkr/contracts/8158_JAN26_C.json` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/fixtures/ibkr/contracts/8158_JAN26_P.json` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/fixtures/ibkr/contracts/8158_JAN26_P.json` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/fixtures/ibkr/contracts/8158_JUN26_C.json` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/fixtures/ibkr/contracts/8158_JUN26_C.json` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/fixtures/ibkr/contracts/8158_JUN26_P.json` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/fixtures/ibkr/contracts/8158_JUN26_P.json` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/fixtures/ibkr/futures.json` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/fixtures/ibkr/futures.json` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/fixtures/ibkr/ledger.json` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/fixtures/ibkr/ledger.json` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/fixtures/ibkr/oauth/README.md` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/fixtures/ibkr/oauth/README.md` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/fixtures/ibkr/oauth/authorization_server.json` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/fixtures/ibkr/oauth/authorization_server.json` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/fixtures/ibkr/oauth/protected_resource.json` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/fixtures/ibkr/oauth/protected_resource.json` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/fixtures/ibkr/option_months.json` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/fixtures/ibkr/option_months.json` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/fixtures/ibkr/positions.json` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/fixtures/ibkr/positions.json` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/fixtures/ibkr/quotes.json` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/fixtures/ibkr/quotes.json` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/fixtures/ibkr/strikes/320227571_APR26.json` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/fixtures/ibkr/strikes/320227571_APR26.json` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/fixtures/ibkr/strikes/320227571_DEC26.json` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/fixtures/ibkr/strikes/320227571_DEC26.json` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/fixtures/ibkr/strikes/320227571_JAN26.json` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/fixtures/ibkr/strikes/320227571_JAN26.json` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/fixtures/ibkr/strikes/320227571_JUN26.json` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/fixtures/ibkr/strikes/320227571_JUN26.json` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/fixtures/ibkr/strikes/4046_APR26.json` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/fixtures/ibkr/strikes/4046_APR26.json` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/fixtures/ibkr/strikes/4046_DEC26.json` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/fixtures/ibkr/strikes/4046_DEC26.json` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/fixtures/ibkr/strikes/4046_JAN26.json` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/fixtures/ibkr/strikes/4046_JAN26.json` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/fixtures/ibkr/strikes/4046_JUN26.json` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/fixtures/ibkr/strikes/4046_JUN26.json` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/fixtures/ibkr/strikes/756733_APR26.json` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/fixtures/ibkr/strikes/756733_APR26.json` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/fixtures/ibkr/strikes/756733_DEC26.json` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/fixtures/ibkr/strikes/756733_DEC26.json` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/fixtures/ibkr/strikes/756733_JAN26.json` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/fixtures/ibkr/strikes/756733_JAN26.json` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/fixtures/ibkr/strikes/756733_JUN26.json` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/fixtures/ibkr/strikes/756733_JUN26.json` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/fixtures/ibkr/strikes/8158_APR26.json` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/fixtures/ibkr/strikes/8158_APR26.json` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/fixtures/ibkr/strikes/8158_DEC26.json` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/fixtures/ibkr/strikes/8158_DEC26.json` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/fixtures/ibkr/strikes/8158_JAN26.json` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/fixtures/ibkr/strikes/8158_JAN26.json` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/fixtures/ibkr/strikes/8158_JUN26.json` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/fixtures/ibkr/strikes/8158_JUN26.json` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/fixtures/macro/README.md` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/fixtures/macro/README.md` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/fixtures/macro/fred_hicp_de_initial_release.json` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/fixtures/macro/fred_hicp_de_initial_release.json` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/fixtures/macro/fred_hicp_de_latest_vintage.json` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/fixtures/macro/fred_hicp_de_latest_vintage.json` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/fixtures/macro/fred_hicp_de_series.json` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/fixtures/macro/fred_hicp_de_series.json` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/fixtures/macro/ons_d7g7_mm23.json` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/fixtures/macro/ons_d7g7_mm23.json` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/fixtures/macro/statcan_v41690973.json` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/fixtures/macro/statcan_v41690973.json` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/fixtures/mcp_capture/connector_2026_09_03/manifest.json` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/fixtures/mcp_capture/connector_2026_09_03/manifest.json` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/fixtures/mcp_capture/connector_2026_09_03/snapshot_117589399.json` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/fixtures/mcp_capture/connector_2026_09_03/snapshot_117589399.json` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/fixtures/mcp_capture/connector_2026_09_03/snapshot_756733.json` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/fixtures/mcp_capture/connector_2026_09_03/snapshot_756733.json` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/fixtures/mcp_capture/raw/futures_GC.json` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/fixtures/mcp_capture/raw/futures_GC.json` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/fixtures/mcp_capture/raw/history_117589399_1d.json` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/fixtures/mcp_capture/raw/history_117589399_1d.json` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/fixtures/mcp_capture/raw/manifest.json` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/fixtures/mcp_capture/raw/manifest.json` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/fixtures/mcp_capture/raw/option_data_117589399_FTA_20261218_ASL.json` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/fixtures/mcp_capture/raw/option_data_117589399_FTA_20261218_ASL.json` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/fixtures/mcp_capture/raw/option_parameters_117589399.json` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/fixtures/mcp_capture/raw/option_parameters_117589399.json` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/fixtures/mcp_capture/raw/snapshot_117589399.json` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/fixtures/mcp_capture/raw/snapshot_117589399.json` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/fixtures/mcp_capture/raw/snapshot_534202940.json` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/fixtures/mcp_capture/raw/snapshot_534202940.json` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/fixtures/mcp_capture/raw/snapshot_683142621.json` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/fixtures/mcp_capture/raw/snapshot_683142621.json` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/fixtures/mcp_capture/raw/snapshot_683450790.json` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/fixtures/mcp_capture/raw/snapshot_683450790.json` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/fixtures/mcp_capture/raw/snapshot_683450796.json` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/fixtures/mcp_capture/raw/snapshot_683450796.json` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/fixtures/mcp_capture/raw/snapshot_683450807.json` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/fixtures/mcp_capture/raw/snapshot_683450807.json` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/fixtures/mcp_capture/raw/snapshot_689641419.json` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/fixtures/mcp_capture/raw/snapshot_689641419.json` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/fixtures/mcp_capture/raw/snapshot_732156872.json` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/fixtures/mcp_capture/raw/snapshot_732156872.json` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/fixtures/mcp_capture/raw/snapshot_820636093.json` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/fixtures/mcp_capture/raw/snapshot_820636093.json` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/fixtures/mcp_capture/raw/snapshot_820636165.json` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/fixtures/mcp_capture/raw/snapshot_820636165.json` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/fixtures/mcp_capture/raw/snapshot_843226255.json` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/fixtures/mcp_capture/raw/snapshot_843226255.json` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/fixtures/mcp_capture/raw/snapshot_843226257.json` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/fixtures/mcp_capture/raw/snapshot_843226257.json` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/fixtures/mcp_capture/raw/snapshot_843907744.json` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/fixtures/mcp_capture/raw/snapshot_843907744.json` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/fixtures/mcp_capture/raw/snapshot_846827392.json` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/fixtures/mcp_capture/raw/snapshot_846827392.json` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/fixtures/option_vendor/raw/chain_SPY_both_rights.json` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/fixtures/option_vendor/raw/chain_SPY_both_rights.json` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/fixtures/option_vendor/raw/chain_SPY_live.json` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/fixtures/option_vendor/raw/chain_SPY_live.json` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/fixtures/option_vendor/raw/chain_blocked.json` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/fixtures/option_vendor/raw/chain_blocked.json` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/fixtures/option_vendor/raw/chain_no_data.json` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/fixtures/option_vendor/raw/chain_no_data.json` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/fixtures/option_vendor/raw/chain_ragged.json` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/fixtures/option_vendor/raw/chain_ragged.json` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/fixtures/option_vendor/raw/chain_two_expiries.json` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/fixtures/option_vendor/raw/chain_two_expiries.json` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/fixtures/option_vendor/raw/manifest.json` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/fixtures/option_vendor/raw/manifest.json` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/test_bsm.py` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/test_bsm.py` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/test_collect_backfill.py` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/test_collect_backfill.py` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/test_collect_chain.py` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/test_collect_chain.py` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/test_collect_cli.py` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/test_collect_cli.py` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/test_collect_conid_supersede.py` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/test_collect_conid_supersede.py` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/test_collect_curve.py` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/test_collect_curve.py` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/test_collect_european_chain.py` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/test_collect_european_chain.py` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/test_collect_idempotence.py` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/test_collect_idempotence.py` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/test_collect_mcp_capture.py` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/test_collect_mcp_capture.py` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/test_collect_nav.py` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/test_collect_nav.py` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/test_collect_option_vendor.py` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/test_collect_option_vendor.py` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/test_collect_plumbing.py` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/test_collect_plumbing.py` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/test_collect_regressions.py` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/test_collect_regressions.py` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/test_collect_run_continuity.py` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/test_collect_run_continuity.py` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/test_collect_sequence_isolation.py` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/test_collect_sequence_isolation.py` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/test_collect_status.py` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/test_collect_status.py` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/test_collect_strikes.py` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/test_collect_strikes.py` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/test_collect_universe.py` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/test_collect_universe.py` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/test_collect_vol.py` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/test_collect_vol.py` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/test_collector_outage.py` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/test_collector_outage.py` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/test_compute_calendar.py` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/test_compute_calendar.py` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/test_compute_census.py` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/test_compute_census.py` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/test_compute_cli.py` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/test_compute_cli.py` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/test_compute_commodity.py` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/test_compute_commodity.py` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/test_compute_fx.py` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/test_compute_fx.py` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/test_compute_gates.py` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/test_compute_gates.py` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/test_compute_grid.py` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/test_compute_grid.py` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/test_compute_indicators.py` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/test_compute_indicators.py` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/test_compute_isolation.py` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/test_compute_isolation.py` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/test_compute_penalty.py` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/test_compute_penalty.py` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/test_compute_rankings.py` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/test_compute_rankings.py` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/test_compute_risk.py` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/test_compute_risk.py` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/test_compute_screen.py` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/test_compute_screen.py` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/test_compute_smile.py` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/test_compute_smile.py` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/test_compute_stats.py` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/test_compute_stats.py` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/test_config.py` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/test_config.py` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/test_cp_client.py` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/test_cp_client.py` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/test_deploy_units.py` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/test_deploy_units.py` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/test_dividend_yield_provenance.py` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/test_dividend_yield_provenance.py` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/test_env.py` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/test_env.py` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/test_env_template_is_complete.py` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/test_env_template_is_complete.py` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/test_hostile_input.py` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/test_hostile_input.py` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/test_ibkr_connector.py` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/test_ibkr_connector.py` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/test_ibkr_field_units.py` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/test_ibkr_field_units.py` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/test_iv_solve.py` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/test_iv_solve.py` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/test_ledger.py` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/test_ledger.py` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/test_macro_pull.py` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/test_macro_pull.py` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/test_macro_series.py` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/test_macro_series.py` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/test_mcp_auth.py` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/test_mcp_auth.py` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/test_mcp_cli.py` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/test_mcp_cli.py` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/test_mcp_guards.py` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/test_mcp_guards.py` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/test_mcp_tools.py` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/test_mcp_tools.py` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/test_migration_chain.py` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/test_migration_chain.py` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/test_no_account_history.py` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/test_no_account_history.py` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/test_no_committed_secrets.py` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/test_no_committed_secrets.py` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/test_no_magic_numbers.py` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/test_no_magic_numbers.py` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/test_no_unread_config_keys.py` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/test_no_unread_config_keys.py` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/test_no_unread_universe_keys.py` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/test_no_unread_universe_keys.py` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/test_oauth.py` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/test_oauth.py` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/test_oauth_lifecycle.py` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/test_oauth_lifecycle.py` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/test_oauth_preflight.py` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/test_oauth_preflight.py` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/test_packaging.py` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/test_packaging.py` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/test_point_in_time.py` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/test_point_in_time.py` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/test_rates.py` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/test_rates.py` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/test_skill_config_consistency.py` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/test_skill_config_consistency.py` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/test_store.py` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/test_store.py` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/test_util.py` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/test_util.py` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/test_validation_real_data.py` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/test_validation_real_data.py` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/tests/test_window_honesty.py` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `tests/test_window_honesty.py` | AGPL-3.0 | `tests/` (with their modules) | port |
| `legacy/lite/universe.yaml` | Almoon-D/ObliskIQ-lite-MCP | `b1ebd49` | `universe.yaml` | AGPL-3.0 | `config/` (defaults, owner, universe, jurisdictions) | port |

## `legacy/old/` — Almoon-D/Inversor-Inteligente @ `68280e1`

| Legacy path | Repository | SHA | Original path | Licence | Target | Action |
|---|---|---|---|---|---|---|
| `legacy/old/LICENSE` | Almoon-D/Inversor-Inteligente | `68280e1` | `LICENSE` | Apache-2.0 | — | reference |
| `legacy/old/ObliskIQ/config/event_calendar.json` | Almoon-D/Inversor-Inteligente | `68280e1` | `ObliskIQ/config/event_calendar.json` | Apache-2.0 | `data/events/` | port |
| `legacy/old/ObliskIQ/config/gdelt_regions.json` | Almoon-D/Inversor-Inteligente | `68280e1` | `ObliskIQ/config/gdelt_regions.json` | Apache-2.0 | `data/events/` | port |
| `legacy/old/ObliskIQ/config/polymarket_markets.json` | Almoon-D/Inversor-Inteligente | `68280e1` | `ObliskIQ/config/polymarket_markets.json` | Apache-2.0 | `data/events/` | port |
| `legacy/old/ObliskIQ/docs/AI_PROVIDERS.md` | Almoon-D/Inversor-Inteligente | `68280e1` | `ObliskIQ/docs/AI_PROVIDERS.md` | Apache-2.0 | `docs/knowledge/legacy-findings.md` | knowledge |
| `legacy/old/ObliskIQ/docs/AI_SCOREBOARD.md` | Almoon-D/Inversor-Inteligente | `68280e1` | `ObliskIQ/docs/AI_SCOREBOARD.md` | Apache-2.0 | `docs/knowledge/legacy-findings.md` | knowledge |
| `legacy/old/ObliskIQ/docs/ASYMMETRY_HUNTER.md` | Almoon-D/Inversor-Inteligente | `68280e1` | `ObliskIQ/docs/ASYMMETRY_HUNTER.md` | Apache-2.0 | `docs/knowledge/legacy-findings.md` | knowledge |
| `legacy/old/ObliskIQ/docs/COT_REAL.md` | Almoon-D/Inversor-Inteligente | `68280e1` | `ObliskIQ/docs/COT_REAL.md` | Apache-2.0 | `docs/knowledge/legacy-findings.md` | knowledge |
| `legacy/old/ObliskIQ/docs/GEO_INPUTS.md` | Almoon-D/Inversor-Inteligente | `68280e1` | `ObliskIQ/docs/GEO_INPUTS.md` | Apache-2.0 | `docs/knowledge/legacy-findings.md` | knowledge |
| `legacy/old/ObliskIQ/docs/HUNTER_READINESS.md` | Almoon-D/Inversor-Inteligente | `68280e1` | `ObliskIQ/docs/HUNTER_READINESS.md` | Apache-2.0 | `docs/knowledge/legacy-findings.md` | knowledge |
| `legacy/old/ObliskIQ/docs/PAPER_TRADING_GATE.md` | Almoon-D/Inversor-Inteligente | `68280e1` | `ObliskIQ/docs/PAPER_TRADING_GATE.md` | Apache-2.0 | `docs/knowledge/legacy-findings.md` | knowledge |
| `legacy/old/ObliskIQ/docs/STRATEGY_V1_1_VERDICT.md` | Almoon-D/Inversor-Inteligente | `68280e1` | `ObliskIQ/docs/STRATEGY_V1_1_VERDICT.md` | Apache-2.0 | `docs/knowledge/legacy-findings.md` | knowledge |
| `legacy/old/ObliskIQ/docs/STRATEGY_V2_DESIGN.md` | Almoon-D/Inversor-Inteligente | `68280e1` | `ObliskIQ/docs/STRATEGY_V2_DESIGN.md` | Apache-2.0 | `docs/knowledge/legacy-findings.md` | knowledge |
| `legacy/old/ObliskIQ/modules/analysis/analyst_router.py` | Almoon-D/Inversor-Inteligente | `68280e1` | `ObliskIQ/modules/analysis/analyst_router.py` | Apache-2.0 | `llm/` | port |
| `legacy/old/ObliskIQ/modules/analysis/llm_analyst.py` | Almoon-D/Inversor-Inteligente | `68280e1` | `ObliskIQ/modules/analysis/llm_analyst.py` | Apache-2.0 | `llm/` | port |
| `legacy/old/ObliskIQ/modules/analysis/providers.py` | Almoon-D/Inversor-Inteligente | `68280e1` | `ObliskIQ/modules/analysis/providers.py` | Apache-2.0 | `llm/` | port |
| `legacy/old/ObliskIQ/modules/analytics/ai_scoreboard.py` | Almoon-D/Inversor-Inteligente | `68280e1` | `ObliskIQ/modules/analytics/ai_scoreboard.py` | Apache-2.0 | `scoreboard/` | port |
| `legacy/old/ObliskIQ/modules/analytics/asymmetry_hunter.py` | Almoon-D/Inversor-Inteligente | `68280e1` | `ObliskIQ/modules/analytics/asymmetry_hunter.py` | Apache-2.0 | `engines/satellite/` | port |
| `legacy/old/ObliskIQ/modules/analytics/forecast_panel.py` | Almoon-D/Inversor-Inteligente | `68280e1` | `ObliskIQ/modules/analytics/forecast_panel.py` | Apache-2.0 | `scoreboard/` | port |
| `legacy/old/ObliskIQ/modules/analytics/opportunistic_optimizer.py` | Almoon-D/Inversor-Inteligente | `68280e1` | `ObliskIQ/modules/analytics/opportunistic_optimizer.py` | Apache-2.0 | `quant/risk_budget.py` (risk parity fixed; crisis switch, regime leverage, Kelly-then-normalise dropped) | port |
| `legacy/old/ObliskIQ/modules/analytics/pattern_library.py` | Almoon-D/Inversor-Inteligente | `68280e1` | `ObliskIQ/modules/analytics/pattern_library.py` | Apache-2.0 | `research/pattern_registry.yaml` (definitions only) | registry |
| `legacy/old/ObliskIQ/modules/analytics/stat_utils.py` | Almoon-D/Inversor-Inteligente | `68280e1` | `ObliskIQ/modules/analytics/stat_utils.py` | Apache-2.0 | `quant/inference.py` | port |
| `legacy/old/ObliskIQ/modules/analytics/transaction_costs.py` | Almoon-D/Inversor-Inteligente | `68280e1` | `ObliskIQ/modules/analytics/transaction_costs.py` | Apache-2.0 | `quant/costs.py` | port |
| `legacy/old/ObliskIQ/modules/config/ai_providers.py` | Almoon-D/Inversor-Inteligente | `68280e1` | `ObliskIQ/modules/config/ai_providers.py` | Apache-2.0 | `llm/` | port |
| `legacy/old/ObliskIQ/modules/config/global_vehicles.py` | Almoon-D/Inversor-Inteligente | `68280e1` | `ObliskIQ/modules/config/global_vehicles.py` | Apache-2.0 | universe seeds | reference |
| `legacy/old/ObliskIQ/modules/config/settings.py` | Almoon-D/Inversor-Inteligente | `68280e1` | `ObliskIQ/modules/config/settings.py` | Apache-2.0 | — | reference |
| `legacy/old/ObliskIQ/modules/config/strategy_universe.py` | Almoon-D/Inversor-Inteligente | `68280e1` | `ObliskIQ/modules/config/strategy_universe.py` | Apache-2.0 | — | reference |
| `legacy/old/ObliskIQ/modules/data/crisis_database.py` | Almoon-D/Inversor-Inteligente | `68280e1` | `ObliskIQ/modules/data/crisis_database.py` | Apache-2.0 | `decision/portfolio_risk.py`, `research/` (stress scenarios only) | port |
| `legacy/old/ObliskIQ/modules/execution/continuity_guard.py` | Almoon-D/Inversor-Inteligente | `68280e1` | `ObliskIQ/modules/execution/continuity_guard.py` | Apache-2.0 | `engines/satellite/` | port |
| `legacy/old/ObliskIQ/modules/execution/monitoring_suite.py` | Almoon-D/Inversor-Inteligente | `68280e1` | `ObliskIQ/modules/execution/monitoring_suite.py` | Apache-2.0 | `execution/` (concepts) | port |
| `legacy/old/ObliskIQ/modules/execution/paper_gate.py` | Almoon-D/Inversor-Inteligente | `68280e1` | `ObliskIQ/modules/execution/paper_gate.py` | Apache-2.0 | `execution/`, `scoreboard/` (concepts) | port |
| `legacy/old/ObliskIQ/modules/execution/phase4_decision_engine.py` | Almoon-D/Inversor-Inteligente | `68280e1` | `ObliskIQ/modules/execution/phase4_decision_engine.py` | Apache-2.0 | `decision/sizing.py` (`apply_quarter_kelly_clamp`) | port |
| `legacy/old/ObliskIQ/modules/market/cftc_cot_analyzer.py` | Almoon-D/Inversor-Inteligente | `68280e1` | `ObliskIQ/modules/market/cftc_cot_analyzer.py` | Apache-2.0 | `data/positioning/` | port |
| `legacy/old/ObliskIQ/modules/market/gdelt_client.py` | Almoon-D/Inversor-Inteligente | `68280e1` | `ObliskIQ/modules/market/gdelt_client.py` | Apache-2.0 | `data/events/` | port |
| `legacy/old/ObliskIQ/modules/market/polymarket_client.py` | Almoon-D/Inversor-Inteligente | `68280e1` | `ObliskIQ/modules/market/polymarket_client.py` | Apache-2.0 | `data/events/` | port |
| `legacy/old/ObliskIQ/modules/market/rss_news_enhanced.py` | Almoon-D/Inversor-Inteligente | `68280e1` | `ObliskIQ/modules/market/rss_news_enhanced.py` | Apache-2.0 | `data/events/` (fallback) | reference |
| `legacy/old/ObliskIQ/modules/market/vader_financial_lexicon.py` | Almoon-D/Inversor-Inteligente | `68280e1` | `ObliskIQ/modules/market/vader_financial_lexicon.py` | Apache-2.0 | `data/events/` (fallback) | reference |
| `legacy/old/ObliskIQ/modules/notifications/email_notifier.py` | Almoon-D/Inversor-Inteligente | `68280e1` | `ObliskIQ/modules/notifications/email_notifier.py` | Apache-2.0 | — | reference |
| `legacy/old/ObliskIQ/modules/notifications/run_summary.py` | Almoon-D/Inversor-Inteligente | `68280e1` | `ObliskIQ/modules/notifications/run_summary.py` | Apache-2.0 | `reporting/` | port |
| `legacy/old/ObliskIQ/modules/notifications/telegram_notifier.py` | Almoon-D/Inversor-Inteligente | `68280e1` | `ObliskIQ/modules/notifications/telegram_notifier.py` | Apache-2.0 | `reporting/` | port |
| `legacy/old/ObliskIQ/modules/testing/walk_forward_backtester.py` | Almoon-D/Inversor-Inteligente | `68280e1` | `ObliskIQ/modules/testing/walk_forward_backtester.py` | Apache-2.0 | `research/backtest/` | reference |
| `legacy/old/ObliskIQ/modules/utils/risk_enforcer.py` | Almoon-D/Inversor-Inteligente | `68280e1` | `ObliskIQ/modules/utils/risk_enforcer.py` | Apache-2.0 | — | reference |
| `legacy/old/ObliskIQ/research/validation/monte_carlo.py` | Almoon-D/Inversor-Inteligente | `68280e1` | `ObliskIQ/research/validation/monte_carlo.py` | Apache-2.0 | `decision/portfolio_risk.py`, `research/` (stress scenarios only) | port |
| `legacy/old/ObliskIQ/research/validation/stress_tester.py` | Almoon-D/Inversor-Inteligente | `68280e1` | `ObliskIQ/research/validation/stress_tester.py` | Apache-2.0 | `decision/portfolio_risk.py`, `research/` (stress scenarios only) | port |
| `legacy/old/ObliskIQ/scripts/backfill_cot_history.py` | Almoon-D/Inversor-Inteligente | `68280e1` | `ObliskIQ/scripts/backfill_cot_history.py` | Apache-2.0 | `data/positioning/` | port |
| `legacy/old/ObliskIQ/tests/fixtures/cot_annual_sample.csv` | Almoon-D/Inversor-Inteligente | `68280e1` | `ObliskIQ/tests/fixtures/cot_annual_sample.csv` | Apache-2.0 | `data/positioning/` | port |
| `legacy/old/ObliskIQ/tests/fixtures/phase4_responses/flip_with_new_info.json` | Almoon-D/Inversor-Inteligente | `68280e1` | `ObliskIQ/tests/fixtures/phase4_responses/flip_with_new_info.json` | Apache-2.0 | `engines/satellite/` | port |
| `legacy/old/ObliskIQ/tests/fixtures/phase4_responses/flip_with_trigger_citation.json` | Almoon-D/Inversor-Inteligente | `68280e1` | `ObliskIQ/tests/fixtures/phase4_responses/flip_with_trigger_citation.json` | Apache-2.0 | `engines/satellite/` | port |
| `legacy/old/ObliskIQ/tests/fixtures/phase4_responses/flip_without_citation.json` | Almoon-D/Inversor-Inteligente | `68280e1` | `ObliskIQ/tests/fixtures/phase4_responses/flip_without_citation.json` | Apache-2.0 | `engines/satellite/` | port |
| `legacy/old/ObliskIQ/tests/fixtures/phase4_responses/malformed_not_json.txt` | Almoon-D/Inversor-Inteligente | `68280e1` | `ObliskIQ/tests/fixtures/phase4_responses/malformed_not_json.txt` | Apache-2.0 | `engines/satellite/` | port |
| `legacy/old/ObliskIQ/tests/fixtures/phase4_responses/schema_violation.json` | Almoon-D/Inversor-Inteligente | `68280e1` | `ObliskIQ/tests/fixtures/phase4_responses/schema_violation.json` | Apache-2.0 | `engines/satellite/` | port |
| `legacy/old/ObliskIQ/tests/fixtures/phase4_responses/valid_response.json` | Almoon-D/Inversor-Inteligente | `68280e1` | `ObliskIQ/tests/fixtures/phase4_responses/valid_response.json` | Apache-2.0 | `engines/satellite/` | port |
| `legacy/old/ObliskIQ/tests/llm_fakes.py` | Almoon-D/Inversor-Inteligente | `68280e1` | `ObliskIQ/tests/llm_fakes.py` | Apache-2.0 | `llm/` | port |
| `legacy/old/ObliskIQ/tests/test_ai_providers.py` | Almoon-D/Inversor-Inteligente | `68280e1` | `ObliskIQ/tests/test_ai_providers.py` | Apache-2.0 | `llm/` | port |
| `legacy/old/ObliskIQ/tests/test_ai_scoreboard.py` | Almoon-D/Inversor-Inteligente | `68280e1` | `ObliskIQ/tests/test_ai_scoreboard.py` | Apache-2.0 | `scoreboard/` | port |
| `legacy/old/ObliskIQ/tests/test_analyst_router.py` | Almoon-D/Inversor-Inteligente | `68280e1` | `ObliskIQ/tests/test_analyst_router.py` | Apache-2.0 | `llm/` | port |
| `legacy/old/ObliskIQ/tests/test_asymmetry_hunter.py` | Almoon-D/Inversor-Inteligente | `68280e1` | `ObliskIQ/tests/test_asymmetry_hunter.py` | Apache-2.0 | `engines/satellite/` | port |
| `legacy/old/ObliskIQ/tests/test_asymmetry_risk_rails.py` | Almoon-D/Inversor-Inteligente | `68280e1` | `ObliskIQ/tests/test_asymmetry_risk_rails.py` | Apache-2.0 | `engines/satellite/` | port |
| `legacy/old/ObliskIQ/tests/test_cftc_cot_history.py` | Almoon-D/Inversor-Inteligente | `68280e1` | `ObliskIQ/tests/test_cftc_cot_history.py` | Apache-2.0 | `data/positioning/` | port |
| `legacy/old/ObliskIQ/tests/test_continuity_guard.py` | Almoon-D/Inversor-Inteligente | `68280e1` | `ObliskIQ/tests/test_continuity_guard.py` | Apache-2.0 | `engines/satellite/` | port |
| `legacy/old/ObliskIQ/tests/test_crisis_database.py` | Almoon-D/Inversor-Inteligente | `68280e1` | `ObliskIQ/tests/test_crisis_database.py` | Apache-2.0 | `decision/portfolio_risk.py`, `research/` (stress scenarios only) | port |
| `legacy/old/ObliskIQ/tests/test_crisis_sizing.py` | Almoon-D/Inversor-Inteligente | `68280e1` | `ObliskIQ/tests/test_crisis_sizing.py` | Apache-2.0 | `decision/portfolio_risk.py`, `research/` (stress scenarios only) | port |
| `legacy/old/ObliskIQ/tests/test_decision_contract.py` | Almoon-D/Inversor-Inteligente | `68280e1` | `ObliskIQ/tests/test_decision_contract.py` | Apache-2.0 | `decision/sizing.py` (`apply_quarter_kelly_clamp`) | port |
| `legacy/old/ObliskIQ/tests/test_degraded_provider_policy.py` | Almoon-D/Inversor-Inteligente | `68280e1` | `ObliskIQ/tests/test_degraded_provider_policy.py` | Apache-2.0 | `llm/` | port |
| `legacy/old/ObliskIQ/tests/test_forecast_panel.py` | Almoon-D/Inversor-Inteligente | `68280e1` | `ObliskIQ/tests/test_forecast_panel.py` | Apache-2.0 | `scoreboard/` | port |
| `legacy/old/ObliskIQ/tests/test_gdelt_client.py` | Almoon-D/Inversor-Inteligente | `68280e1` | `ObliskIQ/tests/test_gdelt_client.py` | Apache-2.0 | `data/events/` | port |
| `legacy/old/ObliskIQ/tests/test_kill_switch_wiring.py` | Almoon-D/Inversor-Inteligente | `68280e1` | `ObliskIQ/tests/test_kill_switch_wiring.py` | Apache-2.0 | `execution/` (concepts) | port |
| `legacy/old/ObliskIQ/tests/test_llm_analyst.py` | Almoon-D/Inversor-Inteligente | `68280e1` | `ObliskIQ/tests/test_llm_analyst.py` | Apache-2.0 | `llm/` | port |
| `legacy/old/ObliskIQ/tests/test_monitoring_suite.py` | Almoon-D/Inversor-Inteligente | `68280e1` | `ObliskIQ/tests/test_monitoring_suite.py` | Apache-2.0 | `execution/` (concepts) | port |
| `legacy/old/ObliskIQ/tests/test_opportunistic_optimizer.py` | Almoon-D/Inversor-Inteligente | `68280e1` | `ObliskIQ/tests/test_opportunistic_optimizer.py` | Apache-2.0 | `quant/risk_budget.py` (risk parity fixed; crisis switch, regime leverage, Kelly-then-normalise dropped) | port |
| `legacy/old/ObliskIQ/tests/test_paper_gate.py` | Almoon-D/Inversor-Inteligente | `68280e1` | `ObliskIQ/tests/test_paper_gate.py` | Apache-2.0 | `execution/`, `scoreboard/` (concepts) | port |
| `legacy/old/ObliskIQ/tests/test_polymarket_client.py` | Almoon-D/Inversor-Inteligente | `68280e1` | `ObliskIQ/tests/test_polymarket_client.py` | Apache-2.0 | `data/events/` | port |
| `legacy/old/ObliskIQ/tests/test_rss_sources.py` | Almoon-D/Inversor-Inteligente | `68280e1` | `ObliskIQ/tests/test_rss_sources.py` | Apache-2.0 | `data/events/` (fallback) | reference |
| `legacy/old/ObliskIQ/tests/test_run_summary.py` | Almoon-D/Inversor-Inteligente | `68280e1` | `ObliskIQ/tests/test_run_summary.py` | Apache-2.0 | `reporting/` | port |
| `legacy/old/ObliskIQ/tests/test_scoreboard_statistics.py` | Almoon-D/Inversor-Inteligente | `68280e1` | `ObliskIQ/tests/test_scoreboard_statistics.py` | Apache-2.0 | `scoreboard/` | port |
| `legacy/old/ObliskIQ/tests/test_stat_utils_inference.py` | Almoon-D/Inversor-Inteligente | `68280e1` | `ObliskIQ/tests/test_stat_utils_inference.py` | Apache-2.0 | `quant/inference.py` | port |
| `legacy/old/ObliskIQ/tests/test_transaction_costs.py` | Almoon-D/Inversor-Inteligente | `68280e1` | `ObliskIQ/tests/test_transaction_costs.py` | Apache-2.0 | `quant/costs.py` | port |
| `legacy/old/ObliskIQ/tests/test_vader_financial_lexicon.py` | Almoon-D/Inversor-Inteligente | `68280e1` | `ObliskIQ/tests/test_vader_financial_lexicon.py` | Apache-2.0 | `data/events/` (fallback) | reference |
| `legacy/old/ObliskIQ/tests/test_walk_forward_backtester.py` | Almoon-D/Inversor-Inteligente | `68280e1` | `ObliskIQ/tests/test_walk_forward_backtester.py` | Apache-2.0 | `research/backtest/` | reference |
| `legacy/old/docs/KNOWN_LIMITATIONS.md` | Almoon-D/Inversor-Inteligente | `68280e1` | `docs/KNOWN_LIMITATIONS.md` | Apache-2.0 | `docs/knowledge/legacy-findings.md` | knowledge |
| `legacy/old/docs/PROVENANCE_PURGE_REPORT.md` | Almoon-D/Inversor-Inteligente | `68280e1` | `docs/PROVENANCE_PURGE_REPORT.md` | Apache-2.0 | `docs/knowledge/legacy-findings.md` | knowledge |

## `legacy/pulse/` — Almoon-D/market-pulse-bot @ `900dbf4`

| Legacy path | Repository | SHA | Original path | Licence | Target | Action |
|---|---|---|---|---|---|---|
| `legacy/pulse/.env.example` | Almoon-D/market-pulse-bot | `900dbf4` | `.env.example` | AGPL-3.0 | — | reference |
| `legacy/pulse/.github/workflows/ci.yml` | Almoon-D/market-pulse-bot | `900dbf4` | `.github/workflows/ci.yml` | AGPL-3.0 | — | reference |
| `legacy/pulse/.github/workflows/publish-discord.yaml` | Almoon-D/market-pulse-bot | `900dbf4` | `.github/workflows/publish-discord.yaml` | AGPL-3.0 | — | reference |
| `legacy/pulse/.gitignore` | Almoon-D/market-pulse-bot | `900dbf4` | `.gitignore` | AGPL-3.0 | — | reference |
| `legacy/pulse/LICENSE` | Almoon-D/market-pulse-bot | `900dbf4` | `LICENSE` | AGPL-3.0 | — | reference |
| `legacy/pulse/README.md` | Almoon-D/market-pulse-bot | `900dbf4` | `README.md` | AGPL-3.0 | — | reference |
| `legacy/pulse/config/exchanges.yaml` | Almoon-D/market-pulse-bot | `900dbf4` | `config/exchanges.yaml` | AGPL-3.0 | `config/defaults/venues.yaml` | port |
| `legacy/pulse/data/.gitkeep` | Almoon-D/market-pulse-bot | `900dbf4` | `data/.gitkeep` | AGPL-3.0 | — | reference |
| `legacy/pulse/data/manual_incidents.yaml.example` | Almoon-D/market-pulse-bot | `900dbf4` | `data/manual_incidents.yaml.example` | AGPL-3.0 | `calendars/incidents.py` | port |
| `legacy/pulse/locales/de.yaml` | Almoon-D/market-pulse-bot | `900dbf4` | `locales/de.yaml` | AGPL-3.0 | `reporting/` (badge language, message state) | reference |
| `legacy/pulse/locales/en.yaml` | Almoon-D/market-pulse-bot | `900dbf4` | `locales/en.yaml` | AGPL-3.0 | `reporting/` (badge language, message state) | reference |
| `legacy/pulse/locales/es.yaml` | Almoon-D/market-pulse-bot | `900dbf4` | `locales/es.yaml` | AGPL-3.0 | `reporting/` (badge language, message state) | reference |
| `legacy/pulse/locales/fr.yaml` | Almoon-D/market-pulse-bot | `900dbf4` | `locales/fr.yaml` | AGPL-3.0 | `reporting/` (badge language, message state) | reference |
| `legacy/pulse/main.py` | Almoon-D/market-pulse-bot | `900dbf4` | `main.py` | AGPL-3.0 | — | reference |
| `legacy/pulse/pyproject.toml` | Almoon-D/market-pulse-bot | `900dbf4` | `pyproject.toml` | AGPL-3.0 | — | reference |
| `legacy/pulse/src/market_pulse_bot/__init__.py` | Almoon-D/market-pulse-bot | `900dbf4` | `src/market_pulse_bot/__init__.py` | AGPL-3.0 | — | reference |
| `legacy/pulse/src/market_pulse_bot/app.py` | Almoon-D/market-pulse-bot | `900dbf4` | `src/market_pulse_bot/app.py` | AGPL-3.0 | `reporting/` (badge language, message state) | reference |
| `legacy/pulse/src/market_pulse_bot/calendar_engine.py` | Almoon-D/market-pulse-bot | `900dbf4` | `src/market_pulse_bot/calendar_engine.py` | AGPL-3.0 | `calendars/` | port |
| `legacy/pulse/src/market_pulse_bot/config.py` | Almoon-D/market-pulse-bot | `900dbf4` | `src/market_pulse_bot/config.py` | AGPL-3.0 | `calendars/` | port |
| `legacy/pulse/src/market_pulse_bot/halt_detector.py` | Almoon-D/market-pulse-bot | `900dbf4` | `src/market_pulse_bot/halt_detector.py` | AGPL-3.0 | `calendars/incidents.py` | port |
| `legacy/pulse/src/market_pulse_bot/i18n.py` | Almoon-D/market-pulse-bot | `900dbf4` | `src/market_pulse_bot/i18n.py` | AGPL-3.0 | `reporting/` (badge language, message state) | reference |
| `legacy/pulse/src/market_pulse_bot/market_engine.py` | Almoon-D/market-pulse-bot | `900dbf4` | `src/market_pulse_bot/market_engine.py` | AGPL-3.0 | `calendars/` | port |
| `legacy/pulse/src/market_pulse_bot/notification_backend.py` | Almoon-D/market-pulse-bot | `900dbf4` | `src/market_pulse_bot/notification_backend.py` | AGPL-3.0 | `reporting/` (badge language, message state) | reference |
| `legacy/pulse/src/market_pulse_bot/text_formatter.py` | Almoon-D/market-pulse-bot | `900dbf4` | `src/market_pulse_bot/text_formatter.py` | AGPL-3.0 | `reporting/` (badge language, message state) | reference |
| `legacy/pulse/tests/test_app.py` | Almoon-D/market-pulse-bot | `900dbf4` | `tests/test_app.py` | AGPL-3.0 | `reporting/` (badge language, message state) | reference |
| `legacy/pulse/tests/test_config.py` | Almoon-D/market-pulse-bot | `900dbf4` | `tests/test_config.py` | AGPL-3.0 | `calendars/` | port |
| `legacy/pulse/tests/test_formatter.py` | Almoon-D/market-pulse-bot | `900dbf4` | `tests/test_formatter.py` | AGPL-3.0 | `reporting/` (badge language, message state) | reference |
| `legacy/pulse/tests/test_incidents.py` | Almoon-D/market-pulse-bot | `900dbf4` | `tests/test_incidents.py` | AGPL-3.0 | `calendars/incidents.py` | port |
| `legacy/pulse/tests/test_legend.py` | Almoon-D/market-pulse-bot | `900dbf4` | `tests/test_legend.py` | AGPL-3.0 | `reporting/` (badge language, message state) | reference |
| `legacy/pulse/tests/test_market_engine.py` | Almoon-D/market-pulse-bot | `900dbf4` | `tests/test_market_engine.py` | AGPL-3.0 | `calendars/` | port |
| `legacy/pulse/tests/test_notification_state.py` | Almoon-D/market-pulse-bot | `900dbf4` | `tests/test_notification_state.py` | AGPL-3.0 | `reporting/` (badge language, message state) | reference |

## Imports the `legacy/old/` files make that the snapshot does not contain

MASTER_PLAN §6.2 snapshots only what is ported, referenced or kept as knowledge, so some imports in `legacy/old/` point at modules left behind (several are marked "not ported"). They are listed so a porting session knows what it will not find; port the behaviour, not the dependency.

| Missing module | Imported by |
|---|---|
| `modules.analysis.gemini_integration` | `ObliskIQ/tests/test_llm_analyst.py` |
| `modules.analytics.backtester` | `ObliskIQ/modules/testing/walk_forward_backtester.py`, `ObliskIQ/research/validation/monte_carlo.py` |
| `modules.analytics.correlation_monitor` | `ObliskIQ/modules/execution/monitoring_suite.py` |
| `modules.analytics.drawdown_monitor` | `ObliskIQ/modules/execution/monitoring_suite.py` |
| `modules.analytics.historical_reflexivity` | `ObliskIQ/modules/testing/walk_forward_backtester.py` |
| `modules.analytics.ic_calculator` | `ObliskIQ/modules/analytics/ai_scoreboard.py`, `ObliskIQ/modules/testing/walk_forward_backtester.py` |
| `modules.analytics.ic_provenance` | `ObliskIQ/modules/analytics/pattern_library.py` |
| `modules.analytics.liquidity_analyzer` | `ObliskIQ/modules/execution/phase4_decision_engine.py` |
| `modules.analytics.reflexivity_learner` | `ObliskIQ/modules/execution/phase4_decision_engine.py` |
| `modules.analytics.screening_engine` | `ObliskIQ/modules/execution/phase4_decision_engine.py` |
| `modules.analytics.signal_decay_monitor` | `ObliskIQ/modules/execution/monitoring_suite.py` |
| `modules.config.operator_panel` | `ObliskIQ/modules/config/settings.py` |
| `modules.data.warehouse` | `ObliskIQ/modules/analytics/asymmetry_hunter.py`, `ObliskIQ/modules/data/crisis_database.py`, `ObliskIQ/modules/market/gdelt_client.py`, `ObliskIQ/modules/market/polymarket_client.py`, `ObliskIQ/modules/testing/walk_forward_backtester.py`, `ObliskIQ/scripts/backfill_cot_history.py`, `ObliskIQ/tests/test_cftc_cot_history.py` |
| `modules.exceptions` | `ObliskIQ/modules/analysis/analyst_router.py`, `ObliskIQ/modules/analysis/llm_analyst.py`, `ObliskIQ/modules/config/ai_providers.py`, `ObliskIQ/modules/testing/walk_forward_backtester.py`, `ObliskIQ/tests/test_ai_providers.py`, `ObliskIQ/tests/test_analyst_router.py`, `ObliskIQ/tests/test_decision_contract.py`, `ObliskIQ/tests/test_llm_analyst.py`, `ObliskIQ/tests/test_walk_forward_backtester.py` |
| `modules.execution.ibkr_client` | `ObliskIQ/tests/test_paper_gate.py` |
| `modules.execution.orchestrator` | `ObliskIQ/tests/test_analyst_router.py`, `ObliskIQ/tests/test_degraded_provider_policy.py` |
| `modules.execution.paper_trader` | `ObliskIQ/modules/execution/paper_gate.py`, `ObliskIQ/tests/test_paper_gate.py`, `ObliskIQ/tests/test_walk_forward_backtester.py` |
| `modules.execution.tranche_planner` | `ObliskIQ/modules/execution/phase4_decision_engine.py` |
| `modules.market.price_history` | `ObliskIQ/modules/analytics/ai_scoreboard.py` |
| `modules.market.regional_sectoral_config` | `ObliskIQ/tests/test_rss_sources.py` |
| `modules.models.data_models` | `ObliskIQ/tests/test_paper_gate.py`, `ObliskIQ/tests/test_walk_forward_backtester.py` |
| `modules.models.schemas` | `ObliskIQ/modules/analysis/llm_analyst.py`, `ObliskIQ/modules/utils/risk_enforcer.py`, `ObliskIQ/tests/test_continuity_guard.py`, `ObliskIQ/tests/test_decision_contract.py` |
| `modules.storage.database_manager` | `ObliskIQ/tests/test_ai_scoreboard.py`, `ObliskIQ/tests/test_decision_contract.py`, `ObliskIQ/tests/test_forecast_panel.py`, `ObliskIQ/tests/test_scoreboard_statistics.py` |
| `modules.utils.audit_logger` | `ObliskIQ/modules/utils/risk_enforcer.py` |
| `modules.utils.circuit_breaker` | `ObliskIQ/modules/analysis/analyst_router.py` |
| `modules.utils.health_monitor` | `ObliskIQ/modules/execution/monitoring_suite.py` |
| `modules.utils.http` | `ObliskIQ/modules/market/gdelt_client.py`, `ObliskIQ/modules/market/polymarket_client.py` |
| `modules.utils.hybrid_formatter` | `ObliskIQ/modules/analysis/llm_analyst.py` |
| `modules.utils.prompt_composer` | `ObliskIQ/modules/analysis/analyst_router.py`, `ObliskIQ/modules/analysis/llm_analyst.py`, `ObliskIQ/modules/analytics/ai_scoreboard.py` |
| `modules.utils.token_budget` | `ObliskIQ/modules/analysis/analyst_router.py`, `ObliskIQ/modules/analysis/llm_analyst.py`, `ObliskIQ/tests/test_analyst_router.py` |
| `modules.utils.version` | `ObliskIQ/modules/analytics/ai_scoreboard.py` |
