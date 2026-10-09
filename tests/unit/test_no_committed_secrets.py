"""No tracked file carries a credential, an account id, key material or personal data.

Adapted from ObliskIQ-lite-MCP. A value committed once is in every clone forever, and
deleting the line does not remove it from history, so the guard runs on every change.

* Every assignment in a tracked environment template is **empty**: templates name
  variables, ``.env`` (gitignored) holds values. Exempting a variable is a visible entry in
  :data:`NON_SECRET`, never a silent widening.
* No key material (``.pem``, ``.key``, ``.p12``, …) and no filled ``.env`` is tracked.
* Every tracked text file — ``legacy/`` included, because it is committed too — is scanned
  for private keys, IBKR account ids, API-token shapes and email addresses. The few
  placeholders that look like one are listed with the reason.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
THIS_FILE = Path(__file__).resolve()

#: Tracked files that declare environment variables. Each is scanned in full.
TEMPLATES = [".env.example"]

#: Variables whose template value is configuration rather than a credential, with the
#: reason. Anything not named here must be empty in every template.
NON_SECRET: dict[str, str] = {}

#: Environment templates that may be tracked, with the reason.
TRACKED_ENV_FILES = {
    ".env.example": "names every variable ObliskIQ reads; every value is empty",
    "legacy/pulse/.env.example": "market-pulse-bot's template: paths and display settings only",
    "legacy/lite/.env.template": "ObliskIQ-lite-MCP's template: credential slots empty; only "
    "paths, hosts, a port and log settings carry values",
}

KEY_MATERIAL_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".jks", ".keystore", ".crt", ".der")

#: KEY=value at the start of a line, with an optional `export `.
ASSIGNMENT_RE = re.compile(r"^\s*(?:export\s+)?([A-Z][A-Z0-9_]*)\s*=([^#\n]*)")

#: A PEM private key WITH a body: a header followed by base64. A template that only shows
#: where a key goes (a header and a placeholder) is not key material.
PRIVATE_KEY_RE = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----\s*\n[A-Za-z0-9+/=]{40,}")

#: IBKR account ids: live (U, F) and paper (DU, DF) prefixes with six to eight digits.
ACCOUNT_ID_RE = re.compile(r"\b(?:DU|DF|U|F)\d{6,8}\b")

TOKEN_RES = {
    "GitHub token": re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{22,})"),
    "AWS access key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "Google API key": re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
    "Telegram bot token": re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b"),
    "secret-key token": re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"),
}

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,})")

#: Strings that look like an account id or an address but are not anybody's, with why.
PLACEHOLDERS = {
    "U0000000": "an all-zero account id in ObliskIQ-lite-MCP's recorded IBKR fixtures",
    "U1234567": "a sequence account id in ObliskIQ-lite-MCP's client tests",
    "your_email@gmail.com": "the placeholder in Inversor-Inteligente's email notifier docs",
    "cpi@ons.gov.uk": "the ONS's published contact, inside a recorded ONS API response",
}

#: Domains that are never a person's address.
NON_PERSONAL_DOMAINS = ("example.com", "example.org", "example.net", "users.noreply.github.com")

#: systemd unit names (``obliskiq-collect@daily.timer``) are shaped like addresses.
SYSTEMD_UNIT_SUFFIXES = (".service", ".timer", ".socket", ".target", ".path", ".mount")


def _tracked_files() -> list[str]:
    try:
        listing = subprocess.run(
            ["git", "ls-files", "-z"], cwd=ROOT, check=True, capture_output=True
        ).stdout.decode("utf-8")
    except OSError, subprocess.CalledProcessError:  # pragma: no cover - not a git checkout
        pytest.skip("not a git checkout")
    return [name for name in listing.split("\0") if name]


def _assignments(text: str) -> list[tuple[int, str, str]]:
    found = []
    for line_no, line in enumerate(text.splitlines(), start=1):
        match = ASSIGNMENT_RE.match(line)
        if match:
            found.append((line_no, match.group(1), match.group(2).strip()))
    return found


def findings(text: str) -> list[str]:
    """What in ``text`` looks like a credential, an account id or a personal address."""
    found: list[str] = []
    if PRIVATE_KEY_RE.search(text):
        found.append("a PEM private key")
    for match in ACCOUNT_ID_RE.finditer(text):
        if match.group(0) not in PLACEHOLDERS:
            found.append(f"an IBKR account id ({match.group(0)[:2]}…)")
    for label, pattern in TOKEN_RES.items():
        if pattern.search(text):
            found.append(f"a {label}")
    for match in EMAIL_RE.finditer(text):
        address, domain = match.group(0), match.group(1).lower()
        if address in PLACEHOLDERS or domain.endswith(SYSTEMD_UNIT_SUFFIXES):
            continue
        if domain.endswith(NON_PERSONAL_DOMAINS) or address.lower().startswith("noreply@"):
            continue
        found.append("an email address")
    return found


def test_every_slot_in_every_template_is_empty():
    offenders = []
    for relative in TEMPLATES:
        text = (ROOT / relative).read_text(encoding="utf-8")
        for line_no, name, value in _assignments(text):
            if name in NON_SECRET or not value:
                continue
            offenders.append(f"  {relative}:{line_no}  {name}= (a value is present)")
    assert not offenders, (
        "A tracked template carries a value. Templates name variables; .env holds values.\n"
        + "\n".join(offenders)
        + "\nBlank it here, and reissue anything already committed: deleting the line does "
        "not remove it from the history."
    )


def test_the_templates_this_guards_still_exist():
    """A guard pointed at a renamed file passes vacuously."""
    for relative in TEMPLATES:
        assert (ROOT / relative).is_file(), relative


def test_no_key_material_or_filled_env_file_is_tracked():
    bad = []
    for name in _tracked_files():
        base = name.rsplit("/", 1)[-1]
        is_env_file = base == ".env" or base.startswith(".env.")
        if name.endswith(KEY_MATERIAL_SUFFIXES) or (is_env_file and name not in TRACKED_ENV_FILES):
            bad.append(name)
    assert not bad, f"key material or an environment file is tracked: {bad}"


def test_no_tracked_file_carries_a_credential_or_personal_data():
    offenders: dict[str, list[str]] = {}
    for name in _tracked_files():
        path = ROOT / name
        if path.resolve() == THIS_FILE or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        found = findings(text)
        if found:
            offenders[name] = sorted(set(found))
    assert not offenders, (
        f"tracked files carry what looks like credentials or personal data: {offenders}. "
        "Remove it (and rotate anything real). A placeholder that is nobody's belongs in "
        "PLACEHOLDERS with the reason."
    )


# ── positive controls: the scanners must still see what they exist to catch ──


def test_the_template_scan_catches_a_value():
    sample = (
        "# a comment\n"
        "FRED_API_KEY=872b05a055ba41ad95f50a7c1064e274\n"
        "EMPTY_ONE=\n"
        "export TELEGRAM_BOT_TOKEN=abc123   # with a trailing comment\n"
    )
    found = {name: value for _line, name, value in _assignments(sample)}
    assert found["FRED_API_KEY"]
    assert found["TELEGRAM_BOT_TOKEN"] == "abc123"
    assert found["EMPTY_ONE"] == ""


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("-----BEGIN PRIVATE KEY-----\n" + "A" * 64 + "\n", "a PEM private key"),
        ("-----BEGIN RSA PRIVATE KEY-----\n" + "Qk" * 40 + "\n", "a PEM private key"),
        ("account DU7654321 holds", "an IBKR account id (DU…)"),
        ('{"acctId": "U7654321"}', "an IBKR account id (U7…)"),
        ("token ghp_" + "a" * 36, "a GitHub token"),
        ("AKIA" + "B" * 16, "a AWS access key"),
        ("AIza" + "c" * 35, "a Google API key"),
        ("1234567890:" + "d" * 35, "a Telegram bot token"),
        ("key sk-" + "e" * 24, "a secret-key token"),
        ("mail someone.real@gmail.com today", "an email address"),
    ],
)
def test_the_content_scan_catches_each_kind(text, expected):
    assert expected in findings(text)


@pytest.mark.parametrize(
    "text",
    [
        "-----BEGIN PRIVATE KEY-----\n<paper signature key>\n-----END PRIVATE KEY-----\n",
        "the fixture uses U0000000",
        "systemctl start obliskiq-collect@daily.timer",
        "contact noreply@anthropic.com or someone@example.com",
        "IBKR paper accounts start with DU",
    ],
)
def test_the_content_scan_spares_placeholders(text):
    assert findings(text) == []


def test_every_placeholder_has_a_reason():
    assert all(PLACEHOLDERS.values())
    assert all(TRACKED_ENV_FILES.values())
