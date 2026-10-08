"""The ``obliskiq`` command line.

``obliskiq config validate`` checks the whole configuration: the expert defaults, the
owner's panel, the jurisdiction profile it selects and the policies that profile declares.
``obliskiq settings show`` prints every effective setting and the file it comes from.
Any problem is printed for the owner, in plain words, with exit status 1 and no traceback.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import argparse
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from .. import __version__
from ..config import LoadedConfig, load_config
from ..env import Environment, default_env_file, load_environment
from ..errors import ConfigError, EnvVarError
from ..jurisdiction import (
    SHIPPED_POLICY_KINDS,
    JurisdictionProfile,
    check_declared_policies,
    load_profile,
)
from ..logging_setup import configure_logging

EXIT_OK = 0
EXIT_INVALID = 1


@dataclass(frozen=True)
class Loaded:
    """Everything a command needs: the validated configuration and the active profile."""

    config: LoadedConfig
    profile: JurisdictionProfile


type Handler = Callable[[Loaded, TextIO], int]


def load_all(config_dir: Path, env: Environment) -> Loaded:
    """Load the configuration and its jurisdiction profile, failing loudly on any problem."""
    loaded = load_config(config_dir, overrides=env.config_overrides())
    settings = loaded.settings
    configure_logging(settings.logging.level, settings.logging.format)
    profile = load_profile(config_dir, settings.jurisdiction)
    check_declared_policies(profile, SHIPPED_POLICY_KINDS)
    return Loaded(config=loaded, profile=profile)


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


def _settings_show(loaded: Loaded, out: TextIO) -> int:
    profile = loaded.profile
    rows = [("SETTING", "VALUE", "SOURCE")]
    rows += [(key, str(value), source) for key, value, source in loaded.config.leaves()]
    key_width = max(len(row[0]) for row in rows)
    value_width = max(len(row[1]) for row in rows)
    print(f"Effective settings — jurisdiction {profile.id} ({profile.name})", file=out)
    print("", file=out)
    for key, value, source in rows:
        print(f"{key:<{key_width}}  {value:<{value_width}}  {source}", file=out)
    return EXIT_OK


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
    return parser


def run(
    argv: Sequence[str] | None,
    *,
    env_file: Path | None,
    stdout: TextIO,
    stderr: TextIO,
) -> int:
    """Parse ``argv`` and run the command. Returns the exit status."""
    args = build_parser().parse_args(argv)
    try:
        env = load_environment(env_file=env_file)
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
