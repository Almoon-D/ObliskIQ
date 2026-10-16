"""Exception hierarchy.

Errors are values the caller must handle, never a plausible-looking substitute: a missing
value raises, it does not become a default. Ported from ObliskIQ-lite-MCP; each error class
lives here once something raises it.

SPDX-License-Identifier: AGPL-3.0-or-later
"""


class ObliskError(Exception):
    """Base for every error raised by this package."""


class ConfigError(ObliskError):
    """Configuration is missing, malformed, or fails validation."""


class PolicyError(ConfigError):
    """A jurisdiction profile selects a policy that is undeclared or not registered."""


class EnvVarError(ObliskError):
    """A required environment variable is unset or unusable.

    Named to avoid the builtin ``EnvironmentError``, which is an alias of ``OSError``:
    shadowing it would make ``except EnvironmentError`` catch unrelated I/O failures.
    """


class SettingsAuditError(ObliskError):
    """The settings-change audit log cannot be written, or fails its integrity check."""


class UpstreamError(ObliskError):
    """An upstream API failed, and no substitute value exists."""


class CalendarError(ObliskError):
    """The calendar layer cannot answer, and a guess would be a permissive one."""


class CalendarCoverageError(CalendarError):
    """A date falls outside the calendar a venue was built with.

    The library and the configuration know sessions only inside a window; outside it a venue
    is neither open nor closed but unknown, and callers must not read that as either.
    """


class UnknownVenueError(CalendarError):
    """A venue code that no configured venue carries."""


class UnknownExchangeCodeError(CalendarError):
    """An IBKR exchange code that maps to no venue (``SMART`` is a router, not a venue)."""


class ScheduleInconsistentError(CalendarError):
    """A session whose own times contradict one another (a break after the close, say)."""


class IncidentFileError(CalendarError):
    """The manual incident file is malformed. Every venue stays blocked until it is fixed."""


class IncidentRejectedError(CalendarError):
    """An incident outside the scope its source is trusted to raise."""


class StoreError(ObliskError):
    """The store cannot open, read or write, and a guess at what it holds would be wrong."""


class MigrationError(StoreError):
    """A migration is edited, missing, out of order, unsafe or failed; the store stays as it was."""


class ExecutionError(ObliskError):
    """The order path cannot take a step; nothing was sent that it cannot account for."""


class IllegalTransitionError(ExecutionError):
    """An intent was asked to move to a state its current state cannot reach."""


class WriteRefusedError(ExecutionError):
    """A broker write was refused before anything was sent. ``code`` names the rule."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


class ModeChangeRefusedError(ExecutionError):
    """A mode upgrade the rules do not allow (the live levels need the promotion gate)."""
