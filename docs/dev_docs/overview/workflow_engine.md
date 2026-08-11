# Workflow Engine Architecture

This document describes the internal architecture of the `ValidationRunService`, which orchestrates the execution of validation workflows.

## Overview

The **Workflow Engine** is responsible for:

1. Iterating through `WorkflowStep`s defined in a `Workflow`
2. Executing each step against a `Submission`
3. Recording the results (`ValidationFinding`, status updates)
4. Handling workflow-level status transitions

## Architecture: Two-Layer Dispatch

The workflow engine uses a two-layer dispatch pattern:

1. **ValidationRunService** - Orchestrates the workflow loop and delegates individual steps
2. **Processors/Handlers** - Execute individual steps based on their type

```
ValidationRunService.execute_workflow_steps()
        │
        ├── For validator steps:
        │       │
        │       └── ValidationStepProcessor
        │               ├── SimpleValidationProcessor (JSON, XML, Basic, AI)
        │               └── AdvancedValidationProcessor (EnergyPlus, FMU)
        │
        └── For action steps:
                │
                └── StepHandler
                        ├── SlackMessageActionHandler
                        ├── SignedCredentialActionHandler
                        └── ...
```

## Validator Step Execution: The Processor Pattern

Validator steps are executed through the **ValidationStepProcessor** abstraction. This provides a clean separation between:

- **Workflow orchestration** (ValidationRunService) - loops, aggregation, status management
- **Step lifecycle** (Processors) - call validator, persist findings, handle errors
- **Validation logic** (Validators) - schema checking, AI prompts, assertions

### How Validator Steps Execute

```python
# Inside StepOrchestrator.execute_workflow_steps()
for step in workflow_steps:
    step_run = self._start_step_run(validation_run, step)

    if step.validator:
        # Use processors for validator steps
        result: StepProcessingResult = self._execute_validator_step(
            validation_run=validation_run,
            step_run=step_run,
        )
    else:
        # Use existing handler flow for action steps
        validation_result = self.execute_workflow_step(step=step, ...)
```

The `_execute_validator_step()` method delegates to the appropriate processor and returns a typed `StepProcessingResult`:

```python
def _execute_validator_step(self, validation_run, step_run) -> StepProcessingResult:
    from validibot.validations.services.step_processor import get_step_processor

    processor = get_step_processor(validation_run, step_run)
    return processor.execute()
```

### Processor Types

| Processor | Validator Types | Execution Mode |
|-----------|-----------------|----------------|
| `SimpleValidationProcessor` | Basic, JSON Schema, XML Schema, AI | Synchronous, inline |
| `AdvancedValidationProcessor` | EnergyPlus, FMU, custom | Sync (Docker) or Async (Cloud Run) |

For detailed documentation on processors, see [Validation Step Processor Architecture](step_processor.md).

## Action Step Execution: The Handler Pattern

Action steps (non-validation operations) use the **StepHandler** protocol for extensibility.

### Core Components

#### 1. Protocol (`StepHandler`)
All execution logic must implement the `StepHandler` protocol defined in `validibot/actions/protocols.py`:

```python
class StepHandler(Protocol):
    def execute(self, run_context: RunContext) -> StepResult: ...
```

- **RunContext**: Contains the run, current step, workflow namespaces, and
  canonical values from completed upstream steps.
- **StepResult**: Standardized output indicating pass/fail, issues, operational
  statistics, and contract-keyed `output_values`. The orchestrator persists
  those values on `ValidationStepRun.output_values`; action outputs never hide
  inside the statistics mapping.

#### 2. Dispatcher (`ValidationRunService`)
For action steps, `StepOrchestrator`:
- Resolves the appropriate implementation (`Action` subclass)
- Looks up the registered `StepHandler`
- Invokes `handler.execute(context)`

### Available Handlers

| Handler | Purpose |
|---------|---------|
| `SlackMessageActionHandler` | Sends Slack notifications |
| `SignedCredentialActionHandler` | Generates and attaches credentials |

## Async Validator Completion: Callbacks

When advanced validators run on async backends (like GCP Cloud Run), execution follows a two-phase pattern:

### Phase 1: Launch (ValidationRunService)
1. Processor calls `engine.validate()`, which launches container
2. Container job starts running on Cloud Run
3. Processor returns `StepProcessingResult(passed=None)`
4. Run stays in `RUNNING` status, waiting for callback

### Phase 2: Complete (ValidationCallbackService)
1. Container completes and POSTs callback to `/api/internal/callbacks/validation/`
2. `ValidationCallbackService` downloads output envelope from cloud storage
3. Creates processor and calls `processor.complete_from_callback(output_envelope)`
4. Processor finalizes step and either:
   - Resumes workflow with next step, OR
   - Finalizes run as SUCCEEDED/FAILED

```python
# Inside ValidationCallbackService._process_callback()
from validibot.validations.services.step_processor import get_step_processor

processor = get_step_processor(run, step_run)
processor.complete_from_callback(output_envelope)
```

## Extending the System

### Adding a New Validator Type

1. **Create the validator** in `validibot/validations/validators/`:
   - Extend `BaseValidator`
   - Implement `validate()` method
   - For container-based validators, implement `post_execute_validate()` too

2. **Register the validator** by adding a `config.py` to your validator sub-package:
   ```python
   # validations/validators/my_validator/config.py
   config = ValidatorConfig(
       validation_type="MY_VALIDATOR",
       validator_class="validibot.validations.validators.my_validator.validator.MyValidator",
       ...
   )
   ```

3. **Update processor factory** (if needed) in `step_processor/factory.py`:
   ```python
   advanced_types = {
       ValidationType.ENERGYPLUS,
       ValidationType.FMU,
       ValidationType.MY_VALIDATOR,  # Add here if container-based or compute-intensive
   }
   ```

### Adding a New Action Type

1. **Define the Model**: Create a new `Action` subclass in `validibot/actions/models.py`
2. **Implement Handler**: Create a class implementing `StepHandler` in `validibot/actions/handlers.py`
3. **Register**: Map the action type to your handler in `validibot/actions/registry.py`:

```python
# validibot/actions/registry.py
register_action_handler(MyActionType.CUSTOM, MyCustomHandler)
```

## Execution Flow Summary

```
┌────────────────────────────────────────────────────────────────────┐
│                     API Request Arrives                             │
└─────────────────────────────┬──────────────────────────────────────┘
                              │
                              ▼
┌────────────────────────────────────────────────────────────────────┐
│          ValidationRunService.execute_workflow_steps()              │
│                                                                     │
│   1. Mark run as RUNNING                                            │
│   2. Log VALIDATION_RUN_STARTED event                               │
│   3. For each workflow step:                                        │
│      a. Create/get ValidationStepRun                                │
│      b. Route to processor (validator) or handler (action)          │
│      c. Aggregate metrics                                           │
│   4. Build run summary                                              │
│   5. Finalize run status                                            │
│   6. Log VALIDATION_RUN_SUCCEEDED/FAILED event                      │
└────────────────────────────────────────────────────────────────────┘
                              │
              ┌───────────────┴───────────────┐
              │                               │
              ▼                               ▼
┌─────────────────────────┐     ┌─────────────────────────┐
│   Validator Step        │     │   Action Step           │
│                         │     │                         │
│   get_step_processor()  │     │   get_action_handler()  │
│   processor.execute()   │     │   handler.execute()     │
└─────────────────────────┘     └─────────────────────────┘
```

## Signal Flow Through Workflow Execution

Signals flow through a workflow execution in a defined sequence. Understanding
this sequence is essential for debugging assertion failures and for writing
cross-step assertions.

### Phase 1: Workflow-level signals resolved before steps run

Before any step executes, `StepOrchestrator._resolve_workflow_signals()` reads
the workflow's `WorkflowSignalMapping` rows and resolves each source path
against the submission data. The result is a dict of `{name: value}` pairs
stored in `RunContext.workflow_signals`.

If any mapping with `on_missing="error"` fails to resolve and has no default
value, a `SignalResolutionError` is raised and the run fails before any step
is attempted.

The resolved signals are available in the `s` (signal) namespace for all
steps in the workflow. For example, a mapping `name="target_eui"`,
`source_path="metadata.target_eui_kwh_m2"` becomes accessible as
`s.target_eui` in every step's CEL expressions.

### Phase 2: Signals available in the `s` namespace for all steps

When `_build_cel_context()` runs for each step, the `s` / `signal` namespace
is populated from two sources (in priority order):

1. **Workflow-level signals** from `RunContext.workflow_signals` (highest
   priority -- these represent the author's explicit domain vocabulary)
2. **Promoted step inputs and outputs** injected by `_inject_promotions()`.
   Promotions come from two storage paths: the in-row
   `promoted_signal_name` field on step-owned `StepIODefinition` rows, and
   `WorkflowStepIOPromotion` overlay rows for validator-owned definitions
   (which are shared across workflows, so the promotion must live in a
   workflow-scoped table)
The `p` / `payload` namespace contains the raw submission data. The `o` /
`output` namespace contains this step's declared output values (populated
from the validator output during output-stage assertion evaluation).

### Phase 3: Promotions reconstructed before each step

`_inject_promotions()` runs inside `_build_cel_context()` for each step
(not once per run). It gathers promotions across all steps in the workflow
from both storage paths -- in-row `promoted_signal_name` on step-owned
`StepIODefinition` rows, and `WorkflowStepIOPromotion` overlay rows on
validator-owned ones. Promotion is symmetric per ADR-2026-05-22b: both
INPUT- and OUTPUT-direction definitions can promote, and the direction
picks whether the value is read from the producing step's `input` or
`output` values on its `ValidationStepRun`.

This means promoted values from step N are available as
`s.<promoted_signal_name>` in step N+1, N+2, and so on -- but not in step N
itself (the producing step accesses its own output via `o.<contract_key>`).

### Phase 4: Cross-step output access via `steps`

After each step completes, the processor persists its contract values on
`ValidationStepRun.input_values` and `ValidationStepRun.output_values`. Before
the next step runs, `RunContextBuilder` reads completed earlier step rows and
builds the `steps` namespace:

```json
{
  "steps": {
    "envelope_check": {
      "output": {
        "floor_area_m2": 10000.0,
        "wall_r_value": 18.0
      }
    },
    "energyplus_sim": {
      "output": {
        "site_eui_kwh_m2": 75.2,
        "site_electricity_kwh": 12345.0
      }
    }
  }
}
```

Downstream steps can access any prior step's output via the full path:
`steps.envelope_check.output.floor_area_m2`. This is available alongside
promoted outputs -- the `steps` namespace provides the raw access path while
`s.<promoted_signal_name>` provides the author-friendly alias.

### Signal flow diagram

```
                    Submission data arrives
                            |
                            v
              resolve_workflow_signals()
              (WorkflowSignalMapping rows)
                            |
                            v
                  RunContext.workflow_signals
                  = {"target_eui": 95, ...}
                            |
            +---------------+---------------+
            |               |               |
            v               v               v
         Step 1          Step 2          Step 3
            |               |               |
     _build_cel_context  _build_cel_context  _build_cel_context
            |               |               |
     s: workflow sigs   s: workflow sigs   s: workflow sigs
        + step inputs      + step inputs      + step inputs
                            + promoted from 1  + promoted from 1,2
     o: step 1 output   o: step 2 output   o: step 3 output
     steps: {}           steps: {step1}     steps: {step1, step2}
     p: raw payload      p: raw payload     p: raw payload
            |               |               |
   output_values       output_values      output_values
     on step run         on step run        on step run
```

### Key implementation files

| File | Responsibility |
|------|----------------|
| `validations/services/signal_resolution.py` | `resolve_workflow_signals()` -- pre-step resolution |
| `validations/services/run_context.py` | Build workflow signals and canonical upstream step values |
| `validations/validators/base/base.py` | `_build_cel_context()` and `_inject_promotions()` |
| `validations/services/step_processor/base.py` | Persist canonical step input/output values |
| `actions/protocols.py` | `RunContext` dataclass with `workflow_signals` and `upstream_steps` |

For the architectural model, see
[Workflow Data Architecture](workflow_data_architecture.md). For the detailed
models and resolution algorithms, see
[Workflow Signals and Step I/O Reference](../data-model/signals.md).

## Related Documentation

- [Validation Step Processor Architecture](step_processor.md) - Deep dive into processor pattern
- [Validator Architecture](validator_architecture.md) - Execution backends and deployment
- [How Validibot Works](how_it_works.md) - End-to-end system overview
- [Workflow Data Architecture](workflow_data_architecture.md) - Scope, provenance, namespaces, and artifact boundaries
- [Workflow Signals and Step I/O Reference](../data-model/signals.md) - Detailed models, CEL context, and resolution
