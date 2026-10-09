"""Every environment variable the code reads is named in ``.env.template``.

The inverse rule, applied to the third declaration file that had no guard.
``tests/test_no_unread_config_keys.py`` enforces it for ``config.yaml`` and
``tests/test_no_unread_universe_keys.py`` for ``universe.yaml``, both under the
same standing rule: a declaration nothing reads is a decision that looks made
and is not. This is the other direction, and for the environment it is the one
that bites — a variable the CODE reads and the TEMPLATE never mentions is an
operator provisioning a VM from a file that does not tell them the whole truth,
and the symptom arrives as a startup error on a machine whose whole purpose is
to need no attention.

``tests/test_no_committed_secrets.py`` already scans these templates, but it
asks the opposite question: that every slot present is EMPTY. A variable that
is missing altogether is empty in exactly the way it cannot see.

The drift this catches is real and was close. ``CLAUDE.md``'s standing rule said
"nine credentials" for three phases before anybody counted; it says seven now,
"``OBLISK_IBKR_REALM`` and ``OBLISK_IBKR_ACCOUNT_ID`` are read by nothing ...
Neither env template ships them." Both halves of that were verified by hand, and
a hand sweep is exactly what stops happening.

Runs standalone, with no dependencies at all:

    python3 tests/test_env_template_is_complete.py
"""

import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: The one template an operator copies to start from. The three under deploy/
#: are deliberately PARTIAL — the three-file split exists so the compute unit
#: holds no credential it cannot use — so completeness is asked of this one only.
#: See docs/DECISIONS.md, "the three files stay three".
TEMPLATE = ".env.template"

#: Where the variables are read. Every environment lookup in this package goes
#: through env.py's accessors; nothing else calls os.environ for configuration.
ENV_MODULE = os.path.join("src", "obliskiq", "env.py")

#: A name read by env.py that the template correctly does NOT ship, with the
#: reason. Exempting one is a visible decision, the way NON_SECRET is in
#: test_no_committed_secrets.py — never a silent widening.
NOT_IN_TEMPLATE = {
    # It NAMES the env file. Setting it inside the file it points at cannot
    # work, and docs/COLLECTORS.md documents it where it is actually used: on
    # the `sudo -u obliskiq OBLISK_ENV_FILE=/etc/obliskiq/collect.env` command
    # line, because sudo does not read the unit's EnvironmentFile.
    "OBLISK_ENV_FILE": "names the env file itself, so it cannot live inside it",
}

#: A bare UPPER_SNAKE string literal in env.py. Deliberately NOT keyed to the
#: accessor that wraps it: the first draft of this guard matched `_get(...)`,
#: `require(...)` and their siblings by name, and missed the three key paths
#: because they go through a `_path_or_none` closure — a hand-kept list of
#: function names that goes stale the next time somebody adds a helper, which
#: is the failure mode this repository deletes config keys over. env.py's whole
#: job is reading the environment, so an UPPER_SNAKE literal in it is a
#: variable name unless it is one of the few VALUES below.
_READ_RE = re.compile(r"""["']([A-Z][A-Z0-9_]*)["']""")

#: UPPER_SNAKE literals in env.py that are values rather than variable names,
#: with what each one is. Two, and both are defaults for a variable that is
#: itself in the list. Exempting one is a visible decision, never a silent
#: widening — the same convention NON_SECRET follows in
#: test_no_committed_secrets.py.
NOT_A_VARIABLE = {
    "INFO": "the default OBLISK_LOG_LEVEL",
    "UTC": "the default OBLISK_TZ",
}

#: An assignment in a template: optional `export`, NAME, `=`. Mirrors the
#: parser in test_no_committed_secrets.py; only the NAME is needed here.
_ASSIGNMENT_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")


def _read(relative):
    with open(os.path.join(ROOT, relative), encoding="utf-8") as handle:
        return handle.read()


def variables_read(source):
    """Every environment variable name ``source`` looks up."""
    return set(_READ_RE.findall(source)) - set(NOT_A_VARIABLE)


def variables_declared(text):
    """Every variable name a template assigns, commented-out lines included.

    A template documents a variable by naming it; whether the shipped default is
    active or commented is a separate question from whether an operator reading
    this file learns the variable exists.
    """
    declared = set()
    for line in text.splitlines():
        candidate = line.lstrip()
        if candidate.startswith("#"):
            candidate = candidate.lstrip("#").strip()
        match = _ASSIGNMENT_RE.match(candidate)
        if match:
            declared.add(match.group(1))
    return declared


def test_the_template_names_every_variable_the_code_reads():
    read = variables_read(_read(ENV_MODULE))
    declared = variables_declared(_read(TEMPLATE))

    missing = sorted(read - declared - set(NOT_IN_TEMPLATE))
    if missing:
        raise AssertionError(
            "\n".join(
                [
                    f"{ENV_MODULE} reads a variable {TEMPLATE} never names:",
                    "",
                    *(f"  {name}" for name in missing),
                    "",
                    f"Fix: add it to {TEMPLATE} with an empty value and a line saying",
                    "where to get it — an operator provisions from that file. If it is",
                    "genuinely not settable there, add it to NOT_IN_TEMPLATE in this",
                    "file with the reason.",
                ]
            )
        )


def test_the_template_names_nothing_the_code_does_not_read():
    """The orphan half, and the one CLAUDE.md's credential count got wrong.

    A variable in the template that nothing reads is the ``OBLISK_IBKR_REALM``
    mistake: an operator fills it in, it changes nothing, and the file reads as
    though somebody decided it mattered.
    """
    read = variables_read(_read(ENV_MODULE))
    declared = variables_declared(_read(TEMPLATE))

    orphans = sorted(declared - read)
    if orphans:
        raise AssertionError(
            "\n".join(
                [
                    f"{TEMPLATE} names a variable nothing in {ENV_MODULE} reads:",
                    "",
                    *(f"  {name}" for name in orphans),
                    "",
                    "Fix: delete it. A slot nothing reads is a decision that looks",
                    "made and is not — see docs/DECISIONS.md on OBLISK_IBKR_REALM.",
                ]
            )
        )


def test_the_retired_credentials_are_in_neither_place():
    """``CLAUDE.md`` says seven cp_api credentials, not the nine it said for
    three phases. These two are the difference, and the rule states outright
    that neither env template ships them."""
    retired = ("OBLISK_IBKR_REALM", "OBLISK_IBKR_ACCOUNT_ID")
    source = _read(ENV_MODULE)
    template = _read(TEMPLATE)
    for name in retired:
        assert name not in source, (
            f"{name} is read again in {ENV_MODULE}; CLAUDE.md says nothing reads it"
        )
        assert name not in template, (
            f"{name} is back in {TEMPLATE}; CLAUDE.md says neither template ships it"
        )


def test_the_scan_catches_a_missing_variable():
    """The positive control. A test that cannot fail is not a test.

    Both directions are exercised, because a scan that found nothing either way
    would pass on a parser that matched nothing at all.
    """
    read = variables_read('    value = _get("OBLISK_INVENTED_SETTING")\n')
    assert read == {"OBLISK_INVENTED_SETTING"}, "the reader scan no longer finds a lookup"

    declared = variables_declared(
        "# a comment\nOBLISK_REAL=1\n#OBLISK_COMMENTED=\nexport FOO=bar\n"
    )
    assert "OBLISK_REAL" in declared, "a plain assignment is no longer detected"
    assert "OBLISK_COMMENTED" in declared, "a commented-out declaration is no longer detected"
    assert "FOO" in declared, "an exported assignment is no longer detected"

    assert read - declared - set(NOT_IN_TEMPLATE), (
        "a variable read but never declared must be reported as missing"
    )


def test_the_guard_reads_the_files_it_claims_to():
    """Neither path may quietly stop existing: a scan over a missing file finds
    nothing and passes, which is the failure this whole family exists to stop."""
    for relative in (TEMPLATE, ENV_MODULE):
        assert os.path.isfile(os.path.join(ROOT, relative)), f"{relative} is missing"
    assert variables_read(_read(ENV_MODULE)), "no variable was found in env.py at all"
    assert variables_declared(_read(TEMPLATE)), "no variable was found in the template at all"


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        try:
            fn()
        except AssertionError as exc:
            failures += 1
            sys.stderr.write(f"FAIL {name}\n{exc}\n\n")
        else:
            sys.stdout.write(f"ok   {name}\n")
    sys.exit(1 if failures else 0)
