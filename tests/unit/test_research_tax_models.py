"""The validators of the research and tax models: every refusal names its reason.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import io
import logging
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from obliskiq.cli.app import EXIT_OK, run
from obliskiq.config import ObliskConfig
from obliskiq.config.schema_research import ResearchConfig
from obliskiq.domain.research import Frequency, PointInTime, ResearchSeries, Vendor
from obliskiq.domain.tax import IncomeEvent, TaxLot, TaxYearSummary
from obliskiq.jurisdiction import load_profile
from obliskiq.jurisdiction.tax import ProfileTaxRules

D = Decimal


@pytest.fixture(autouse=True)
def _restore_root_logging():
    yield
    logging.getLogger().handlers.clear()


def test_dataset_ids_are_unique(shipped_settings: ObliskConfig):
    data = shipped_settings.research.model_dump(mode="json")
    data["datasets"].append(data["datasets"][0])
    with pytest.raises(ValidationError, match="datasets ids must be unique"):
        ResearchConfig.model_validate(data)
    assert shipped_settings.research.dataset("nope") is None


def test_a_series_hash_is_a_sha256():
    with pytest.raises(ValidationError, match="SHA-256 digest"):
        ResearchSeries(
            series_id="s", dataset_id="d", vendor=Vendor.ECB, field="USD", units="u",
            currency="USD", frequency=Frequency.DAILY, point_in_time=PointInTime.AS_OBSERVED,
            source_url="x", downloaded_at=datetime(2026, 1, 1, tzinfo=UTC),
            content_sha256="not a digest", bias_notes=("x",),
        )  # fmt: skip


def lot(**overrides) -> TaxLot:
    fields = {
        "lot_id": "L1",
        "security": "S",
        "acquired_on": date(2025, 1, 2),
        "quantity": D(10),
        "cost": D(100),
        "deferred_loss": D(0),
        "replacement_units": D(0),
    }
    fields.update(overrides)
    return TaxLot(**fields)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"replacement_units": D(11)}, "cannot exceed the lot's quantity"),
        ({"cost": D(-1)}, "greater than or equal to 0"),
        ({"quantity": D(0)}, "greater than 0"),
        ({"deferred_loss": D("NaN")}, "finite number"),
    ],
)
def test_a_lot_refuses_impossible_amounts(overrides, message):
    with pytest.raises(ValidationError, match=message):
        lot(**overrides)


def test_income_cannot_withhold_more_than_it_pays_and_money_is_finite():
    with pytest.raises(ValidationError, match="cannot exceed the gross income"):
        IncomeEvent(
            security="S", received_on=date(2025, 1, 2), gross=D(10), withheld=D(11),
            source_country="US",
        )  # fmt: skip
    with pytest.raises(ValidationError, match="finite number"):
        TaxYearSummary(
            tax_year=2025, income=D("Infinity"), gains=D(0), base=D(0), tax_before_credit=D(0),
            foreign_tax_credit=D(0), tax_due=D(0), carried_in_used=D(0), carried_out=(),
            expired=D(0), rate_table_covers_year=True, payment_date=date(2026, 6, 30),
        )  # fmt: skip


def test_the_shared_rules_describe_themselves_without_a_policys_own(repo_root):
    profile = load_profile(repo_root / "config", "es")
    assert profile.tax is not None
    shared = ProfileTaxRules("shared", profile.tax).describe()
    assert shared[0].startswith("policy shared; lots fifo")
    assert not any("two-month rule" in line for line in shared)


def test_a_profile_without_a_tax_policy_still_validates(config_dir):
    (config_dir / "jurisdictions" / "es.yaml").write_text(
        "id: es\nname: Spain\npolicies:\n  eligibility: skeleton\n", encoding="utf-8"
    )
    out, err = io.StringIO(), io.StringIO()
    status = run(["--config-dir", str(config_dir), "config", "validate"], env_file=None,
                 stdout=out, stderr=err)  # fmt: skip
    assert status == EXIT_OK, err.getvalue()
    assert "policies       eligibility: skeleton" in out.getvalue()
