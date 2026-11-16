"""The daily dashboard: one self-contained HTML file from the board (MASTER_PLAN §5.17).

NAV and its time-weighted return beside the references, the sleeves and their budgets, the
positions and targets, the theses, the patterns, today's intents, the cash reservations and open
questions, the venues, health and the settings in force. Everything comes from the board the
service wrote after its digest (``reporting/board.py``), so the dashboard shows what the service
read and nothing else; every value is escaped, a part the service could not read says why, and the
page loads nothing from anywhere (no script, no font, no image). It is written beside the board,
sent through Telegram and served by the cockpit behind its token.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import html
from collections.abc import Sequence
from typing import Any

_STYLE = """
:root { --bg: #ffffff; --fg: #1d2330; --muted: #5f6b7a; --line: #d9dee5; --card: #f5f7fa;
  --warn: #9a5b00; --bad: #b42318; --good: #1a7f37; }
@media (prefers-color-scheme: dark) {
  :root { --bg: #11151c; --fg: #e6e9ef; --muted: #9aa5b4; --line: #2a313c; --card: #181d26;
    --warn: #e3a33b; --bad: #f97066; --good: #5ac47d; }
}
* { box-sizing: border-box; }
body { margin: 0; padding: 16px; background: var(--bg); color: var(--fg);
  font: 15px/1.45 system-ui, -apple-system, "Segoe UI", sans-serif; }
main { max-width: 1100px; margin: 0 auto; }
h1 { font-size: 1.4rem; margin: 0 0 4px; }
h2 { font-size: 1.05rem; margin: 24px 0 8px; border-bottom: 1px solid var(--line);
  padding-bottom: 4px; }
.meta { color: var(--muted); margin: 0 0 12px; }
.tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 8px; }
.tile { background: var(--card); border: 1px solid var(--line); border-radius: 8px; padding: 10px; }
.tile b { display: block; font-size: 1.15rem; font-variant-numeric: tabular-nums; }
.tile span { color: var(--muted); font-size: .85rem; }
.scroll { overflow-x: auto; }
table { border-collapse: collapse; width: 100%; font-variant-numeric: tabular-nums; }
th, td { text-align: left; padding: 4px 8px; border-bottom: 1px solid var(--line);
  vertical-align: top; }
th { color: var(--muted); font-weight: 600; font-size: .85rem; }
ul { margin: 0; padding-left: 20px; }
.bad { color: var(--bad); font-weight: 600; }
.warn { color: var(--warn); }
.good { color: var(--good); }
.none { color: var(--muted); font-style: italic; }
"""


def _e(value: Any) -> str:
    return "—" if value is None else html.escape(str(value))


def _pct(value: Any) -> str:
    if value is None:
        return "—"
    return f"{float(value):.2%}"


def _table(headers: Sequence[str], rows: Sequence[Sequence[Any]], empty: str) -> str:
    if not rows:
        return f'<p class="none">{_e(empty)}</p>'
    head = "".join(f"<th>{_e(h)}</th>" for h in headers)
    body = "".join("<tr>" + "".join(f"<td>{_e(c)}</td>" for c in row) + "</tr>" for row in rows)
    return f'<div class="scroll"><table><tr>{head}</tr>{body}</table></div>'


def _lines(items: Sequence[str], empty: str) -> str:
    kept = [item.strip() for item in items if item.strip()]
    if not kept:
        return f'<p class="none">{_e(empty)}</p>'
    return "<ul>" + "".join(f"<li>{_e(item)}</li>" for item in kept) + "</ul>"


def _tile(label: str, value: str, css: str = "") -> str:
    style = f' class="{css}"' if css else ""
    return f'<div class="tile"><span>{_e(label)}</span><b{style}>{value}</b></div>'


def _tiles(board: dict[str, Any]) -> str:
    nav = board.get("nav")
    twr = board.get("twr")
    nav_text = "none recorded" if nav is None else f"{_e(nav['value'])} {_e(nav['currency'])}"
    if twr is None:
        twr_text = "not measured"
    elif twr.get("value") is None:
        twr_text = f"not measured ({_e(twr.get('reason'))})"
    else:
        twr_text = _pct(twr["value"])
    kill = bool(board.get("kill_switch"))
    run = board.get("reconciliation")
    reconciliation = "none yet" if run is None else _e(run["status"])
    return (
        '<div class="tiles">'
        + _tile("Mode", _e(board.get("mode") or "unreadable"))
        + _tile("Kill switch", "ON" if kill else "off", "bad" if kill else "good")
        + _tile("NAV", nav_text)
        + _tile("Time-weighted return", twr_text)
        + _tile("Reconciliation", reconciliation)
        + "</div>"
    )


def _performance(board: dict[str, Any]) -> str:
    view = board.get("performance") or {}
    if not view.get("measured"):
        return f'<p class="none">not measured: {_e(view.get("reason"))}</p>'
    rows = [(s["name"], _pct(s["growth"]), s.get("reason") or "") for s in view["series"]]
    span = f'<p class="meta">{_e(view["since"])} to {_e(view["as_of"])}, time-weighted</p>'
    return span + _table(["Series", "Growth", "Note"], rows, "no series")


def _budgets(board: dict[str, Any]) -> str:
    budgets = board.get("budgets") or {}
    configured = budgets.get("configured", {})
    rows = [(name, _pct(share)) for name, share in configured.items()]
    live = budgets.get("live_shares", {})
    live_rows = [(name, _pct(share)) for name, share in live.items()]
    return _table(["Sleeve budget", "Share of investable capital"], rows, "none") + _table(
        ["Layer", "Live share (allocation rule)"], live_rows, "no layer holds a live share yet"
    )


def _book(board: dict[str, Any]) -> str:
    book = board.get("book") or {}
    if not book.get("surveyed"):
        return '<p class="none">the book was not surveyed</p>'
    parts: list[str] = []
    capital = book.get("capital")
    if capital is not None:
        parts.append(
            f"<p>managed {_e(capital['managed'])} ({_e(capital['detail'])}); reserved "
            f"{_e(capital['reserved'])}, unmanaged {_e(capital['unmanaged'])}, investable "
            f"{_e(capital['investable'])}</p>"
        )
    if book.get("blocked"):
        parts.append(f'<p class="warn">no rebalance now: {_e(book["blocked"])}</p>')
    rows = [
        (
            p["role"],
            p["symbol"],
            p["held"],
            p["value"],
            _pct(p["target_weight"]),
            p["target_value"],
            p["action"],
        )
        for p in book.get("positions", [])
    ]
    parts.append(
        _table(
            ["Role", "Vehicle", "Held", "Value", "Target", "Target value", "Action"],
            rows,
            "no position and no target",
        )
    )
    core = book.get("core")
    if core is not None:
        parts.append(f"<p>core variant {_e(core['variant'])}: {_e(core['trigger'])}</p>")
        roles = [
            (r["role"], r["status"], _pct(r["target_weight"]), _pct(r["held_weight"]))
            for r in core["roles"]
        ]
        parts.append(_table(["Role", "Status", "Target", "Held"], roles, "no role"))
    return "".join(parts)


def _patterns(board: dict[str, Any]) -> str:
    patterns = board.get("patterns")
    if isinstance(patterns, str):
        return f'<p class="none">{_e(patterns)}</p>'
    rows = [
        (p["hypothesis"], p["title"], p["status"], p["decision"] or "untested")
        for p in patterns or []
    ]
    return _table(["Hypothesis", "Title", "Status", "Decision"], rows, "no hypothesis registered")


def _intents(board: dict[str, Any]) -> str:
    rows = [
        (i["created_at"][11:16], i["sleeve"], i["side"], i["size"], i["conid"], i["state"])
        for i in board.get("intents", [])
    ]
    return _table(["Time", "Sleeve", "Side", "Size", "Conid", "State"], rows, "no intent today")


def _venues(board: dict[str, Any]) -> str:
    rows = [
        (v["badge"], v["venue"], v["text"], "blocked" if v["blocked"] else "")
        for v in board.get("venues", [])
    ]
    return _table(["", "Venue", "Now", ""], rows, "no venue configured")


def _health(board: dict[str, Any]) -> str:
    items = list(board.get("health", []))
    items += [f"cannot read: {problem}" for problem in board.get("problems", [])]
    items += [f"alert {kind}: {key}" for key, kind in board.get("alerts", {}).items()]
    items += board.get("breakers", [])
    return _lines(items, "nothing to report")


def _settings(board: dict[str, Any]) -> str:
    rows = sorted((key, value) for key, value in board.get("settings", {}).items())
    return _table(["Setting", "Value"], rows, "none")


def render_dashboard(board: dict[str, Any]) -> str:
    """The dashboard page for ``board``."""
    title = f"ObliskIQ dashboard {board.get('day', '')}"
    sections = [
        ("Performance against the references", _performance(board)),
        ("Sleeves and their budgets", _budgets(board)),
        ("Book: positions and targets", _book(board)),
        ("Thesis book", _lines(board.get("theses", []), "nothing to report")),
        ("Patterns (research registry)", _patterns(board)),
        ("Intents today", _intents(board)),
        (
            "Cash reservations and open questions",
            _lines(board.get("capital_lines", []), "no reservation, question or unexplained cash"),
        ),
        ("Venues", _venues(board)),
        ("Health", _health(board)),
        ("Settings in force", _settings(board)),
    ]
    body = "".join(f"<section><h2>{_e(name)}</h2>{content}</section>" for name, content in sections)
    meta = (
        f"read by the service at {_e(board.get('at'))} · version {_e(board.get('version'))} · "
        f"amounts in {_e(board.get('base_currency'))}"
    )
    return (
        "<!doctype html>\n"
        '<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{_e(title)}</title><style>{_STYLE}</style></head>"
        f'<body><main><h1>{_e(title)}</h1><p class="meta">{meta}</p>{_tiles(board)}{body}'
        "</main></body></html>\n"
    )
