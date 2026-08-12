"""FMU service test suite.

Exercises the upload (``create_fmu_validator``) and probe
(``run_fmu_probe``) flows that introspect a real FMU's
modelDescription.xml and seed StepIODefinition rows on the resulting
validator. Tests run against a real Feedthrough FMU fixture so the
parsing layer (services/fmu.py + validibot_shared/fmu/models.py) is
exercised end-to-end, not just mocked.

Phase 6 (ADR-2026-05-22b) added parser-fact StepIODefinitions alongside
the per-variable I/O definitions. Every user-created FMU validator now carries
seven INPUT-direction rows (model_name, fmi_version, variable_count,
input_variable_count, output_variable_count, parameter_count,
has_simulation_defaults) so the ``i.*`` namespace is populated
identically whether a workflow step is bound to the system FMU
validator or a user-created one. ADR-2026-07-06 adds one more
INPUT-direction artifact port, ``fmu_model``, for the FMU file itself.
"""

from __future__ import annotations

from pathlib import Path

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase

from validibot.projects.tests.factories import ProjectFactory
from validibot.users.tests.factories import OrganizationFactory
from validibot.validations.constants import BindingSourceScope
from validibot.validations.constants import CatalogValueType
from validibot.validations.constants import StepIODirection
from validibot.validations.constants import StepIOMedium
from validibot.validations.constants import StepIOOriginKind
from validibot.validations.constants import StepIOSourceKind
from validibot.validations.constants import ValidationType
from validibot.validations.services.fmu import create_fmu_validator
from validibot.validations.services.fmu import run_fmu_probe

# Parser-fact contract keys seeded by ``_seed_parser_fact_io_definitions``
# (per Phase 6, post May-2026 review). Tests use this constant rather
# than re-deriving from ``services.fmu.PARSER_FACT_KEYS`` so a desync
# between this test fixture and the production specs surfaces as a
# test failure (rather than silently passing in lockstep with a buggy
# refactor). Last reviewed: 2026-05-24, after the
# has_default_experiment → has_simulation_defaults rename
# (P3 finding).
PARSER_FACT_KEYS = {
    "model_name",
    "fmi_version",
    "variable_count",
    "input_variable_count",
    "output_variable_count",
    "parameter_count",
    "has_simulation_defaults",
}


def _make_fake_fmu(name: str = "demo") -> SimpleUploadedFile:
    """Load the canned Feedthrough FMU from test assets."""

    asset = (
        Path(__file__).resolve().parents[3]
        / "tests"
        / "assets"
        / "fmu"
        / "Feedthrough.fmu"
    )
    payload = asset.read_bytes()
    return SimpleUploadedFile(
        asset.name, payload, content_type="application/octet-stream"
    )


class FMUServiceTests(TestCase):
    """Exercises FMU creation and probe flows for FMU validators."""

    def setUp(self):
        self.org = OrganizationFactory()
        self.project = ProjectFactory(org=self.org)

    def test_create_fmu_validator_introspects_and_seeds_catalog(self):
        upload = _make_fake_fmu()

        validator = create_fmu_validator(
            org=self.org,
            project=self.project,
            name="Test FMU",
            upload=upload,
        )

        self.assertEqual(validator.validation_type, ValidationType.FMU)
        # Feedthrough FMU declares 4 inputs and 4 outputs in
        # modelDescription.xml. Per Phase 6 we additionally seed seven
        # parser-fact step inputs (model_name, fmi_version, etc.) on
        # every user-created FMU validator so the i.* namespace matches
        # the system FMU validator catalog regardless of binding. The
        # fmu_model artifact input port is an additional INPUT row.
        self.assertEqual(
            validator.step_io_definitions.filter(
                direction=StepIODirection.INPUT
            ).count(),
            4 + len(PARSER_FACT_KEYS) + 1,
        )
        self.assertEqual(
            validator.step_io_definitions.filter(
                direction=StepIODirection.OUTPUT
            ).count(),
            4,
        )
        fmu_model = validator.fmu_model
        self.assertIsNotNone(fmu_model)
        # The Feedthrough FMU defines 11 variables (inputs, outputs, parameters).
        self.assertEqual(fmu_model.variables.count(), 11)
        self.assertTrue(fmu_model.is_approved)
        self.assertTrue(fmu_model.file.name)
        self.assertTrue(fmu_model.file.storage.exists(fmu_model.file.name))
        self.assertEqual(fmu_model.gcs_uri, "")

        fmu_port = validator.step_io_definitions.get(
            contract_key="fmu_model",
            direction=StepIODirection.INPUT,
        )
        self.assertEqual(fmu_port.origin_kind, StepIOOriginKind.CATALOG)
        self.assertEqual(fmu_port.data_type, CatalogValueType.ARTIFACT_REF)
        self.assertEqual(fmu_port.io_medium, StepIOMedium.ARTIFACT)
        self.assertEqual(fmu_port.role, "fmu")
        self.assertEqual(fmu_port.accepted_data_formats, ["fmu"])
        self.assertEqual(fmu_port.accepted_media_types, ["application/vnd.fmi.fmu"])
        self.assertEqual(fmu_port.accepted_extensions, ["fmu"])
        self.assertIn(BindingSourceScope.SYSTEM, fmu_port.allowed_source_scopes)

    def test_run_fmu_probe_refreshes_variables(self):
        """Probe parses modelDescription.xml in-process and refreshes catalog."""
        upload = _make_fake_fmu()
        validator = create_fmu_validator(
            org=self.org,
            project=self.project,
            name="Test FMU",
            upload=upload,
        )
        fmu_model = validator.fmu_model
        self.assertIsNotNone(fmu_model)

        result = run_fmu_probe(fmu_model)

        # Probe should succeed since parsing happens in-process
        self.assertEqual(result.status, "success")
        # Feedthrough FMU declares 11 variables
        self.assertEqual(len(result.variables), 11)
        self.assertIsNotNone(result.execution_seconds)
        self.assertGreater(result.execution_seconds, 0)

        # Verify FMU model was updated
        fmu_model.refresh_from_db()
        self.assertTrue(fmu_model.is_approved)

        # Step I/O definitions should still match: 4 FMU inputs +
        # ``len(PARSER_FACT_KEYS)`` parser facts + fmu_model port +
        # 4 FMU outputs.
        # ``_refresh_variables_from_probe`` reconciles in-place via
        # ``_persist_variables`` (update_or_create on the
        # (validator, contract_key, direction) tuple), so surviving
        # rows keep their PK across probes — identity-stable, no
        # cascade of downstream StepInputBinding / WorkflowStepIOPromotion /
        # RulesetAssertion FKs.
        self.assertEqual(
            validator.step_io_definitions.filter(
                direction=StepIODirection.INPUT
            ).count(),
            4 + len(PARSER_FACT_KEYS) + 1,
        )
        self.assertEqual(
            validator.step_io_definitions.filter(
                direction=StepIODirection.OUTPUT
            ).count(),
            4,
        )

    # ── Phase 6 (ADR-2026-05-22b) parser-fact coverage ───────────────────────
    #
    # These tests pin the three-way alignment between
    # build_introspection_metadata (services/fmu.py), the parser-fact
    # specs seeded on user FMU validators, and FMUValidator.extract_input_values
    # (validators/fmu/validator.py). Drift in any one of those three
    # places without matching updates in the others would silently
    # break input-stage assertions, so the tests run against a real
    # FMU fixture rather than mocks.

    def test_introspection_metadata_includes_phase6_parser_facts(self):
        """``build_introspection_metadata`` stamps the seven Phase 6 keys.

        At upload time the FMU's modelDescription.xml is parsed once
        and the facts persisted on ``FMUModel.introspection_metadata``.
        Doing it at upload rather than on every validation run avoids
        re-parsing a ~MB-scale FMU zip per validation — the runtime
        hook (``FMUValidator.extract_input_values``) just reads the
        stamped dict.
        """
        upload = _make_fake_fmu()
        validator = create_fmu_validator(
            org=self.org,
            project=self.project,
            name="Test FMU",
            upload=upload,
        )

        metadata = validator.fmu_model.introspection_metadata
        # Each Phase 6 key must be present, otherwise extract_input_values
        # would silently return None for that fact at runtime.
        for key in PARSER_FACT_KEYS:
            self.assertIn(key, metadata)
        # Spot-check shape: counts are ints, version is a string,
        # has_simulation_defaults is bool. We don't pin exact values
        # because they depend on the Feedthrough fixture; this just
        # guards against type drift.
        self.assertIsInstance(metadata["variable_count"], int)
        self.assertEqual(metadata["variable_count"], 11)
        self.assertIsInstance(metadata["fmi_version"], str)
        self.assertIsInstance(metadata["has_simulation_defaults"], bool)
        # Feedthrough declares 4 inputs and 4 outputs (matches the
        # per-variable I/O counts above).
        self.assertEqual(metadata["input_variable_count"], 4)
        self.assertEqual(metadata["output_variable_count"], 4)

    def test_parser_fact_step_io_definitions_match_specs(self):
        """Seven INPUT parser-fact StepIODefinitions land per FMU validator.

        The rows must be INTERNAL source (parser-extracted, no
        author-supplied path), is_path_editable=False (the value is
        derived, not bound), and contract_keys must match
        ``PARSER_FACT_KEYS`` exactly — drift would mean
        ``i.fmi_version`` etc. resolve in some validators but not
        others, the exact mental-model bug Phase 6 set out to fix.
        """
        upload = _make_fake_fmu()
        validator = create_fmu_validator(
            org=self.org,
            project=self.project,
            name="Test FMU",
            upload=upload,
        )

        parser_definitions = validator.step_io_definitions.filter(
            direction=StepIODirection.INPUT,
            origin_kind=StepIOOriginKind.FMU,
            source_kind=StepIOSourceKind.INTERNAL,
        )
        seeded_keys = set(
            parser_definitions.values_list("contract_key", flat=True),
        )
        self.assertEqual(seeded_keys, PARSER_FACT_KEYS)
        for io_definition in parser_definitions:
            self.assertFalse(
                io_definition.is_path_editable,
                f"{io_definition.contract_key} must not be path-editable "
                "(values are parser-derived, not author-bound).",
            )

    def test_extract_input_values_reads_from_introspection_metadata(self):
        """``FMUValidator.extract_input_values`` returns the stamped dict.

        At runtime, the parser-fact hook reads from
        ``self.run_context.step.validator.fmu_model.introspection_metadata``
        — it never re-parses the FMU zip. This is the contract that
        makes the pattern cheap enough to run on every validation
        without I/O cost.

        We assert the dict matches what ``build_introspection_metadata``
        stamped at upload time. Anything else would break the i.*
        namespace at runtime.
        """
        from unittest.mock import MagicMock

        from validibot.validations.validators.fmu.validator import FMUValidator

        upload = _make_fake_fmu()
        validator = create_fmu_validator(
            org=self.org,
            project=self.project,
            name="Test FMU",
            upload=upload,
        )

        # Build a minimal RunContext substitute pointing at the
        # validator we just created. The hook only walks
        # run_context → step → validator → fmu_model → introspection_metadata
        # so mock objects are sufficient — no need for a full ValidationRun.
        step = MagicMock(validator=validator)
        run_context = MagicMock(step=step)

        engine = FMUValidator()
        engine.run_context = run_context
        facts = engine.extract_input_values(payload={"unused": True})

        self.assertIsNotNone(facts)
        self.assertEqual(facts, validator.fmu_model.introspection_metadata)
        # Defensive copy invariant: mutating the returned dict must
        # not leak into the persisted metadata (the hook returns
        # ``dict(metadata)`` rather than the bare reference).
        facts["model_name"] = "MUTATED"
        self.assertNotEqual(
            validator.fmu_model.introspection_metadata.get("model_name"),
            "MUTATED",
        )

    def test_extract_input_values_returns_none_without_run_context(self):
        """Hook returns None when there's no run context to walk.

        The system FMU validator has no FMU bound (its catalog comes
        from config.py via sync_validators) — and at runtime, edge
        cases like sync validators or unit tests that construct the
        validator directly won't supply a run context. Returning None
        in those cases lets the catalog's on_missing="null" policy
        keep i.* empty rather than raising.
        """
        from validibot.validations.validators.fmu.validator import FMUValidator

        engine = FMUValidator()
        # Don't set run_context — simulates direct construction.
        self.assertIsNone(engine.extract_input_values(payload={}))

    def test_extract_input_values_uses_step_config_for_step_level_uploads(self):
        """Step-level FMU uploads resolve i.* via step.config['fmu_introspection'].

        Per the May 2026 P1 finding: the primary product path is the
        system FMU validator + step-level FMU upload, where the FMU
        does NOT live on ``Validator.fmu_model``. Without the
        step.config fallback, ``i.fmi_version`` etc. would always
        resolve to null on the most common path, defeating the entire
        Phase 6 parser-fact contract for the use case it most needed
        to support.

        This test pins the precedence order: when both the step
        config AND a validator FMU model exist, step.config wins
        (most-specific takes precedence over fallback).
        """
        from unittest.mock import MagicMock

        from validibot.validations.validators.fmu.validator import FMUValidator

        # Step-config introspection dict (primary path).
        step_metadata = {
            "model_name": "StepLevel",
            "fmi_version": "2.0",
            "variable_count": 3,
            "input_variable_count": 2,
            "output_variable_count": 1,
            "parameter_count": 0,
            "has_simulation_defaults": True,
        }
        step = MagicMock()
        step.config = {"fmu_introspection": step_metadata}
        # Validator FK present but should NOT be consulted because
        # the step.config path wins.
        step.validator = None  # cleanly out of the path
        run_context = MagicMock(step=step)

        engine = FMUValidator()
        engine.run_context = run_context
        facts = engine.extract_input_values(payload={"unused": True})

        self.assertEqual(facts, step_metadata)

    def test_run_fmu_probe_preserves_step_io_definition_pk(self):
        """Probe re-run keeps the same StepIODefinition.pk for surviving rows.

        Identity stability is the whole point of the May 2026 P1/P2
        fix: ``_persist_variables`` uses ``update_or_create`` keyed on
        ``(validator, contract_key, direction)`` so a re-probe of the
        same FMU reuses existing rows. Without this, every probe would
        recreate the rows with fresh PKs, cascading any
        ``StepInputBinding``, ``WorkflowStepIOPromotion``, and
        ``RulesetAssertion`` FKs the author had built up.

        We probe twice and assert PK equality for both per-variable
        rows AND parser-fact rows — they both go through the same
        identity-stable code path.
        """
        upload = _make_fake_fmu()
        validator = create_fmu_validator(
            org=self.org,
            project=self.project,
            name="Test FMU",
            upload=upload,
        )

        # Snapshot row PKs after the initial seed.
        pre_pks = {
            (io_definition.contract_key, io_definition.direction): io_definition.pk
            for io_definition in validator.step_io_definitions.all()
        }
        # Sanity: both per-variable rows and parser-fact rows are in
        # the snapshot. If either set is empty something's wrong before
        # we even reach the probe.
        self.assertTrue(any(k in PARSER_FACT_KEYS for k, _ in pre_pks))
        self.assertTrue(any(k not in PARSER_FACT_KEYS for k, _ in pre_pks))

        # Re-probe the same FMU. Nothing has changed, so all rows
        # should reconcile in place via update_or_create.
        run_fmu_probe(validator.fmu_model)

        post_pks = {
            (io_definition.contract_key, io_definition.direction): io_definition.pk
            for io_definition in validator.step_io_definitions.all()
        }

        # Every (contract_key, direction) tuple that existed before
        # must still exist after, AND with the same PK. A regression
        # to delete-and-recreate would change every PK.
        self.assertEqual(set(pre_pks.keys()), set(post_pks.keys()))
        for key, pre_pk in pre_pks.items():
            self.assertEqual(
                pre_pk,
                post_pks[key],
                f"{key} PK changed across re-probe — identity-stable "
                "reconciliation regression",
            )

    def test_extract_input_values_filters_to_catalog_keys(self):
        """The hook drops keys not in PARSER_FACT_KEYS.

        Per the May 2026 P2/P3 finding: the catalog (config.py +
        PARSER_FACT_SPECS) is the contract for which i.* keys are
        public. Older metadata dicts (or future fields stamped
        before the catalog catches up) must not leak into the i.*
        namespace — EnergyPlus's output extractor enforces the same
        rule (extract_output_values filters to catalog OUTPUT keys).

        Without this filter, a metadata dict carrying a legacy or
        unrelated key would silently expose it as i.<key>, weakening
        the "catalog is the contract" invariant.
        """
        from unittest.mock import MagicMock

        from validibot.validations.validators.fmu.validator import FMUValidator

        # Mix valid parser-fact keys with a legacy field that isn't
        # part of the catalog. The legacy field MUST be filtered out.
        step = MagicMock()
        step.config = {
            "fmu_introspection": {
                "model_name": "OK",
                "fmi_version": "2.0",
                # Phase 5 / pre-Phase-6 legacy field; not in catalog.
                "legacy_descriptor": "should-be-dropped",
            },
        }
        step.validator = None
        run_context = MagicMock(step=step)

        engine = FMUValidator()
        engine.run_context = run_context
        facts = engine.extract_input_values(payload={})

        self.assertEqual(facts, {"model_name": "OK", "fmi_version": "2.0"})
        self.assertNotIn("legacy_descriptor", facts)
