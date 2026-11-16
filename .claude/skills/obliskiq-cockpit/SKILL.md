---
name: obliskiq-cockpit
description: Reads and steers the owner's ObliskIQ bot through its MCP cockpit (the obliskiq-cockpit server) — what the bot is doing, what it holds and plans, the evidence behind it, its settings, and the owner's commands (cash deposits and withdrawals, answers to its questions, approvals, settings, pause, kill switch). Use this whenever the owner asks how the bot or the portfolio is doing, what it holds, what it will buy or sell, why it did something, what a setting is, or asks to deposit, withdraw, answer, approve, change a setting, pause or stop it — even if they do not name the cockpit.
---

# ObliskIQ cockpit

ObliskIQ manages one Interactive Brokers account on its own: a reserve held by the core engine,
a thesis book of written ideas graded forward, and a hard decision, risk and execution layer. The
cockpit is the owner's window on it and the only remote way to change it. Adapted from the
ObliskIQ-lite skill (`.claude/skills/obliskiq-lite/SKILL.md` in ObliskIQ-lite-MCP @b1ebd49): the gates,
the evidence and the judgment now live in the bot; this skill reads them and never re-derives
them.

---

## Never invent a number

Every figure you give comes from a tool call made in this session, with the moment it was read.
The bot's configuration owns every threshold and budget; `get_settings` returns their values.
Never copy a number from memory, from an earlier session or from this file, and never estimate
one the tools did not return: say the tool did not return it, and why when it said why.

A value the bot could not read is returned as missing with its reason (`"nav": null`,
`"measured": false, "reason": ...`). Quote the reason. A missing value is never a zero, and an
empty list from a part that could not be read is never "nothing to report".

---

## The tools, in order

| Step | Tool | What it answers |
|---|---|---|
| 1. Always first | `get_status` | mode, kill switch, pause, heartbeat age, reconciliation, NAV, open intents and questions |
| 2. Is it healthy | `get_health` | the heartbeat's facts, each data source, open alerts, recent runs |
| 3. What it holds | `get_portfolio` | NAV, time-weighted return, managed capital, positions, sleeve budgets |
| 4. What it aims at | `get_targets` | the core's variant, trigger and each role's target |
| 5. What it is doing | `get_intents` | order intents and their states |
| 6. The ideas | `get_theses` | the thesis book: open theses, record, second opinions |
| 7. The evidence | `get_scoreboard`, `get_patterns` | growth beside the references; every hypothesis and its decision |
| 8. The views | `get_grid_view`, `get_macro_board` | the measurement store's grid and board |
| 9. The settings | `get_settings` | the owner's settings, what a remote change may touch and how |
| 10. What waits for the owner | `list_questions`, `list_cash` | questions with options, costs and deadline; reservations and flows |
| Commands | `update_setting`, `cash_deposit`, `cash_reserve`, `answer_question`, `approve_intent`, `reject_intent`, `set_pause`, `kill_switch` | below |

**`get_status` comes first.** A stale heartbeat (`"stale": true`), the kill switch ON, a pause, a
reconciliation that is not green or the mode held in shadow change how everything after it reads.
Lead every answer with them when they are not normal.

**The book is the service's last board.** The cockpit opens no brokerage session (the bot holds
the one IBKR allows), so positions and targets come from the board the service wrote at its last
daily digest. Every such answer carries `read_by_service_at` and `age_days`; when it carries
`"stale": true`, say so before any figure taken from it, and never mix it with a live quote
unmarked. The cockpit holds no live quotes.

**Long tables are capped.** A response with `"truncated": true` is partial: say so, and narrow
the request rather than reading it as complete.

---

## Commands

The bot acts only on the owner's word. Never send a command the owner did not ask for, and never
confirm one on your own judgment.

**Preview, then confirm.** `update_setting`, `cash_deposit`, `cash_reserve`, `answer_question` and
`approve_intent` record nothing on the first call: they return `"confirmed": false` and a
`preview` of exactly what would happen. Show the owner the preview in their words, wait for their
yes, then call again with the same arguments and `confirm=true`. A changed argument is a new
preview.

**At once.** `reject_intent`, `set_pause` and `kill_switch` only lower risk, so they act on the
first call. A pause skips the decision cycles (protective exits and the collections go on); the
kill switch also cancels working entry orders and blocks every order that adds risk. Turning the
kill switch off is done on the machine (`obliskiq kill off`), never here.

**Settings.** Remote changes may only tighten a risk limit or move a budget or a cash-flow setting
inside its bounds (`get_settings` → `remote_changeable` lists each with its direction or bounds).
Loosening a risk limit, and any other setting, is `obliskiq settings set` on the machine. A change
is validated with the whole configuration, audited, and applies at the service's next cycle
boundary: say that, and check `service_runs_on_these` afterwards rather than assuming.

**Amounts and dates.** Amounts are digits with a point for decimals (`1237.57`); dates are
`YYYY-MM-DD`. Write the owner's "1.000 €" as `1000` and "1.237,57" as `1237.57`, and show the
preview so they can see you read it right.

---

## A refusal is the answer

The bot's gates refuse in the owner's words: `loosens a risk limit; that needs obliskiq settings
set on the machine`, `Q4 has no option 'later' (options: proceed, postpone)`, `the owner's file
changed meanwhile`. Quote the reason verbatim. Do not retry with altered arguments to get around
it, do not offer a workaround the gate exists to prevent, and do not describe a refusal as a
fault.

---

## What the bot will not do here

- Size, build or place an order: intents come from the bot's own cycles; the cockpit approves or
  rejects what its approval queue holds.
- Turn the kill switch off, loosen a risk limit or upgrade the mode: those are commands on the
  machine.
- Give investment advice: the bot reports what it holds and why; close any analysis with a note
  that it is information, not personalised investment advice.

---

## Output

Lead with what is not normal from `get_status`. Then answer the question, each figure with the
tool and moment it came from. For a command: the preview, the owner's decision, the confirmed
result. End with what waits for the owner (open questions with their deadlines, unexplained cash,
intents held for approval) when there is any.

---

## Known failure modes

These happened with the lite workflow this skill replaces, and each has a rule above.

**Frozen data read as live.** A weekend session built recommendations on Friday's marks. Every
board answer carries its age; say it.

**Discovering the constraint last.** Whole analyses were produced and then found impossible.
`get_status` comes first; a kill switch, pause or stale heartbeat comes before anything else.

**Repeating a prior answer without saying so.** If nothing changed since the last session, say
so and say what was re-read.

**A number nobody measured.** A metric with no source was specified and then filled in. If a
tool did not return it, it is not in the answer.
