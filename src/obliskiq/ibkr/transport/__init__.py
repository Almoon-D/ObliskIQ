"""The two transports and the choice between them (``OBLISK_IBKR_TRANSPORT``).

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from pathlib import Path

from ...config.schema import ObliskConfig
from ...env import Environment
from ...errors import EnvVarError
from ...util.http import RetryPolicy
from .base import Transport, TransportKind
from .gateway import GatewayTransport
from .gateway_login import LoginDriver
from .oauth import OAuthCredentials, OAuthSession, OAuthTransport

__all__ = ["Transport", "TransportKind", "build_transport", "missing_credentials"]


def missing_credentials(env: Environment) -> list[str]:
    """The variables the selected transport needs and the environment lacks.

    Empty when the environment can reach IBKR; the profile, the account and the transport are
    always needed, then the variables of the chosen transport.
    """
    missing = [
        name
        for name, value in (
            ("OBLISK_IBKR_PROFILE", env.ibkr_profile),
            ("OBLISK_IBKR_ACCOUNT_ID", env.ibkr_account_id),
            ("OBLISK_IBKR_TRANSPORT", env.ibkr_transport),
        )
        if value is None
    ]
    if env.ibkr_transport == TransportKind.GATEWAY.value:
        missing += [
            name
            for name, value in (
                ("OBLISK_IBKR_GATEWAY_USERNAME", env.ibkr_gateway_username),
                ("OBLISK_IBKR_GATEWAY_PASSWORD", env.ibkr_gateway_password),
            )
            if value is None
        ]
    elif env.ibkr_transport == TransportKind.OAUTH.value:
        missing += [
            name
            for name, value in (
                ("OBLISK_IBKR_CP_BASE_URL", env.ibkr_cp_base_url),
                ("OBLISK_IBKR_CONSUMER_KEY", env.ibkr_consumer_key),
                ("OBLISK_IBKR_ACCESS_TOKEN", env.ibkr_access_token),
                ("OBLISK_IBKR_ACCESS_TOKEN_SECRET", env.ibkr_access_token_secret),
                ("OBLISK_IBKR_SIGNATURE_KEY", env.ibkr_signature_key_path),
                ("OBLISK_IBKR_ENCRYPTION_KEY", env.ibkr_encryption_key_path),
                ("OBLISK_IBKR_DH_PARAM", env.ibkr_dh_param_path),
            )
            if value is None
        ]
    return missing


def build_transport(
    env: Environment,
    settings: ObliskConfig,
    *,
    data_dir: Path,
    login_driver: LoginDriver | None = None,
) -> Transport:
    """The transport ``OBLISK_IBKR_TRANSPORT`` selects, built from the environment."""
    missing = missing_credentials(env)
    if missing:
        raise EnvVarError("cannot reach IBKR: " + ", ".join(missing) + " unset")
    ibkr = settings.ibkr
    policy = RetryPolicy.from_settings(settings.http)
    if env.ibkr_transport == TransportKind.OAUTH.value:
        if env.ibkr_cp_base_url is None:  # pragma: no cover - missing_credentials checked it
            raise EnvVarError("OBLISK_IBKR_CP_BASE_URL unset")
        session = OAuthSession(OAuthCredentials.from_environment(env, ibkr), ibkr)
        return OAuthTransport(
            session,
            origin=env.ibkr_cp_base_url,
            base_path=ibkr.base_path,
            token_path=ibkr.oauth.live_session_token_path,
            timeout_seconds=ibkr.request_timeout_seconds,
            policy=policy,
        )
    if (
        env.ibkr_gateway_username is None
        or env.ibkr_gateway_password is None
        or env.ibkr_profile is None
    ):  # pragma: no cover - missing_credentials checked them
        raise EnvVarError("the Gateway login variables are unset")
    return GatewayTransport(
        ibkr,
        profile=env.ibkr_profile,
        username=env.ibkr_gateway_username,
        password=env.ibkr_gateway_password,
        base_path=ibkr.base_path,
        install_dir=data_dir / settings.paths.ibkr_gateway,
        policy=policy,
        timeout_seconds=ibkr.request_timeout_seconds,
        java_home=env.ibkr_gateway_java_home,
        driver=login_driver,
    )
