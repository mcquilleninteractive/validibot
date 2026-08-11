# Advanced Validator Container Interface

Advanced validators run as isolated Docker containers, enabling complex domain-specific validation logic like simulations, external tools, or custom processing. This document describes the container interface that all advanced validators must implement.

For information about how Validibot orchestrates these containers across different deployment targets (Docker Compose, GCP Cloud Run, etc.), see [Execution Backends](execution_backends.md).

## Overview

An advanced validator is a Docker container that:

1. Reads an **input envelope** (JSON) describing what to validate
2. Performs validation (runs a simulation, calls external tools, etc.)
3. Writes an **output envelope** (JSON) with results

This simple contract makes it straightforward to package any validation logic as a container.

```
┌─────────────────────────────────────────────────────────────┐
│                   Validator Container                       │
│                                                             │
│   ┌──────────────┐    ┌──────────────┐    ┌──────────────┐  │
│   │ Read Input   │───▶│   Process    │───▶│ Write Output │  │
│   │   Envelope   │    │  Validation  │    │   Envelope   │  │
│   └──────────────┘    └──────────────┘    └──────────────┘  │
│                                                             │
└─────────────────────────────────────────────────────────────┘
        ▲                                          │
        │                                          ▼
   input.json                                 output.json
```

## Environment Variables

Validibot passes configuration to containers via environment variables:

| Variable | Description | Example |
|----------|-------------|---------|
| `VALIDIBOT_INPUT_URI` | URI to the input envelope | `file:///data/input.json` or `gs://bucket/run/input.json` |
| `VALIDIBOT_OUTPUT_URI` | URI where output envelope should be written | `file:///data/output.json` or `gs://bucket/run/output.json` |

The URI scheme indicates the storage backend:
- `file://` — Local filesystem (Docker Compose deployments)
- `gs://` — Google Cloud Storage (GCP deployments)

## Input Envelope

The input envelope is a JSON document that tells the validator what to do. It contains:

### Base Fields (all validators)

```python
from pydantic import BaseModel
from typing import Literal

class InputFileItem(BaseModel):
    """A file to be validated."""
    name: str                    # Filename (e.g., "model.idf")
    uri: str                     # Where to download the file
    mime_type: str               # MIME type for format detection
    role: str | None             # Optional descriptive backend metadata
    port_key: str                # Required declared Validibot file port

class ResourceFileItem(BaseModel):
    """An auxiliary resource file needed by the validator."""
    id: str                      # Resource UUID
    type: str                    # Resource type (e.g., "energyplus_weather")
    uri: str                     # Where to download the resource
    port_key: str                # Required declared Validibot file port

class ValidatorInfo(BaseModel):
    """Information about the validator being run."""
    id: str                      # Validator UUID
    type: str                    # Validator type (e.g., "ENERGYPLUS", "FMU")
    version: str                 # Validator version

class ExecutionContext(BaseModel):
    """Execution metadata for callbacks and storage."""
    callback_url: str | None     # Where to POST results (async mode)
    callback_id: str | None      # Unique ID for idempotent callbacks
    execution_bundle_uri: str    # Base URI for storing artifacts
    execution_attempt_id: str    # Durable retry/attempt UUID
    step_run_id: str             # Exact step execution being completed
    attempt_contract_version: str
    expected_output_uri: str     # Exact attempt-bound output identity
    timeout_seconds: int         # Maximum execution time

class ValidationInputEnvelope(BaseModel):
    """Base input envelope - extend for your validator."""
    run_id: str                  # Validation run UUID
    input_files: list[InputFileItem]
    resource_files: list[ResourceFileItem]
    validator: ValidatorInfo
    context: ExecutionContext
```

### Validator-Specific Inputs

Each validator type extends the base envelope with domain-specific configuration:

```python
# EnergyPlus validator
class EnergyPlusInputs(BaseModel):
    timestep_per_hour: int = 4
    output_variables: list[str] = []
    invocation_mode: Literal["cli", "api"] = "cli"

class EnergyPlusInputEnvelope(ValidationInputEnvelope):
    inputs: EnergyPlusInputs
```

```python
# FMU validator
class FMUInputs(BaseModel):
    start_time: float = 0.0
    stop_time: float = 1.0
    step_size: float | None = None

class FMUInputEnvelope(ValidationInputEnvelope):
    inputs: FMUInputs
```

### Example Input Envelope

```json
{
  "run_id": "550e8400-e29b-41d4-a716-446655440000",
  "validator": {
    "id": "123e4567-e89b-12d3-a456-426614174000",
    "type": "ENERGYPLUS",
    "version": "24.2.0"
  },
  "input_files": [
    {
      "name": "model.idf",
      "uri": "file:///data/files/model.idf",
      "mime_type": "application/vnd.energyplus.idf",
      "role": "primary-model",
      "port_key": "primary_model",
      "size_bytes": 5241,
      "sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
      "storage_version": "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    }
  ],
  "resource_files": [
    {
      "id": "weather-resource-uuid",
      "name": "weather.epw",
      "type": "energyplus_weather",
      "uri": "file:///data/files/weather.epw",
      "port_key": "weather_file",
      "size_bytes": 88902,
      "sha256": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
      "storage_version": "sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    }
  ],
  "inputs": {
    "timestep_per_hour": 4,
    "output_variables": ["Zone Mean Air Temperature"],
    "invocation_mode": "cli"
  },
  "context": {
    "callback_url": null,
    "callback_id": null,
    "execution_bundle_uri": "file:///data/runs/550e8400/attempts/64a12370/output/",
    "execution_attempt_id": "64a12370-a404-4d37-bf87-cb1692b0af8d",
    "step_run_id": "c1153cd8-80db-4dc4-8464-93b50a4f6e10",
    "attempt_contract_version": "validibot.attempt.v1",
    "expected_output_uri": "file:///data/runs/550e8400/attempts/64a12370/output/output.json",
    "timeout_seconds": 3600
  }
}
```

## File Ports And Cardinality

The envelope uses `input_files` and `resource_files` as wire-level lists, but
new validators should be designed around **declared file ports**.

A file port is the validator contract for one file-like input or output. It
defines:

- a stable port key, such as `primary_model`, `weather_file`, `data_graph`,
  `xml_document`, or `schema_file`;
- the backend-facing role/type rendered into the envelope, such as
  `primary-model`, `weather`, `fmu`, or `data-graph`;
- cardinality, such as exactly one (`1..1`), optional singleton (`0..1`), or a
  future bounded collection;
- accepted data formats and media types;
- allowed sources, such as submitted file, workflow resource, or upstream
  artifact;
- default binding behavior for the common case.

For example, EnergyPlus should be modeled as two ports:

```text
primary_model
  channel: input_files
  role: primary-model
  cardinality: 1..1
  formats: IDF, epJSON
  default: submitted file first

weather_file
  channel: resource_files
  role/type: energyplus_weather
  cardinality: 1..1 for simulation
  formats: EPW
  default: submitted EPW if present, otherwise default weather resource
```

The backend can continue to receive:

```json
{
  "input_files": [{
    "port_key": "primary_model",
    "role": "primary-model",
    "uri": "...",
    "size_bytes": 5241,
    "sha256": "...",
    "storage_version": "..."
  }],
  "resource_files": [{
    "port_key": "weather_file",
    "name": "weather.epw",
    "type": "energyplus_weather",
    "uri": "...",
    "size_bytes": 88902,
    "sha256": "...",
    "storage_version": "..."
  }]
}
```

The file-port contract lives above that envelope rendering. It lets Django
validate cardinality, choose simple defaults, present a clean UI, and bind a
future upstream artifact without mutating the original payload.

When a non-primary file port is bound to a submitted file, Django stores it as a
`SubmissionInputFile` row keyed by workflow step and port. Dispatch then copies
that file into the attempt bundle and passes its complete identity—URI, exact
size, SHA-256, and provider storage version—to the envelope builder under the
port key. The primary submitted payload remains stored on `Submission`.
Attempt materialization may use `primary_file_uri` as an internal adapter key,
but the envelope item always carries the declared port key and the backend
never sees or interprets that adapter convention.

Artifact-port resolution writes `ResolvedInputTrace` rows just like scalar input
resolution. The snapshot records the selected submitted file, workflow
resource, or upstream artifact reference, and failed pre-dispatch validation
writes a failed trace before raising.

`resolve_file_inputs()` is the one application service that interprets these
bindings. Inline validators receive bounded verified bytes. Isolated adapters
pass the immutable attempt-visible file identities and receive the same
descriptor without loading container inputs into Django memory. The envelope
builder formats that descriptor but does not choose a source independently.

On the backend side, use
`validibot_shared.validations.file_ports.select_input_file()` or
`select_resource_file()`. The helper matches the required exact `port_key`,
rejects missing or ambiguous matches, and never depends on list order. A role
or resource type is descriptive metadata only. Do not implement a new
`input_files[0]` or role/type fallback matcher.

The full step form and generic Inputs modal both obtain file-source controls
from `BaseStepConfigForm`. Validator-specific forms may arrange those fields or
restrict a genuinely domain-specific choice, but they should not rebuild
source-choice, upstream-output, revision, or binding-save behavior.

## Output Envelope

The output envelope reports validation results back to Validibot.

### Base Fields (all validators)

```python
from enum import Enum

class ValidationStatus(str, Enum):
    SUCCESS = "success"       # Validation completed, model is valid
    FAILURE = "failure"       # Validation completed, model has issues
    ERROR = "error"           # Validation could not complete (crash, timeout)

class Severity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"

class ValidationMessage(BaseModel):
    """A message from the validation process."""
    severity: Severity
    text: str
    code: str | None = None     # Machine-readable error code
    location: str | None = None # File/line reference

class ValidationMetric(BaseModel):
    """A numeric metric extracted during validation."""
    name: str                   # Metric identifier
    value: float | int | str    # Metric value
    unit: str | None = None     # Unit of measurement

class ValidationOutputEnvelope(BaseModel):
    """Base output envelope - extend for your validator."""
    run_id: str
    validator: ValidatorInfo
    status: ValidationStatus
    timing: dict                # started_at, finished_at timestamps
    messages: list[ValidationMessage] = []
    metrics: list[ValidationMetric] = []
```

### Validator-Specific Outputs

Each validator type can include domain-specific output data:

```python
# EnergyPlus validator
class EnergyPlusOutputs(BaseModel):
    energyplus_returncode: int
    execution_seconds: float
    invocation_mode: str
    # References to output files (SQL, CSV, etc.)

class EnergyPlusOutputEnvelope(ValidationOutputEnvelope):
    outputs: EnergyPlusOutputs
```

### Example Output Envelope

```json
{
  "run_id": "550e8400-e29b-41d4-a716-446655440000",
  "validator": {
    "id": "123e4567-e89b-12d3-a456-426614174000",
    "type": "ENERGYPLUS",
    "version": "24.2.0"
  },
  "status": "success",
  "timing": {
    "started_at": "2024-01-15T10:30:00Z",
    "finished_at": "2024-01-15T10:35:42Z"
  },
  "messages": [
    {
      "severity": "info",
      "text": "Simulation completed successfully"
    },
    {
      "severity": "warning",
      "text": "Zone 'Kitchen' has no windows",
      "code": "EP_NO_WINDOWS",
      "location": "model.idf:1523"
    }
  ],
  "metrics": [
    {
      "name": "total_site_energy_kwh",
      "value": 45230.5,
      "unit": "kWh"
    },
    {
      "name": "simulation_time_seconds",
      "value": 342,
      "unit": "s"
    }
  ],
  "outputs": {
    "energyplus_returncode": 0,
    "execution_seconds": 342.5,
    "invocation_mode": "cli"
  }
}
```

## Container Labels

Validibot identifies and manages validator containers using Docker labels:

| Label | Description | Example |
|-------|-------------|---------|
| `org.validibot.managed` | Marks container as Validibot-managed | `true` |
| `org.validibot.run_id` | Validation run UUID | `550e8400-e29b-...` |
| `org.validibot.validator` | Validator slug | `energyplus` |
| `org.validibot.started_at` | ISO timestamp when started | `2024-01-15T10:30:00Z` |
| `org.validibot.timeout_seconds` | Configured timeout | `3600` |

These labels enable:

1. **On-demand cleanup** — Container removed after run completes
2. **Periodic sweep** — Background task removes orphaned containers
3. **Startup cleanup** — Worker removes leftover containers from crashed processes

## Building Your Own Validator

### 1. Define Envelope Schemas

Create Pydantic models in `validibot-shared` for your validator's inputs and outputs:

```python
# validibot_shared/myvalidator/envelopes.py
from validibot_shared.validations.envelopes import (
    ValidationInputEnvelope,
    ValidationOutputEnvelope,
)

class MyValidatorInputs(BaseModel):
    setting_a: str
    setting_b: int = 10

class MyValidatorInputEnvelope(ValidationInputEnvelope):
    inputs: MyValidatorInputs

class MyValidatorOutputs(BaseModel):
    result_code: int
    summary: str

class MyValidatorOutputEnvelope(ValidationOutputEnvelope):
    outputs: MyValidatorOutputs
```

### 2. Create the Container

Structure your validator container:

```
myvalidator/
├── Dockerfile
├── main.py           # Entry point
├── runner.py         # Validation logic
└── requirements.txt
```

**Dockerfile:**

```dockerfile
FROM python:3.13-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
CMD ["python", "main.py"]
```

**main.py:**

```python
import os
import json
from myvalidator.runner import run_validation
from validibot_shared.myvalidator.envelopes import (
    MyValidatorInputEnvelope,
    MyValidatorOutputEnvelope,
)

def main():
    # Read input envelope
    input_uri = os.environ["VALIDIBOT_INPUT_URI"]
    output_uri = os.environ["VALIDIBOT_OUTPUT_URI"]

    with open(input_uri.replace("file://", "")) as f:
        input_envelope = MyValidatorInputEnvelope.model_validate_json(f.read())

    # Run validation
    output_envelope = run_validation(input_envelope)

    # Write output envelope
    with open(output_uri.replace("file://", ""), "w") as f:
        f.write(output_envelope.model_dump_json(indent=2))

if __name__ == "__main__":
    main()
```

**runner.py:**

```python
from datetime import datetime, timezone
from validibot_shared.validations.envelopes import (
    ValidationStatus,
    ValidationMessage,
    Severity,
)

def run_validation(input_envelope):
    started_at = datetime.now(timezone.utc)

    # Download input files
    for file_item in input_envelope.input_files:
        download_file(file_item.uri, f"/tmp/{file_item.name}")

    # Your validation logic here
    # ...

    finished_at = datetime.now(timezone.utc)

    return MyValidatorOutputEnvelope(
        run_id=input_envelope.run_id,
        validator=input_envelope.validator,
        status=ValidationStatus.SUCCESS,
        timing={
            "started_at": started_at.isoformat(),
            "finished_at": finished_at.isoformat(),
        },
        messages=[
            ValidationMessage(
                severity=Severity.INFO,
                text="Validation completed successfully",
            )
        ],
        metrics=[],
        outputs=MyValidatorOutputs(
            result_code=0,
            summary="All checks passed",
        ),
    )
```

### 3. Register in Validibot

Create a `ValidatorConfig` to register your validator with the system. This is the
single source of truth for all validator metadata, class binding, and UI extensions.

**For package-based validators**, create `config.py` in your validator package:

```python
# validibot/validations/validators/myvalidator/config.py
from validibot.validations.validators.base.config import (
    CatalogEntrySpec,
    ValidatorConfig,
)

config = ValidatorConfig(
    slug="myvalidator",
    name="My Validator",
    description="Validates things using my custom logic.",
    validation_type="MY_VALIDATOR",
    validator_class="validibot.validations.validators.myvalidator.validator.MyValidator",
    has_processor=True,
    supported_file_types=["application/json"],
    catalog_entries=[
        CatalogEntrySpec(
            slug="result-metric",
            label="Result Metric",
            entry_type="io_definition",
            run_stage="output",
            data_type="number",
        ),
    ],
)
```

For community validators, `ValidationType` constants are still useful when
application code needs to branch on a built-in type. For plugin validators,
the `validation_type` string belongs to the plugin and does not need an enum
entry. Register the config at startup, then run `python manage.py sync_validators`
to sync to the database. The validator class is automatically resolved at startup by
`register_validators()` or by the external package's `AppConfig.ready()` hook.

## Django-Side Orchestration (`AdvancedValidator`)

Before a container is started, the Django-side `AdvancedValidator` base class orchestrates the full validation lifecycle. This is the Template Method pattern — the base class handles shared orchestration while subclasses override hooks for domain-specific behavior.

### Lifecycle

```
AdvancedValidator.validate()
  1. Validate run_context (must have validation_run and step)
  2. Get the configured ExecutionBackend (Docker or Cloud Run)
  3. ★ preprocess_submission() — domain-specific input transformation
  4. Build ExecutionRequest and call backend.execute()
  5. Convert ExecutionResponse to ValidationResult
```

### Preprocessing Hook

`preprocess_submission()` is called **before** backend dispatch. This is where domain-specific input transformations happen — for example, EnergyPlus resolves parameterized IDF templates into concrete model files:

```python
# In EnergyPlusValidator
def preprocess_submission(self, *, step, submission):
    from .preprocessing import preprocess_energyplus_submission
    result = preprocess_energyplus_submission(step=step, submission=submission)
    return result.template_metadata
```

Key design principle: **preprocessing happens in Django, not in containers.** After preprocessing, the submission looks identical to a direct upload — execution backends never need to know that preprocessing occurred. This ensures all platforms (Docker Compose, Cloud Run, future backends) see identical input.

The default implementation is a no-op (returns empty dict). Subclasses override when needed.

### Django-side Hook Contract

The Django-side validator class is the plugin mount point. The base classes own
the lifecycle; subclasses only fill in the domain-specific hooks.

Do not override `validate()` for ordinary advanced validators. The base
implementation validates run context, runs preprocessing, evaluates input-stage
assertions, dispatches the execution backend, handles sync and async responses,
and assembles the `ValidationResult`.

Do not override `post_execute_validate()` unless the validator has a genuinely
different output-processing lifecycle. The base implementation extracts issues
from the output envelope, calls `extract_output_values()`, evaluates output
assertions, and returns the final result.

| Hook | Required | Purpose |
|--------|----------|---------|
| `validator_display_name` | Yes | Human-readable name for error messages. |
| `preprocess_submission()` | No | Transform the submission before backend dispatch. |
| `extract_input_values()` | No | Extract input-stage facts for `i.*` assertions before dispatch. |
| `extract_output_values()` | Yes | Extract output-stage facts for `o.*` assertions after the backend returns. |
| `get_cel_helpers()` | No | Customize the CEL helper allowlist for this validator. Treat this as security-sensitive. |

`extract_input_values()` runs after preprocessing, so template-mode
submissions are parsed as the resolved payload that the backend will actually
receive. `extract_output_values()` runs after the output envelope is parsed
with the `output_envelope_class` declared in `ValidatorConfig`.

Simple in-process validators use the same pattern with a different set of
hooks: `validate_file_type()`, `parse_content()`, `run_domain_checks()`, and
optional `extract_output_values()`. Those are documented in
`validibot/validations/validators/base/simple.py`.

## Container Lifecycle

A validator container goes through these stages:

### Sync Path (Docker Compose)

```
1. SPAWN    → DockerValidatorRunner.run() creates container with labels
              Security: cap_drop=ALL, no-new-privileges, read_only, network_mode=none
2. EXECUTE  → Container reads input from VALIDIBOT_INPUT_URI, runs validation
3. WAIT     → Worker blocks on container.wait(timeout=N)
4. COLLECT  → Worker reads output.json from shared storage volume
5. CLEANUP  → container.remove(force=True) in finally block
```

**If the worker crashes mid-execution:** The container becomes an orphan. It is cleaned up by:
- Periodic sweep (Celery Beat every 10 minutes) — checks container labels for timeout
- Startup cleanup (AppConfig.ready()) — removes all labeled containers on worker restart

### Async Path (GCP Cloud Run)

```
1. RESOLVE  → Django pins the exact ready Service or retained Job deployment
2. UPLOAD   → Django stages the input envelope/files under one GCS attempt prefix
3. DISPATCH → Service: deterministic provider Cloud Task; Job: Cloud Run Jobs API
4. EXECUTE  → Runtime receives only the attempt capability and runs one-shot work
5. CALLBACK → Runtime POSTs exact output generation to the Django worker
6. PROCESS  → ValidationCallbackService verifies and processes immutable output
```

**If the callback is lost:** `cleanup_stuck_runs` uses the capability declared by
the pinned deployment. Jobs have queryable provider status. Services do not
have a durable per-request status resource, so reconciliation retries bounded
immutable-output salvage and never treats a transport 2xx as a validation
result. A verified output is processed through the same idempotent
`ValidationCallbackService` path; otherwise the absolute attempt deadline wins.

### Container Security Hardening

All Docker containers are launched with these security settings:

| Setting | Value | Purpose |
|---------|-------|---------|
| `cap_drop` | `["ALL"]` | Drop all Linux capabilities |
| `security_opt` | `["no-new-privileges:true"]` | Prevent privilege escalation |
| `pids_limit` | `512` | Prevent fork bombs |
| `read_only` | `True` | Read-only root filesystem |
| `tmpfs` | `{"/tmp": "size=2g,mode=1777"}` | Writable /tmp only |
| `user` | `"1000:1000"` | Run as non-root |
| `network_mode` | `"none"` | No network access (sync mode) |
| `mem_limit` | `"4g"` (configurable) | Memory ceiling |
| `nano_cpus` | `2_000_000_000` (configurable) | CPU ceiling |

## Reference Implementations

See the [validibot-validator-backends](https://github.com/mcquilleninteractive/validibot-validator-backends) repository for complete examples:

- **EnergyPlus validator** — Building energy simulation
- **FMU validator** — Functional Mock-up Unit simulation

## Type Safety

The envelope schemas are defined in `validibot-shared` and used by both Validibot (Django) and the validator containers. This ensures:

- **Compile-time validation** — Type errors caught before runtime
- **IDE support** — Autocomplete and type hints work correctly
- **Schema evolution** — Changes to envelopes are versioned and tested

```python
# Django side - creating jobs
envelope = EnergyPlusInputEnvelope(
    inputs=EnergyPlusInputs(timestep_per_hour=6)  # Fully typed!
)

# Validator side - processing jobs
envelope = load_input_envelope(EnergyPlusInputEnvelope)
timestep = envelope.inputs.timestep_per_hour  # IDE knows this is int
```

## The validator vs validator backend distinction

This document describes the **validator backend** interface — the external Docker image that does the heavyweight work. The Django-side code that orchestrates around it is an **advanced validator**. The two are different things, with different trust roles:

| Term | Meaning |
|---|---|
| **Validator** | The Django-side `BaseValidator` subclass. Sees full Validibot run context. |
| **Simple validator** | A `SimpleValidator` subclass that runs synchronously inside Django. No container. |
| **Advanced validator** | An `AdvancedValidator` subclass that orchestrates external compute via an execution backend. |
| **Validator backend** | The external implementation an advanced validator delegates to. **This page.** |
| **Validator backend runtime** | The concrete container/job launched for one run. |

**The advanced validator is the policy boundary; the validator backend is the compute boundary.** The advanced validator owns trust decisions (access, contract, retention, evidence). The backend just runs the simulation.

The repository was renamed from `validibot-validators` → `validibot-validator-backends` in March 2026 to make this role unambiguous. The Python package and Docker image prefixes match. See [Terminology](terminology.md) for the full vocabulary.

## Immutable execution boundary

Treat each advanced-validator execution as a sealed, attempt-specific package,
not as a collection of convenient storage paths. A URI tells a backend where
to look; it does not prove which bytes will be there when the backend reads it.
The useful trust statement is stronger:

> This attempt verified and executed these exact input bytes, using this
> validator contract and backend image, and produced this verified result.

The conceptual flow is:

```text
trusted app chooses contract and expected identities
    → materialise attempt-specific inputs
    → stream and verify input size, digest, and storage version
    → execute only the verified local files
    → write one attempt-bound candidate output
    → trusted app verifies output identity and schema
    → process the domain verdict and construct evidence
```

Four values solve different parts of the identity problem:

| Value | Why it is needed |
|---|---|
| Exact byte size | Bounds the transfer and detects short or unexpectedly long content |
| SHA-256 | Identifies the exact content independently of its location |
| Storage version | Pins a GCS generation, S3 version, or equivalent immutable object identity where available |
| Execution attempt ID | Prevents a retry from accepting another attempt's input or stale output |

These values are complementary. Hash verification detects changed bytes, but
does not prevent a broadly privileged runtime from reading another run. A
scoped mount or credential limits access, but does not prove that a mutable
object still has its expected content. Validibot needs both integrity and
isolation, and deployment diagnostics should report them separately.

Output follows the same rule. Docker completion, an authenticated callback,
or successful Cloud Run reconciliation only delivers a candidate result. The
trusted output verifier selects the expected Pydantic class from Django's
attempt state, bounds the raw result before parsing, and checks the run, step,
attempt, validator, contract, canonical input-envelope digest, and exact output
URI before the result reaches validator-specific processing. Untrusted output
never gets to choose its own parser.

Transport success and validation outcome are also separate. A correctly
formed, identity-matching output can report that the submitted data failed its
rules; that is an ordinary validation verdict. Missing, malformed, oversized,
or identity-mismatched output is an execution-system error.

The coordinated `validibot-shared` 0.16, backend 0.12, and Django application
slices make required file identities, streaming verification, and create-only
attempt publication current behavior. Django computes or resolves each input's
exact size, SHA-256, and storage version before dispatch; every backend fetches
the pinned version and verifies the streamed bytes before domain execution.
Local output artifacts are checked against the bytes in the attempt workspace
before their strict `ArtifactRef` is persisted.

`ArtifactRef` construction is intentionally limited to artifacts created and
schema-validated during the current run. The application does not rebuild
references by sweeping historical `Artifact` rows. Before adding an evidence
rebuild, admin export, or other historical traversal, audit legacy rows for
non-empty `sha256` and `storage_version` values (or backfill them) so the strict
reference schema cannot encounter incomplete pre-contract data.

Evidence-manifest rewiring and narrower storage capabilities remain subsequent
slices. Identity mismatches and attempts to reuse a committed storage identity
now both fail closed.

## Attempt-scoped isolation

Before April 2026, the local Docker runner mounted the entire `DATA_STORAGE_ROOT` read-write into every validator backend runtime. A buggy or partner-authored backend could read other runs' inputs, mutate other runs' outputs, exhaust shared disk, or leak data between runs.

Validibot first replaced that with a per-run workspace, then tightened retries
to a distinct workspace for every execution attempt.

### Per-attempt workspace layout

```text
<DATA_STORAGE_ROOT>/runs/<org_id>/<run_id>/attempts/<attempt_id>/
  input/                       # mode 755 — readable by container UID 1000
    input.json                 # mode 644
    <original_filename>        # mode 644 — primary submission file
    resources/                 # mode 755
      <resource_filename>      # workflow resource files (e.g. weather)
  output/                      # owned 1000:1000, mode 770 — writable by UID 1000 only
    output.json                # written by container
    outputs/                   # backend-uploaded artifacts
```

### Container mounts

| Host path | Container path | Mode |
|---|---|---|
| `runs/<org>/<run>/attempts/<attempt>/input` | `/validibot/attempts/<attempt>/input` | read-only |
| `runs/<org>/<run>/attempts/<attempt>/output` | `/validibot/attempts/<attempt>/output` | read-write |
| (none) | `/tmp` | tmpfs (`size=2g,mode=1777`) |

The container does **not** receive the global storage root, other run directories, Django media paths, database credentials, signing keys, Stripe/x402 credentials, or arbitrary host directories.

### Envelope identity rewriting

The Docker dispatch path materializes each input and rewrites its complete file
identity so the container sees only container-visible paths:

- `input_files[].uri` → the attempt's container input directory
- `resource_files[].uri` → the attempt's container resource directory
- `context.execution_bundle_uri` → the attempt's container output directory

The backends' existing artifact-upload logic composes
`f"{execution_bundle_uri}/outputs"`, so artifacts automatically land below the
correct attempt's output directory. Backend 0.12 additionally opens local files
under the declared input root or downloads the exact GCS generation, verifying
size and SHA-256 while streaming before the domain runner is called, and
publishes outputs without replacing an existing attempt object.

Cloud Run keeps `gs://...` URIs, with the same
`runs/<org>/<run>/attempts/<attempt>/` prefix shape.

### Create-only attempt publication

An attempt UUID names one publication, not a mutable workspace. Local dispatch
reserves the attempt directory exclusively and publishes input files through a
temporary sibling plus an atomic no-replace link. Cloud dispatch uploads input
files and `input.json` with GCS `if_generation_match=0`. Backend 0.12 applies
the corresponding rule to local and GCS outputs.

This rule deliberately rejects an identical replay as well as conflicting
bytes. Duplicate delivery therefore cannot silently replace attempt state; an
explicit retry must allocate a new execution-attempt UUID and receives a new
local directory or GCS prefix.

### Workspace materialisation runs after preprocessing

Some advanced validators (notably EnergyPlus) preprocess the submission
in-memory. For example, EnergyPlus template resolution rewrites
`submission.content` and `submission.original_filename` before dispatch. The
attempt-workspace builder must therefore run **after** the validator's
`validate()` preprocessing has completed, inside `ExecutionBackend.execute()`.

### The implicit sentinel: how completion is detected

Validibot uses the **presence and parseability of `output.json`** as the implicit sentinel that says "the container ran and reported a result." Pattern adopted from Flyte (`_ERROR`), Cromwell (`rc` file), and Argo (exit code + artifact existence).

| State after container exit | Orchestrator interpretation |
|---|---|
| `output.json` present, parses, validator reports success | Run succeeded with the validator's verdict |
| `output.json` present, parses, validator reports validation failure | Run completed; user data did not pass |
| `output.json` present but unparseable | Backend bug; orchestrator records as `RuntimeError` |
| `output.json` absent + container exit code 0 | Backend bug; orchestrator records as `RuntimeError` |
| `output.json` absent + container exit code ≠ 0 | Container died unexpectedly (OOM, segfault, image pull, callback timeout); orchestrator records as `SystemError` |

This makes the boundary between **"validator reported the user's data is bad"** (a `FAIL` result the user can act on) and **"the platform itself had a problem"** (an `ERROR` the platform owner needs to triage) determinable from on-disk state alone, without reading container logs.

We do **not** add a separate `_status.json` file. The attempt-bound output
envelope is already the artifact every backend produces, and the trusted
application verifier decides whether it is an acceptable completion sentinel.

### Hardening posture vs comparable systems

Validibot's design closely mirrors Pachyderm's `/pfs/<input>` + `/pfs/out`, Flyte's `/var/inputs` + `/var/outputs`, and Cromwell's `/cromwell-executions/...` patterns. Validibot is **stricter than the median** on read-only inputs, default-deny network, non-root UID, and dropped capabilities. Most workflow engines (Argo, Flyte, Nextflow, Pachyderm, Airflow, Cromwell) leave these open by default.

## Validator backend metadata as security metadata

Validator backend metadata should describe runtime capabilities, not just UI metadata:

```json
{
  "slug": "energyplus",
  "version": "24.2.0",
  "supported_file_types": ["text", "json"],
  "requires_network": false,
  "requires_writeable_paths": ["/validibot/output", "/tmp"],
  "default_timeout_seconds": 900,
  "max_input_bytes": 104857600,
  "produces_artifacts": true,
  "evidence_level": "hashes-and-logs"
}
```

The runner enforces the deployment policy for capabilities it controls: network, writable paths, mounts, user, privileges, timeout, memory, CPU, root filesystem mode. A self-hosted deployment can choose "no validator network access" globally and refuse validators that request network. The runtime policy must not depend on trusting the image's self-description.

## Trust tiers (Phase 5, future)

A future `trust_tier` field on `Validator` will select a hardening profile:

- **Tier 1 — first-party** (EnergyPlus, FMU, future Validibot-built backends): current Phase 1 hardening.
- **Tier 2 — user-added or partner-authored**: tier 1 + explicit egress allowlist (or `network=none`), tighter resource caps, gVisor or Kata runtime when available, cosign-signed image required, pre-flight scan on registration.

Tier 1 defaults are sufficient for first-party containers Validibot builds itself. Tier 2 becomes load-bearing when self-service validator registration ships.

## See also

- [Terminology](terminology.md) — validator vs validator backend vs execution backend
- [Trust Architecture](trust-architecture.md) — the four invariants
- [Execution Backends](execution_backends.md) — Docker vs Cloud Run dispatch
- [Evidence Bundles](evidence-bundles.md) — what gets recorded about each validator backend run
