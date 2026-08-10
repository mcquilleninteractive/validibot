# Submission Data in Advanced Validators

When an advanced validator (EnergyPlus, FMU) runs, the submission content needs to travel from the database into a container that can't access Django. This document explains how that works, how the data gets cleaned up afterwards, and the tradeoffs behind the current approach.

For container interface details, see [Advanced Validator Interface](validator_architecture.md). For backend infrastructure, see [Execution Backends](execution_backends.md). For retention policies, see [Submissions (Data Model)](../data-model/submissions.md).

## The Execution Bundle

Every advanced-validator execution attempt creates an **execution bundle** — a
directory in storage containing everything the container needs:

```
runs/{org_id}/{run_id}/attempts/{attempt_id}/
├── input.json           # Input envelope (validator config, file URIs, callback info)
├── <submission file>    # Copy of the submission content (see below)
├── submitted/           # Extra submitted artifact-port files, keyed by port
└── output.json          # Written by the container after execution
```

The submission file name and format depend on the validator type. For example, an EnergyPlus run might contain `model.epjson` or `model.idf`, while an FMU run references the FMU model via URI in the envelope rather than copying it into the bundle. Each validator's launcher determines what gets written here.

Some validators also have declared file ports beyond the primary submission.
For EnergyPlus, the primary model stays in `Submission.content` /
`Submission.input_file`, while a launch-time EPW weather upload is stored as a
`SubmissionInputFile` keyed to the `weather_file` port. Dispatch copies that
extra file into the execution bundle and passes the bundle URI to the envelope
builder by port key.

A port may instead select an artifact produced by an earlier workflow step.
The application resolves that relational binding to the exact artifact from the
same run and verifies its format, media type, filename, size, hash, and storage
identity before execution. It does not replace or mutate the original
`Submission`, and a missing earlier output never falls back to submission data.

Both inline and container validators use the same resolved-file descriptor.
Inline execution asks the descriptor service for bounded verified bytes.
Container dispatch supplies the immutable file identities already staged for
the attempt (or the authoritative earlier artifact/resource identity) and
renders them into `input_files` or `resource_files`.

The bundle path includes both the org and a server-generated attempt UUID.
Docker Compose and GCP use the same prefix shape; local Docker then separates
the bundle into read-only ``input/`` and writable ``output/`` mounts. A retry
gets a new attempt UUID and cannot reuse the earlier bundle.

## Why We Copy the Submission

The execution backend **copies** the submission content into the bundle rather than referencing the original location. There are three reasons for this:

**1. The container can't read from the database.** Submissions are often stored inline in the `Submission.content` database field, not as files on disk. The container has no database access, so the content must be written to storage where the container can read it.

**2. Isolation from concurrent changes.** The copy gives the container stable
attempt-scoped bytes. Retention services also refuse to purge a submission
while any related run is active. Launch and purge take the same submission row
lock, so a new run cannot use content that a concurrent purge already removed.

**3. Debuggability.** When investigating a failed run, all inputs and outputs live in one directory. You can inspect exactly what the container received without cross-referencing other storage locations.

The downside is storage duplication, especially on GCP where both the original and copy may live in the same GCS bucket. We've considered referencing the original URI instead (see below), but the complexity isn't worth the marginal cost saving.

## Data Flow

### Docker Compose (Synchronous)

```
1. Celery worker receives validation task
2. DockerComposeExecutionBackend.execute():
   a. Reads submission content from DB
   b. Writes copy below runs/{org}/{run}/attempts/{attempt}/input/
   c. Writes input.json to the attempt input directory
   d. Spawns Docker container (blocking)
   e. Container reads input, writes output.json
   f. Worker reads output.json, processes results
3. Run reaches a terminal state → common retention scheduler runs
```

### GCP Cloud Run (Asynchronous)

```
1. Celery worker receives validation task
2. Selected GCP execution backend dispatches the pinned deployment:
   a. Reads submission content from DB
   b. Uploads copy to gs://bucket/runs/{org}/{run}/attempts/{attempt}/model.epjson
   c. Uploads input.json to that attempt prefix
   d. Resolves an immutable deployment and dispatches a private Service task or
      retained Cloud Run Job (non-blocking)
   e. Returns immediately with pending status
3. Container runs in Cloud Run, writes output to GCS
4. Container POSTs callback to worker service
5. ValidationCallbackService processes results
6. Run finalized → common retention scheduler runs
```

## Cleanup and Retention

Input and output policies are independent and both default to
`DO_NOT_STORE`. A workflow author must opt in to either post-processing
retention stream.

### Input Purging

When `Submission.purge_content()` runs (triggered by retention expiration or `DO_NOT_STORE` policy), it:

1. refuses to run while any related validation is active;
2. selectively deletes copied input envelopes/files from every attempt bundle;
3. deletes original and auxiliary submission bytes;
4. clears submitter names, filenames, arbitrary metadata, step input values,
   and input-envelope identities; and
5. writes `content_purged_at` only after external deletion succeeds.

Output envelopes and output directories are explicitly preserved during this
operation when their independent retention window remains open.

### Output Purging

`purge_expired_outputs` runs every five minutes. Once a terminal run's output
window expires, it deletes the complete run bundle, findings, artifacts,
evidence bytes, step values, detailed errors, output/callback URIs, and output
hashes. Minimal run status/timing and aggregate summary counts remain.

Finite input and output deadlines begin at terminal completion. A submission
shared by several runs starts its finite window after the last run finishes.
Receipt-time input deadlines are provisional so abandoned submissions still
expire. Repair scans reconstruct deadlines/work missed by a terminal hook.

### DO_NOT_STORE Flow

For submissions with `DO_NOT_STORE` retention:

1. Run completes (either sync or via async callback)
2. `queue_submission_purge()` creates a `PurgeRetry` record
3. `process_purge_retries` scheduled job picks it up (runs every 5 minutes)
4. Calls `submission.purge_content()`, which deletes original and copied inputs
   but not independently retained outputs
5. On failure: capped exponential backoff retries (1m, 5m, 1h, 6h, then 24h)
   continue indefinitely; five attempts is an alert threshold

!!! note "Window of exposure"
    `DO_NOT_STORE` means no post-processing retention, not memory-only
    execution. Between completion and the frequent purge worker (normally
    under five minutes), transient input/output bytes can still exist in
    access-controlled application storage so synchronous and asynchronous
    results can be delivered. They are not kept as an author-selected history.

## Alternative Considered: Reference Original URI

[Issue #73](https://github.com/mcquilleninteractive/validibot/issues/73) proposed referencing the original submission URI in the input envelope instead of copying. We decided against this because:

- Docker Compose requires the copy regardless (DB content isn't a file)
- Introduces ordering dependencies between purge and container execution
- Marginal storage savings on GCP (same bucket) don't justify the complexity
- Copy approach is simpler to reason about and debug

## Key Code Locations

| Component | File |
|-----------|------|
| Docker Compose backend | `validations/services/execution/docker_compose.py` |
| GCP backend | `validations/services/execution/gcp.py` |
| Cloud Run launcher | `validations/services/cloud_run/launcher.py` |
| Submission purge | `submissions/models.py` → `purge_content()` |
| Selective input deletion | `submissions/models.py` → `_delete_run_input_files()` |
| Complete output deletion | `validations/services/retention.py` → `purge_run_outputs()` |
| Terminal scheduling | `validations/services/retention.py` |
| Purge retry queue | `submissions/models.py` → `queue_submission_purge()` |
| Output expiration | `validations/management/commands/purge_expired_outputs.py` |
| Callback handler | `validations/services/validation_callback.py` |
