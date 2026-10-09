"""Exception hierarchy.

Errors are values the caller must handle, never a plausible-looking substitute.
A missing risk-free rate raises; it does not become 4.5.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

#: How much of an upstream response body an error message quotes. Enough to
#: identify the failure, bounded so a stray HTML page cannot fill a log line or
#: a tool response. One value: six modules each carried their own, and three of
#: them disagreed for no reason anybody could state.
ERROR_EXCERPT_CHARS = 400


class ObliskError(Exception):
    """Base for every error raised by this package."""


class ConfigError(ObliskError):
    """config.yaml is missing, malformed, or lacks a key the caller asked for."""


class EnvVarError(ObliskError):
    """A required environment variable is unset or unusable.

    Named to avoid the builtin ``EnvironmentError``, which is an alias of
    ``OSError``: shadowing it would make ``except EnvironmentError`` catch
    unrelated I/O failures.
    """


class StoreError(ObliskError):
    """The SQLite store could not be opened, migrated or written."""


class NoMatchingTargets(StoreError):
    """A ``--only`` filter matched none of a collector's targets.

    Distinct from the empty-table case, which stays fatal everywhere: a collector
    reporting success over nothing is the failure this package exists to prevent.
    This one says something narrower — the targets exist, the filter simply does
    not name any of them. Inside the ``daily`` sequence that is an artifact of
    scoping one run, because the collectors do not share a namespace: ``--only SPY``
    is meaningful to the chain collector and meaningless to the curve collector,
    which knows futures roots. Asked for directly it is still an error, because
    there the caller named something specific and did not get it.
    """


class StepDeadlineExceeded(ObliskError):
    """A step of the ``daily`` sequence outran its own wall-clock budget.

    Not an upstream failure and not a bug: it is the sequence refusing to let one
    slow step spend the window the steps behind it need. The budgets live in
    ``collectors.daily.step_deadline_seconds``, one per step, and their sum sits
    inside the unit's ``TimeoutStartSec``. What used to be a SIGTERM against the
    whole process — which no ``except`` can catch, and which ``Restart=on-failure``
    then re-ran from the top, re-spending the vendor budget — is now one step
    abandoned with the rest still to run.

    A run that ends this way is recorded ``interrupted`` rather than ``error``:
    an error names what went wrong, an interruption names that nothing got the
    chance to reach a verdict. :mod:`obliskiq.collect.runlog` already draws that
    distinction for a killed process and this is the same fact arriving early
    enough to be written down properly.
    """


class UpstreamError(ObliskError):
    """An upstream API failed, and no substitute value exists."""


class PermanentUpstreamError(UpstreamError):
    """An upstream failure that trying again cannot fix.

    The per-target retry in :mod:`obliskiq.collect.retry` caught bare
    ``Exception`` and gave every failure the same three attempts with backoff. A
    timeout deserves that. A contract that does not exist, a fixture that was
    never recorded, or a credential the broker has rejected does not: the second
    and third attempts cost the same wait and return the same answer, and on a
    collector walking dozens of targets that is most of the run spent sleeping
    before a conclusion nothing could change.

    **The default stays "retry".** Anything unclassified keeps the old
    behaviour, so raising this can only make the machine faster at failing,
    never quieter about it — the failure is still recorded, still degrades the
    run, and still reaches the runlog with its reason.
    """


class AuthError(PermanentUpstreamError):
    """Authentication against an upstream API failed.

    Permanent by nature: a rejected signature, an expired live-session token or a
    revoked consumer key returns the same answer however many times it is asked.
    Retrying one is also the shape that gets an account rate-limited.
    """


class ConvergenceError(ObliskError):
    """A numerical solver did not converge and has no value to report."""
