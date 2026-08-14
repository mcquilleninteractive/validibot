# Validator Images

This page covers the validator image story for self-hosted operators: what's pre-installed, how to inventory them, how to pin versions, how to add custom validators, and how the run-scoped isolation guarantees work.

## What's an "advanced validator"?

Validibot has two classes of validator:

- **Simple validators** run synchronously inside the Django process. Examples: JSON Schema and XML Schema (with optional step assertions afterward), Basic (CEL assertions), and AI (LLM-backed checks). They never touch a container.
- **Advanced validators** delegate the heavyweight work to an external Docker container — usually a third-party simulation engine like EnergyPlus or FMU. The Django code dispatches an input envelope and reads the output envelope when the container exits.

Advanced validators are a Pro feature. Community deployments only see simple validators.

For the developer-facing reference on how this works, see the dev-docs companion at `docs/dev_docs/overview/validator_architecture.md`.

## What's pre-installed

The current shipped advanced validators:

| Slug | Backend image (built locally) | Purpose |
|---|---|---|
| `energyplus` | `validibot-validator-backend-energyplus:<git_sha>` | Building energy simulation |
| `fmu` | `validibot-validator-backend-fmu:<git_sha>` | Functional Mock-up Unit simulation |
| `shacl` | `validibot-validator-backend-shacl:<git_sha>` | RDF graph validation |
| `schematron` | `validibot-validator-backend-schematron:<git_sha>` | Schematron XML validation |
| `portfolio_manager` | `validibot-validator-backend-portfolio-manager:<git_sha>` | ENERGY STAR® Portfolio Manager® report and collection validation |
| `pdf` | `validibot-validator-backend-pdf:<git_sha>` | Restricted static-text PDF package and attachment validation |

Self-hosted recipes **build validator images locally** from a sibling checkout
of `validibot-validator-backends` by default. Signed backend releases also
publish every managed image to GHCR with attestations and SPDX SBOM assets; hosted
GCP mirrors those exact digests into Artifact Registry rather than rebuilding
them. Build the self-hosted images with:

```bash
just self-hosted validator-build energyplus
just self-hosted validator-build fmu
just self-hosted validator-build portfolio_manager
just self-hosted validators-build-all      # builds every managed backend
```

The build stamps OCI labels (`org.opencontainers.image.version`, `revision`, `source`, `io.validibot.validator-backend.slug`) onto the image, so a future `docker inspect` can read the human-readable backend version straight from the image metadata.

## Inventory

```bash
just self-hosted validators
```

Lists every `validibot-validator-backend-*` image on the local Docker daemon, with its OCI version label, content digest, size, and age:

```text
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  Validator backends — local Docker daemon
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

REPOSITORY                                   TAG       BACKEND VERSION  DIGEST                  SIZE       AGE
-------------------------------------------  --------  -----------------  ----------------------  ---------  ---
validibot-validator-backend-energyplus       abc1234   25.2.0           sha256:f7a3c4d8e2b1...  456MB      2 days ago
validibot-validator-backend-energyplus       latest    25.2.0           sha256:f7a3c4d8e2b1...  456MB      2 days ago
validibot-validator-backend-fmu              abc1234   0.3.29           sha256:9b1c2d3e4f5a...  234MB      2 days ago
validibot-validator-backend-fmu              latest    0.3.29           sha256:9b1c2d3e4f5a...  234MB      2 days ago

Tip: backend version comes from org.opencontainers.image.version
     (set when the image was built from validibot-validator-backends).
     Use the digest for trust-critical verification, not the tag.
```

The "BACKEND VERSION" column is the **human-readable identity** (e.g. EnergyPlus 25.2.0). The "DIGEST" column is the **cryptographic identity** — that's what cosign signs and what trust verification commits to. They serve different audiences:

- **Operator browsing inventory** → reads BACKEND VERSION
- **Audit / cryptographic verifier** → reads DIGEST

## Smoke testing the validation pipeline

There's no separate `validators smoke-test` subcommand — the main `just self-hosted smoke-test` exercises the JSON Schema validator end-to-end through the same code path real validations use. If that passes, the pipeline (queue, worker, dispatcher) is healthy.

For verifying the *advanced* validators specifically (EnergyPlus, FMU), the operational path is:

1. Run a real workflow against the validator with a known-good input.
2. Inspect the `ValidationRun` outcome.

A failed advanced-validator run points at:

- Docker socket unreachable (self-hosted), provider-queue/Service invoker IAM,
  or retained Job controller permissions (GCP);
- validator image not built locally (see Inventory above) or pull credentials wrong (cloud);
- storage misconfiguration (data root not writable, run workspace can't be created);
- network policy blocking required outbound calls (most validators run with `network=none` by default).

A future `validators smoke-test` recipe could automate this; for the MVP, the operator-driven path is sufficient because most operators have at least one workflow they care about and can run manually.

## Image pinning

Production self-hosted docs recommend pinning validator images by exact version, not `latest`. The Compose stack reads validator images from the `validibot-pro` package metadata — when you upgrade Pro, you get the validator image versions Pro was tested against.

For risk-averse customers who want stricter pinning, set the
`VALIDATOR_BACKEND_IMAGE_POLICY` env var:

```bash
VALIDATOR_BACKEND_IMAGE_POLICY=digest just self-hosted deploy
```

Policy values:

| Policy | What it does |
|---|---|
| `tag` | Default for community quick-start. Image references like `ghcr.io/mcquilleninteractive/validibot-validator-backend-energyplus:v0.15.4`. |
| `digest` | Production-recommended. Image references include `@sha256:...` digests pinned at deploy time. |
| `signed-digest` | High-trust deployments. Requires digest pinning plus enabled/configured cosign verification. |

`signed-digest` and optional cosign verification are implemented, but they are
separate from the hosted release mirror's GitHub attestation check. Enable the
policy only after configuring the cosign verification key and proving the
runtime verification path; otherwise doctor and launch fail closed.

!!! warning "Enable `digest` only once your images are digest-pinned"
    The policy is **enforced at launch**. When it is `digest` (or
    `signed-digest`), the runner **refuses to start** any validator backend
    whose image is referenced by a floating tag instead of a `@sha256:…`
    digest — that is the point (it stops a swapped tag from running), but it
    means you must pin your validator images by digest *before* turning the
    policy up, or every validation will fail to launch. The default stays `tag`
    so the community quick-start works out of the box. The hosted/GCP path
    deploys signed releases to Services and retained Jobs by Artifact Registry
    digest and can therefore enforce `digest` after the exact registered
    deployments have been verified.

## Run-scoped isolation

The supported self-hosted Docker path gives every validator backend runtime:

- a per-attempt input directory mounted **read-only** at
  `/validibot/attempts/<attempt-id>/input`;
- a per-attempt output directory mounted **read-write** at
  `/validibot/attempts/<attempt-id>/output`;
- a tmpfs at `/tmp` for scratch work;
- nothing else from the host.

Default container policy:

- `network_mode="none"` unless the operator globally configures `VALIDATOR_NETWORK`;
- `cap_drop=["ALL"]`;
- `security_opt=["no-new-privileges:true"]`;
- non-root user (UID 1000);
- read-only root filesystem;
- pids, memory, CPU, and timeout limits;
- container labels for cleanup;
- image pinned by digest when policy is `digest` or `signed-digest`.

A buggy or compromised validator backend on this local path cannot read another
attempt's input mount or mutate another attempt's output mount. Run
`just self-hosted doctor --json` and inspect `storage_capability` (also reported
as `VB205`) to confirm the effective mode. Object-store deployments are not
automatically equivalent: supported GCS + Cloud Run execution requires a
prefix-bound attempt token and a runtime identity with no ambient object
access; production acceptance proves the provider-side IAM boundary.
S3-compatible storage remains unsupported until its conditional and version
semantics are capability-tested. See
[Security Hardening](security-hardening.md) for the architectural rationale.

## Container-engine hardening

- **rootless Docker** — the supported default for fresh self-hosted installs.
  `VALIDATOR_CONTAINER_SOCKET` resolves to the deployment user's rootless
  socket; confirm `VB322` before running the EnergyPlus integration acceptance
  test. Existing rootful deployments remain compatible through an explicit
  `/var/run/docker.sock` override.
- **rootless Podman** — a Docker-API compatibility path, not yet a drop-in
  supported replacement. Qualify each Podman release with the full advanced
  validator acceptance suite before production use.
- **Docker socket proxy** — a possible future control. It is not currently
  shipped or acceptance-tested, and the API surface needed for image
  inspection, launch, waits, logs, volume inspection, and cleanup must be
  allowed together.
- **gVisor runtime** — sandbox containers with a user-space kernel.
- **Kubernetes Job runner** — alternative to Docker Compose; future hardening track.
- **per-validator seccomp profiles** — fine-grained syscall filtering.
- **egress deny-by-default network policies** — at the network layer rather than the container layer.

The next major hardening track is **two-tier validator trust** (Phase 5):

- **Tier 1 — first-party** (current EnergyPlus, FMU): current Phase 1 hardening.
- **Tier 2 — user-added** (future self-service registration): tier 1 + explicit egress allowlist, tighter resource caps, gVisor or Kata runtime, cosign-signed image required, pre-flight scan.

## Adding custom validators

User-supplied validator backends are not yet supported in the self-hosted MVP. The infrastructure exists (`AdvancedValidator` base class, `ExecutionBackend` abstraction, envelope schema in `validibot-shared`), but the self-service registration flow + tier-2 hardening profile + image scan + cosign verification will land in Phase 5.

If you have a custom validator backend you want to run today, the path is a paid professional services engagement: we build it as a first-party container in `validibot-validator-backends`, ship it as part of `validibot-pro`, and you pull it via the standard upgrade flow. Talk to support.

## Cleanup

Validator containers are short-lived but accumulate as exit artifacts. So do manifested backups past their retention window and old upgrade reports. The `cleanup` recipe walks all of these in one pass:

```bash
just self-hosted cleanup --dry-run    # list candidates without deleting
just self-hosted cleanup              # interactive: list, prompt, delete
just self-hosted cleanup --yes        # cron-friendly: list, delete (no prompt)
```

Three retention scopes, each configurable via env var:

| Scope | Default | Env var override |
|---|---|---|
| Stopped validator containers (filtered by `io.validibot.validator-backend.slug` label) | 24h | `VALIDATOR_RETAIN_HOURS` |
| Manifested backups (read `manifest.json::created_at`) | 30d | `BACKUP_RETAIN_DAYS` |
| Upgrade reports (`backups/upgrades/*/report.json` mtime) | 90d | `UPGRADE_REPORT_RETAIN_DAYS` |

Plus a "bonus pass" that prunes Docker dangling images (always safe — nothing references them).

The recipe **always lists candidates before any deletion**, even without `--dry-run`. The operator sees what will be removed, then confirms (or re-runs with `--yes` for cron). Pattern adopted from Discourse's `./launcher cleanup`.

What `cleanup` does NOT touch:

- Validator backend **images** themselves — re-pulling/re-building is expensive. Use `docker image prune` directly when you genuinely want to reclaim image storage.
- Working-set volumes (Postgres, Redis, `validibot_storage`). Those are part of the live deployment; `clean-all` is the recipe for that.
- Ad-hoc `backup-db` `.sql.gz` dumps at the top of `backups/`. Those are operator-managed; we don't know your retention policy.

A reasonable cron entry:

```cron
0 3 * * 0  cd /srv/validibot/repo && just self-hosted cleanup --yes >> /var/log/validibot-cleanup.log 2>&1
```

Weekly cleanup at 3am Sunday. The log shows what was removed; if nothing matched, the recipe prints "Nothing to clean up." and exits 0.

## Public validator images

Released validator images are published as public GHCR packages under the
McQuillen Interactive organization:

```text
ghcr.io/mcquilleninteractive/validibot-validator-backend-<slug>:v<release>
```

For example:

```text
ghcr.io/mcquilleninteractive/validibot-validator-backend-energyplus:v0.15.4
```

Public pulls do not require a GHCR login. Validibot does not maintain a Docker
Hub mirror, and `ghcr.io/validibot/...` is not a Validibot-owned namespace.
Operators can still build the images locally from the
[`validibot-validator-backends`](https://github.com/mcquilleninteractive/validibot-validator-backends)
checkout when auditing or customizing a backend.

Use an exact release tag for evaluation. For production, resolve that tag and
register the corresponding `@sha256:...` digest before enabling the `digest`
image policy.

## See also

- [Install](install.md) — initial setup
- [Upgrades](upgrades.md) — validator images update with `validibot-pro`
- [Security Hardening](security-hardening.md) — full hardening recommendations
- [Doctor Check IDs](doctor-check-ids.md) — VB320/VB321/VB322 container checks
- [Operator Recipes](operator-recipes.md)
- The dev-docs companion at `docs/dev_docs/overview/validator_architecture.md`
