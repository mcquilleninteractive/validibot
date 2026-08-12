# MCP Server

Validibot exposes an authenticated Model Context Protocol endpoint from the
normal Django ASGI application. The implementation uses the official `mcp`
Python SDK's stateless Streamable HTTP transport at `<SITE_URL>/mcp`; there is
no separate MCP package, image, service, port, or service-to-service proxy.

The source is public Community code, while serving the route is a Pro feature.
Installing `validibot-pro` registers the `mcp_server` feature. Without that
feature, the ASGI router does not mount `/mcp`.

Maintainers can find the architectural decision and private operational
acceptance checklist in the adjacent `validibot-project` repository:
`docs/adr/2026-08-12-mcp-refactor-and-improvement.md` and
`docs/operations/mcp-server.md`.

## Code map

| Concern | Path |
|---|---|
| ASGI route selection | `config/asgi.py` |
| Official-SDK server and tool declarations | `validibot/mcp_server/server.py` |
| Direct Django application services | `validibot/mcp_server/services.py` |
| OAuth token verification | `validibot/mcp_server/auth.py` |
| Opaque workflow and run references | `validibot/mcp_server/references.py` |
| Typed tool schemas | `validibot/mcp_server/schemas.py` |
| Per-principal quotas | `validibot/mcp_server/rate_limits.py` |
| Protocol and application tests | `validibot/mcp_server/tests/` |
| OAuth provider and public-client registration | `validibot/idp/` |
| Pro feature registration | `validibot-pro/validibot_pro/license.py` |

`validibot/mcp_api/` is not used by the embedded endpoint. It remains only as
legacy compatibility code for the Cloud x402 agent channel and should not be
used for new MCP work.

## Request path

```text
ChatGPT, Codex, or another MCP client
  -> HTTPS <SITE_URL>/mcp
  -> Gunicorn with UvicornWorker (production) or Uvicorn (local)
  -> config.asgi
  -> official SDK authentication and Streamable HTTP handler
  -> typed MCP tool
  -> Django application service
  -> canonical queryset, policy, launch, and audit services
```

The endpoint is stateless and returns JSON. Any worker can handle any request;
no sticky session, Redis-backed MCP session, or second internal HTTP hop is
required.

## Authentication

The MCP endpoint is an OAuth 2.1 protected resource. Django is both the
authorization server and the application containing the resource server.

- The protected resource is exactly `<SITE_URL>/mcp` unless
  `IDP_OIDC_MCP_RESOURCE_AUDIENCE` explicitly overrides it.
- Tokens require the `validibot:mcp` scope.
- ChatGPT uses a predefined public OAuth client with authorization code and
  PKCE; it has no client secret.
- django-allauth owns login, consent, authorization/token/revocation endpoints,
  PKCE, refresh rotation, token persistence, and both discovery documents. The
  RFC 8414 path is an alias of allauth's discovery view; Validibot only uses
  allauth's supported adapter hook for the MCP scope, exact resource policy,
  canonical public origin, and RFC 8707 refresh inheritance. The last hook
  reads the exact resource retained on allauth's refresh-token row when a
  client correctly omits `resource` on refresh.
- The verifier checks signature algorithm, issuer, audience, subject, client,
  scopes, resource binding, expiry, revocation, the allauth token record, and
  the active local user.
- OAuth protected-resource metadata is served at
  `/.well-known/oauth-protected-resource/mcp`.

There is no MCP service account and no shared MCP-to-Django key in the embedded
path. Do not add `MCP_SERVICE_KEY`, `VALIDIBOT_MCP_SERVICE_KEY`, an MCP
confidential client, or an `mcp-env` secret for a new deployment.

## Tools

The deliberately small surface contains five tools:

| Tool | Purpose |
|---|---|
| `list_workflows` | Discover a bounded page of MCP-enabled workflows. |
| `get_workflow` | Inspect one workflow's file constraints and ordered steps. |
| `start_validation` | Start one idempotent validation from a ChatGPT attachment. |
| `get_validation_run` | Poll a run's current state and aggregate counts. |
| `list_validation_findings` | Read a bounded findings page after completion. |

Every declaration includes a title, description, strict input/output schemas,
tool behavior annotations, and OAuth `securitySchemes` metadata for ChatGPT.
`start_validation` additionally declares `_meta["openai/fileParams"] =
["file"]`; its top-level `file` object follows OpenAI's required
`download_url`, `file_id`, optional `mime_type`, and optional `file_name`
schema.
The application returns opaque references rather than database identifiers,
applies canonical Django authorization, and records bounded audit evidence
without file bodies, bearer tokens, or secrets.

The server downloads the temporary attachment URL without forwarding cookies
or bearer credentials. It requires credential-free HTTPS on port 443, rejects
non-public DNS results at every redirect, streams into a conservative byte
limit, and rechecks that limit in the launch service. Real ChatGPT text and
binary attachment behavior remains an external acceptance gate; do not claim
general file support until the production-like developer-mode test in the
operations guide is complete.

## Configuration

All current MCP settings belong in the web application's normal Django
environment:

| Setting | Default | Purpose |
|---|---:|---|
| `IDP_OIDC_MCP_RESOURCE_AUDIENCE` | `<SITE_URL>/mcp` | Exact OAuth resource and JWT audience. |
| `IDP_OIDC_CHATGPT_REDIRECT_URIS` | empty | Exact ChatGPT callback URI or URIs. |
| `MCP_FILE_MAX_BYTES` | `2500000` | Maximum downloaded attachment size. |
| `MCP_MAX_REQUEST_BODY_BYTES` | `4194304` | Maximum Streamable HTTP request body. |
| `MCP_READS_PER_MINUTE` | `120` | Shared per-principal budget across all read tools. |
| `MCP_STARTS_PER_MINUTE` | `20` | Per-principal validation-start budget. |
| `MCP_ALLOWED_ORIGINS` | empty | Additional exact trusted browser origins. |

Keep `SITE_URL`, the audience, OAuth metadata, callback registration, and the
public route on one canonical HTTPS origin. A Pro process restart is required
after changing the installed license or feature registration because the ASGI
route is selected at process startup.

## Running and testing locally

Community mode leaves `/mcp` unmounted:

```bash
just local up
```

Pro and Cloud settings activate the embedded route on the normal web port:

```bash
just local-pro up --build
just local-cloud up --build
```

Run the implementation and protocol suites from the Community repository:

```bash
uv run pytest -q validibot/mcp_server/tests validibot/idp/tests
```

`test_protocol_integration.py` uses the official client against the real ASGI
application. It covers discovery, authentication, generated schemas, all five
tools, idempotent replay, privacy-preserving errors, shared throttling, and
audit recording. Token-verifier tests separately exercise real JWT and allauth
record validation.

## Production deployment

The production web container runs the existing Django image under Gunicorn
with `uvicorn_worker.UvicornWorker`. GCP routes `/mcp` through the same load
balancer and Cloud Run service as normal Django traffic. Self-hosted deployments
route it through the same reverse proxy and web container as the site.

Do not create a second hostname unless the main deployment has a documented
reason to do so. The simplest supported URL is `<SITE_URL>/mcp`.

Before publication, complete the external checklist in the operations guide:
real HTTPS OAuth from ChatGPT developer mode, representative file attachment
tests, denied-access checks, deployment rollback, and OpenAI review assets.

## Troubleshooting

| Symptom | Check |
|---|---|
| `/mcp` is 404 | Confirm the Pro package is installed, the `mcp_server` feature is registered, and the web process was restarted. |
| `/mcp` is 401 | Inspect the `WWW-Authenticate` metadata URL, JWT audience/scope, callback registration, and token revocation state. |
| Host or origin rejected | Align `SITE_URL`, `ALLOWED_HOSTS`, proxy host forwarding, and any explicit `MCP_ALLOWED_ORIGINS`. |
| Tool is not listed | Run the official-client protocol test and inspect `build_mcp_server()` registration. |
| Tool returns `NOT_FOUND` | The reference is malformed or the authenticated user cannot access the object; the response deliberately does not distinguish those cases. |
| Tool returns `RATE_LIMITED` | Wait for the current fixed minute window; all reads share one budget and starts use a separate budget. |
| File is rejected | Check that the temporary URL is still valid, the file is below `MCP_FILE_MAX_BYTES`, and its type satisfies workflow policy. |
