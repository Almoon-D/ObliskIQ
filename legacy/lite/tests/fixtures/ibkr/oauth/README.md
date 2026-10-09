# IBKR OAuth 2.0 discovery documents

Fetched verbatim on 2026-09-03 from the Custom Connector's advertised locations:

- `protected_resource.json` — `GET https://api.ibkr.com/v1/api/mcp-public/.well-known/oauth-protected-resource`
  (RFC 9728). Note the **suffix** form: the RFC-canonical form, which inserts the
  well-known segment between host and path, returns `404` here. `connector.py`
  reads the URL out of the `WWW-Authenticate` challenge for exactly this reason.
- `authorization_server.json` — `GET https://api.ibkr.com/oauth2/.well-known/oauth-authorization-server`
  (RFC 8414). This one is served at **both** the insertion and suffix forms.

They are evidence, not configuration. Nothing reads them at runtime; they exist so
`tests/test_ibkr_connector.py` decodes the real documents rather than a paraphrase
of them, and so a later change at IBKR is visible as a diff.
