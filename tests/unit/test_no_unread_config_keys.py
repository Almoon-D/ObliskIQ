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

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import ast
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


def unread_keys(leaves: dict[str, str], chains: list[list[str]]) -> list[str]:
    orphans: list[str] = []
    for key in sorted(leaves):
        section = next((s for s in RUNTIME_KEYED if key.startswith(f"{s}.")), None)
        wanted = (section or key).split(".")
        if not any(_contains(chain, wanted) for chain in chains):
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
    orphans = unread_keys(leaves, _source_chains())
    listed = "\n  ".join(f"{key}  ({leaves[key]})" for key in orphans)
    assert not orphans, (
        f"config/ declares {len(orphans)} key(s) nothing in src/obliskiq/ reads:\n  {listed}\n\n"
        "Give each a consumer or delete it. A key set to a value no code path can observe "
        "reads as a guarantee while enforcing nothing."
    )


def test_the_scan_catches_an_orphan_key():
    """Positive control: without it, a resolver that matched everything would pass."""
    leaves = {**config_leaves(), "account.a_key_nobody_reads": "config/owner.yaml"}
    assert unread_keys(leaves, _source_chains()) == ["account.a_key_nobody_reads"]


def test_a_namespaced_profile_key_needs_its_namespace():
    """``name`` alone is read everywhere; ``profile.name`` must be read off a profile."""
    chains = attribute_chains("def f(x):\n    return x.name\n")
    assert unread_keys({"profile.name": "x"}, chains) == ["profile.name"]
    chains = attribute_chains("def f(loaded):\n    return loaded.profile.name\n")
    assert unread_keys({"profile.name": "x"}, chains) == []


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
        assert section.split(".")[0] == PROFILE_PREFIX or section in config_leaves(), section
        assert reason
