# Validation Step Processor Architecture

This document provides a comprehensive guide to how Validibot executes validation steps, explaining the processor pattern, the different validator types, and how the system handles both synchronous and asynchronous execution.

## Overview

The **Validation Step Processor** is the core abstraction that orchestrates the execution of individual validation steps within a workflow. It sits between the step orchestrator (which iterates through workflow steps) and the low-level validation logic (validators), providing a clean separation of concerns.

```
┌─────────────────────────────────────────────────────────────────────┐
│               ValidationRunService (Facade)                         │
│            (Launch, Cancel, Delegation)                              │
└──────────────────────────┬──────────────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────────────────┐
│                    StepOrchestrator                                  │
│                 (Step Iteration & Dispatch)                          │
│                                                                      │
│   Responsibilities:                                                  │
│   - Loop through workflow steps                                      │
│   - Create ValidationStepRun records                                 │
│   - Route to processors (validators) or handlers (actions)           │
│   - Handle workflow-level status transitions                         │
│   - Delegate to SummaryBuilder and FindingsPersistence               │
└─────────────────────────────────────────────────────────────────────┘
                                │
                                ▼
┌─────────────────────────────────────────────────────────────────────┐
│                  ValidationStepProcessor                             │
│                   (Step Lifecycle)                                   │
│                                                                      │
│   Responsibilities:                                                  │
│   - Call engine methods at the right time                            │
│   - Persist findings to database                                     │
│   - Store step output values for downstream steps                    │
│   - Handle errors gracefully                                         │
│   - Finalize step with timing and status                             │
└─────────────────────────────────────────────────────────────────────┘
                                │
                                ▼
┌─────────────────────────────────────────────────────────────────────┐
│                       Validators                                    │
│                  (Validation Logic)                                  │
│                                                                      │
│   Responsibilities:                                                  │
│   - Execute validation logic (schema checking, AI prompts, etc.)     │
│   - Evaluate CEL assertions                                          │
│   - Extract declared values/metrics from outputs                     │
│   - Return structured ValidationResult                               │
└─────────────────────────────────────────────────────────────────────┘

See [Service Layer Architecture](service_architecture.md) for the full
decomposition of the service layer.
```

## Two Types of Validators

Validibot distinguishes between two categories of validators based on how they execute:

### Simple Validators (Inline)

**Built-in validators**: Basic, JSON Schema, XML Schema, AI

These validators:
- Run directly in the Django process
- Complete synchronously (blocking)
- Have a single assertion stage (input-only) — they check the submitted data
  but don't transform it into something new
- Are fast and lightweight

```python
# Simple validator flow
result = engine.validate(submission, ruleset, run_context)
# → Validation logic runs
# → Input-stage assertions evaluated
# → Returns complete result immediately
```

### Advanced Validators (Dedicated Compute)

**Container-based validators**: EnergyPlus, FMU, user-added custom validators
**Compute-intensive validators**: AI (via external API calls)

These validators:
- Run outside the Django worker process (in Docker containers or via external APIs)
- May complete synchronously or asynchronously (depending on deployment)
- Have two assertion stages (input AND output)
- Can be computationally intensive

Advanced validators are **container-based** — they run as isolated Docker
containers (locally or on Cloud Run). An FMU validator takes input parameters
(e.g. outdoor temperature, equipment load) and runs a simulation to produce
step output values (e.g. room temperature, cooling power). EnergyPlus takes a
building model and produces energy metrics.

Advanced validators always have `has_processor=True` on the `Validator` model,
which means they have both input and output assertion stages. But
`has_processor` is a broader concept than "advanced" — future validators that
are not container-based could still set `has_processor=True` if they transform
input data to produce output data. Any validator with `has_processor=True` gets
both assertion stages, and its output payload keys are automatically exposed as
CEL variables. See [Signals — CEL context building](../data-model/signals.md#cel-context-building-in-detail)
for details.

```python
# Advanced validator flow (sync)
result = engine.validate(...)  # Launches container, blocks until done
post_result = engine.post_execute_validate(output_envelope)  # Processes results

# Advanced validator flow (async)
result = engine.validate(...)  # Launches container, returns immediately
# ... later, callback arrives ...
post_result = engine.post_execute_validate(output_envelope)  # Processes results
```

## The Processor Pattern

### Why Processors?

Before the processor pattern, validation step logic was scattered across:
- `StepOrchestrator._record_step_result()` - for sync execution
- `ValidationCallbackService._process_callback()` - for async callbacks

This led to code duplication, inconsistent behavior, and difficult maintenance. The processor pattern consolidates validator step logic into a single, testable abstraction. `_record_step_result()` now only handles action steps (Slack, signed credential issuance, and similar side effects).

### Processor Class Hierarchy

```
ValidationStepProcessor (abstract base)
├── SimpleValidationProcessor
│   └── Handles: Basic, JSON Schema, XML Schema, AI validators
└── AdvancedValidationProcessor
    └── Handles: EnergyPlus, FMU, custom container validators
```

### Processor Responsibilities

| Responsibility | Description |
|----------------|-------------|
| Validator dispatch | Call `engine.validate()` and `engine.post_execute_validate()` |
| Finding persistence | Save `ValidationFinding` records to database |
| Output storage | Store extracted step output values for downstream steps |
| Assertion tracking | Record assertion counts for run summaries |
| Error handling | Catch exceptions and set appropriate status |
| Step finalization | Set ended_at, duration_ms, status, output |

### What Processors Do NOT Do

Processors handle lifecycle, not logic. They do NOT:
- Evaluate CEL assertions (validator's job)
- Extract declared output values from output data (validator's job)
- Know about validation semantics (validator's job)

## Detailed Execution Flows

### Flow 1: Simple Validator (JSON Schema)

This is the simplest case - a single method call that completes synchronously.

```
┌─────────────────┐
│ ValidationRun   │
│ Service         │
└────────┬────────┘
         │
         │ 1. Get processor for step
         ▼
┌─────────────────┐
│ SimpleValidation│
│ Processor       │
└────────┬────────┘
         │
         │ 2. processor.execute()
         ▼
┌─────────────────┐
│ JsonSchema      │
│ Validator       │
└────────┬────────┘
         │
         │ 3. engine.validate()
         │    - Load schema from ruleset
         │    - Parse submission JSON
         │    - Run jsonschema validation
         │    - Evaluate input-stage CEL assertions
         │    - Return ValidationResult
         │
         ▼
┌─────────────────┐
│ SimpleValidation│
│ Processor       │
└────────┬────────┘
         │
         │ 4. persist_findings(result.issues)
         │ 5. store_assertion_counts(...)
         │ 6. finalize_step(status, stats)
         │
         ▼
┌─────────────────┐
│ StepProcessing  │
│ Result          │
│ (passed=True)   │
└─────────────────┘
```

**Code path:**
```
validibot/validations/services/validation_run.py  (facade)
  └── execute_workflow_steps() → delegates to StepOrchestrator

validibot/validations/services/step_orchestrator.py
  └── execute_workflow_steps()
      └── _execute_validator_step()
          └── processor.execute()

validibot/validations/services/step_processor/simple.py
  └── SimpleValidationProcessor.execute()
      └── engine.validate()
      └── persist_findings()
      └── store_assertion_counts()
      └── finalize_step()
```

### Flow 2: Advanced Validator - Sync (Docker Compose Deployments)

When running with Docker Compose, container execution blocks until complete.

```
┌─────────────────┐
│ ValidationRun   │
│ Service         │
└────────┬────────┘
         │
         │ 1. Get processor for step
         ▼
┌─────────────────┐
│ AdvancedValidation│
│ Processor       │
└────────┬────────┘
         │
         │ 2. processor.execute()
         ▼
┌─────────────────┐
│ EnergyPlus      │
│ Validator       │
└────────┬────────┘
         │
         │ 3. engine.validate()
         │    - Evaluate INPUT-stage assertions
         │    - backend = DockerComposeExecutionBackend
         │    - backend.execute() → Runs container, BLOCKS
         │    - Returns ValidationResult with output_envelope
         │
         ▼
┌─────────────────┐
│ AdvancedValidation│
│ Processor       │
└────────┬────────┘
         │
         │ 4. persist_findings(input_stage_issues)
         │ 5. result.passed is NOT None (sync!)
         │
         ▼
┌─────────────────┐      ┌─────────────────┐
│ _complete_with_ │      │ EnergyPlus      │
│ envelope()      │─────▶│ Validator       │
└────────┬────────┘      └────────┬────────┘
         │                        │
         │                        │ 6. engine.post_execute_validate()
         │                        │    - Extract output values from envelope
         │                        │    - Evaluate OUTPUT-stage assertions
         │                        │    - Return ValidationResult with output values
         │                        │
         │◀───────────────────────┘
         │
         │ 7. persist_findings(output_stage_issues)
         │ 8. store_output_values(output_values)
         │ 9. store_assertion_counts(combined)
         │ 10. finalize_step(status, stats)
         │
         ▼
┌─────────────────┐
│ StepProcessing  │
│ Result          │
│ (passed=True)   │
└─────────────────┘
```

### Flow 3: Advanced Validator - Async (GCP Cloud Run)

When running on GCP, containers are launched asynchronously and report back via callback.

**Phase 1: Launch Container**
```
┌─────────────────┐
│ ValidationRun   │
│ Service         │
└────────┬────────┘
         │
         │ 1. Get processor for step
         ▼
┌─────────────────┐
│ AdvancedValidation│
│ Processor       │
└────────┬────────┘
         │
         │ 2. processor.execute()
         ▼
┌─────────────────┐
│ EnergyPlus      │
│ Validator       │
└────────┬────────┘
         │
         │ 3. engine.validate()
         │    - Evaluate INPUT-stage assertions
         │    - resolve exact ready deployment
         │    - Service backend → deterministic provider task
         │    - Jobs backend → retained Cloud Run Job
         │    - Returns IMMEDIATELY with passed=None
         │
         ▼
┌─────────────────┐
│ AdvancedValidation│
│ Processor       │
└────────┬────────┘
         │
         │ 4. persist_findings(input_stage_issues)
         │ 5. result.passed IS None (async!)
         │ 6. _record_pending_state()
         │
         ▼
┌─────────────────┐
│ StepProcessing  │
│ Result          │
│ (passed=None)   │ ◀─── Run stays RUNNING, waiting for callback
└─────────────────┘
```

**Phase 2: Callback Processing (minutes later)**
```
┌─────────────────┐
│ Cloud Run       │
│ Service or Job  │
└────────┬────────┘
         │
         │ 1. Container completes
         │    - Writes output envelope to GCS
         │    - POSTs callback to Django
         │
         ▼
┌─────────────────┐
│ ValidationCallback│
│ Service         │
└────────┬────────┘
         │
         │ 2. Download output envelope from GCS
         │ 3. Get processor for step
         ▼
┌─────────────────┐
│ AdvancedValidation│
│ Processor       │
└────────┬────────┘
         │
         │ 4. processor.complete_from_callback(output_envelope)
         │
         ▼
┌─────────────────┐
│ _complete_with_ │
│ envelope()      │
└────────┬────────┘
         │
         │ 5. Get existing finding counts (INPUT-stage preserved!)
         │ 6. engine.post_execute_validate()
         │ 7. persist_findings(output_issues, append=True)  ◀─── APPEND, not replace!
         │ 8. store_output_values(output_values)
         │ 9. store_assertion_counts(combined)
         │ 10. finalize_step(status, stats)
         │
         ▼
┌─────────────────┐
│ StepProcessing  │
│ Result          │
│ (passed=True)   │
└─────────────────┘
         │
         ▼
┌─────────────────┐
│ Finalize run or │
│ resume next step│
└─────────────────┘
```

## Assertion Evaluation

### What Are CEL Assertions?

CEL (Common Expression Language) assertions allow users to define custom pass/fail conditions beyond the basic validation logic. For example:

```cel
# Input-stage assertion (runs before container)
submission.metadata.version >= "2.0"

# Output-stage assertion (runs after container completes)
output.metrics.site_eui_kwh_m2 < 100
```

### Two Assertion Stages

| Stage | When Evaluated | Available Data | Applies To |
|-------|----------------|----------------|------------|
| Input | During `engine.validate()` | Submission content, metadata | All validators |
| Output | During `engine.post_execute_validate()` | Processor output values and metrics | Validators with `has_processor=True` only |

The output stage only exists for validators that perform a transformation —
they take input data, do something with it (run a simulation, execute a model),
and produce new output data. At the output stage, every key in the output
payload is automatically exposed as a CEL variable, so workflow authors can
write assertions against processor-generated values without those values
needing to be pre-declared as catalog entries. This is critical for validators
like FMU, where the output variable names (e.g. `T_room`, `Q_cooling_actual`)
come from the model itself and vary between models.

### The `output` namespace

At the output stage, advanced validators merge submission inputs with step
output values into a single assertion payload via `_build_assertion_payload()`.
All output values are placed in a **nested `output` dict** so that `output.T_room`
resolves correctly via both CEL member access and basic-assertion dot-path
navigation.

**Name collision convention**: When a submission key shares a name with an output
value, the input keeps the bare name and the output is reachable only via
`output.<name>`. Example payload:

```python
{
    "Q_cooling_max": 6000,  # input (bare)
    "T_room": 296.63,  # output (no collision → bare)
    "Q_cooling_actual": 5172.83,  # output (no collision → bare)
    "output": {  # nested namespace
        "T_room": 296.63,
        "Q_cooling_actual": 5172.83,
    },
}
```

The assertion form enforces this convention: when a target contract key is
ambiguous (exists as both input and output), the form requires the `output.`
prefix for the output value.

This `output.T_room` syntax is **standard CEL member access**, not a custom
extension. The `output` variable is a real Python dict that cel-python
converts to a CEL `MapType`, and `.T_room` is standard field selection on
that map. See [Signals — CEL context building in detail](../data-model/signals.md#cel-context-building-in-detail) for
the full pipeline from container output to evaluable CEL expression.

### Assertion Evaluation Happens in Validators

A key design decision: **validators evaluate assertions, not processors**.

Why?
1. Validators know how to extract the assertion payload from their specific data structures
2. Some validators (Basic, AI) were already evaluating assertions in `validate()`
3. Keeps the processor focused on lifecycle, not logic

```python
# Inside JsonSchemaValidator.validate():
result = self._run_schema_validation(submission)
assertion_findings = self.evaluate_cel_assertions(
    payload=parsed_json,
    stage="input",
    run_context=run_context,
)
return ValidationResult(
    passed=result.passed,
    issues=result.issues + assertion_findings,
    assertion_stats=AssertionStats(total=N, failures=M),
)
```

## Step Outputs and Cross-Step Communication

### What Are Step Outputs?

Step outputs are declared values extracted from validator results. They remain
owned by their producing step and can be used by downstream steps. For example,
an EnergyPlus step might extract:

```json
{
  "site_eui_kwh_m2": 87.5,
  "site_electricity_kwh": 12500,
  "site_natural_gas_kwh": 8200
}
```

A downstream step can then reference these outputs in its assertions
using the ``steps`` namespace:

```cel
# In a subsequent step's assertion, access the upstream step's output
steps.energyplus_step.output.site_eui_kwh_m2 < 100
```

### Output Flow

1. **Extraction**: Validator extracts output values during `post_execute_validate()`
2. **Return**: Validator returns step output values in `ValidationResult.output_values`
3. **Storage**: Processor persists values on `ValidationStepRun`
4. **Access**: Downstream steps access them via `run_context.upstream_steps`

These values are not workflow signals merely because they cross a step
boundary. They become `s.*` signals only when the author explicitly uses
"Copy to Signal" promotion. Without promotion, the canonical downstream
reference remains `steps.<step_key>.output.<contract_key>`.

## File Structure

```
validibot/validations/services/step_processor/
├── __init__.py          # Package exports: get_step_processor
├── base.py              # ValidationStepProcessor abstract base class
├── simple.py            # SimpleValidationProcessor
├── advanced.py          # AdvancedValidationProcessor
├── factory.py           # get_step_processor() factory function
└── result.py            # StepProcessingResult dataclass
```

## Key Classes and Methods

### StepProcessingResult

The return type from all processor `execute()` methods:

```python
@dataclass
class StepProcessingResult:
    passed: bool | None  # None = async, waiting for callback
    step_run: ValidationStepRun
    severity_counts: Counter  # {Severity.ERROR: 2, Severity.WARNING: 5}
    total_findings: int
    assertion_failures: int
    assertion_total: int
```

### ValidationStepProcessor (Base)

Shared methods used by both subclasses:

| Method | Purpose |
|--------|---------|
| `_get_engine()` | Get validator instance from registry |
| `_build_run_context()` | Build the canonical workflow context |
| `persist_findings()` | Save ValidationFinding records |
| `store_input_values()` | Store canonical contract-keyed inputs |
| `store_output_values()` | Store canonical contract-keyed outputs |
| `store_assertion_counts()` | Save assertion stats for run summary |
| `finalize_step()` | Set ended_at, duration_ms, status, output |

### SimpleValidationProcessor

```python
def execute(self) -> StepProcessingResult:
    engine = self._get_engine()
    result = engine.validate(...)
    self.persist_findings(result.issues)
    self.store_assertion_counts(...)
    self.finalize_step(status, stats)
    return StepProcessingResult(passed=result.passed, ...)
```

### AdvancedValidationProcessor

```python
def execute(self) -> StepProcessingResult:
    engine = self._get_engine()
    result = engine.validate(...)  # May launch container
    self.persist_findings(result.issues)  # Input-stage findings

    if result.passed is None:
        # Async - container launched, waiting for callback
        self._record_pending_state(result)
        return StepProcessingResult(passed=None, ...)
    else:
        # Sync - container completed
        return self._complete_with_envelope(engine, result.output_envelope, ...)

def complete_from_callback(self, output_envelope) -> StepProcessingResult:
    # Called by ValidationCallbackService after async completion
    return self._complete_with_envelope(engine, output_envelope, append_findings=True)
```

## Verdict trust model: `SUCCESS` envelopes with ERROR findings

An advanced (container) validator is authoritative about pass/fail via its
envelope `status`. A finding's `severity` is normally *display metadata*, not a
pass/fail signal — a shipped validator may legitimately report `status=SUCCESS`
while emitting ERROR-severity findings. **EnergyPlus, for example, exits 0
(`SUCCESS`) while writing `** Severe **` ERROR-severity lines it considers
non-fatal.** A blanket "any ERROR fails" rule would wrongly fail those runs.

That trust is safe only for validators we ship. A **user-added custom
container** (`Validator.is_system=False`) is not trusted to honour the
contract — a naive or buggy one can set `status=SUCCESS` ("my container ran")
while emitting ERROR findings ("the data is invalid"). Because a passing run can
lead to a signed credential, `AdvancedValidationProcessor._complete_with_envelope`
applies a trust gate:

| Validator | `SUCCESS` envelope + container ERROR findings | Result |
|---|---|---|
| Shipped (`is_system=True`) | trusted | **PASSED**, with a WARNING finding |
| Custom (`is_system=False`) | not trusted | **FAILED**, with an ERROR finding |

This lives in the step processor (the single completion chokepoint for both sync
and async paths), not in the validator's `_determine_passed`/`post_execute_validate`
— those are severity-agnostic by design and their `passed` is *not* the persisted
verdict. Output-stage assertion failures always fail the step regardless of
validator trust. Do not "simplify" this gate away: dropping it either re-creates
the custom-container credential gap or breaks shipped validators like EnergyPlus.

## Error Handling

Each processor handles errors gracefully:

1. **Validator not found**: Returns `StepProcessingResult(passed=False)` with error finding
2. **Validation exception**: Catches exception, creates error finding, finalizes step as FAILED
3. **Missing envelope (sync)**: Creates error finding explaining configuration issue

All error paths ensure:
- A `ValidationFinding` with severity ERROR is created
- The step is finalized with status FAILED
- The error message is stored in `step_run.error`

## Testing

### Unit Tests

Tests for processor classes use mocked validators:

```python
def test_simple_processor_passes_on_valid():
    """Test SimpleValidationProcessor with passing validation."""
    mock_engine = Mock()
    mock_engine.validate.return_value = ValidationResult(passed=True, ...)

    processor = SimpleValidationProcessor(run, step_run)
    result = processor.execute()

    assert result.passed is True
    assert step_run.status == StepStatus.PASSED.value
```

### Integration Tests

Full workflow tests verify end-to-end behavior:

- JSON Schema validation with CEL assertions
- EnergyPlus sync execution (Docker Compose)
- Callback flow with mocked async backend
- Input/output-stage assertion preservation

## Related Documentation

- [Workflow Data Architecture](workflow_data_architecture.md) - Namespace scope, step I/O, signals, and artifact boundaries
- [Workflow Orchestration Architecture](workflow_engine.md) - Higher-level orchestration
- [Validator Architecture](validator_architecture.md) - Execution backends and deployment
- [How Validibot Works](how_it_works.md) - End-to-end system overview
