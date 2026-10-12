"""No IBKR fixture carries a credential, an account id, a session id or machine identity.

The committed-secrets guard scans every tracked file for account ids and token shapes; this test
is narrower and stricter for the recorded broker answers, which are the files most likely to be
pasted from a live session: every account-like field holds the placeholder, every session, MAC,
server and user field holds a placeholder, and every file has a sidecar note.
"""

import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "ibkr"

#: The one account id a fixture may name.
PLACEHOLDER_ACCOUNT = "DU_PAPER"

#: Keys whose values identify an account.
ACCOUNT_KEYS = {"id", "accountId", "acctId", "acctcode", "accountVan", "displayName"}

#: Keys whose values identify a session, a machine or a user, and the placeholders allowed.
IDENTITY_KEYS = {
    "session": {"SESSION_PLACEHOLDER"},
    "MAC": {"00:00:00:00:00:00"},
    "serverName": {"SERVER"},
    "serverVersion": {"VERSION"},
    "hardware_info": set(),
    "userId": {0},
}

ACCOUNT_ID_RE = re.compile(r"\b(?:DU|DF|U|F)\d{5,9}\b")
CREDENTIAL_RE = re.compile(
    r"(oauth_signature|oauth_token|live_session_token|BEGIN [A-Z ]*PRIVATE KEY|password)", re.I
)


def _files() -> list[Path]:
    return sorted(path for path in FIXTURES.rglob("*") if path.is_file())


def _walk(node: Any) -> Iterator[tuple[str, Any]]:
    if isinstance(node, dict):
        for key, value in node.items():
            yield str(key), value
            yield from _walk(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk(item)


def test_there_are_fixtures_to_scan():
    assert len([p for p in _files() if p.suffix == ".json"]) >= 10


@pytest.mark.parametrize("path", _files(), ids=lambda p: p.name)
def test_no_fixture_carries_an_account_id_or_a_credential(path: Path):
    text = path.read_text(encoding="utf-8")
    assert not ACCOUNT_ID_RE.search(text), f"{path.name} carries an account id"
    if path.suffix == ".json":
        assert not CREDENTIAL_RE.search(text), f"{path.name} carries a credential"


@pytest.mark.parametrize("path", [p for p in _files() if p.suffix == ".json"], ids=lambda p: p.name)
def test_identity_fields_hold_placeholders(path: Path):
    data = json.loads(path.read_text(encoding="utf-8"))
    for key, value in _walk(data):
        if key in ACCOUNT_KEYS and isinstance(value, str) and value.startswith(("D", "U", "F")):
            assert value.startswith("DU_"), f"{path.name}: {key} is not a placeholder"
        if key in IDENTITY_KEYS:
            assert value in IDENTITY_KEYS[key], f"{path.name}: {key} is not a placeholder"
        if key == "canTradeAcctIds":
            assert all(item == PLACEHOLDER_ACCOUNT for item in value)


def test_every_fixture_is_described_in_the_sidecar_note():
    note = (FIXTURES / "README.md").read_text(encoding="utf-8")
    for path in _files():
        if path.suffix == ".json":
            assert f"`{path.name}`" in note, f"{path.name} has no sidecar entry"


def test_the_scan_catches_a_planted_account_id():
    """Positive control: built at runtime so this file holds no id itself."""
    planted = "DU" + "7654321"
    assert ACCOUNT_ID_RE.search(f'{{"acctId": "{planted}"}}')
