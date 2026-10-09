"""Helpers every tool module shares.

Two conventions live here and nowhere else:

**Errors reach the model as text it can act on.** A gate closing is not a server
fault — it is the answer. So a refusal is raised as a ``ToolError`` carrying the
gate's own reason verbatim, because the reason is the part the session has to
read out and quote.

**Every response says what it was measured on.** ``as_of``, the store's own
freshness, and whether the row set was truncated. A number arriving without its
date is how a weekend session builds recommendations on Friday's marks.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from typing import Any, TypeVar

from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations

from ...errors import ObliskError
from ...util.timeutil import iso_date
from ..context import ServerContext, run_blocking
from ..encode import to_jsonable

T = TypeVar("T")

#: Annotations for a tool that only reads. A client may call it freely.
READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)

#: Annotations for a tool that writes to the ledger. Never destructive — the
#: ledger is append-only and a thesis is frozen at entry — but not idempotent
#: either: calling it twice writes two rows.
LEDGER_WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False)

#: Annotations for a write that converges on the same state when repeated.
UPSERT = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True)


def refuse(message: str) -> ToolError:
    """Build the error a caller sees when a rule closed on them."""
    return ToolError(message)


async def call(name: str, fn: Callable[[], T]) -> T:
    """Run one tool's blocking work off the event loop, translating failures.

    A rule closing on the caller is the answer, not a fault, so a ``ToolError``
    raised inside ``fn`` passes through with its reason intact. Anything else
    becomes one, named, rather than reaching the client as an unattributed
    traceback.
    """
    try:
        return await run_blocking(fn)
    except ToolError:
        raise
    except (ObliskError, ValueError) as exc:
        raise ToolError(f"{name}: {exc}") from exc
    except Exception as exc:
        raise ToolError(f"{name} failed: {exc}") from exc


def envelope(
    context: ServerContext,
    *,
    as_of: date,
    data: dict[str, Any],
    source_as_of: date | None = None,
    truncated: bool = False,
) -> dict[str, Any]:
    """Wrap a payload with the provenance every table has to carry.

    ``source_as_of`` is the date the underlying rows were actually computed for,
    which is not always the date asked about: a grid read on a Sunday legitimately
    returns Friday's build. Reporting it, and how stale it is, is the difference
    between using old data and being handed old data.
    """
    payload: dict[str, Any] = {"as_of": iso_date(as_of), **data}
    if source_as_of is not None:
        age = (as_of - source_as_of).days
        payload["source_as_of"] = iso_date(source_as_of)
        payload["age_days"] = age
        payload["stale"] = age > context.stale_after_days
        if payload["stale"]:
            payload["stale_note"] = (
                f"the newest data is {age} day(s) old, beyond server.stale_after_days "
                f"({context.stale_after_days}). Label anything derived from it as "
                "indicative, and never mix it with live quotes unmarked."
            )
    if truncated:
        payload["truncated"] = True
        payload["truncated_note"] = (
            f"the result was capped at server.max_rows_per_response "
            f"({context.max_rows}). Narrow the request rather than reading a "
            "partial table as a complete one."
        )
    # Encoded once, here, at the only exit. A tool that forgot to encode a nested
    # engine result should not be discovered by a client failing to deserialise it.
    return to_jsonable(payload)
