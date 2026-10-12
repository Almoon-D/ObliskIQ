"""The ``obliskiq`` command line.

``obliskiq config validate`` checks the whole configuration: the expert defaults, the
owner's panel, the jurisdiction profile it selects and the policies that profile declares.
``obliskiq settings show`` prints every effective setting and the file it comes from.
Any problem is printed for the owner, in plain words, with exit status 1 and no traceback.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import argparse
import asyncio
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from .. import __version__
from ..config import LoadedConfig, load_config
from ..env import Environment, default_env_file, load_environment
from ..errors import ConfigError, EnvVarError
from ..ibkr.preflight import run_read_only
from ..ibkr.transport import missing_credentials, transport_refusal
from ..jurisdiction import (
    SHIPPED_POLICY_KINDS,
    JurisdictionProfile,
    check_declared_policies,
    load_profile,
)
from ..logging_setup import configure_logging

EXIT_OK = 0
EXIT_INVALID = 1
#: The preflight found no IBKR credentials in the environment: nothing was attempted.
EXIT_NO_CREDENTIALS = 2

#: Longest value ``settings show`` prints in full; longer ones (a list of phase windows) are cut.
VALUE_LIMIT = 48


@dataclass(frozen=True)
class Loaded:
    """Everything a command needs: the validated configuration and the active profile."""

    config: LoadedConfig
    profile: JurisdictionProfile
    env: Environment


type Handler = Callable[[Loaded, TextIO], int]


def load_all(config_dir: Path, env: Environment) -> Loaded:
    """Load the configuration and its jurisdiction profile, failing loudly on any problem."""
    loaded = load_config(config_dir, overrides=env.config_overrides())
    settings = loaded.settings
    configure_logging(settings.logging.level, settings.logging.format)
    profile = load_profile(config_dir, settings.jurisdiction)
    check_declared_policies(profile, SHIPPED_POLICY_KINDS)
    return Loaded(config=loaded, profile=profile, env=env)


def _config_validate(loaded: Loaded, out: TextIO) -> int:
    settings = loaded.config.settings
    profile = loaded.profile
    declared = sorted(profile.policies.items())
    policies = ", ".join(f"{kind}: {pid}" for kind, pid in declared) or "none declared"
    print(f"configuration is valid ({', '.join(loaded.config.files)})", file=out)
    print(f"  base currency  {settings.account.base_currency}", file=out)
    print(f"  jurisdiction   {profile.id} ({profile.name})", file=out)
    print(f"  policies       {policies}", file=out)
    return EXIT_OK


def _shorten(text: str) -> str:
    return text if len(text) <= VALUE_LIMIT else f"{text[: VALUE_LIMIT - 1]}…"


def _settings_show(loaded: Loaded, out: TextIO) -> int:
    profile = loaded.profile
    rows = [("SETTING", "VALUE", "SOURCE")]
    rows += [(key, _shorten(str(value)), source) for key, value, source in loaded.config.leaves()]
    key_width = max(len(row[0]) for row in rows)
    value_width = max(len(row[1]) for row in rows)
    print(f"Effective settings — jurisdiction {profile.id} ({profile.name})", file=out)
    print("", file=out)
    for key, value, source in rows:
        print(f"{key:<{key_width}}  {value:<{value_width}}  {source}", file=out)
    return EXIT_OK


def _preflight(loaded: Loaded, out: TextIO) -> int:
    """``preflight --read-only``: walk the IBKR read path and report each step."""
    env = loaded.env
    # An unset profile is reported as unset (below), not as a refused combination.
    refusal = transport_refusal(env) if env.ibkr_profile is not None else None
    if refusal is not None:
        # Before the credential check: a refused combination never asks for its keys.
        print(f"refused: {refusal}", file=out)
        return EXIT_INVALID
    missing = missing_credentials(env)
    if missing:
        print("no credentials", file=out)
        print(f"  unset: {', '.join(missing)}", file=out)
        return EXIT_NO_CREDENTIALS
    report = asyncio.run(run_read_only(env, loaded.config.settings, data_dir=env.data_dir))
    account = env.ibkr_account_id.get_secret_value() if env.ibkr_account_id else ""
    print(report.render((account,)), file=out)
    return EXIT_OK if report.ok else EXIT_INVALID


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="obliskiq",
        description="ObliskIQ: an autonomous, risk-first portfolio manager for one IBKR account.",
    )
    parser.add_argument("--version", action="version", version=f"obliskiq {__version__}")
    parser.add_argument(
        "--config-dir",
        type=Path,
        help="the directory holding owner.yaml, defaults/ and jurisdictions/ "
        "(default: OBLISK_CONFIG_DIR, else config/ of the source checkout)",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        help="read environment variables from this file "
        "(default: .env at the root of the source checkout, when present)",
    )
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    config = commands.add_parser("config", help="check the configuration")
    config_actions = config.add_subparsers(dest="action", required=True, metavar="ACTION")
    validate = config_actions.add_parser(
        "validate", help="validate the defaults, the owner's settings and the jurisdiction"
    )
    validate.set_defaults(handler=_config_validate)

    settings = commands.add_parser("settings", help="the owner's settings")
    settings_actions = settings.add_subparsers(dest="action", required=True, metavar="ACTION")
    show = settings_actions.add_parser(
        "show", help="print every effective setting and where its value comes from"
    )
    show.set_defaults(handler=_settings_show)

    preflight = commands.add_parser(
        "preflight", help="check the IBKR connection end to end (read-only)"
    )
    preflight.add_argument(
        "--read-only",
        action="store_true",
        required=True,
        help="read only: authentication, accounts, guard, positions, cash, a snapshot, history, "
        "contract rules, algos and the trading schedule for one stock; no order endpoint",
    )
    preflight.set_defaults(handler=_preflight)
    return parser


def run(
    argv: Sequence[str] | None,
    *,
    env_file: Path | None,
    stdout: TextIO,
    stderr: TextIO,
) -> int:
    """Parse ``argv`` and run the command. Returns the exit status.

    ``env_file`` is the env file read when ``--env-file`` is not given.
    """
    args = build_parser().parse_args(argv)
    try:
        env = load_environment(env_file=args.env_file if args.env_file is not None else env_file)
        config_dir: Path = (
            args.config_dir if args.config_dir is not None else (env.resolved_config_dir())
        )
        loaded = load_all(config_dir.expanduser(), env)
    except (ConfigError, EnvVarError) as exc:
        print(f"obliskiq: {exc}", file=stderr)
        return EXIT_INVALID
    handler: Handler = args.handler
    return handler(loaded, stdout)


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point of the ``obliskiq`` script."""
    return run(argv, env_file=default_env_file(), stdout=sys.stdout, stderr=sys.stderr)
