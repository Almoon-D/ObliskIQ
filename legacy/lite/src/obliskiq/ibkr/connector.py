"""Read the IBKR Custom Connector's advertised auth model, and say what it proves.

Why this exists
---------------

The build was designed around one premise: that the Client Portal Web API over
OAuth 1.0a is the **only** headless route, because the IBKR MCP connector "lives
inside a chat session". IBKR now publishes a **Custom Connector** — a remote MCP
endpoint — and that premise is worth testing rather than inheriting.

This module is the test, and it is deliberately small. It performs the RFC 9728
discovery walk against the connector endpoint and reports what the authorization
server *advertises*: which grants, which client-authentication methods, which
scopes, and whether dynamic client registration is offered. It needs **no
credentials**, so it runs in CI, in a sandbox, and on the VM before anything is
provisioned.

What it establishes, and what it deliberately does not
------------------------------------------------------

A discovery document is a claim by the server about what it will accept. It is
**not** evidence that this build can obtain a token. Those are different facts and
conflating them is exactly the error the spike existed to avoid, so
:meth:`AuthModel.render` states the boundary in its own output and
:attr:`AuthModel.headless_grant_available` is named for what it measures — a grant
*advertised* — rather than for a capability.

Observed live on 2026-09-03, recorded in ``references/ibkr-playbook.md``:

* **The endpoint is an OAuth 2.0 protected resource, not OAuth 1.0a.**
  Unauthenticated it answers ``401`` with a ``WWW-Authenticate: Bearer`` challenge
  carrying ``resource_metadata``. So the two IBKR routes this repository can reach
  do not share a credential system at all.
* **The metadata URL comes from the challenge header, and IBKR does not serve the
  RFC-canonical path.** RFC 9728 §3.1 inserts ``/.well-known/oauth-protected-
  resource`` between host and path; that form returns ``404`` here. IBKR serves the
  **suffix** form instead. Following the header is therefore not merely tidier, it
  is the only form observed to work — which is why this module never constructs
  that URL itself.
* **A headless grant is advertised.** ``client_credentials`` appears in
  ``grant_types_supported`` and is genuinely wired: at the token endpoint it is
  routed and fails while resolving the client, whereas a grant name the server does
  not know is rejected by name. That distinguishes a live grant from metadata
  boilerplate.
* **Nobody here has held a token.** Registration was not attempted, no token was
  minted, and the token lifetime is unmeasured. Those stay UNTESTED, and this
  module says so rather than letting a green report imply otherwise.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

import httpx

from ..config import Config
from ..errors import ERROR_EXCERPT_CHARS, UpstreamError
from ..logging_setup import get_logger
from ..util.http import RetryPolicy, get_with_retry

log = get_logger("ibkr.connector")

#: The challenge an OAuth 2.0 protected resource answers an unauthenticated
#: request with. Anything else means the endpoint is not the shape this module
#: knows how to read, and it says so rather than guessing.
HTTP_UNAUTHORIZED = 401
HTTP_OK = 200

#: RFC 8414 publishes authorization-server metadata by INSERTING the well-known
#: segment between host and path; a good many servers also serve it as a suffix.
#: IBKR serves BOTH — verified live — so both are tried, insertion first because
#: it is the one the RFC mandates. (Its RFC 9728 sibling for the protected
#: resource is NOT constructed here at all: see the module docstring.)
_AS_WELL_KNOWN = ".well-known/oauth-authorization-server"

#: ``resource_metadata="<url>"`` inside a ``WWW-Authenticate`` header. The value is
#: quoted per RFC 9728, and the header carries other parameters beside it.
_RESOURCE_METADATA_RE = re.compile(r'resource_metadata\s*=\s*"([^"]+)"', re.IGNORECASE)


class ConnectorDiscoveryError(UpstreamError):
    """The connector endpoint did not answer the discovery walk as an OAuth 2.0 resource."""


def _strings(body: Mapping[str, Any], key: str) -> tuple[str, ...]:
    """A list-of-strings metadata field, or empty where the server omitted it.

    Absence is reported as empty rather than raising: a server is entitled to omit
    an optional field, and a probe that refuses to report on a partial document is
    less useful than one that reports the partiality.
    """
    value = body.get(key)
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ()
    return tuple(str(item) for item in value if isinstance(item, str))


def _text(body: Mapping[str, Any], key: str) -> str | None:
    value = body.get(key)
    return value.strip() or None if isinstance(value, str) else None


@dataclass(frozen=True)
class ProtectedResource:
    """RFC 9728 protected-resource metadata: which server guards this endpoint."""

    resource: str | None
    authorization_servers: tuple[str, ...]
    scopes_supported: tuple[str, ...]
    bearer_methods_supported: tuple[str, ...]


@dataclass(frozen=True)
class AuthorizationServer:
    """RFC 8414 authorization-server metadata: what the server says it will accept."""

    issuer: str | None
    authorization_endpoint: str | None
    token_endpoint: str | None
    registration_endpoint: str | None
    grant_types_supported: tuple[str, ...]
    token_endpoint_auth_methods_supported: tuple[str, ...]
    scopes_supported: tuple[str, ...]
    code_challenge_methods_supported: tuple[str, ...]


def decode_protected_resource(body: Mapping[str, Any]) -> ProtectedResource:
    """Decode RFC 9728 metadata. Pure function over decoded JSON."""
    return ProtectedResource(
        resource=_text(body, "resource"),
        authorization_servers=_strings(body, "authorization_servers"),
        scopes_supported=_strings(body, "scopes_supported"),
        bearer_methods_supported=_strings(body, "bearer_methods_supported"),
    )


def decode_authorization_server(body: Mapping[str, Any]) -> AuthorizationServer:
    """Decode RFC 8414 metadata. Pure function over decoded JSON."""
    return AuthorizationServer(
        issuer=_text(body, "issuer"),
        authorization_endpoint=_text(body, "authorization_endpoint"),
        token_endpoint=_text(body, "token_endpoint"),
        registration_endpoint=_text(body, "registration_endpoint"),
        grant_types_supported=_strings(body, "grant_types_supported"),
        token_endpoint_auth_methods_supported=_strings(
            body, "token_endpoint_auth_methods_supported"
        ),
        scopes_supported=_strings(body, "scopes_supported"),
        code_challenge_methods_supported=_strings(body, "code_challenge_methods_supported"),
    )


def resource_metadata_url(header_value: str | None) -> str | None:
    """The metadata URL a ``WWW-Authenticate`` challenge points at, or ``None``.

    Read from the header rather than constructed, because IBKR does not serve the
    RFC-canonical constructed path — that form returns ``404``, while the URL the
    header names returns the document. A probe that built the URL itself would
    report "not an OAuth 2.0 resource" about an endpoint that plainly is.
    """
    if not header_value:
        return None
    match = _RESOURCE_METADATA_RE.search(header_value)
    return match.group(1).strip() if match else None


def authorization_server_metadata_urls(issuer: str) -> tuple[str, ...]:
    """Candidate RFC 8414 metadata URLs for an issuer, in the order to try them.

    Insertion first, because RFC 8414 §3 mandates it; the suffix form second,
    because plenty of deployments serve only that one. IBKR serves both.
    """
    base = issuer.rstrip("/")
    split = httpx.URL(base)
    origin = f"{split.scheme}://{split.netloc.decode()}"
    path = split.path.strip("/")
    inserted = f"{origin}/{_AS_WELL_KNOWN}" + (f"/{path}" if path else "")
    suffix = f"{base}/{_AS_WELL_KNOWN}"
    # An issuer with no path makes the two forms identical. Deduplicated so a
    # failure does not fetch the same URL twice and report it as two locations.
    return (inserted,) if inserted == suffix else (inserted, suffix)


@dataclass(frozen=True)
class AuthModel:
    """What the connector's authorization server advertises, and what that proves.

    Every boolean here is named for a claim the *server* makes. None of them says a
    token was obtained, because none of them was measured that way.
    """

    endpoint: str
    challenged: bool
    resource: ProtectedResource
    server: AuthorizationServer
    #: Configured grants that count as headless, intersected with what is advertised.
    headless_grants_advertised: tuple[str, ...]
    #: Configured scopes the resource does not list.
    missing_scopes: tuple[str, ...]

    @property
    def headless_grant_available(self) -> bool:
        """A grant needing no browser is ADVERTISED. Not: a token can be obtained."""
        return bool(self.headless_grants_advertised)

    @property
    def required_scopes_served(self) -> bool:
        return not self.missing_scopes

    @property
    def ok(self) -> bool:
        """The walk completed and the endpoint is an OAuth 2.0 resource serving our scopes."""
        return self.challenged and self.required_scopes_served and bool(self.server.token_endpoint)

    def render(self) -> str:
        observed = (
            ("challenge", "Bearer (OAuth 2.0)" if self.challenged else "ABSENT"),
            ("resource", self.resource.resource),
            ("authorization", ", ".join(self.resource.authorization_servers)),
            ("scopes served", ", ".join(self.resource.scopes_supported)),
            ("grants", ", ".join(self.server.grant_types_supported)),
            ("client auth", ", ".join(self.server.token_endpoint_auth_methods_supported)),
            ("PKCE", ", ".join(self.server.code_challenge_methods_supported)),
            ("token endpoint", self.server.token_endpoint),
            ("registration", self.server.registration_endpoint or "not offered"),
        )
        lines = [
            f"IBKR Custom Connector auth model: {self.endpoint}",
            "",
            "  OBSERVED (this command just executed it):",
        ]
        lines.extend(f"    {label:<18} {value or '-'}" for label, value in observed)
        lines.append("")

        if self.missing_scopes:
            lines.append(
                "  Scopes this build asks for and the resource does not list: "
                + ", ".join(self.missing_scopes)
            )
            lines.append("")
        if self.headless_grant_available:
            lines.append(
                "  A headless grant is ADVERTISED: "
                + ", ".join(self.headless_grants_advertised)
                + "."
            )
        else:
            lines.append(
                "  NO headless grant is advertised. Every route to a token goes through a browser."
            )

        lines.extend(
            [
                "",
                "  UNTESTED, and this command cannot settle any of it:",
                "    - whether THIS account may use that grant. A grant the server",
                "      advertises is not a grant every client is entitled to.",
                "    - whether a client can be registered for it. Nothing was POSTed to",
                "      the registration endpoint; doing so creates state on a live",
                "      brokerage account.",
                "    - the token lifetime, and whether a refresh survives unattended.",
                "",
                "  So this reports what the server ACCEPTS, never that this build can",
                "  authenticate. Until a token is actually held, the unattended",
                "  collection route is unchanged.",
            ]
        )
        return "\n".join(lines)


async def _fetch_json(
    client: httpx.AsyncClient, url: str, *, policy: RetryPolicy
) -> Mapping[str, Any]:
    response = await get_with_retry(client, url, policy=policy)
    if response.status_code != HTTP_OK:
        raise ConnectorDiscoveryError(
            f"GET {url} returned {response.status_code}: {response.text[:ERROR_EXCERPT_CHARS]!r}"
        )
    try:
        body = response.json()
    except ValueError as exc:
        raise ConnectorDiscoveryError(
            f"GET {url} did not return JSON: {response.text[:ERROR_EXCERPT_CHARS]!r}"
        ) from exc
    if not isinstance(body, Mapping):
        raise ConnectorDiscoveryError(f"GET {url} returned {type(body).__name__}, not an object")
    return body


async def probe_connector(
    endpoint: str,
    config: Config,
    client: httpx.AsyncClient,
) -> AuthModel:
    """Walk RFC 9728 discovery against ``endpoint`` and report the advertised auth model.

    Sends no credentials and creates nothing. The unauthenticated request is
    expected to be refused: the ``401`` and the challenge it carries ARE the
    finding, so a ``200`` here would mean the endpoint is not the protected
    resource this walk assumes.
    """
    cfg = config.section("ibkr.connector")
    # The shared http.* retry policy, with only the timeout overridden: discovery
    # is four small GETs against a CDN and has no reason to wait as long as a
    # market-data call, but the backoff and the Retry-After ceiling are the same
    # upstream's and should not be restated here.
    policy = replace(
        RetryPolicy.from_config(config),
        timeout_seconds=cfg.require_float("discovery_timeout_seconds"),
    )

    challenge = await get_with_retry(client, endpoint, policy=policy)
    challenged = challenge.status_code == HTTP_UNAUTHORIZED
    metadata_url = resource_metadata_url(challenge.headers.get("www-authenticate"))
    if metadata_url is None:
        raise ConnectorDiscoveryError(
            f"{endpoint} answered {challenge.status_code} with no "
            f"'resource_metadata' in its WWW-Authenticate header, so it is not an "
            f"OAuth 2.0 protected resource this probe can read."
        )
    log.info("connector challenge read", extra={"endpoint": endpoint, "metadata": metadata_url})

    resource = decode_protected_resource(await _fetch_json(client, metadata_url, policy=policy))
    issuer = next(iter(resource.authorization_servers), None)
    if issuer is None:
        raise ConnectorDiscoveryError(
            f"{metadata_url} named no authorization server, so there is nothing to ask "
            f"which grants it accepts."
        )

    server, failures = None, []
    for candidate in authorization_server_metadata_urls(issuer):
        try:
            server = decode_authorization_server(
                await _fetch_json(client, candidate, policy=policy)
            )
        except ConnectorDiscoveryError as exc:
            failures.append(f"{candidate}: {exc}")
        else:
            break
    if server is None:
        raise ConnectorDiscoveryError(
            "no authorization-server metadata at either published location — " + "; ".join(failures)
        )

    headless = tuple(cfg.require_list("headless_grant_types"))
    required = tuple(cfg.require_list("required_scopes"))
    return AuthModel(
        endpoint=endpoint,
        challenged=challenged,
        resource=resource,
        server=server,
        headless_grants_advertised=tuple(
            grant for grant in headless if grant in server.grant_types_supported
        ),
        missing_scopes=tuple(scope for scope in required if scope not in resource.scopes_supported),
    )
