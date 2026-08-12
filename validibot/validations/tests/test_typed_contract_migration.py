"""Acceptance tests for the typed validator input-contract data migration.

The migration is the cutover from validator-wide format summaries to explicit
input ports and per-step bindings.  These focused historical-model substitutes
pin both the forward JSON Schema contract and the summaries needed by a
transactional rollback without moving the test database between migrations.
"""

from __future__ import annotations

import importlib
from types import SimpleNamespace


class _QuerySet(list):
    """Provide the small historical QuerySet surface used by the migration."""

    def iterator(self):
        """Yield retained rows as Django's iterator would."""

        return iter(self)

    def exists(self):
        """Report whether a historical filtering operation found a row."""

        return bool(self)


class _Manager:
    """Store simple historical rows with filter and create operations."""

    def __init__(self, rows=None):
        self.rows = list(rows or [])

    def all(self):
        """Return all retained rows through the migration QuerySet surface."""

        return _QuerySet(self.rows)

    def filter(self, **lookups):
        """Match direct historical fields used by the migration."""

        return _QuerySet(
            row
            for row in self.rows
            if all(getattr(row, key) == value for key, value in lookups.items())
        )

    def create(self, **values):
        """Record a row created by the forward migration."""

        row = SimpleNamespace(**values)
        self.rows.append(row)
        return row


class _PortManager(_Manager):
    """Implement idempotent port creation for historical I/O definitions."""

    def get_or_create(self, *, defaults, **lookups):
        """Return an existing contract port or build it from migration defaults."""

        existing = self.filter(**lookups)
        if existing:
            return existing[0], False
        row = SimpleNamespace(
            **lookups,
            **defaults,
            metadata={},
            save=lambda **_kwargs: None,
        )
        self.rows.append(row)
        return row, True


class _Apps:
    """Resolve fake historical model classes by app and model name."""

    def __init__(self, models):
        self.models = models

    def get_model(self, app_label, model_name):
        """Return the model substitute requested by the migration."""

        return self.models[(app_label, model_name)]


def _model(manager):
    """Create a historical model substitute exposing one manager."""

    return SimpleNamespace(objects=manager)


def test_forward_migration_creates_json_schema_metadata_contract_and_binding():
    """Forward cutover must make whole submission metadata an explicit source."""

    migration = importlib.import_module(
        "validibot.validations.migrations.0042_typed_validator_input_contracts"
    )
    validator = SimpleNamespace(
        validation_type="JSON_SCHEMA",
        slug="json-schema",
        version=1,
        name="JSON Schema Validator",
        supported_data_formats=["json"],
    )
    step = SimpleNamespace(validator=validator)
    ports = _PortManager()
    bindings = _Manager()
    apps = _Apps(
        {
            ("validations", "Validator"): _model(_Manager([validator])),
            ("validations", "StepIODefinition"): _model(ports),
            ("validations", "StepInputBinding"): _model(bindings),
            ("workflows", "WorkflowStep"): _model(_Manager([step])),
        }
    )

    migration.populate_typed_contracts(apps, schema_editor=None)

    assert len(ports.rows) == 1
    port = ports.rows[0]
    assert port.contract_key == "json_document"
    assert port.accepted_data_formats == ["json"]
    assert port.accepted_file_types == ["json"]
    assert port.allowed_source_scopes == [
        "submission_file",
        "upstream_artifact",
        "submission_metadata",
    ]
    assert len(bindings.rows) == 1
    assert bindings.rows[0].source_scope == "submission_file"
    assert bindings.rows[0].source_data_path == "primary"


def test_reverse_migration_restores_deduplicated_validator_summaries():
    """Rollback must reconstruct legacy summaries from all typed input ports."""

    migration = importlib.import_module(
        "validibot.validations.migrations.0042_typed_validator_input_contracts"
    )
    saved_fields = []
    validator = SimpleNamespace(
        supported_file_types=[],
        supported_data_formats=[],
        save=lambda **kwargs: saved_fields.extend(kwargs["update_fields"]),
    )
    ports = _Manager(
        [
            SimpleNamespace(
                validator=validator,
                direction="input",
                io_medium="artifact",
                accepted_file_types=["json"],
                accepted_data_formats=["json"],
            ),
            SimpleNamespace(
                validator=validator,
                direction="input",
                io_medium="artifact",
                accepted_file_types=["json", "text"],
                accepted_data_formats=["json", "text"],
            ),
        ]
    )
    apps = _Apps(
        {
            ("validations", "Validator"): _model(_Manager([validator])),
            ("validations", "StepIODefinition"): _model(ports),
        }
    )

    migration.restore_validator_type_summaries(apps, schema_editor=None)

    assert validator.supported_file_types == ["json", "text"]
    assert validator.supported_data_formats == ["json", "text"]
    assert saved_fields == ["supported_file_types", "supported_data_formats"]
