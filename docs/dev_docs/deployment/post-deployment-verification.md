# Post-Deployment Verification (PDV)

After deploying to any environment, run the PDV smoke tests to verify that critical functionality is working correctly.

## Quick Start

```bash
# After deploying to dev
just gcp deploy-all dev
just gcp verify-deployment dev

# After deploying to production
just gcp deploy-all prod
just gcp verify-deployment prod
```

## Application Health Check (doctor)

For self-hosted Compose deployments, the operator-facing entry point
is `just self-hosted doctor`. That recipe shells into the web
container and runs the same `check_validibot` management command
internally.

```bash
# From the repo root, against a running self-hosted stack:
just self-hosted doctor                       # human-readable
just self-hosted doctor --json                # stable JSON schema (validibot.doctor.v1)
just self-hosted doctor --strict              # warnings cause non-zero exit
just self-hosted doctor --verbose             # show detail blocks
```

For developers running locally without going through `just`, you can
invoke the underlying command directly:

```bash
# Inside the web container (e.g. via just local manage):
python manage.py check_validibot
python manage.py check_validibot --target self_hosted
python manage.py check_validibot --target gcp --stage prod
python manage.py check_validibot --json
python manage.py check_validibot --strict
python manage.py check_validibot --fix
```

Each result includes a stable check ID (`VB101`, `VB401`, etc.). See
[`docs/operations/self-hosting/doctor-check-ids.md`][check-ids] for
the ID-to-fix mapping.

[check-ids]: https://github.com/mcquilleninteractive/validibot/blob/main/docs/operations/self-hosting/doctor-check-ids.md

The `check_validibot` command verifies:

| Check | What it verifies |
|-------|------------------|
| **Database** | Connection, PostgreSQL version |
| **Migrations** | All migrations applied |
| **Cache** | Redis/cache connectivity |
| **Storage** | File storage read/write access |
| **Site** | Django Sites configuration |
| **Roles & Permissions** | Required roles and permissions exist |
| **Validators** | System validators configured |
| **Background Tasks** | Celery broker connectivity, schedules |
| **Docker** | Docker availability, validator images |
| **Email** | SMTP server reachability |
| **Security** | DEBUG mode, SECRET_KEY, API-key digest key, ALLOWED_HOSTS, HTTPS settings |

This is similar to GitLab's `gitlab:check` rake task or Zulip's health check plugins.

### When to Run

- After initial `setup_validibot` to verify everything is configured
- After upgrades to catch configuration drift
- When troubleshooting issues
- As part of monitoring/alerting (use `--json` output)

## What Gets Tested

The PDV suite verifies:

### Web Service

- Homepage is accessible (returns 200)
- Static files are served (robots.txt)
- API documentation is accessible (/api/v1/docs/)
- API endpoints require authentication

### Worker Service Security

- **IAM protection**: Unauthenticated requests are rejected with 403
- **Callback endpoint**: Cannot be spoofed by external attackers
- **Scheduled task endpoints**: Protected from external access
- **Authenticated requests**: Reach Django when properly authenticated

The worker security tests are particularly important because the callback
endpoint is how validator Services and retained Jobs report their results. If
this endpoint were exposed, attackers could spoof validation results.

### MCP Server (Pro)

On a Pro deployment, run these checks against the same public application
origin as normal Django traffic:

```bash
# 1. Public OAuth metadata describes the exact embedded resource
curl -s https://app.your-domain.example/.well-known/oauth-protected-resource/mcp | jq .
curl -s https://app.your-domain.example/.well-known/oauth-authorization-server | jq .

# 2. Unauthenticated MCP requests get a 401 (not 4xx-other)
curl -s -o /dev/null -w "%{http_code}\n" -X POST https://app.your-domain.example/mcp
# → expect 401

# 3. The normal web revision serves both route families
just gcp status prod
just gcp logs prod
```

Connecting ChatGPT in developer mode, completing OAuth, calling all tools, and
testing representative attachments is the final validation. Maintainers should
follow `docs/operations/mcp-server.md` in the adjacent `validibot-project`
repository for the private deployment checklist.

## Commands

### Full Verification

Runs the complete pytest smoke test suite:

```bash
just gcp verify-deployment <stage>
```

Options:
- `just gcp verify-deployment dev` - Test dev environment
- `just gcp verify-deployment staging` - Test staging environment
- `just gcp verify-deployment prod` - Test production

You can pass pytest arguments:
```bash
# Run only callback tests
just gcp verify-deployment prod -k "callback"

# Show more detail
just gcp verify-deployment prod -vv

# Stop on first failure
just gcp verify-deployment prod -x
```

### Quick Verification

A faster check that just verifies services are up and IAM is working:

```bash
just gcp verify-deployment-quick <stage>
```

This uses curl to check:
1. Web service returns 200
2. Worker service returns 403 (IAM protected)

Good for a quick sanity check, but doesn't test as thoroughly as the full suite.

## Prerequisites

1. **Deployed services**: Both web and worker services must be deployed to the target stage
2. **gcloud CLI**: Must be installed and in your PATH
3. **Valid credentials**: Must be logged in with `gcloud auth login`
4. **Cloud Run Invoker role**: Your account needs permission to invoke the worker service (for authenticated tests)

## How It Works

All smoke tests **run locally on your machine** and make HTTP requests to the remote deployed services. No code runs on the server as part of PDV.

```
Your laptop                          GCP Cloud Run
─────────────────                    ─────────────────
just gcp verify-deployment prod
    │
    ├─► gcloud: resolve service URLs
    │
    ├─► pytest tests/smoke/
    │       │
    │       ├─► HTTP requests ──────────────────► Web Service
    │       │
    │       ├─► HTTP requests ──────────────────► Worker Service
    │       │
    │       └─► Verify responses
    │
    └─► Results displayed locally
```

This approach:
- Tests the **real deployed infrastructure** (Cloud Run, IAM, load balancers, DNS)
- Requires no additional deployment or management commands on the server
- Is simple to run - just needs gcloud credentials locally

The tests use the `SMOKE_TEST_STAGE` environment variable to know which stage to test. The `just gcp verify-deployment` command sets this automatically - you don't need to add it to any secrets or environment files.

## Test Structure

```
tests/smoke/
├── __init__.py               # Module docstring
├── conftest.py               # Fixtures (URLs, HTTP sessions)
├── test_web_service.py       # Web service health tests
├── test_authenticated.py     # Tests requiring a signed-in user
└── test_worker_security.py   # Worker IAM/security tests
```

## Adding New Tests

To add a new smoke test:

1. Create a new test file in `tests/smoke/` or add to an existing file
2. Use the provided fixtures:
   - `web_url` - The deployed web service URL
   - `worker_url` - The deployed worker service URL
   - `http_session` - Unauthenticated requests session
   - `authenticated_http_session` - Session with gcloud identity token
   - `stage` - The current stage (dev/staging/prod)

Example:

```python
def test_my_new_endpoint(web_url: str, http_session):
    """Verify my new endpoint works."""
    response = http_session.get(f"{web_url}/api/v1/my-endpoint/", timeout=30)
    assert response.status_code == 200
```

## Troubleshooting

### "SMOKE_TEST_STAGE must be set"

Run via `just gcp verify-deployment <stage>` instead of running pytest directly, or set the environment variable:

```bash
SMOKE_TEST_STAGE=dev uv run pytest tests/smoke/ -v
```

### "Failed to get service URL"

The service isn't deployed or you don't have permission to describe it:

```bash
# Check if service exists (substitute your configured region)
gcloud run services describe $GCP_APP_NAME-web-dev --region=$GCP_REGION
```

### "Authenticated request was rejected by IAM"

Your gcloud account doesn't have the Cloud Run Invoker role on the worker service:

```bash
# Grant yourself invoker access (if you're an admin)
gcloud run services add-iam-policy-binding $GCP_APP_NAME-worker-dev \
  --region=$GCP_REGION \
  --member="user:you@example.com" \
  --role="roles/run.invoker"
```

### Worker returns 200 instead of 403

The worker service may have been deployed with `--allow-unauthenticated`. This is a security issue - redeploy with:

```bash
just gcp deploy-worker <stage>
```

The deploy script uses `--no-allow-unauthenticated` by default.

## Related

- [Deployment Overview](./overview.md)
- [Callback Security](../overview/validator_architecture.md)
