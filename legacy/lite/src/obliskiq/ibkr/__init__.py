"""IBKR Client Portal Web API access.

The downloadable Client Portal Gateway and IB Gateway are out of scope: both need
an interactive local session and cannot run unattended on a VM. This package
speaks to the Web API directly over OAuth 1.0a with RSA signing.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from .cp_client import ContractMatch, CpApiClient
from .oauth import LiveSessionToken, OAuthCredentials, OAuthSession

__all__ = [
    "ContractMatch",
    "CpApiClient",
    "LiveSessionToken",
    "OAuthCredentials",
    "OAuthSession",
]
