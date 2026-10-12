"""The `ibkr` and `http` configuration refuse values that would mislead, and the transport is
chosen by the environment."""

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from obliskiq.config.schema_ibkr import (
    GatewayConfig,
    HistoryConfig,
    HttpConfig,
    IbkrConfig,
    MarketDataConfig,
    OAuthConfig,
    PreflightConfig,
    RateLimitConfig,
)
from obliskiq.env import Environment
from obliskiq.errors import EnvVarError
from obliskiq.ibkr.errors import ProfileRefusedError
from obliskiq.ibkr.transport import build_transport, missing_credentials
from obliskiq.ibkr.transport.base import TransportKind


def _dump(model: Any) -> dict[str, Any]:
    data: dict[str, Any] = model.model_dump()
    return data


def _with(model: Any, **changes: Any) -> dict[str, Any]:
    data = _dump(model)
    data.update(changes)
    return data


@pytest.mark.parametrize("base_path", ["v1/api", "/v1/api/", "/v1//api", "/v1/api?x=1"])
def test_the_base_path_must_be_a_clean_absolute_path(shipped_settings, base_path):
    with pytest.raises(ValidationError, match="absolute path"):
        IbkrConfig.model_validate(_with(shipped_settings.ibkr, base_path=base_path))


@pytest.mark.parametrize(
    ("address", "message"),
    [
        ("http://localhost:5000", "origin with a port"),
        ("https://localhost", "origin with a port"),
        ("https://localhost:5000/v1", "bare origin"),
    ],
)
def test_the_gateway_address_must_be_an_https_origin_with_a_port(
    shipped_settings, address, message
):
    data = _with(shipped_settings.ibkr.gateway, local_address=address)
    with pytest.raises(ValidationError, match=message):
        GatewayConfig.model_validate(data)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"download_url": "http://download2.example/x.zip"}, "https:// URL"),
        ({"sha256": "ABC"}, "SHA-256"),
    ],
)
def test_the_gateway_download_is_https_and_pinned(shipped_settings, changes, message):
    with pytest.raises(ValidationError, match=message):
        GatewayConfig.model_validate(_with(shipped_settings.ibkr.gateway, **changes))


def test_a_field_id_must_be_a_quoted_number_or_null(shipped_settings):
    data = _dump(shipped_settings.ibkr.marketdata)
    data["snapshot_fields"][0]["id"] = "thirty-one"
    with pytest.raises(ValidationError, match="numeric IBKR field id"):
        MarketDataConfig.model_validate(data)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda d: d["snapshot_fields"].append({"name": "last", "id": "99"}), "name appears twice"),
        (lambda d: d["snapshot_fields"].append({"name": "theta", "id": "31"}), "id appears twice"),
        (lambda d: d.update(max_fields_per_snapshot=2), "more ids than"),
        (
            lambda d: d.update(implied_vol_max_plausible=0.0001, implied_vol_min_plausible=1.0),
            "must exceed",
        ),
    ],
)
def test_the_field_maps_are_coherent(shipped_settings, mutate, message):
    data = _dump(shipped_settings.ibkr.marketdata)
    data["snapshot_fields"] = [f for f in data["snapshot_fields"] if f["name"] != "theta"]
    mutate(data)
    with pytest.raises(ValidationError, match=message):
        MarketDataConfig.model_validate(data)


def test_a_paper_prefix_is_capital_letters(shipped_settings):
    with pytest.raises(ValidationError, match="capital letters"):
        IbkrConfig.model_validate(_with(shipped_settings.ibkr, paper_account_prefixes=["du"]))


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"base_delay_seconds": 9.0}, "max_delay_seconds must not be below"),
        ({"retry_after_max_seconds": 1.0}, "retry_after_max_seconds must not be below"),
    ],
)
def test_the_http_policy_is_ordered(shipped_settings, changes, message):
    with pytest.raises(ValidationError, match=message):
        HttpConfig.model_validate(_with(shipped_settings.http, **changes))


def test_the_refresh_margin_sits_inside_the_token_lifetime(shipped_settings):
    data = _with(
        shipped_settings.ibkr.oauth,
        live_session_token_refresh_margin_seconds=shipped_settings.ibkr.oauth.live_session_token_ttl_seconds,
    )
    with pytest.raises(ValidationError, match="refresh margin"):
        OAuthConfig.model_validate(data)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda d: d["groups"].append(dict(d["groups"][0])), "distinct name"),
        (
            lambda d: d["groups"].append({**d["groups"][0], "name": "other"}),
            "one rate group",
        ),
        (lambda d: d["groups"][0].update(paths=["/leading"]), "leading slash"),
    ],
)
def test_rate_groups_are_distinct(shipped_settings, mutate, message):
    data = _dump(shipped_settings.ibkr.ratelimit)
    mutate(data)
    with pytest.raises(ValidationError, match=message):
        RateLimitConfig.model_validate(data)


def test_the_step_down_floor_sits_below_the_chunk(shipped_settings):
    data = _with(shipped_settings.ibkr.history, span_stepdown_min_days=10_000)
    with pytest.raises(ValidationError, match="must not exceed chunk_days"):
        HistoryConfig.model_validate(data)


def test_the_preflight_period_is_ibkr_notation(shipped_settings):
    with pytest.raises(ValidationError):
        PreflightConfig.model_validate(
            _with(shipped_settings.ibkr.preflight, history_period="1 month")
        )


def test_a_skipped_venue_must_be_declared(shipped_settings):
    data = shipped_settings.model_dump()
    data["ibkr"]["trading_schedule"]["skip_venues"] = ["XXXX"]
    with pytest.raises(ValidationError, match="skip_venues: XXXX"):
        type(shipped_settings).model_validate(data)


# ── choosing the transport ──


def _env(monkeypatch: pytest.MonkeyPatch, **values: str) -> Environment:
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    return Environment(_env_file=None)


def test_nothing_set_names_every_basic_variable(monkeypatch):
    assert missing_credentials(_env(monkeypatch)) == [
        "OBLISK_IBKR_PROFILE",
        "OBLISK_IBKR_ACCOUNT_ID",
        "OBLISK_IBKR_TRANSPORT",
    ]


def test_build_refuses_without_credentials(monkeypatch, shipped_settings, tmp_path):
    env = _env(monkeypatch, OBLISK_IBKR_TRANSPORT="gateway")
    with pytest.raises(EnvVarError, match="OBLISK_IBKR_GATEWAY_USERNAME"):
        build_transport(env, shipped_settings, data_dir=tmp_path)


def _gateway_env(monkeypatch: pytest.MonkeyPatch, profile: str) -> Environment:
    return _env(
        monkeypatch,
        OBLISK_IBKR_PROFILE=profile,
        OBLISK_IBKR_ACCOUNT_ID="DU_PAPER",
        OBLISK_IBKR_TRANSPORT="gateway",
        OBLISK_IBKR_GATEWAY_USERNAME="paper-user",
        OBLISK_IBKR_GATEWAY_PASSWORD="paper-pass",
    )


def test_the_gateway_transport_is_built_for_paper(monkeypatch, shipped_settings, tmp_path):
    transport = build_transport(
        _gateway_env(monkeypatch, "paper"), shipped_settings, data_dir=tmp_path
    )
    assert transport.kind is TransportKind.GATEWAY
    assert transport.base_url == "https://localhost:5000/v1/api"


def test_the_gateway_transport_refuses_profile_live(monkeypatch, shipped_settings, tmp_path):
    with pytest.raises(ProfileRefusedError):
        build_transport(_gateway_env(monkeypatch, "live"), shipped_settings, data_dir=tmp_path)


def _keys(tmp_path: Path) -> dict[str, str]:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    paths: dict[str, str] = {}
    for name in ("signature", "encryption"):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        path = tmp_path / f"{name}.pem"
        path.write_bytes(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
        paths[name] = str(path)
    # RFC 2409 Oakley group 2 (1024 bits), generator 2.
    prime = int(
        "FFFFFFFFFFFFFFFFC90FDAA22168C234C4C6628B80DC1CD129024E088A67CC74020BBEA63B139B22514A0879"
        "8E3404DDEF9519B3CD3A431B302B0A6DF25F14374FE1356D6D51C245E485B576625E7EC6F44C42E9A637ED6B"
        "0BFF5CB6F406B7EDEE386BFB5A899FA5AE9F24117C4B1FE649286651ECE65381FFFFFFFFFFFFFFFF",
        16,
    )
    from cryptography.hazmat.primitives.asymmetric import dh

    params = dh.DHParameterNumbers(prime, 2).parameters()
    dh_path = tmp_path / "dhparam.pem"
    dh_path.write_bytes(
        params.parameter_bytes(serialization.Encoding.PEM, serialization.ParameterFormat.PKCS3)
    )
    paths["dh"] = str(dh_path)
    return paths


def test_the_oauth_transport_is_built_from_its_variables(monkeypatch, shipped_settings, tmp_path):
    keys = _keys(tmp_path)
    env = _env(
        monkeypatch,
        OBLISK_IBKR_PROFILE="live",
        OBLISK_IBKR_ACCOUNT_ID="U_LIVE",
        OBLISK_IBKR_TRANSPORT="oauth",
        OBLISK_IBKR_CP_BASE_URL="https://api.ibkr.test",
        OBLISK_IBKR_CONSUMER_KEY="consumer",
        OBLISK_IBKR_ACCESS_TOKEN="token",
        OBLISK_IBKR_ACCESS_TOKEN_SECRET="c2VjcmV0",
        OBLISK_IBKR_SIGNATURE_KEY=keys["signature"],
        OBLISK_IBKR_ENCRYPTION_KEY=keys["encryption"],
        OBLISK_IBKR_DH_PARAM=keys["dh"],
    )
    assert missing_credentials(env) == []
    transport = build_transport(env, shipped_settings, data_dir=tmp_path)
    assert transport.kind is TransportKind.OAUTH
    assert transport.base_url == "https://api.ibkr.test/v1/api"
    assert transport.verify() is True


@pytest.mark.parametrize("prefix", ["U", "F", "DU1", "XU"])
def test_a_live_or_malformed_prefix_is_never_a_paper_prefix(shipped_settings, prefix):
    with pytest.raises(ValidationError, match="starting with D"):
        IbkrConfig.model_validate(_with(shipped_settings.ibkr, paper_account_prefixes=[prefix]))


@pytest.mark.parametrize(
    ("changes", "field"),
    [
        ({"max_conids_per_snapshot": 101}, "max_conids_per_snapshot"),
        ({"max_fields_per_snapshot": 51}, "max_fields_per_snapshot"),
    ],
)
def test_snapshot_batches_stay_inside_ibkrs_ceilings(shipped_settings, changes, field):
    with pytest.raises(ValidationError, match=field):
        MarketDataConfig.model_validate(_with(shipped_settings.ibkr.marketdata, **changes))


def test_an_override_cannot_drop_a_paced_endpoints_group(shipped_settings):
    data = _dump(shipped_settings.ibkr.ratelimit)
    data["groups"] = [group for group in data["groups"] if group["name"] != "history"]
    with pytest.raises(ValidationError, match="iserver/marketdata/history"):
        RateLimitConfig.model_validate(data)


@pytest.mark.parametrize("name", ["OBLISK_IBKR_GATEWAY_PASSWORD", "OBLISK_ANYTHING", "1BAD"])
def test_the_gateway_environment_can_never_list_obliskiqs_own_variables(shipped_settings, name):
    data = _with(
        shipped_settings.ibkr.gateway,
        inherited_environment=[*shipped_settings.ibkr.gateway.inherited_environment, name],
    )
    with pytest.raises(ValidationError):
        GatewayConfig.model_validate(data)


def test_the_oauth_transport_refuses_profile_paper_before_reading_a_key(
    monkeypatch, shipped_settings, tmp_path
):
    """OAuth keys are the live account's (D42): a paper run is refused, and the key paths,
    which point at nothing, are never opened."""
    env = _env(
        monkeypatch,
        OBLISK_IBKR_PROFILE="paper",
        OBLISK_IBKR_ACCOUNT_ID="DU_PAPER",
        OBLISK_IBKR_TRANSPORT="oauth",
        OBLISK_IBKR_CP_BASE_URL="https://api.ibkr.test",
        OBLISK_IBKR_CONSUMER_KEY="consumer",
        OBLISK_IBKR_ACCESS_TOKEN="token",
        OBLISK_IBKR_ACCESS_TOKEN_SECRET="c2VjcmV0",
        OBLISK_IBKR_SIGNATURE_KEY=str(tmp_path / "absent-signature.pem"),
        OBLISK_IBKR_ENCRYPTION_KEY=str(tmp_path / "absent-encryption.pem"),
        OBLISK_IBKR_DH_PARAM=str(tmp_path / "absent-dhparam.pem"),
    )
    with pytest.raises(ProfileRefusedError, match="live account only"):
        build_transport(env, shipped_settings, data_dir=tmp_path)
