"""No tracked file carries a credential. The templates name variables, not values.

``.env.template`` used to ship working FRED, EIA and marketdata.app keys under a
"the repository is private" caveat. Private is a property of today's access
list, not of the history: a value committed once is in every clone forever, and
deleting the line does not remove it. The keys were removed and must be
reissued; this test is what stops the next one arriving.

The rule is deliberately blunt. **Every assignment in a tracked ``*.env*``
template is empty**, whatever the variable is called, because a scanner that
tries to decide which names are secret will eventually decide wrong. A default
that genuinely belongs in the template — a host, a path, a log level — is not a
credential and is exempt by name, in ``NON_SECRET``, where adding one is a
visible decision rather than a silent widening.

The private-key files themselves are covered by ``secrets/.gitignore`` and by
``test_no_pem_is_tracked`` below.

Runs standalone, with no dependencies at all:

    python3 tests/test_no_committed_secrets.py
"""

import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: Tracked files that declare environment variables. Each is scanned in full.
TEMPLATES = [
    ".env.template",
    os.path.join("deploy", "collect.env.example"),
    os.path.join("deploy", "compute.env.example"),
    os.path.join("deploy", "server.env.example"),
]

#: Variables whose value is configuration rather than a credential, with the
#: reason. Anything not named here must be empty in every template.
NON_SECRET = {
    "OBLISK_DB_PATH": "a filesystem path",
    "OBLISK_CONFIG_PATH": "a filesystem path",
    "OBLISK_LOG_LEVEL": "a log level",
    "OBLISK_LOG_FORMAT": "json or text",
    "OBLISK_TZ": "an IANA timezone",
    "OBLISK_MCP_HOST": "an interface to bind",
    "OBLISK_MCP_PORT": "a port",
    "OBLISK_IBKR_CP_BASE_URL": "a public API base URL",
    "OBLISK_IBKR_CONNECTOR_URL": "a public MCP endpoint URL, never a credential",
    "OBLISK_IBKR_SIGNATURE_KEY": "a path to a key file, never the key",
    "OBLISK_IBKR_ENCRYPTION_KEY": "a path to a key file, never the key",
    "OBLISK_IBKR_DH_PARAM": "a path to a parameter file, never a secret",
    "OBLISK_IBKR_CA_BUNDLE": "a path to a CA bundle",
    "PATH": "the shell's own PATH, set by the deployment example",
    "SHELL": "the shell cron runs commands under",
    "BASH_ENV": "a path to the environment file cron sources",
}

#: KEY=value at the start of a line, with an optional `export `. The value runs
#: to a trailing comment or the end of the line.
ASSIGNMENT_RE = re.compile(r"^\s*(?:export\s+)?([A-Z][A-Z0-9_]*)\s*=([^#\n]*)")


def _assignments(text):
    """[(line_no, name, value)] for every assignment in a template."""
    found = []
    for line_no, line in enumerate(text.splitlines(), start=1):
        match = ASSIGNMENT_RE.match(line)
        if match:
            found.append((line_no, match.group(1), match.group(2).strip()))
    return found


def test_every_secret_slot_in_every_template_is_empty():
    offenders = []
    for relative in TEMPLATES:
        path = os.path.join(ROOT, relative)
        if not os.path.isfile(path):
            continue
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
        for line_no, name, value in _assignments(text):
            if name in NON_SECRET or not value:
                continue
            offenders.append(f"  {relative}:{line_no}  {name}= (a value is present)")

    if offenders:
        raise AssertionError(
            "\n".join(
                [
                    "A tracked template carries a value. Templates name variables;",
                    ".env holds values.",
                    "",
                    *offenders,
                    "",
                    "Fix: blank the value here and put it in .env, which is gitignored.",
                    "If the variable is configuration rather than a credential, add it to",
                    "NON_SECRET in this file with the reason — and reissue anything that has",
                    "already been committed, because deleting the line does not remove it",
                    "from the history.",
                ]
            )
        )


def test_the_templates_this_guards_still_exist():
    """A guard pointed at a renamed file passes vacuously."""
    present = [r for r in TEMPLATES if os.path.isfile(os.path.join(ROOT, r))]
    assert present, "none of the guarded templates exist; TEMPLATES needs updating"
    assert ".env.template" in present, ".env.template is the one that must always be scanned"


def test_no_pem_or_key_material_is_tracked():
    """Private keys live under secrets/, which is gitignored except for its own rule."""
    try:
        listing = subprocess.check_output(
            ["git", "ls-files"], cwd=ROOT, stderr=subprocess.DEVNULL
        ).decode("utf-8")
    except (OSError, subprocess.CalledProcessError):  # pragma: no cover - not a git checkout
        return
    tracked = [line for line in listing.splitlines() if line]
    bad = [
        name
        for name in tracked
        if name.endswith((".pem", ".key", ".p12", ".pfx")) or name == ".env"
    ]
    assert not bad, "key material or a filled .env is tracked: {}".format(", ".join(bad))


def test_the_scan_catches_a_value():
    """Positive control: the regex must still see a filled slot.

    Without this, a broken ASSIGNMENT_RE would make the scan above pass over a
    template full of live keys.
    """
    sample = (
        "# a comment\n"
        "FRED_API_KEY=872b05a055ba41ad95f50a7c1064e274\n"
        "EMPTY_ONE=\n"
        "export MARKETDATA_API_TOKEN=abc123   # with a trailing comment\n"
        "OBLISK_MCP_PORT=8000\n"
    )
    found = {name: value for _line, name, value in _assignments(sample)}
    assert found["FRED_API_KEY"], "a filled slot is no longer detected"
    assert found["MARKETDATA_API_TOKEN"] == "abc123", "export and trailing comments broke parsing"
    assert found["EMPTY_ONE"] == "", "an empty slot must read as empty, not as missing"
    assert "OBLISK_MCP_PORT" in NON_SECRET, "the exemption list no longer covers the port"


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
