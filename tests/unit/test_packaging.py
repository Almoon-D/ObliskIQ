"""Packaging invariants. Adapted from ObliskIQ-lite-MCP.

The wheel and sdist are built in-process with hatchling, the project's build backend, so
what is asserted is what a build actually produces.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import importlib
import re
import tarfile
import tomllib
import zipfile
from pathlib import Path
from typing import Any

import pytest
from hatchling.builders.sdist import SdistBuilder
from hatchling.builders.wheel import WheelBuilder

import obliskiq

ROOT = Path(__file__).resolve().parents[2]


def _pyproject() -> dict[str, Any]:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def built(tmp_path_factory: pytest.TempPathFactory) -> dict[str, list[str]]:
    out = tmp_path_factory.mktemp("dist")
    names: dict[str, list[str]] = {}
    (wheel,) = WheelBuilder(str(ROOT)).build(directory=str(out), versions=["standard"])
    with zipfile.ZipFile(wheel) as archive:
        names["wheel"] = archive.namelist()
    (sdist,) = SdistBuilder(str(ROOT)).build(directory=str(out), versions=["standard"])
    with tarfile.open(sdist) as archive:
        names["sdist"] = archive.getnames()
    return names


def test_the_project_metadata():
    project = _pyproject()["project"]
    assert project["name"] == "obliskiq"
    assert re.fullmatch(r"\d+\.\d+\.\d+", project["version"])
    assert project["license"] == "AGPL-3.0-or-later"
    assert project["requires-python"] == ">=3.14,<3.15"
    assert project["scripts"] == {"obliskiq": "obliskiq.cli.app:main"}


def test_the_installed_version_is_the_project_version():
    assert obliskiq.__version__ == _pyproject()["project"]["version"]


def test_the_script_entry_point_resolves():
    module_name, _, attribute = _pyproject()["project"]["scripts"]["obliskiq"].partition(":")
    assert callable(getattr(importlib.import_module(module_name), attribute))


def test_the_python_version_is_pinned_for_uv():
    assert (ROOT / ".python-version").read_text(encoding="utf-8").strip() == "3.14"


def test_the_lock_file_is_committed():
    assert (ROOT / "uv.lock").is_file()


def test_the_wheel_ships_the_package_with_its_type_marker(built):
    names = built["wheel"]
    assert "obliskiq/py.typed" in names
    assert "obliskiq/cli/app.py" in names
    assert "obliskiq/config/loader.py" in names
    allowed_roots = ("obliskiq/", "obliskiq-")
    assert all(name.startswith(allowed_roots) for name in names), names


def test_neither_artifact_ships_the_legacy_snapshot(built):
    for kind, names in built.items():
        assert not [name for name in names if "/legacy/" in f"/{name}"], kind


def test_the_sdist_ships_the_sources_and_the_licence(built):
    names = {name.split("/", 1)[1] for name in built["sdist"] if "/" in name}
    assert {"src/obliskiq/py.typed", "pyproject.toml", "LICENSE"} <= names
