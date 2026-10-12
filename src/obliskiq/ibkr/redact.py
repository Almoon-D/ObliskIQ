"""Keeping account ids out of logs, messages and reports.

An account id identifies the owner at the broker, so it never reaches a log line, an exception
message, a preflight report or a fixture. Every text that may carry one — an error body, a path
such as ``portfolio/<account>/ledger`` — passes through :func:`redact` first; a single id shown to
the owner goes through :func:`mask_account_id`.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import re
from collections.abc import Iterable

#: IBKR account ids: live (U, F) and paper (DU, DF) prefixes followed by digits. The same shape the
#: committed-secrets guard scans for.
ACCOUNT_ID_RE = re.compile(r"\b(?:DU|DF|U|F)\d{5,9}\b")

#: The letters of an id that survive masking: the prefix says paper or live and nothing more.
_PREFIX_RE = re.compile(r"^[A-Z]+")

MASK = "*"


def mask_account_id(account_id: str) -> str:
    """The id's letter prefix followed by one mask character per remaining character."""
    match = _PREFIX_RE.match(account_id)
    prefix = match.group(0) if match is not None else ""
    return prefix + MASK * (len(account_id) - len(prefix))


def redact(text: str, known_ids: Iterable[str] = ()) -> str:
    """``text`` with every known account id and anything shaped like one masked."""
    for account_id in sorted({i for i in known_ids if i}, key=len, reverse=True):
        text = text.replace(account_id, mask_account_id(account_id))
    return ACCOUNT_ID_RE.sub(lambda match: mask_account_id(match.group(0)), text)
