"""The Custom Connector's advertised auth model, and the line the probe must not cross.

The connector spike turned on one distinction: what IBKR's authorization server
*advertises* is not what this build can *do*. A discovery document saying
``client_credentials`` is a claim about what the server accepts from a registered
client; it is not evidence that a token can be obtained, and a report that let a
reader draw the second conclusion from the first would have moved the collection
route on no evidence at all.

So these tests do two jobs. They check the decoders against the documents IBKR
actually served on 2026-09-03 — recorded verbatim under
``tests/fixtures/ibkr/oauth/`` — and they check that the rendered verdict keeps
saying what it has not established, because that sentence is the finding.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from obliskiq.ibkr.connector import (
    AuthModel,
    ConnectorDiscoveryError,
    authorization_server_metadata_urls,
    decode_authorization_server,
    decode_protected_resource,
    probe_connector,
    resource_metadata_url,
)

OAUTH = Path(__file__).parent / "fixtures" / "ibkr" / "oauth"

ENDPOINT = "https://api.ibkr.com/v1/api/mcp-public"
METADATA_URL = f"{ENDPOINT}/.well-known/oauth-protected-resource"
AS_METADATA_URL = "https://api.ibkr.com/.well-known/oauth-authorization-server/oauth2"
CHALLENGE = f'Bearer resource_metadata="{METADATA_URL}"'

HTTP_UNAUTHORIZED = 401
HTTP_NOT_FOUND = 404


def load(name):
    return json.loads((OAUTH / f"{name}.json").read_text(encoding="utf-8"))


# ── the recorded documents ──


def test_the_protected_resource_document_names_its_server_and_scopes():
    resource = decode_protected_resource(load("protected_resource"))
    assert resource.resource == ENDPOINT
    assert resource.authorization_servers == ("https://api.ibkr.com/oauth2",)
    assert "mcp.read" in resource.scopes_supported
    assert resource.bearer_methods_supported == ("header",)


def test_the_authorization_server_advertises_a_headless_grant():
    """The finding the spike turned on: ``client_credentials`` needs no browser.

    ``authorization_code`` is here too and is the flow IBKR's own material
    describes, but it puts a human at a login screen. The two are not
    interchangeable for a machine that must run unattended.
    """
    server = decode_authorization_server(load("authorization_server"))
    assert "client_credentials" in server.grant_types_supported
    assert "authorization_code" in server.grant_types_supported
    assert "private_key_jwt" in server.token_endpoint_auth_methods_supported
    assert server.token_endpoint == "https://api.ibkr.com/oauth2/api/v1/token"
    assert server.registration_endpoint == "https://api.ibkr.com/oauth2/register"


def test_a_missing_optional_field_reads_as_empty_rather_than_raising():
    """A server may omit an optional field. Reporting the partiality beats refusing
    to report at all — but empty must never render as if it were populated."""
    server = decode_authorization_server({"issuer": "https://example.test"})
    assert server.grant_types_supported == ()
    assert server.registration_endpoint is None


def test_a_scalar_where_a_list_belongs_is_not_split_into_characters():
    """``"mcp.read"`` is a string, and a string is a Sequence. Iterating it would
    yield eight one-character scopes, every one of them false."""
    resource = decode_protected_resource({"scopes_supported": "mcp.read"})
    assert resource.scopes_supported == ()


# ── the challenge header ──


def test_the_metadata_url_is_read_from_the_challenge():
    assert resource_metadata_url(CHALLENGE) == METADATA_URL


@pytest.mark.parametrize("header", [None, "", "Bearer", 'Basic realm="x"'])
def test_a_challenge_naming_no_metadata_yields_none(header):
    assert resource_metadata_url(header) is None


def test_both_published_authorization_server_locations_are_tried():
    """RFC 8414 mandates insertion; many deployments serve only the suffix. IBKR
    serves both, so the order is the RFC's and the fallback is the pragmatic one."""
    assert authorization_server_metadata_urls("https://api.ibkr.com/oauth2") == (
        AS_METADATA_URL,
        "https://api.ibkr.com/oauth2/.well-known/oauth-authorization-server",
    )


def test_an_issuer_with_no_path_yields_one_candidate_not_the_same_url_twice():
    """Both forms collapse to the same URL when there is no path to insert before.
    Fetching it twice on failure would report one location as two."""
    assert authorization_server_metadata_urls("https://example.test") == (
        "https://example.test/.well-known/oauth-authorization-server",
    )


# ── the walk ──


def _mock_discovery(*, as_url=AS_METADATA_URL):
    respx.get(ENDPOINT).mock(
        return_value=httpx.Response(HTTP_UNAUTHORIZED, headers={"WWW-Authenticate": CHALLENGE})
    )
    respx.get(METADATA_URL).mock(return_value=httpx.Response(200, json=load("protected_resource")))
    respx.get(as_url).mock(return_value=httpx.Response(200, json=load("authorization_server")))


@respx.mock
async def test_the_walk_reports_the_live_auth_model(fast_config):
    _mock_discovery()
    async with httpx.AsyncClient() as client:
        model = await probe_connector(ENDPOINT, fast_config, client)
    assert model.challenged
    assert model.headless_grant_available
    assert model.headless_grants_advertised == ("client_credentials",)
    assert model.required_scopes_served
    assert model.server.registration_endpoint is not None
    assert model.ok


@respx.mock
async def test_the_suffix_location_is_used_when_the_rfc_form_is_absent(fast_config):
    """Falling back is the point of having two candidates; a 404 on the first is
    not a failed probe."""
    respx.get(AS_METADATA_URL).mock(return_value=httpx.Response(HTTP_NOT_FOUND))
    _mock_discovery(as_url="https://api.ibkr.com/oauth2/.well-known/oauth-authorization-server")
    async with httpx.AsyncClient() as client:
        model = await probe_connector(ENDPOINT, fast_config, client)
    assert model.server.token_endpoint


@respx.mock
async def test_an_endpoint_that_issues_no_bearer_challenge_is_refused(fast_config):
    """Not an OAuth 2.0 protected resource. Saying so beats inventing a metadata
    URL and reporting whatever happens to be served there."""
    respx.get(ENDPOINT).mock(return_value=httpx.Response(HTTP_UNAUTHORIZED))
    async with httpx.AsyncClient() as client:
        with pytest.raises(ConnectorDiscoveryError, match="resource_metadata"):
            await probe_connector(ENDPOINT, fast_config, client)


@respx.mock
async def test_a_resource_naming_no_authorization_server_is_refused(fast_config):
    respx.get(ENDPOINT).mock(
        return_value=httpx.Response(HTTP_UNAUTHORIZED, headers={"WWW-Authenticate": CHALLENGE})
    )
    respx.get(METADATA_URL).mock(return_value=httpx.Response(200, json={"resource": ENDPOINT}))
    async with httpx.AsyncClient() as client:
        with pytest.raises(ConnectorDiscoveryError, match="no authorization server"):
            await probe_connector(ENDPOINT, fast_config, client)


# ── the verdict, and what it refuses to claim ──


def _model(**overrides) -> AuthModel:
    base = {
        "endpoint": ENDPOINT,
        "challenged": True,
        "resource": decode_protected_resource(load("protected_resource")),
        "server": decode_authorization_server(load("authorization_server")),
        "headless_grants_advertised": ("client_credentials",),
        "missing_scopes": (),
    }
    base.update(overrides)
    return AuthModel(**base)


def test_the_report_never_claims_a_token_can_be_held():
    """The whole spike rests on this sentence surviving. A green report that read
    as "this build can authenticate headlessly" would move the collection route on
    the strength of a discovery document."""
    # Normalised, because the claim is the sentence and not its line breaks: the
    # report is wrapped for a terminal and rewrapping it must not silently drop
    # the caveat this test exists to hold in place.
    rendered = " ".join(_model().render().split())
    assert "UNTESTED" in rendered
    assert "never that this build can authenticate" in rendered
    assert "registration endpoint" in rendered
    assert "token lifetime" in rendered


def test_a_server_offering_only_a_browser_flow_reports_no_headless_grant():
    """The positive control. Without it, a probe that always said "headless
    available" would pass every other test in this file."""
    browser_only = decode_authorization_server(
        {
            "issuer": "https://example.test",
            "token_endpoint": "https://example.test/token",
            "grant_types_supported": ["authorization_code", "refresh_token"],
        }
    )
    model = _model(server=browser_only, headless_grants_advertised=())
    assert not model.headless_grant_available
    assert "NO headless grant is advertised" in " ".join(model.render().split())


def test_a_resource_not_serving_the_scope_we_need_is_not_ok():
    model = _model(missing_scopes=("mcp.read",))
    assert not model.required_scopes_served
    assert not model.ok
    assert "mcp.read" in " ".join(model.render().split())


def test_a_registration_endpoint_is_reported_but_never_exercised():
    """Discovery is read-only by design: POSTing here creates a client on a live
    brokerage account, which is a decision for the account holder, not a probe."""
    assert _model().server.registration_endpoint is not None
    assert "Nothing was POSTed" in " ".join(_model().render().split())
