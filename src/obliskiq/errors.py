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
