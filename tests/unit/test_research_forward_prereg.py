"""A forward test's pre-registration (``kind: forward_test``, DECISIONS D70): H7's file loads,
binds the extractor's segment (the prompt hash the code computes), and the harness commands that
read research history refuse it.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import io
from pathlib import Path

import pytest

from obliskiq.cli.app import EXIT_INVALID, run
from obliskiq.config import ObliskConfig
from obliskiq.errors import PreregistrationError
from obliskiq.llm.prompts import prompt_hash
from obliskiq.research.prereg import ForwardTestPrereg, load_any


def h7(repo_root: Path) -> Path:
    return repo_root / "research" / "preregistrations" / "llm-events.md"


def test_h7_loads_as_a_forward_test_bound_to_the_extractor_s_segment(
    repo_root: Path, shipped_settings: ObliskConfig
) -> None:
    prereg = load_any(h7(repo_root))
    assert isinstance(prereg, ForwardTestPrereg)
    spec = prereg.spec
    assert spec.hypothesis == "H7"
    assert spec.trial_budget == 0
    llm = shipped_settings.llm
    assert spec.segment.model_id == llm.extractor_model().model_id
    assert spec.segment.provider == llm.extractor_model().provider
    assert spec.segment.prompt_version == llm.extractor.prompt_version
    # The hash the extractor stamps on its outputs: a change of prompt is a new segment.
    assert spec.segment.prompt_hash == prompt_hash(llm.extractor)
    issuers = {n for n, f in shipped_settings.sources.news.feeds.items() if f.kind == "issuer"}
    assert set(spec.events.feeds) == issuers
    assert spec.benchmark in {i.symbol for i in shipped_settings.universe.instruments}


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ("  prompt_hash: 5bed4ad9", "  prompt_hash: XYZ", "prompt_hash"),
        ("trial_budget: 0", "trial_budget: 3", "trial_budget"),
        ("hypothesis: H7", "hypothesis: seven", "hypothesis"),
    ],
)
def test_a_forward_test_out_of_shape_is_refused(
    repo_root: Path, tmp_path: Path, old: str, new: str, message: str
) -> None:
    text = h7(repo_root).read_text(encoding="utf-8")
    assert old in text
    path = tmp_path / "forward.md"
    path.write_text(text.replace(old, new, 1), encoding="utf-8")
    with pytest.raises(PreregistrationError, match=message):
        load_any(path)


@pytest.mark.parametrize(
    "argv",
    [
        ["research", "power"],
        ["research", "data", "coverage", "--prereg"],
    ],
)
def test_the_history_commands_refuse_a_forward_test(repo_root: Path, argv: list[str]) -> None:
    out, err = io.StringIO(), io.StringIO()
    status = run(
        ["--config-dir", str(repo_root / "config"), *argv, str(h7(repo_root))],
        env_file=None,
        stdout=out,
        stderr=err,
    )
    assert status == EXIT_INVALID
    assert "a forward test is scored on the outputs produced live" in out.getvalue()
