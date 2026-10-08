# ObliskIQ-lite-MCP

Read BUILD-BRIEF.md before any work — it owns the architecture, the store
schema and the tool surface. **ROADMAP.md says what is open and which phase is
next; docs/NEXT-SESSIONS.md holds the paste-ready prompt for an open phase when
there is one, the record of the prompts that have run, and the one item that is
not a session.** docs/DECISIONS.md says why the design is as
it is, and which alternatives were rejected on what evidence; read it before
proposing to change a rule, and add to it when a decision is taken.

Two components: an MCP server (obliskiq-lite-mcp) owning the SQLite store,
collectors and computation, and a skill at
.claude/skills/obliskiq-lite/ owning judgment. The skill's frontmatter name and
its folder name must always match each other.

## Standing rules

- Python. All thresholds and windows from config.yaml. No magic numbers.
- config.yaml owns every threshold; SKILL.md owns judgment and evidence and
  references config keys instead of restating values. Never copy a number from
  config into SKILL.md, BUILD-BRIEF.md or README.md —
  tests/test_skill_config_consistency.py fails the build if you do.
- Every config key has a consumer, or it is not in config.yaml: code in
  src/obliskiq/, a test pinning the value, or SKILL.md naming it as a dotted
  key. tests/test_no_unread_config_keys.py fails the build on an orphan. A key
  nothing reads is a decision that looks made and is not.
- One owner per fact: config.yaml thresholds, SKILL.md judgment,
  references/*-playbook.md observed API behaviour, BUILD-BRIEF.md architecture
  and schema, ROADMAP.md what is open, docs/DECISIONS.md why.
- All secrets, paths and ports from environment variables. Never hardcoded.
- Collectors run as a standalone entrypoint, separate from the server process,
  deployable to a small always-on cloud VM with no human present.
- What makes a session able to reach IBKR is the seven `cp_api` credentials, not
  the machine. WITHOUT them — the usual case — there is no live access at all:
  build against mocks and recorded fixtures, and mark everything needing live
  verification. WITH them, run live and take the reading, because a route built
  only against fixtures is a route nothing has checked. Tag every finding with
  the route and the date; a fixture is evidence about our parsing and never about
  their format. (Seven, not the nine this rule said for three phases:
  `OBLISK_IBKR_REALM` and `OBLISK_IBKR_ACCOUNT_ID` are read by nothing — the
  realm comes from `ibkr.cp_api.oauth.realm` and the account id from
  `/portfolio/accounts`. Neither env template ships them.)
- references/ibkr-playbook.md findings are UNVERIFIED until tested live.
  Append every new API quirk you encounter.
- Do not build analysis or reporting until the store is populated and grid
  rankings return real output.
- Ledger-write tools validate against config.yaml and reject gate violations
  server-side. The skill is never the only thing standing between a bad trade
  and the ledger.
- Order staging never executes without explicit confirmation of a displayed
  order table.
- Record data source per row (mcp | cp_api | marketdata_app) so collection can
  be migrated wholesale if needed.
- Client Portal (Web) API is IN scope via OAuth 1.0a. The downloadable
  Client Portal Gateway and IB Gateway are OUT of scope — both need an
  interactive local session, unusable for unattended collection.
