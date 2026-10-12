"""The account guard: this process uses exactly one account, and under paper only a paper one.

The configured account (``OBLISK_IBKR_ACCOUNT_ID``) must be one ``/portfolio/accounts`` lists for
the logged-in user, and under profile ``paper`` it must start with a configured paper prefix
(IBKR paper accounts start with ``DU``). Otherwise nothing starts: the client refuses before any
other read, and a later session's order path inherits the refusal. The id never appears in the
refusal, only its masked form.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Literal

from .errors import AccountGuardError
from .redact import mask_account_id


@dataclass(frozen=True)
class AccountGuard:
    account_id: str
    profile: Literal["paper", "live"]
    paper_prefixes: Sequence[str]

    def __repr__(self) -> str:
        return f"AccountGuard({mask_account_id(self.account_id)}, profile={self.profile})"

    @property
    def masked(self) -> str:
        return mask_account_id(self.account_id)

    def check_prefix(self) -> None:
        """Under profile paper, the account must carry a paper prefix."""
        if self.profile == "paper" and not any(
            self.account_id.startswith(prefix) for prefix in self.paper_prefixes
        ):
            raise AccountGuardError(
                "profile paper requires a paper account (prefix "
                f"{', '.join(self.paper_prefixes)}); the configured account {self.masked} is not "
                "one. Nothing starts."
            )

    def check(self, available: Iterable[str]) -> None:
        """Refuse unless the account is listed for this login and passes the prefix rule."""
        self.check_prefix()
        listed = {account for account in available if account}
        if self.account_id not in listed:
            raise AccountGuardError(
                f"the configured account {self.masked} is not among the {len(listed)} account(s) "
                "this login may use. Nothing starts."
            )
