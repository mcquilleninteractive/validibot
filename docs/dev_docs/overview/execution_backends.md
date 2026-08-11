# Execution Backends

Validibot supports multiple deployment targets through an abstracted execution backend system. This document describes how the platform orchestrates advanced validator containers across different infrastructure.

For the container interface that validators must implement, see [Advanced Validator Container Interface](validator_architecture.md).

## Overview

The execution layer sits between the validator and the infrastructure:

```
Validator → ExecutionBackend → Infrastructure
                          ↓
          ┌───────────────┼───────────────┐
          ↓               ↓               ↓
  DockerComposeBackend   GCP deployments              AWSBackend
  (Docker socket)        (Service or retained Job)    (future)
```

Each backend handles:

- Preparing input data (uploading envelopes to storage)
- Launching validator containers
- Collecting results (synchronously or via callbacks)
- Cleaning up resources

## Backend Selection

`DEPLOYMENT_TARGET` determines whether validator execution uses local containers
or operator-managed provider routes. On GCP, the attempt resolver first pins an
eligible `ValidatorExecutionDeployment`; its provider/deployment-kind pair then
selects the exact Service or Job adapter:

| Deployment target | Backend | Execution model |
| --- | --- | --- |
| Test, local, or self-hosted | `DockerComposeExecutionBackend` | Synchronous |
| GCP managed Service route | `CloudRunServiceExecutionBackend` | Asynchronous |
| GCP managed Job route | `CloudRunJobsExecutionBackend` | Asynchronous |

`VALIDATOR_RUNNER` remains an unmanaged/legacy runner setting used by local
execution and diagnostics. It does not override a deployment already pinned to
a managed attempt.

Managed routes currently exist for shipped validator backends. User-supplied
custom container images run through the Docker backend on local and self-hosted
deployments; GCP execution fails closed unless that exact validator has an
operator-verified and activated managed deployment.

## Execution Models

### Synchronous (Docker Compose)

Used for Docker Compose deployments where validators run as local Docker containers.

```
1. Validator calls backend.execute(request)
2. Backend creates ``runs/<org>/<run>/attempts/<attempt>/`` in local storage
3. Backend mounts only that attempt's input (read-only) and output (read-write)
4. Backend spawns the Docker container and waits for completion
5. Backend verifies the attempt-bound output envelope
6. Returns complete ExecutionResponse with results
```

**Characteristics:**

- Blocking call — validation completes before returning
- Simple deployment — just Docker and shared volumes
- Retry-safe — every execution attempt receives a fresh workspace
- Resource limits enforced via Docker
- Container cleanup handled by labels (Ryuk pattern)

### Asynchronous (GCP Cloud Run)

Used for GCP deployments where workflow authors choose a provider-neutral
execution profile. The authoring UI discovers this through the central
deployment-capability policy rather than testing a provider name or probing its
environment. Fast-response attempts normally run on private Cloud Run
Services; long-running attempts use retained Cloud Run Jobs.

```
1. Validator calls backend.execute(request)
2. Backend uploads inputs below
   ``gs://<bucket>/runs/<org>/<run>/attempts/<attempt>/``
3. Resolver maps the authored Fast response or Long-running profile to an
   active route, then pins its exact immutable route/image facts
4. Service path creates one deterministic provider Cloud Task; Job path calls
   the Cloud Run Jobs API
5. Returns ExecutionResponse with is_complete=False
6. Container writes immutable output and POSTs an authenticated callback
7. Callback handler verifies and loads the exact output envelope from GCS
```

**Characteristics:**

- Non-blocking — validation runs in background
- Scalable — Cloud Run handles concurrency
- Retry-safe — attempts never share an object prefix or accepted output
- Callback-based — results require OIDC plus attempt-bound credentials
- IAM-secured — a dedicated identity is the sole Service invoker; runtime
  identities have no ambient GCS authority
- Author-selectable — workflow steps express workload intent without exposing
  GCP resource names; an attempt never changes routes after provider contact

### Release identity and retention

Cloud Run validator resources are release-specific rather than mutable stable
names. A backend release creates one private Service and one private Job from
the same digest; the application database activates their `PRIMARY` and
`LONG_RUNNING` route roles. The selected deployment is copied to the
`ExecutionAttempt` before provider contact, so a later release or route change
cannot redirect an existing attempt.

The current empty hosted installation uses
`VALIDATOR_PROVIDER_RETENTION_MODE=latest-only`: after acceptance, only the
latest active pair remains deployed and superseded provider resources are
removed before live capacity is restored. Historical database rows, evidence,
and attempt records remain intact. A live installation must use
`drain-and-retain` instead, preserving the previous accepted pair through the
normal seven-day drain for rollback and unfinished attempts.

The release-specific architecture supports concurrent existence of the active
pair, one rollback/drain pair, and providers needed by attempts already pinned
to an older deployment. It does not yet define arbitrary simultaneous routing
of multiple backend versions by tenant, workflow, or semantic Validator. If
that becomes a product requirement, the project ADR must first define version
selection, route ownership, acceptance, quotas, image retention, and attempt
reconciliation. See the sibling `validibot-project` repository's
`docs/operations/validator-backend-gcp-architecture.md` for the hosted policy.

## Two-Layer Architecture

The execution system uses a two-layer architecture:

```
ExecutionBackend (high-level orchestration)
    ├── Storage management (upload/download envelopes)
    ├── Envelope building (input envelope construction)
    ├── Status checking (check_status() for reconciliation)
    └── Delegates to → local/Job runner or Service provider-task dispatcher
                            ├── Container spawn/wait/remove
                            ├── Security hardening (cap_drop, read_only, etc.)
                            ├── Container labeling (Ryuk pattern)
                            └── Container cleanup (orphan sweep, startup cleanup)
```

**Why two layers?**

- **ExecutionBackend** handles orchestration: it knows about storage URIs, envelopes, and the callback protocol. It doesn't know how containers are spawned.
- **ValidatorRunner/provider dispatcher** handles the infrastructure launch:
  local Docker lifecycle, the Cloud Run Jobs API, or deterministic Cloud Tasks
  HTTP delivery to a private Service.

This separation means new deployment targets only need a new runner (for container execution) and a new backend (for storage integration), without duplicating orchestration logic.

For a managed Service, dispatch has a second fail-closed boundary immediately
before provider contact. The dispatcher locks the attempt and deployment,
rechecks that the deployment is still `READY` and not emergency blocked, and
moves a pending attempt to `DISPATCHING` in the same transaction. It also
compares the live deployment FK with the immutable attempt snapshot. The
provider URL, revision, audience, resource name, image digest, and execution
limit then come from that snapshot rather than mutable launch arguments.

Activating a route writes an audit entry for both the newly active deployment
and any previous slot occupant that becomes inactive. This makes rollback and
operator investigation visible from either deployment's history. Once a
deployment reaches `READY`, it may remain ready or move to `RETIRED`; it cannot
return to `DRAFT`, `VERIFYING`, or `FAILED` to reopen immutable route fields.

| Layer | Docker Compose | GCP |
|-------|---------------|-----|
| Backend | `DockerComposeExecutionBackend` | `CloudRunServiceExecutionBackend` / `CloudRunJobsExecutionBackend` |
| Runner/dispatch | `DockerValidatorRunner` | Provider HTTP task dispatcher / Cloud Run Jobs launcher |
| Storage | Local filesystem (`file://`) | GCS (`gs://`) |
| Execution | Sync (blocking) | Async (callback) |

## Code Location

```
validibot/validations/services/
├── execution/                    # Backend layer (high-level)
│   ├── __init__.py               # Exports get_execution_backend()
│   ├── base.py                   # ExecutionBackend ABC, ExecutionRequest, ExecutionResponse
│   ├── docker_compose.py         # DockerComposeExecutionBackend
│   ├── gcp.py                    # CloudRunJobsExecutionBackend
│   ├── gcp_service.py            # CloudRunServiceExecutionBackend
│   ├── gcp_service_dispatch.py   # deterministic private-Service task
│   ├── deployments.py            # resolution, activation, audit, retirement
│   └── registry.py               # deployment-aware backend selection
├── runners/                      # Runner layer (low-level)
│   ├── __init__.py               # Exports get_validator_runner()
│   ├── base.py                   # ValidatorRunner ABC, ExecutionStatus, ExecutionResult
│   ├── docker.py                 # DockerValidatorRunner (labels, security, cleanup)
│   └── google_cloud_run.py       # GoogleCloudRunValidatorRunner
└── validation_callback.py        # Callback processing (for async backends)
```

## Usage in Validators

```python
from validibot.validations.services.execution import get_execution_backend
from validibot.validations.services.execution.base import ExecutionRequest

backend = get_execution_backend()

request = ExecutionRequest(
    run=validation_run,
    validator=validator,
    submission=submission,
    step=workflow_step,
)

response = backend.execute(request)

if backend.is_async:
    # Results will arrive via callback
    return ValidationResult(passed=None, issues=[], stats={...})
else:
    # Results available immediately
    return process_output_envelope(response.output_envelope)
```

## Docker Compose Backend Details

### Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│  Docker Host                                                     │
│                                                                  │
│  ┌──────────────────┐    ┌─────────────────────────────────┐    │
│  │  Django + Worker │    │  Validator Container            │    │
│  │                  │    │  ($GCP_APP_NAME-validator-X)    │    │
│  │  - Web app       │───▶│                                 │    │
│  │  - Celery        │    │  Reads: attempt input mount     │    │
│  │                  │◀───│  Writes: attempt output mount   │    │
│  └──────────────────┘    └─────────────────────────────────┘    │
│           │                         │                            │
│           ▼                         ▼                            │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │  Worker storage volume                                   │   │
│  │  validator sees only one attempt's input and output      │   │
│  └──────────────────────────────────────────────────────────┘   │
└──────────────────────────────────────────────────────────────────┘
```

### Configuration

```python
# config/settings/production.py (when DEPLOYMENT_TARGET=self_hosted)
VALIDATOR_RUNNER = "docker"
VALIDATOR_RUNNER_OPTIONS = {
    "memory_limit": "4g",
    "cpu_limit": "2.0",
    "network": None,  # None = no network (default, most secure)
    "timeout_seconds": 3600,
}

# Container images
VALIDATOR_IMAGE_TAG = "latest"
VALIDATOR_IMAGE_REGISTRY = ""  # Or your private registry
```

### Network Isolation (Security)

By default, advanced validator containers run with **no network access** (`network_mode='none'`). This is the most secure configuration because:

- Containers cannot reach other services (web, database, redis)
- Containers cannot access the internet
- All I/O happens through one attempt-specific pair of mounts

This works because:

1. Input files are materialized below the attempt's private workspace
2. Docker exposes only that attempt's input and output directories
3. The worker verifies the output after the container exits

**When to enable network access:**

Set `VALIDATOR_NETWORK` only if advanced validators need to:

- Download files from external URLs during execution
- Call external APIs as part of validation logic

```yaml
# In docker-compose.*.yml, uncomment to enable network:
environment:
  - VALIDATOR_NETWORK=validibot_validibot
```

With network enabled, advanced validator containers can reach:

- Other containers on the same Docker network
- External internet (if the host has connectivity)

### Compose Project Naming Requirements

The Docker Compose backend requires specific naming for networks and volumes. By default, Docker Compose prefixes resource names with the project name (derived from the directory name or `COMPOSE_PROJECT_NAME`).

The shipped compose files assume `COMPOSE_PROJECT_NAME=validibot`, which creates:

| Resource       | Full Name                                   |
| -------------- | ------------------------------------------- |
| Network        | `validibot_validibot`                       |
| Storage Volume | `validibot_validibot_storage` (production)  |
| Storage Volume | `validibot_validibot_local_storage` (local) |

These names are configured in the compose files via environment variables:

```yaml
environment:
  - VALIDATOR_NETWORK=validibot_validibot
  - VALIDATOR_STORAGE_VOLUME=validibot_validibot_storage
```

**If you change the project name** (via `COMPOSE_PROJECT_NAME` or running from a different directory), you must update these environment variables to match. Otherwise, the worker cannot attach advanced validator containers to the correct network or volume.

To check your current project name:

```bash
# The project name is the prefix before the underscore in container names
docker compose -f docker-compose.production.yml ps --format "{{.Name}}"
# Example output: validibot_web_1 → project name is "validibot"
```

To override explicitly:

```bash
# Set project name explicitly
COMPOSE_PROJECT_NAME=validibot docker compose -f docker-compose.production.yml up -d
```

### Private Registry Authentication

Published validator releases are public packages in GitHub Container
Registry (GHCR), under
`ghcr.io/mcquilleninteractive/validibot-validator-backend-<slug>:v<release>`.
Public pulls do not require a registry login. Configure Docker credentials on
the host only when using a private or internally mirrored registry (for
example, private GHCR, AWS ECR, or Google Artifact Registry).

**Option 1: Docker login on the host**

```bash
# Log in to your registry on the Docker host
echo "$TOKEN" | docker login ghcr.io -u USERNAME --password-stdin

# Or for AWS ECR
aws ecr get-login-password --region us-east-1 | docker login --username AWS --password-stdin 123456789.dkr.ecr.us-east-1.amazonaws.com
```

The Docker daemon stores credentials in `~/.docker/config.json` and uses them for pulls. Since the worker spawns containers via the host's Docker socket, these credentials apply to validator image pulls automatically.

**Option 2: Pass credentials via environment**

For registries that support credential helpers, configure them on the host:

```bash
# Install and configure credential helper (e.g., for GCR)
gcloud auth configure-docker
```

**Image naming:**

Configure the validator image registry in your environment:

```bash
# .envs/.production/.self-hosted/.django
VALIDATOR_IMAGE_REGISTRY=ghcr.io/your-org
VALIDATOR_IMAGE_TAG=v1.2.0
```

Images are pulled as `{VALIDATOR_IMAGE_REGISTRY}/validibot-validator-backend-{type}:{tag}`.
The image name matches `ValidatorConfig.image_name` for each validator
(see `validators/<slug>/config.py`). For example:

- `ghcr.io/your-org/validibot-validator-backend-energyplus:v1.2.0`
- `ghcr.io/your-org/validibot-validator-backend-fmu:v1.2.0`

**Image availability:**

The Docker backend does not automatically pull images. Ensure validator images are available before running validations:

```bash
# Pre-pull images on the host
docker pull ghcr.io/your-org/validibot-validator-backend-energyplus:v1.2.0
```

Or configure a pull policy by extending the runner options if automatic pulls are needed.

### Container Management

Validator containers are labeled for identification and cleanup:

```
org.validibot.managed=true
org.validibot.run_id=<run-id>
org.validibot.validator=<slug>
org.validibot.started_at=<iso>
org.validibot.timeout_seconds=N
```

Cleanup strategies:

1. **On-demand** — Container removed after run completes
2. **Periodic sweep** — Background task every 10 minutes
3. **Startup cleanup** — Worker removes leftover containers on start

Management command:

```bash
# Show what would be cleaned up
python manage.py cleanup_containers --dry-run

# Remove orphaned containers
python manage.py cleanup_containers

# Remove ALL managed containers
python manage.py cleanup_containers --all
```

## GCP Backend Details

For detailed GCP architecture including Services, retained Jobs, IAM, and callbacks, see:

- [Validator Containers (Cloud Run)](../validator_jobs_cloud_run.md) — Service/Job execution and callbacks
- [GCP Deployment](../google_cloud/deployment.md) — Service deployment
- [IAM & Service Accounts](../google_cloud/iam.md) — Security configuration

### Key Concepts

**Web/Worker Split:**

- `$GCP_APP_NAME-web` — Public UI and API
- `$GCP_APP_NAME-worker` — Private, receives callbacks from validator Services and Jobs

**Callback Authentication:**

- Validator Services and Jobs use Google-signed callback ID tokens
- Worker service requires IAM authentication
- No shared secrets in envelopes

**Storage:**

- Input/output envelopes stored in GCS
- URIs use `gs://` scheme
- Validator runtimes receive attempt-scoped GCS capability tokens; their
  service account has no ambient bucket object permissions

## Status Checking

The `ExecutionBackend` base class provides a `check_status()` method for querying the state of a running or completed execution:

```python
def check_status(self, execution_id: str) -> ExecutionResponse | None:
    """Check execution status. Returns None if not supported."""
    return None
```

| Backend | Behavior |
|---------|----------|
| `DockerComposeExecutionBackend` | Queries Docker daemon for container state. Primarily for debugging (sync execution already returns results). |
| `CloudRunJobsExecutionBackend` | Queries the Cloud Run Jobs API for execution state. |
| `CloudRunServiceExecutionBackend` | Explicitly reports status lookup as unsupported because Cloud Run has no durable per-request Service resource. |

This method is **not abstract** — backends that don't need status checking (sync backends) can leave the default `None` return.

## Container Cleanup

Container lifecycle management happens at the **runner layer**, not the backend layer:

### Docker Compose (three strategies)

1. **Immediate cleanup** — `container.remove(force=True)` in the runner's `finally` block after every execution
2. **Periodic sweep** — `cleanup_orphaned_containers()` runs via Celery Beat every 10 minutes, removes containers past timeout + grace period
3. **Startup cleanup** — `cleanup_all_managed_containers()` runs in `AppConfig.ready()`, removes all labeled containers from previous worker incarnation

All strategies use Docker container labels (`org.validibot.managed`, `org.validibot.run_id`, etc.) for identification.

### GCP Cloud Run

Cloud Run Jobs are ephemeral. Validator Services reuse a bounded HTTP parent
but create a fresh one-shot child, operating-system session, and scratch
directory for every request. A hard deadline sends `SIGTERM` and then
`SIGKILL`, if required, to the complete child process group. Native tools such
as EnergyPlus therefore cannot survive their Python child on a reused warm
instance. Cloud infrastructure needs no per-container cleanup; attempt scratch
cleanup is enforced by the runtime, and application recovery is handled by the
reconciliation system.

## Error Recovery

### Lost Callback Recovery (GCP)

If a validator runtime writes output but its callback never reaches Django,
`cleanup_stuck_runs` attempts capability-aware reconciliation:

1. Finds runs stuck in `RUNNING` status past the timeout threshold
2. Reads the attempt's immutable deployment snapshot and output generation
3. For Jobs, queries status through `CloudRunJobsExecutionBackend`; for
   Services, records that provider status is unsupported and retries bounded
   immutable-output salvage
4. Based on verified evidence:
   - **Still running after the configured outer deadline**: Marks the run
     `TIMED_OUT`, then requests provider cancellation
   - **Verified output exists**: Constructs a synthetic callback and processes
     through `ValidationCallbackService` (reuses idempotency, persistence, and
     assertion evaluation)
   - **Failed**: Marks the run as `FAILED` with the Cloud Run error message
   - **Provider lookup unavailable/unsupported and no output**: Retries within
     the lookup grace window, then the absolute attempt deadline wins

This reconciliation runs automatically when `cleanup_stuck_runs` is scheduled (typically every 10 minutes via Cloud Scheduler).

### Stuck Run Timeout (All Backends)

For runs where reconciliation is not possible (non-GCP, no execution metadata,
API errors), the command marks them as `TIMED_OUT` after the configured
`VALIDATOR_TIMEOUT_SECONDS` threshold (default: 3600 seconds / 60 minutes). The
same setting configures local container runtime and the GCP validator Job deploy
recipe. `--timeout-minutes` remains available as an explicit operational
override.

```bash
# Manual invocation
python manage.py cleanup_stuck_runs
python manage.py cleanup_stuck_runs --timeout-minutes 60
python manage.py cleanup_stuck_runs --dry-run
```

## Adding a New Backend

To support a new deployment target (e.g., AWS):

1. Create `validibot/validations/services/execution/aws.py`
2. Implement `ExecutionBackend` abstract class
3. Register in `registry.py`
4. Add setting value to backend selection logic

```python
# aws.py
from .base import ExecutionBackend, ExecutionRequest, ExecutionResponse


class AWSExecutionBackend(ExecutionBackend):
    is_async = True  # or False for synchronous

    def execute(self, request: ExecutionRequest) -> ExecutionResponse:
        # Upload envelope to S3
        # Trigger ECS task or Lambda
        # Return response
        ...
```
