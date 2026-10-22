"""Every key in ``config/`` has a consumer in ``src/obliskiq/``, or it is not in ``config/``.

Adapted from ObliskIQ-lite-MCP. A key nothing reads is a decision that looks made and is
not: setting it would do nothing while reading as a guarantee. Unknown keys are already
refused by the schema (``extra="forbid"``); this test asks the other question — does the
code actually read each key the files declare?

**What counts as reading a key.** The configuration is a tree of frozen pydantic models,
so code reads ``settings.account.base_currency``. A key ``a.b.c`` is consumed when some
attribute chain in ``src/obliskiq/`` contains ``a``, ``b``, ``c`` consecutively, after
resolving local bindings (``account = settings.account`` then ``account.base_currency``).
Keys of a jurisdiction profile are namespaced ``profile.<key>``, because code holds a
profile in a variable or attribute named ``profile``. A section listed in
:data:`RUNTIME_KEYED` is read wholesale and keyed at runtime; reading the section consumes
its children.

A section listed in :data:`KEYED_MAPPINGS` is a mapping keyed by data (a venue's MIC, an IBKR
exchange code) whose entries share one shape. The key means nothing to a reader: code reads
``entry.sources.calendar.origin`` off whichever entry it holds. So for
``venues.XNYS.sources.calendar.origin`` the section and the key are stripped and
``sources.calendar.origin`` must appear as consecutive attributes in a chain inside the
package named for that section. An entry's field nothing in the package reads is an orphan.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import ast
import functools
from pathlib import Path

import yaml

from obliskiq.config.loader import flatten

ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = ROOT / "config"
SOURCE_DIR = ROOT / "src" / "obliskiq"
PROFILE_PREFIX = "profile"

#: Sections read wholesale and keyed at runtime, with the reason. Their children are data.
RUNTIME_KEYED = {
    "profile.policies": "policy kind -> policy id, keyed by the registry resolving that kind",
    "collectors.step_deadline_seconds": "step -> budget, read by the step that runs",
    "collectors.candidates.sectors": "an issuer's sector name -> a grid sector, by the line's",
    "collectors.candidates.locations": "an issuer's location -> a country, by the line's",
    "collectors.candidates.exchanges": "an issuer's exchange name -> an IBKR code, by the line's",
    "research.data.yahoo.suffixes": "IBKR exchange -> Yahoo suffix, by the listing's exchange",
    "research.data.yahoo.digits": "IBKR exchange -> Yahoo's code width, by the listing's",
    "core.roles": "role id -> its name and exposure series, by the exposure asked for",
}

#: Profile sections the code holds by their own name (a typed model passed to a policy), with
#: the reason: ``profile.tax.payment.day`` is read as ``tax.payment.day``.
PROFILE_SECTIONS_HELD_BY_NAME = {
    "tax": "a tax policy is built from the profile's tax section and reads it as `tax`",
    "eligibility": (
        "an eligibility policy is built from the profile's eligibility section and reads it as "
        "`eligibility`"
    ),
}

#: Sections keyed by data, with the package that must read the fields beneath each key.
KEYED_MAPPINGS = {
    "venues": "calendars",
    "synthetic_calendars": "calendars",
    "lunch_breaks": "calendars",
    "seasonal_closes": "calendars",
    "ibkr_exchanges": "calendars",
}

#: Roots that carry no meaning in a chain.
_ANONYMOUS_ROOTS = {"self", "cls"}


# ── reading config/ ──


def config_leaves() -> dict[str, str]:
    """Every leaf the files declare, namespaced, mapped to the file that declares it."""
    leaves: dict[str, str] = {}
    for path in [*sorted((CONFIG_DIR / "defaults").glob("*.yaml")), CONFIG_DIR / "owner.yaml"]:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for key in flatten(data):
            leaves[key] = str(path.relative_to(ROOT))
    for path in sorted((CONFIG_DIR / "jurisdictions").glob("*.yaml")):
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for key in flatten(data):
            leaves[f"{PROFILE_PREFIX}.{key}"] = str(path.relative_to(ROOT))
    return leaves


# ── reading the code ──


def _normalise(name: str) -> str:
    return name.lstrip("_")


def _raw_chain(node: ast.expr) -> list[str] | None:
    """``a.b.c`` → ``["a", "b", "c"]``; a chain rooted in a call keeps only its attributes."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    elif not parts:
        return None
    parts.reverse()
    if parts and parts[0] in _ANONYMOUS_ROOTS:
        parts = parts[1:]
    return [_normalise(part) for part in parts]


def _bindings(tree: ast.AST) -> dict[str, list[str]]:
    """Local names bound to an attribute chain: ``account = settings.account``."""
    bound: dict[str, list[str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target, value = node.targets[0], node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            target, value = node.target, node.value
        else:
            continue
        if not isinstance(value, ast.Attribute):
            continue
        name = _raw_chain(target) if isinstance(target, ast.Attribute | ast.Name) else None
        chain = _raw_chain(value)
        if name and len(name) == 1 and chain:
            bound[name[0]] = chain
    return bound


def attribute_chains(source: str) -> list[list[str]]:
    """Every maximal attribute chain in ``source``, with local bindings resolved."""
    tree = ast.parse(source)
    bound = _bindings(tree)
    inner = {id(node.value) for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    chains: list[list[str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute) or id(node) in inner:
            continue
        chain = _raw_chain(node)
        if not chain:
            continue
        for _ in range(3):  # a binding may itself start from a binding
            if chain[0] in bound and bound[chain[0]] != chain[:1]:
                chain = bound[chain[0]] + chain[1:]
        chains.append(chain)
    return chains


def _source_chains() -> list[list[str]]:
    chains: list[list[str]] = []
    for path in sorted(SOURCE_DIR.rglob("*.py")):
        chains.extend(attribute_chains(path.read_text(encoding="utf-8")))
    return chains


def _contains(chain: list[str], wanted: list[str]) -> bool:
    width = len(wanted)
    return any(chain[i : i + width] == wanted for i in range(len(chain) - width + 1))


def _package_chains(package: str) -> list[list[str]]:
    chains: list[list[str]] = []
    for path in sorted((SOURCE_DIR / package).rglob("*.py")):
        chains.extend(attribute_chains(path.read_text(encoding="utf-8")))
    return chains


@functools.cache
def _dotted_keys() -> dict[str, tuple[str, ...]]:
    """The runtime keys of each keyed section that hold a dot themselves (IBKR's BVME.ETF)."""
    keys: dict[str, set[str]] = {}
    for path in [*sorted((CONFIG_DIR / "defaults").glob("*.yaml")), CONFIG_DIR / "owner.yaml"]:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for section in KEYED_MAPPINGS:
            keys.setdefault(section, set()).update(
                str(k) for k in (data.get(section) or {}) if "." in str(k)
            )
    return {section: tuple(sorted(found)) for section, found in keys.items()}


def _keyed_chains() -> dict[str, list[list[str]]]:
    return {package: _package_chains(package) for package in set(KEYED_MAPPINGS.values())}


def unread_keys(
    leaves: dict[str, str],
    chains: list[list[str]],
    keyed_chains: dict[str, list[list[str]]] | None = None,
) -> list[str]:
    keyed_chains = keyed_chains or {}
    orphans: list[str] = []
    for key in sorted(leaves):
        head = key.split(".")[0]
        if head in KEYED_MAPPINGS:
            # Strip the section and the runtime key, which may itself hold a dot (BVME.ETF).
            dotted = [k for k in _dotted_keys().get(head, ()) if key.startswith(f"{head}.{k}.")]
            cut = 1 + (dotted[0].count(".") + 1 if dotted else 1)
            fields = key.split(".")[cut:]
            pool = keyed_chains.get(KEYED_MAPPINGS[head], [])
            if not any(_contains(chain, fields or [head]) for chain in pool):
                orphans.append(key)
            continue
        section = next((s for s in RUNTIME_KEYED if key.startswith(f"{s}.")), None)
        wanted = (section or key).split(".")
        options = [wanted]
        if (
            wanted[0] == PROFILE_PREFIX
            and wanted[1:2]
            and wanted[1] in PROFILE_SECTIONS_HELD_BY_NAME
        ):
            options.append(wanted[1:])
        if not any(_contains(chain, option) for chain in chains for option in options):
            orphans.append(key)
    return orphans


# ── tests ──


def test_there_is_a_config_to_scan():
    """A scan over nothing passes vacuously."""
    leaves = config_leaves()
    assert {"account.base_currency", "jurisdiction", "profile.id", "profile.name"} <= set(leaves)
    assert len(list(SOURCE_DIR.rglob("*.py"))) >= 10


def test_every_config_key_has_a_consumer():
    leaves = config_leaves()
    orphans = unread_keys(leaves, _source_chains(), _keyed_chains())
    listed = "\n  ".join(f"{key}  ({leaves[key]})" for key in orphans)
    assert not orphans, (
        f"config/ declares {len(orphans)} key(s) nothing in src/obliskiq/ reads:\n  {listed}\n\n"
        "Give each a consumer or delete it. A key set to a value no code path can observe "
        "reads as a guarantee while enforcing nothing."
    )


def test_the_scan_catches_an_orphan_key():
    """Positive control: without it, a resolver that matched everything would pass."""
    leaves = {**config_leaves(), "account.a_key_nobody_reads": "config/owner.yaml"}
    assert unread_keys(leaves, _source_chains(), _keyed_chains()) == ["account.a_key_nobody_reads"]


def test_a_namespaced_profile_key_needs_its_namespace():
    """``name`` alone is read everywhere; ``profile.name`` must be read off a profile."""
    chains = attribute_chains("def f(x):\n    return x.name\n")
    assert unread_keys({"profile.name": "x"}, chains) == ["profile.name"]
    chains = attribute_chains("def f(loaded):\n    return loaded.profile.name\n")
    assert unread_keys({"profile.name": "x"}, chains) == []


def test_a_profile_section_held_by_name_is_read_without_the_profile():
    chains = attribute_chains("def f(tax):\n    return tax.payment.day\n")
    assert unread_keys({"profile.tax.payment.day": "x"}, chains) == []
    assert unread_keys({"profile.tax.payment.month": "x"}, chains) == ["profile.tax.payment.month"]
    # Only a listed section: another profile key still needs its namespace.
    chains = attribute_chains("def f(x):\n    return x.name\n")
    assert unread_keys({"profile.name": "x"}, chains) == ["profile.name"]


def test_a_local_binding_is_resolved():
    source = "def f(settings):\n    account = settings.account\n    return account.base_currency\n"
    assert ["settings", "account", "base_currency"] in attribute_chains(source)
    assert unread_keys({"account.base_currency": "x"}, attribute_chains(source)) == []


def test_a_self_attribute_reads_as_its_name():
    source = "class A:\n    def f(self):\n        return self._profile.policies.get('tax')\n"
    assert ["profile", "policies", "get"] in attribute_chains(source)


def test_a_runtime_keyed_section_covers_its_children_only_when_read():
    leaves = {"profile.policies.tax": "x"}
    assert unread_keys(leaves, [["profile", "policies", "get"]]) == []
    assert unread_keys(leaves, [["profile", "name"]]) == ["profile.policies.tax"]


def test_every_runtime_keyed_section_has_a_reason():
    for section, reason in RUNTIME_KEYED.items():
        leaves = config_leaves()
        held = section in leaves or any(key.startswith(f"{section}.") for key in leaves)
        assert section.split(".")[0] == PROFILE_PREFIX or held, section
        assert reason


# ── keyed mappings ──


def test_every_keyed_section_has_entries_to_check():
    """A keyed section with nothing in it would pass vacuously."""
    heads = {key.split(".")[0] for key in config_leaves()}
    assert set(KEYED_MAPPINGS) <= heads
    for package in set(KEYED_MAPPINGS.values()):
        assert (SOURCE_DIR / package).is_dir(), package


def test_a_keyed_field_nothing_reads_is_reported():
    """Positive control: an orphan field under a venue is caught whatever the venue's key."""
    leaves = {**config_leaves(), "venues.ZZZZ.a_field_nobody_reads": "config/owner.yaml"}
    orphans = unread_keys(leaves, _source_chains(), _keyed_chains())
    assert orphans == ["venues.ZZZZ.a_field_nobody_reads"]


def test_a_keyed_field_is_read_whichever_entry_the_code_holds():
    chains = {"calendars": [["entry", "sources", "calendar", "origin"]]}
    leaves = {
        "venues.XNYS.sources.calendar.origin": "x",
        "venues.XETR.sources.calendar.origin": "x",
    }
    assert unread_keys(leaves, [], chains) == []


def test_a_chain_in_another_package_does_not_consume_a_keyed_field():
    leaves = {"venues.XNYS.name": "x"}
    elsewhere = {"store": [["entry", "name"]]}
    assert unread_keys(leaves, [["entry", "name"]], elsewhere) == ["venues.XNYS.name"]


def test_a_keyed_field_must_be_read_as_a_chain_not_as_separate_names():
    chains = {"calendars": [["entry", "sources"], ["other", "calendar", "origin"]]}
    assert unread_keys({"venues.XNYS.sources.calendar.origin": "x"}, [], chains) != []
