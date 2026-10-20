"""The eligibility policies (``jurisdiction/eligibility.py``): the skeleton allowlist and its
registration.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from typing import Any

from traded import ETF, OTHER_STOCK, STOCK, option

from obliskiq.jurisdiction import ELIGIBILITY_POLICIES, SHIPPED_POLICY_KINDS, load_profile
from obliskiq.jurisdiction.eligibility import SKELETON_POLICY_ID, SkeletonEligibility


def test_the_shipped_profile_selects_the_skeleton(repo_root: Any, shipped_settings: Any) -> None:
    profile = load_profile(repo_root / "config", "es")
    policy = ELIGIBILITY_POLICIES.resolve(profile, shipped_settings)
    assert policy.policy_id == SKELETON_POLICY_ID
    assert policy.skeleton
    assert ELIGIBILITY_POLICIES in SHIPPED_POLICY_KINDS
    listed = {i.conid for i in shipped_settings.eligibility.skeleton_allowlist}
    assert listed == {89005163, 7089}
    for conid in listed:
        described = next(
            i for i in shipped_settings.eligibility.skeleton_allowlist if i.conid == conid
        )
        assert policy.assess(described, underlying=None).eligible


def test_only_the_allowlist_is_eligible_and_every_verdict_says_skeleton() -> None:
    policy = SkeletonEligibility([STOCK])
    allowed = policy.assess(STOCK, underlying=None)
    assert allowed.eligible
    assert allowed.skeleton
    assert "(skeleton policy)" in allowed.reason
    assert not policy.assess(OTHER_STOCK, underlying=None).eligible
    redescribed = STOCK.model_copy(update={"description": "another name"})
    assert not policy.assess(redescribed, underlying=None).eligible


def test_an_option_is_judged_with_its_underlying() -> None:
    on_stock, on_etf, on_other = option(10, STOCK), option(11, ETF), option(12, OTHER_STOCK)
    policy = SkeletonEligibility([STOCK, on_stock, on_etf, on_other])
    both = policy.assess(on_stock, underlying=STOCK)
    assert both.eligible
    assert not both.mandatory_close
    etp = policy.assess(on_etf, underlying=ETF)
    assert etp.eligible
    assert etp.mandatory_close
    assert "closed before expiry" in etp.reason
    assert not policy.assess(on_other, underlying=OTHER_STOCK).eligible
    assert not policy.assess(on_stock, underlying=None).eligible
    assert not policy.assess(on_stock, underlying=OTHER_STOCK).eligible  # not its underlying
