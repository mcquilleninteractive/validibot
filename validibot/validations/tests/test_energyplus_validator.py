"""Integration tests for the EnergyPlus validator execution boundary.

The suite concentrates on behavior that must hold before expensive external
compute is launched: workflow context is mandatory, input assertions gate
dispatch, and the canonical resolved inputs are durably recorded first.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest
from django.db import DatabaseError
from validibot_shared.energyplus.models import EnergyPlusSimulationMetrics

from validibot.actions.protocols import RunContext
from validibot.submissions.constants import SubmissionFileType
from validibot.submissions.tests.factories import SubmissionFactory
from validibot.validations.constants import AssertionOperator
from validibot.validations.constants import AssertionType
from validibot.validations.constants import RulesetType
from validibot.validations.constants import Severity
from validibot.validations.constants import ValidationType
from validibot.validations.services.custom_validator_contracts import (
    sync_configured_io_contract,
)
from validibot.validations.services.execution.registry import clear_backend_cache
from validibot.validations.services.input_bindings import ensure_step_input_bindings
from validibot.validations.tests.factories import RulesetAssertionFactory
from validibot.validations.tests.factories import RulesetFactory
from validibot.validations.tests.factories import ValidationStepRunFactory
from validibot.validations.tests.factories import ValidatorFactory
from validibot.validations.validators.energyplus.validator import EnergyPlusValidator

pytestmark = pytest.mark.django_db


def _energyplus_ruleset():
    """Create a minimal EnergyPlus ruleset for testing.

    Note: weather_file is now stored in step.config, not ruleset.metadata.
    """
    return RulesetFactory(
        ruleset_type=RulesetType.ENERGYPLUS,
        rules_text="{}",
    )


def test_energyplus_validator_requires_run_context():
    """
    Test that the EnergyPlus validator returns error when run_context is not provided.

    The validator requires run_context with validation_run and step to be passed
    to validate(). This is normally done by the handler.
    """
    validator = ValidatorFactory(validation_type=ValidationType.ENERGYPLUS)
    ruleset = _energyplus_ruleset()
    submission = SubmissionFactory(content='{"Building": "Demo"}')

    engine = EnergyPlusValidator(config={})

    # Don't pass run_context - should fail
    result = engine.validate(
        validator=validator,
        submission=submission,
        ruleset=ruleset,
        run_context=None,
    )

    assert result.passed is False
    assert any(
        "workflow context" in issue.message.lower() and issue.severity == Severity.ERROR
        for issue in result.issues
    )
    assert result.stats is not None
    assert result.stats["implementation_status"] == "Missing run_context"


def test_energyplus_validator_backend_not_available():
    """
    Test that the EnergyPlus validator returns error when backend unavailable.

    When run_context is provided but the execution backend is not available,
    the validator should return a helpful error.
    """
    validator = ValidatorFactory(validation_type=ValidationType.ENERGYPLUS)
    ruleset = _energyplus_ruleset()
    submission = SubmissionFactory(content='{"Building": "Demo"}')

    engine = EnergyPlusValidator(config={})

    # Create run_context with mock objects
    run_context = RunContext(
        validation_run=MagicMock(id=1),
        step=MagicMock(id=1),
        upstream_steps={},
    )

    # Mock the backend to be unavailable
    clear_backend_cache()
    with patch(
        "validibot.validations.services.execution.get_execution_backend"
    ) as mock_get_backend:
        mock_backend = MagicMock()
        mock_backend.is_available.return_value = False
        mock_backend.backend_name = "MockBackend"
        mock_get_backend.return_value = mock_backend

        result = engine.validate(
            validator=validator,
            submission=submission,
            ruleset=ruleset,
            run_context=run_context,
        )

    assert result.passed is False
    assert any(
        "not available" in issue.message.lower() and issue.severity == Severity.ERROR
        for issue in result.issues
    )
    assert result.stats is not None
    assert result.stats["implementation_status"] == "Backend not available"


# ── End-to-end: input-stage assertion gates dispatch (ADR-2026-05-22) ─


class TestInputStageAssertionGating:
    """Verifies the headline promise of ADR-2026-05-22.

    Input-stage assertions on EnergyPlus steps must be evaluated against
    parser-extracted facts (``i.*`` namespace) BEFORE the container is
    dispatched. If an ERROR-severity input-stage assertion fails, the
    container must not run — saving the compute cost of a simulation
    that would discover the same problem via post-processing.

    These tests anchor the whole feature: a passing parser + passing
    assertion = dispatch happens; a failing assertion = dispatch is
    skipped, with the assertion failure surfacing as the run's findings.

    Without these tests, the parser unit tests and the CEL context
    namespace tests would pass while the feature was actually broken at
    the runtime integration layer (which is exactly the gap the
    May 2026 code review surfaced).
    """

    def _build_fixture(self, cel_expr: str):
        """Set up the full Django object graph for one gating test.

        Returns (engine, validator, submission, ruleset, run_context).

        We use real factories rather than MagicMocks because
        ``_build_cel_context`` issues Django ORM queries against the
        step's primary key (StepInputBinding, StepIODefinition,
        promoted signals). MagicMocks don't have working pks and the
        ORM rejects them at query-build time — exactly the kind of
        failure that surfaced when I first wrote these tests with
        MagicMocks.
        """
        from validibot.validations.tests.factories import ValidationRunFactory
        from validibot.workflows.tests.factories import WorkflowStepFactory

        validator = ValidatorFactory(
            validation_type=ValidationType.ENERGYPLUS,
        )
        sync_configured_io_contract(validator=validator)
        ruleset = _energyplus_ruleset()
        # CEL assertion with the expression in rhs["expr"] (this is the
        # storage convention CEL assertions follow per the evaluator at
        # cel.py:71). Empty target_data_path is fine — the CEL evaluator
        # doesn't use target_data_path.
        RulesetAssertionFactory(
            ruleset=ruleset,
            assertion_type=AssertionType.CEL_EXPRESSION,
            operator=AssertionOperator.EQ,
            target_data_path="",
            severity=Severity.ERROR,
            rhs={"expr": cel_expr},
            message_template="Input-stage assertion failed: " + cel_expr,
        )
        # IDF with Version + Building + zero Zone objects. The parser
        # extracts idf_version="25.1", north_axis_deg=0.0, zone_count=0.
        idf_text = "Version, 25.1;\nBuilding, EmptyBuilding, 0;"
        step = WorkflowStepFactory(validator=validator)
        ensure_step_input_bindings(step)
        submission = SubmissionFactory(
            content=idf_text,
            workflow=step.workflow,
            file_type=SubmissionFileType.TEXT,
            original_filename="model.idf",
        )
        run = ValidationRunFactory(workflow=step.workflow, submission=submission)
        ValidationStepRunFactory(
            validation_run=run,
            workflow_step=step,
            step_order=step.order,
        )
        run_context = RunContext(
            validation_run=run,
            step=step,
            upstream_steps={},
        )
        engine = EnergyPlusValidator(config={})
        return engine, validator, submission, ruleset, run_context

    def _mock_backend(self):
        """Mock backend that's available and returns a successful response.

        If the input-stage gate works, this won't be called for the
        failing-assertion test. If the gate is broken, this completes
        successfully so the test failure clearly indicates "gate didn't
        fire" rather than "gate fired but dispatch errored downstream."
        """
        from validibot.validations.services.execution.base import ExecutionResponse

        mock_backend = MagicMock()
        mock_backend.is_available.return_value = True
        mock_backend.is_async = False
        mock_backend.backend_name = "MockBackend"
        mock_backend.execute.return_value = ExecutionResponse(
            execution_id="test-exec-1",
            is_complete=True,
            output_envelope=None,
            error_message=None,
        )
        return mock_backend

    def test_passing_input_assertion_allows_dispatch(self):
        """Why this matters: confirms the gate isn't over-eager.

        When all input-stage assertions pass, validate() must proceed
        to dispatch the container. Otherwise we'd block legitimate
        runs and break the existing EnergyPlus user flow.
        """
        engine, validator, submission, ruleset, run_context = self._build_fixture(
            cel_expr="i.zone_count >= 0",
        )
        clear_backend_cache()
        with patch(
            "validibot.validations.services.execution.get_execution_backend"
        ) as mock_get_backend:
            mock_backend = self._mock_backend()
            mock_get_backend.return_value = mock_backend

            result = engine.validate(
                validator=validator,
                submission=submission,
                ruleset=ruleset,
                run_context=run_context,
            )

            issue_summary = [
                (i.severity, (i.message or "")[:140]) for i in (result.issues or [])
            ]
            assert mock_backend.execute.called, (
                "Container dispatch was skipped despite all input-stage "
                "assertions passing. The gate is over-eager — it must "
                "only block dispatch when an ERROR-severity input-stage "
                "assertion fails. Issues seen: " + repr(issue_summary)
            )

        step_run = run_context.validation_run.step_runs.get(
            workflow_step=run_context.step,
        )
        assert step_run.input_values == {
            "idf_version": "25.1",
            "building_name": "EmptyBuilding",
            "north_axis_deg": 0.0,
            "terrain": "Suburbs",
            "solar_distribution": "FullExterior",
            "zone_count": 0,
            "surface_count": 0,
            "window_count": 0,
            "construction_count": 0,
            "run_period_count": 0,
            "has_hvac": False,
        }

    def test_input_persistence_failure_blocks_dispatch(self):
        """A failed canonical-input write must stop external execution.

        A callback can only be interpreted correctly when the exact inputs
        used for dispatch are durable. Continuing after a database failure
        would create accepted provider work with incomplete local state.
        """
        engine, validator, submission, ruleset, run_context = self._build_fixture(
            cel_expr="i.zone_count >= 0",
        )
        clear_backend_cache()
        with (
            patch(
                "validibot.validations.services.execution.get_execution_backend",
            ) as mock_get_backend,
            patch(
                "validibot.validations.models.ValidationStepRun.save",
                side_effect=DatabaseError("database unavailable"),
            ),
        ):
            mock_backend = self._mock_backend()
            mock_get_backend.return_value = mock_backend

            with pytest.raises(DatabaseError, match="database unavailable"):
                engine.validate(
                    validator=validator,
                    submission=submission,
                    ruleset=ruleset,
                    run_context=run_context,
                )

        mock_backend.execute.assert_not_called()

    def test_failing_input_assertion_blocks_dispatch(self):
        """Why this matters: this is the headline feature.

        When an ERROR-severity input-stage assertion fails on a parser
        fact, the validator must NOT dispatch the container. The whole
        point is to catch the problem before paying for simulation.

        Without this gate, the feature is purely cosmetic: the i.*
        namespace exists, the autocomplete shows i.zone_count, the
        author writes an assertion against it — but the assertion
        never fires before the (expensive, slow) simulation runs.
        """
        engine, validator, submission, ruleset, run_context = self._build_fixture(
            cel_expr="i.zone_count >= 1",
        )
        clear_backend_cache()
        with patch(
            "validibot.validations.services.execution.get_execution_backend"
        ) as mock_get_backend:
            mock_backend = self._mock_backend()
            mock_get_backend.return_value = mock_backend

            result = engine.validate(
                validator=validator,
                submission=submission,
                ruleset=ruleset,
                run_context=run_context,
            )

            assert not mock_backend.execute.called, (
                "Container dispatch happened despite a failing "
                "ERROR-severity input-stage assertion. The whole point "
                "of i.* assertions is to gate dispatch on parser facts; "
                "if the container runs anyway, the feature is broken."
            )

        # The result should carry the failure findings so the run
        # report tells the author what went wrong.
        assert result.passed is False
        assert any(issue.severity == Severity.ERROR for issue in (result.issues or []))
        # The stats should mark dispatch as deliberately skipped (vs.
        # an error that prevented dispatch) so debugging is unambiguous.
        assert result.stats is not None
        assert result.stats.get("dispatch_skipped") == "input_stage_assertion_failed"

    def test_file_backed_submission_parser_reads_input_file(self):
        """Why this matters: file-backed submissions store payload in
        ``Submission.input_file``, not ``Submission.content``. The
        input-stage parser must use ``submission.get_content()`` (which
        reads from either field), NOT ``submission.content`` directly
        (which would return empty for file uploads).

        Without this fix, the most common production case (uploading an
        IDF file via the launch form) would parse an empty payload and
        produce wrong facts — e.g., zone_count=0 for an IDF that
        actually has zones — silently failing or passing the input-
        stage gate based on whichever assertion the author wrote.

        This is a regression test for the May 2026 code review's P1
        finding.
        """
        from django.core.files.base import ContentFile

        engine, validator, submission, ruleset, run_context = self._build_fixture(
            cel_expr="i.zone_count >= 1",  # would fail if payload parsed empty
        )

        # Re-shape the submission to be file-backed:
        #   content="" (the default for file-uploaded submissions)
        #   input_file=<IDF with three zones> (where the real payload lives)
        # The fixture's default IDF has zero zones; this overrides with
        # an IDF that has three zones so the assertion i.zone_count >= 1
        # PASSES — proving the parser actually read the file.
        idf_with_three_zones = (
            "Version, 25.1;\n"
            "Building, FileUpload, 0;\n"
            "Zone, ZoneOne;\n"
            "Zone, ZoneTwo;\n"
            "Zone, ZoneThree;\n"
        )
        submission.content = ""
        submission.input_file.save(
            "test.idf",
            ContentFile(idf_with_three_zones.encode("utf-8")),
            save=True,
        )

        clear_backend_cache()
        with patch(
            "validibot.validations.services.execution.get_execution_backend"
        ) as mock_get_backend:
            mock_backend = self._mock_backend()
            mock_get_backend.return_value = mock_backend

            engine.validate(
                validator=validator,
                submission=submission,
                ruleset=ruleset,
                run_context=run_context,
            )

            # If the parser correctly read input_file via get_content(),
            # zone_count is 3 and the assertion passes — dispatch
            # proceeds. If the parser read submission.content directly
            # (the bug), zone_count is 0, the assertion fails, and
            # dispatch is blocked.
            assert mock_backend.execute.called, (
                "File-backed submission parser failed to read input_file. "
                "Check that _resolve_input_stage_payload() uses "
                "submission.get_content() rather than submission.content."
            )

    def test_output_filter_scopes_to_step_validator_version(self):
        """Why this matters: prevents catalog-version drift from silently
        dropping or admitting outputs.

        ``_filter_to_catalog_outputs`` decides which keys from the
        EnergyPlus envelope land in ``o.*``. In production, multiple
        validator revisions co-exist briefly during a rollout — revision 1 still
        bound to some steps, revision 2 the current default. The May 2026
        review found that the original implementation used
        ``Validator.objects.filter(...).first()`` and was therefore
        non-deterministic: it could pick the wrong version and either
        admit retired outputs or silence current ones.

        This test pins the corrected behaviour: when a step's
        validator FK points at revision 1 (which only declares
        ``simulated_conditioned_area_m2`` as an OUTPUT), the filter
        must use that catalog — even if a newer validator row (revision 2,
        declaring ``site_eui_kwh_m2``) also exists in the database.
        Without the run-context scoping fix, the filter would pick
        revision 2 (or some other arbitrary row) and drop the legitimate
        ``simulated_conditioned_area_m2`` value.
        """
        from validibot.validations.constants import StepIODirection
        from validibot.validations.models import StepIODefinition
        from validibot.validations.tests.factories import ValidationRunFactory
        from validibot.workflows.tests.factories import WorkflowStepFactory

        # Two co-existing catalog versions. The step is bound to v1;
        # the filter must honour that binding even though v2 is the
        # newer row in the DB. The exact revision contents are
        # fictional for test purposes — both current versions would actually declare
        # both fields in the real catalog; here we contrive a
        # difference so the filtering machinery has something to
        # disagree about.)
        validator_v1_0 = ValidatorFactory(
            validation_type=ValidationType.ENERGYPLUS,
            version=1,
        )
        StepIODefinition.objects.create(
            validator=validator_v1_0,
            contract_key="simulated_conditioned_area_m2",
            native_name="simulated_conditioned_area_m2",
            direction=StepIODirection.OUTPUT,
            data_type="number",
            label="Simulated Conditioned Area",
        )
        validator_v1_1 = ValidatorFactory(
            validation_type=ValidationType.ENERGYPLUS,
            version=2,
        )
        StepIODefinition.objects.create(
            validator=validator_v1_1,
            contract_key="site_eui_kwh_m2",
            native_name="site_eui_kwh_m2",
            direction=StepIODirection.OUTPUT,
            data_type="number",
            label="Site EUI",
        )

        step = WorkflowStepFactory(validator=validator_v1_0)
        run = ValidationRunFactory(workflow=step.workflow)
        run_context = RunContext(
            validation_run=run,
            step=step,
            upstream_steps={},
        )

        engine = EnergyPlusValidator(config={})
        engine.run_context = run_context

        # Raw envelope contains BOTH keys. The filter should keep only
        # the one declared by revision 1 (this step's validator).
        raw_metrics = {
            "simulated_conditioned_area_m2": 250.0,
            "site_eui_kwh_m2": 87.5,
        }
        filtered = engine._filter_to_catalog_outputs(raw_metrics)
        assert filtered == {"simulated_conditioned_area_m2": 250.0}, (
            "Output filter ignored the step's bound validator version. "
            "Revision 1 of the catalog only declares "
            "simulated_conditioned_area_m2, but the filter included "
            "site_eui_kwh_m2 (declared by revision 2). Check "
            "_resolve_catalog_validator() — it must read "
            "self.run_context.step.validator, not pick an arbitrary "
            "row."
        )

    def test_output_extraction_preserves_null_metrics_and_review_evidence(self):
        """Declared unavailable metrics and execution facts must remain in o.*."""
        from validibot.validations.constants import StepIODirection
        from validibot.validations.models import StepIODefinition
        from validibot.validations.tests.factories import ValidationRunFactory
        from validibot.workflows.tests.factories import WorkflowStepFactory

        validator = ValidatorFactory(validation_type=ValidationType.ENERGYPLUS)
        for key in (
            "site_eui_kwh_m2",
            "peak_electric_demand_w",
            "completed_successfully",
            "warning_count",
            "has_sql_output",
        ):
            StepIODefinition.objects.create(
                validator=validator,
                contract_key=key,
                native_name=key,
                direction=StepIODirection.OUTPUT,
                data_type="number",
                label=key,
            )
        step = WorkflowStepFactory(validator=validator)
        run = ValidationRunFactory(workflow=step.workflow)
        engine = EnergyPlusValidator(config={})
        engine.run_context = RunContext(
            validation_run=run,
            step=step,
            upstream_steps={},
        )
        output_envelope = SimpleNamespace(
            outputs=SimpleNamespace(
                metrics=EnergyPlusSimulationMetrics(site_eui_kwh_m2=82.5),
                completed_successfully=True,
                warning_count=3,
                has_sql_output=True,
            ),
        )

        extracted = engine.extract_output_values(output_envelope)

        assert extracted == {
            "site_eui_kwh_m2": 82.5,
            "peak_electric_demand_w": None,
            "completed_successfully": True,
            "warning_count": 3,
            "has_sql_output": True,
        }

    def test_promoted_input_not_visible_in_producing_step(self):
        """Why this matters: enforces ADR-2026-05-22b's downstream-only rule.

        Inside the producing step, the author references step inputs
        via ``i.<name>``, never the promoted ``s.<promoted_name>``
        form. The promoted form is for downstream steps only.

        Without this guard, the producing step's own assertion
        evaluation could see its own promoted input via ``s.*`` —
        because canonical input persistence on ``ValidationStepRun.input_values``
        runs DURING the producing step's input stage, and a naive
        promoted-signals query would match the producing step's own
        rows. That would violate the temporal rule and confuse the
        author about when ``s.*`` actually becomes available.

        Regression test for the May 2026 code review's P1 finding.
        """
        from validibot.validations.constants import StepIODirection
        from validibot.validations.models import StepIODefinition

        engine, validator, submission, ruleset, run_context = self._build_fixture(
            cel_expr="i.zone_count >= 0",
        )

        # Promote the step input on the SAME step we're about to
        # validate. The promoted name is "zone_count" — if the
        # downstream-only rule is broken, the assertion's CEL context
        # would contain s.zone_count for this step too. We don't
        # assert against s.* directly in this test (would require a
        # second assertion); the regression we're guarding is the
        # SQL query that drives _inject_promotions.
        io_definition = StepIODefinition.objects.get(
            validator=validator,
            contract_key="zone_count",
            direction=StepIODirection.INPUT,
        )
        io_definition.promoted_signal_name = "zone_count"
        io_definition.save(update_fields=["promoted_signal_name"])

        clear_backend_cache()
        with patch(
            "validibot.validations.services.execution.get_execution_backend"
        ) as mock_get_backend:
            mock_backend = self._mock_backend()
            mock_get_backend.return_value = mock_backend

            engine.validate(
                validator=validator,
                submission=submission,
                ruleset=ruleset,
                run_context=run_context,
            )

            # Inspect what _build_cel_context produced for this step:
            # the signals_dict (s.*) should NOT contain "zone_count"
            # because the only promotion is on this same step. The
            # downstream-only filter (workflow_step__order__lt) must
            # exclude self-promotions during the producing step.
            cel_context = engine._build_cel_context(
                payload=engine._resolve_input_stage_payload(submission),
                validator=validator,
                stage="input",
            )
            signals_dict = cel_context.get("s") or {}
            assert "zone_count" not in signals_dict, (
                "Self-promotion leaked into s.* during the producing "
                "step. The downstream-only rule requires that promoted "
                "values are visible only in steps with order > the "
                "producing step's order. Check that "
                "_inject_promotions filters by "
                "workflow_step__order__lt=current_step.order."
            )
