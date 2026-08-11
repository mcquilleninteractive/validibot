# Validators

Validators define the concrete validation class that a workflow step will call. One workflow step uses
one validator. Validators bundle the Validibot contract revision, the catalog
of step inputs/outputs and derivations the validator exposes, and optional organization-specific extensions (custom validators).
The platform ships with stock validation types such as:

- **BASIC** — no provider backing; authors add assertions manually via the UI. These steps still produce
  rulesets so findings remain auditable, but there is no catalog beyond whatever the assertion references.
- **JSON_SCHEMA / XML_SCHEMA** — schema validations that require uploading or pasting schema content.
- **ENERGYPLUS** — advanced simulation validators with IDF/simulation options and catalog entries.
- **FMU** — simulation validators backed by FMI/FMU metadata and execution.
- **SHACL** — RDF graph validators backed by SHACL shapes and ontology resources.
- **SCHEMATRON** — XML validators backed by Schematron rules compiled in the isolated backend.
- **PORTFOLIO_MANAGER** — bounded building-report validation for XLS, XLSX, XML, and ZIP carriers.
- **PDF** — bounded, non-rendering PDF package inventory and exact extraction of selected XML, JSON, and STEP Part 21 members.
- **THERM** — thermal-bridge validation support for THERM workflows.
- **AI_ASSIST** — template-driven AI validations (policy check, critic, etc.).
- **CUSTOM_VALIDATOR** — organization-defined validators registered via the custom validator UI (displayed as “Custom Basic Validator”).

**Terminology note:** `ValidationType` (the enum) describes the _kind of validation_ being performed
(BASIC, JSON_SCHEMA, ENERGYPLUS, etc.), not the validator itself. Multiple `Validator` rows can share
the same `ValidationType` -- for example, several custom validators all use `CUSTOM_VALIDATOR`. Think
of it as "validation class type" rather than "validator type."

Validators are stored in the `validators` table. Each row records the following:

- `slug`, `name`, `description`, `validation_type`, `version`
- relationship to an organization (`org_id`) and `is_system` flag
- timestamp fields plus the related `custom_validator` entry when the row was created by an org

`version` is a positive integer revision of the Validibot validator contract. It is not an
EnergyPlus, FMI, JSON Schema, SHACL, or ontology version. Those domain labels belong in
tags/metadata/capabilities when we expose them; the integer version keeps row identity, URL routing,
and "latest version" ordering deterministic.

Every workflow step references a validator row. During execution the validator tells the runtime which
provider class to load, which helper functions are legal, and how to interpret rule and assertion catalogs.

## Validator version families

Rows with the same `slug` and different integer `version` values are a validator family. The library
index shows only the latest visible row for each family. The default detail route also resolves to the
latest version:

```text
/app/validations/library/custom/basic-validator/
```

Older versions stay addressable through hidden manual routes:

```text
/app/validations/library/custom/basic-validator/versions/
/app/validations/library/custom/basic-validator/versions/1/
```

Older version detail pages are read-only. Workflow steps are not rewritten when a newer validator
version appears; each step remains pinned to the exact `Validator` FK it was configured with.

## Catalog entries (step inputs, step outputs, derivations)

Validators own the canonical catalog describing what the validator can read or emit. The catalog
is stored across two models:

- **`StepIODefinition`** — one row per step input or step output (the values that surface in the
  `i.*` and `o.*` CEL namespaces). The database table is
  `validations_stepiodefinition`, matching the current Python model name.
- **`Derivation`** — one row per computed value, defined by a CEL expression over step values and
  other derivations.

Key fields on `StepIODefinition`:

| Field                  | Meaning                                                                                                                          |
| ---------------------- | -------------------------------------------------------------------------------------------------------------------------------- |
| `contract_key`         | Stable, slug-safe identifier used in CEL expressions, the API, and data path bindings (e.g., `panel_area`).                      |
| `native_name`          | The provider's original name, preserved verbatim (e.g., an FMU variable name or an EnergyPlus template placeholder).             |
| `direction`            | Whether the row is a step **input** (available before the validator runs) or a step **output** (produced by the run).            |
| `data_type`            | Scalar/list metadata (number, datetime, bool, series).                                                                           |
| `source_kind`          | How the value is obtained: `PAYLOAD_PATH` (read from a path in the submission) or `INTERNAL` (the validator computes it itself). |
| `is_path_editable`     | Whether workflow authors can edit the source data path on the step's `StepInputBinding`.                                         |
| `provider_binding`     | Validator-type-specific properties (e.g., FMU `causality`/`value_reference`, EnergyPlus template `min`/`max`/`choices`).         |
| `promoted_signal_name` | Optional workflow-signal name when a step-owned row is promoted to the `s.*` namespace.                                          |
| `on_missing`           | What to do when the source value is absent at runtime.                                                                           |
| `metadata`             | Free-form JSON used by the UI and provider tooling.                                                                              |

Every catalog row is owned by exactly one of a `Validator` (shared by all steps using that
validator) or a `WorkflowStep` (per-step rows for FMU uploads, template scans, or
author-customized inputs/outputs) — an XOR constraint enforced by the model. Centralising these
definitions lets every ruleset reuse them without duplicating structure inside each assertion;
see [Workflow Signals and Step I/O Reference](signals.md) for the full ownership and promotion story and
[Ruleset Assertions](assertions.md) for how assertions reference them.
Basic validators intentionally skip catalog management; every assertion directly references the custom
target path the author entered.

## Validator default assertions (vs. workflow assertions)

- **Default assertions**: logic that ships with a validator itself (for example, default CEL expressions). These are ordinary `RulesetAssertion` rows attached to the validator's `default_ruleset` — a `Ruleset` that `Validator.ensure_default_ruleset()` creates on demand. They run on every step that uses the validator.
- **Step assertions**: logic defined on workflow steps against the step's own ruleset. Stored separately and evaluated in workflow runs. Assertions are the only logic workflow authors manage today.

Both kinds use the same `RulesetAssertion` model and the same evaluators — the only difference is which ruleset they hang off. See [Ruleset Assertions](assertions.md) for the field-by-field breakdown.

## Custom validators

Custom validators let an organization publish its own validators in the validator library on top
of a base validation type. Each one is a `CustomValidator` row (one-to-one with its `Validator`
row), and `clean()` enforces that the linked validator's `validation_type` matches the recorded
`base_validation_type`.

Two authoring flows exist today:

1. **Simple custom validators** — created through the library UI with a name, descriptions,
   notes, a single supported data format (JSON or YAML), and an "allow custom data paths in
   assertions" toggle. These are persisted with `custom_type=SIMPLE` and base type
   `CUSTOM_VALIDATOR`; authors then add assertions against payload paths.
2. **SHACL library validators** — created through the dedicated SHACL flow, which stores
   shapes-related settings (inference mode, submission format, bundled standards) in the default
   ruleset's metadata.

When saved, the system persists a new `Validator` row owned by the org. Rulesets that pick the
custom validator see its catalog, and the validator detail page shows who owns and maintains it.
Custom validators stay scoped to the org that created them; system validators remain read-only.

Catalog changes are versioned on the validator row. Editing the current custom-validator row updates
the catalog for workflows that reference that row. Breaking catalog changes should be represented by a
new integer validator version; user-facing semantic labels will be handled by tags/metadata rather than
overloading `Validator.version`.

## Resource files

Advanced validators may require auxiliary files to run (e.g., EnergyPlus needs EPW weather files,
FMU validators need shared libraries). These are stored as `ValidatorResourceFile` rows linked to
a validator via FK.

Each resource file has:

- **Scoping**: `org=NULL` for system-wide resources visible to all orgs, or `org=<org>` for
  org-specific files. System-wide files are only manageable by superusers.
- **Type**: `resource_type` from the `ResourceFileType` enum (currently `ENERGYPLUS_WEATHER`).
- **Validation**: Each type maps to a `ResourceTypeConfig` in `validations/constants.py` that
  defines allowed extensions, max file size, and optional header validation. Adding a new resource
  type requires only adding a config entry -- no form or view changes needed.

Resource files are referenced by workflow steps via the `WorkflowStepResource` through table
(FK-backed). Each step resource has a `role` (e.g., `WEATHER_FILE`, `MODEL_TEMPLATE`) and
points to either a shared `ValidatorResourceFile` (catalog reference, PROTECT on delete) or
stores its own file directly (step-owned, CASCADE with step). This provides referential
integrity and eliminates the stale-UUID problem of the earlier JSON-based approach.

**RBAC**: Authors can view and select resource files, but only ADMIN/OWNER can create, edit, or
delete them (uses `ADMIN_MANAGE_ORG` permission). Deletion is blocked if the file is referenced
by any active workflow step (checked via `WorkflowStepResource` FK query).

**Content immutability**: every `ValidatorResourceFile` and
step-owned `WorkflowStepResource` row stores a SHA-256 `content_hash` of its file's bytes,
computed in `save()` via `validibot.core.filesafety.sha256_field_file`. If a save would change
the hash AND the file is referenced by any step on a locked or used workflow, `save()` raises
`ValidationError`. Operators must upload a fresh row (new `ValidatorResourceFile` entry, or
clone the workflow to a new version) rather than overwriting bytes in place. This protects
the launch contract that previously-launched runs were operating under: a weather file
labelled "TMY3 SFO 2018" cannot be silently swapped to a 2024 dataset and have past runs
retroactively change meaning.

## Validator detail page (UI)

The validator detail page uses link-based tabs (separate URLs, server-rendered) rather than
JavaScript tabs. The tab layout is:

| Tab                    | URL pattern                             | View                            | Default |
| ---------------------- | --------------------------------------- | ------------------------------- | ------- |
| **Description**        | `library/custom/<slug>/`                | `ValidatorDetailView`           | Yes     |
| **Step I/O**           | `library/custom/<slug>/step-io-tab/`    | `ValidatorStepIOTabView`        |         |
| **Default Assertions** | `library/custom/<slug>/assertions/`     | `ValidatorAssertionsTabView`    |         |
| **Resource Files**     | `library/custom/<slug>/resource-files/` | `ValidatorResourceFilesTabView` |         |

All tabs share the same base template (`validator_detail.html`) and render their content
conditionally via `active_tab`. Each tab includes only its own modals to reduce page weight.

## Provider resolution

The runtime resolves a provider implementation for every validator. Providers are in-process classes
registered per validation type/config. Functions such as
`BaseValidator.resolve_provider()` call the registry and cache the matching provider instance.

Providers must implement the following contract:

- `json_schema()` — optional schema for provider config blocks.
- `catalog_entries(validator)` — canonical catalog rows for the validator (built-in validators read
  bundled definitions; custom validators read DB-backed rows).
- `cel_functions()` — custom helper metadata appended to the default helper set.
- `preflight_validate(ruleset, merged_catalog)` — domain specific validation before a ruleset is accepted.
- `instrument(model_copy, ruleset)` — optional adjustments to the uploaded artifact (e.g., inject EnergyPlus output objects).
- `bind(run_ctx, merged_catalog)` — builds per-run bindings so CEL helpers (e.g., `series('meter')`) can resolve data on demand.

The provider gives the validator its domain-specific abilities without storing Python dotted paths in
the database. Validator contract upgrades happen by creating a new integer `Validator.version` row.

## Validator configuration

`ValidatorConfig` (a Pydantic model in `validations/validators/base/config.py`) is the **single
source of truth** for each system validator. Everything the platform needs to know about a
validator is declared in one place:

- **Identity and DB sync** — `slug`, `name`, `description`, `validation_type`, integer `version`, `order`.
  The `sync_validators` management command reads these fields and creates or updates `Validator`
  rows and their catalog entries in the database.
- **Validator class binding** — `validator_class` is a dotted Python path to the `BaseValidator`
  subclass (e.g., `"validibot.validations.validators.energyplus.validator.EnergyPlusValidator"`).
  At startup, `register_validators()` resolves this path and stores the class in the runtime
  registry so the engine can instantiate validators without storing Python paths in the database.
- **File handling** — `supported_file_types`, `supported_data_formats`, `allowed_extensions`,
  and `resource_types` declare what files the validator accepts.
- **Compute** — `compute_tier` (LOW, MEDIUM, HIGH) tells the platform how much resource the
  validator needs when dispatching containers.
- **Display** — `icon` (Bootstrap Icons class) and `card_image` for the validator library UI.
- **Catalog entries** — a list of `CatalogEntrySpec` objects describing step inputs, step
  outputs, and derivations. These sync to `StepIODefinition` rows and
  `Derivation` rows (computed values) in the database.
- **Step editor cards** — a list of `StepEditorCardSpec` objects that inject custom UI cards
  into the workflow step detail page (see below).

### Where configs live

Validators that are full sub-packages (EnergyPlus, FMU, etc.) declare their config in a
`config.py` module inside the package. For example, the EnergyPlus config lives at
`validibot/validations/validators/energyplus/config.py` and exports a module-level `config`
attribute.

Every validator is a sub-package under `validations/validators/` with its own `config.py`
declaring a `ValidatorConfig` instance.

### Discovery and registry population

At startup, `register_validators()` runs inside `ValidationsConfig.ready()` and does a single
pass over all validator configs. It calls `discover_configs()`, which walks the
`validations/validators/` directory and imports any sub-package that has a `config.py` with a
`ValidatorConfig` instance.

For each config, two registries are populated:

- The **config registry** — keyed by `validation_type`, stores the `ValidatorConfig` for metadata
  lookups (catalog entries, file types, display info, step editor cards).
- The **validator class registry** — keyed by `validation_type`, stores the resolved Python class
  for runtime instantiation. Only populated if the config declares a `validator_class` path.

This unified approach replaced an earlier two-registry system where configs and classes were
registered separately via different mechanisms.

### Syncing to the database

```bash
python manage.py sync_validators
```

This command reads from the config registry and creates or updates `Validator` rows and their
catalog entries. It is idempotent and runs automatically at container startup.

**Versioning:** The `version` field is a positive integer validator-contract revision, and
`sync_validators` keys validator rows by `(slug, version)`. Bumping the integer in a config creates
a *new* `Validator` row instead of mutating the existing one — preserving the launch contract that
workflows pinned to the old version were running under. Do not encode domain-standard versions in
this field; use future tags/metadata for labels such as `EnergyPlus 25.1`, `FMI 3.0`, or
`JSON Schema 2020-12`.

**Drift detection:** Each validator row stores a `semantic_digest` — a SHA-256 of the
behavior-defining fields (`validation_type`, `processor_name`, `validator_class`,
`supported_file_types`, `catalog_entries`, etc.; see
`validibot/validations/services/validator_digest.py` for the full allowlist). On every sync, the
command re-computes the digest from the config and compares it against the stored value:

- **Same digest** → no-op idempotent update.
- **Different digest under the same `(slug, version)`** → `CommandError` ("semantic drift
  detected"). Either bump the config's `version` to declare a new validator row, or pass
  `--allow-drift` (development override only) to overwrite the existing row's digest.

The drift gate exists because a deploy that swaps a validator's processor or class without a
version bump would silently re-write the rules of every workflow that locked onto the old version.
Sync's job is to make that loud at deploy time.

Catalog entries (step I/O + derivations) are updated in place via `update_or_create` on
`(validator, contract_key, direction)`, with stale entries pruned at the end of each sync. If you
need to drop or restructure catalog entries, edit the config and re-run sync.

### Step editor cards

Validators can declare custom UI cards that appear in the workflow step detail page's right
column via `StepEditorCardSpec` objects in the config's `step_editor_cards` list. This extension
point is available for future use, but no validators currently declare custom cards.

!!! note "Template variables use the unified Inputs and Outputs card"
    Template variable editing is handled by the unified "Inputs and Outputs" card that
    appears on every step detail page. Template variables are treated as step inputs
    with `source="template"`, alongside catalog entries with `source="catalog"`. Each
    template variable has a per-variable edit modal for annotations (label, default,
    type, constraints). This replaced the earlier `StepEditorCardSpec`-based approach.

Each card spec has the following fields:

- `slug` — unique identifier, used for the HTML `id` attribute and HTMx targeting.
- `label` — display text shown in the card header.
- `template_name` — Django template path to render the card content.
- `form_class` — optional dotted path to a Form class. If provided, the card renders an
  editable form. Resolved via `import_string()`.
- `view_class` — optional dotted path to a View class that handles GET/POST for the card.
  If omitted, the card is rendered inline with no separate endpoint.
- `order` — position within the right column (lower numbers appear higher).
- `condition` — optional dotted path to a `func(step) -> bool` callable. When set, the card
  only renders if the function returns `True`.

### Unified Inputs and Outputs card

Every step detail page shows an "Inputs and Outputs" card in the right column. This card
merges two sources of step I/O into a unified view:

- **Catalog entries** — defined in the validator's `ValidatorConfig.catalog_entries` and synced
  to the database. These represent values the validator produces (outputs) or consumes
  (inputs). Source badge: "Catalog".
- **Template variables** — discovered from uploaded template files (e.g. `$U_FACTOR` in an
  EnergyPlus IDF). Stored as step-owned `StepIODefinition` rows. Source badge: "Template".

The card has two tabs when both inputs and outputs exist:

- **Step Inputs** — catalog INPUT entries + template variables, merged in order.
  Template-source inputs have an Edit button (pencil icon) that opens a per-variable
  annotation modal (`SingleTemplateVariableForm`).
- **Step Outputs** — catalog OUTPUT entries, each with a "show to user" indicator
  based on the step's `display_step_outputs` settings.

The `build_step_io_context()` helper in `views_helpers.py` builds this merged representation
at the view layer. No database model changes are needed — it's purely a presentation concern.

### Concrete example: EnergyPlus config

Here's a condensed look at the EnergyPlus config (`validators/energyplus/config.py`) to show
how all these pieces fit together:

```python
from validibot.validations.validators.base.config import (
    CatalogEntrySpec,
    ValidatorConfig,
)

config = ValidatorConfig(
    slug="energyplus-idf-validator",
    name="EnergyPlus Validator",
    validation_type=ValidationType.ENERGYPLUS,
    validator_class=(
        "validibot.validations.validators.energyplus.validator.EnergyPlusValidator"
    ),
    compute_tier=ComputeTier.HIGH,
    supports_assertions=True,
    catalog_entries=[
        CatalogEntrySpec(
            slug="site_electricity_kwh",
            label="Site Electricity (kWh)",
            entry_type="io_definition",
            run_stage="output",
            data_type="number",
            binding_config={"source": "metric", "key": "site_electricity_kwh"},
        ),
        CatalogEntrySpec(
            slug="total_unmet_hours",
            label="Total Unmet Hours",
            entry_type="derivation",
            run_stage="output",
            data_type="number",
            binding_config={
                "expr": "unmet_heating_hours + unmet_cooling_hours",
            },
        ),
        # ... more step I/O definitions and derivations ...
    ],
    # Template variable editing is handled by the Inputs and Outputs card,
    # not by step_editor_cards.
)
```

When a workflow step uses this validator with a parameterized IDF template, template variables
appear as step inputs in the unified card, alongside any catalog INPUT entries. Authors can
edit each variable's annotations (label, default, type, constraints) via a per-variable modal.

## Validator lifecycle

1. **Discovery** — `register_validators()` runs inside `ValidationsConfig.ready()` at application
   startup. It calls `discover_configs()` to find package-based validators (those with a
   `config.py` module) and loads `BUILTIN_CONFIGS` for single-file validators. Both the config
   registry and the validator class registry are populated in a single pass.

2. **DB sync** — the `sync_validators` management command reads from the config registry and
   creates or updates `Validator` rows and their `StepIODefinition` and `Derivation` rows in
   the database.
   This runs at container startup and is idempotent. Custom validators are created separately
   through the Validator Library UI.

3. **Selection** — workflow steps reference a `Validator` via FK. The step editor UI uses
   the config registry to display available validators with their metadata, icons, and
   supported file types.

4. **Step editor resolution** — when the step detail page loads, it reads `step_editor_cards`
   from the validator's config, evaluates each card's `condition` callable, instantiates the
   `form_class` if provided, and renders the card template into the right column. This is how
   validators inject custom UI (like EnergyPlus's "Template Variables" card) without modifying
   the core step detail view.

5. **Execution** — the runtime calls `get_validator_class(validation_type)` to retrieve the validator
   class, instantiates it, and runs validation. The provider optionally instruments the uploaded
   artifact, binds helper closures, and the validator evaluates derivations followed by
   assertions. Findings are emitted with references back to the validator and catalog snapshot
   for auditability.

See [Assertions](assertions.md) for how the validator catalog is consumed by rulesets, and
how findings reference these slugs.
