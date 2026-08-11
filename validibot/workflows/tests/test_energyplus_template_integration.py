"""
Integration tests for the EnergyPlus parameterized template workflow.

These tests verify the end-to-end pipeline from form submission through
config building and resource syncing:

1. ``build_energyplus_config()`` — Validates uploaded IDF files, scans for
   ``$VARIABLE_NAME`` placeholders, returns them as a separate list for
   ``_sync_template_step_io()`` to persist as ``StepIODefinition`` rows,
   handles template removal, and preserves existing config as-is when no
   upload occurs.

2. ``_sync_energyplus_resources()`` — Creates/deletes ``WorkflowStepResource``
   rows with ``role=MODEL_TEMPLATE`` for step-owned template files.

3. ``save_workflow_step()`` — File type enforcement ensures workflows with
   parameterized templates accept JSON submissions.

4. Template Variable Annotation — ``TemplateVariableAnnotationForm`` (a
   standalone form) creates dynamic ``tplvar_*`` fields for per-variable
   annotation.  ``merge_template_variable_annotations()`` (an extracted
   helper in ``views_helpers``) merges author annotations into the stored
   config.  These are tested independently of ``build_energyplus_config()``.

5. ``launch_energyplus_validation()`` (Phase 4) — Launcher integration
   testing with real Django models and mocked GCS/Cloud Run I/O to verify
   the upload and job-trigger pipeline.  Template preprocessing (parameter
   merging, validation, substitution) happens earlier in the pipeline in
   ``EnergyPlusValidator.preprocess_submission()`` — those tests live in
   ``test_energyplus_preprocessing.py``.

Unlike the pure-Python scanner tests in ``test_idf_template.py``, these
tests require a Django database because they exercise form objects, model
instances, and ORM queries.

Phases: 2-4 of the EnergyPlus Parameterized Templates ADR.
"""

from __future__ import annotations

import hashlib
from http import HTTPStatus
from unittest.mock import patch

import pytest
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase

from validibot.submissions.constants import SubmissionFileType
from validibot.validations.constants import ENERGYPLUS_MODEL_TEMPLATE
from validibot.validations.constants import ValidationType
from validibot.validations.services.file_identity import FileIdentity
from validibot.validations.tests.factories import ExecutionAttemptFactory
from validibot.validations.tests.factories import ValidationStepRunFactory
from validibot.validations.tests.factories import ValidatorFactory
from validibot.validations.tests.factories import ValidatorResourceFileFactory
from validibot.workflows.forms import EnergyPlusStepConfigForm
from validibot.workflows.forms import TemplateVariableAnnotationForm
from validibot.workflows.models import WorkflowStepResource
from validibot.workflows.tests.factories import WorkflowFactory
from validibot.workflows.tests.factories import WorkflowStepFactory
from validibot.workflows.tests.factories import WorkflowStepResourceFactory
from validibot.workflows.views_helpers import _sync_energyplus_resources
from validibot.workflows.views_helpers import build_energyplus_config
from validibot.workflows.views_helpers import merge_template_variable_annotations
from validibot.workflows.views_helpers import save_workflow_step

pytestmark = pytest.mark.django_db

# ---------------------------------------------------------------------------
# Shared IDF template content for integration tests
# ---------------------------------------------------------------------------

VALID_TEMPLATE_IDF = b"""\
Version,
    24.2;  !- Version Identifier

WindowMaterial:SimpleGlazingSystem,
    Glazing System,          !- Name
    $U_FACTOR,               !- U-Factor {W/m2-K}
    $SHGC,                   !- Solar Heat Gain Coefficient
    $VISIBLE_TRANSMITTANCE;  !- Visible Transmittance
"""


def _make_energyplus_validator():
    """Create an EnergyPlus validator with a weather file resource.

    Also creates a default weather file resource so the form's required
    ``weather_file`` ChoiceField has at least one selectable option.
    Returns the validator instance.
    """
    validator = ValidatorFactory(
        validation_type=ValidationType.ENERGYPLUS,
        slug="energyplus-test",
        name="EnergyPlus Test",
    )
    # The form requires a weather file selection.  Create one so the
    # ChoiceField has a valid option.
    ValidatorResourceFileFactory(validator=validator, is_default=True)
    return validator


def _make_template_upload(
    content: bytes = VALID_TEMPLATE_IDF,
    filename: str = "template.idf",
) -> SimpleUploadedFile:
    """Create a SimpleUploadedFile for a template IDF."""
    return SimpleUploadedFile(filename, content, content_type="text/plain")


def _make_form(
    *,
    step=None,
    org=None,
    validator=None,
    data=None,
    files=None,
):
    """Build an EnergyPlusStepConfigForm with sensible defaults.

    The ``data`` dict must include all required form fields (``name``,
    ``weather_file``, etc.) or validation will fail.  The ``files`` dict
    can contain a ``template_file`` key for upload tests.

    Auto-selects the first available weather file from the database to
    satisfy the required ChoiceField — callers don't need to pass it.
    """
    if data is None:
        data = {}

    # Auto-select a weather file from the DB if one exists and
    # the caller didn't explicitly set one.
    weather_file_id = data.get("weather_file", "")
    if not weather_file_id and validator:
        from validibot.validations.constants import ResourceFileType
        from validibot.validations.models import ValidatorResourceFile

        vrf = ValidatorResourceFile.objects.filter(
            validator=validator,
            resource_type=ResourceFileType.ENERGYPLUS_WEATHER,
        ).first()
        if vrf:
            weather_file_id = str(vrf.pk)

    # Infer a sensible validation_mode default: template mode when the
    # test uploads a template file or the step already has template input definitions.
    has_template_context = bool(files and files.get("template_file"))
    if step and (step.config or {}).get("template_variables"):
        has_template_context = True
    default_mode = (
        EnergyPlusStepConfigForm.VALIDATION_MODE_TEMPLATE
        if has_template_context
        else EnergyPlusStepConfigForm.VALIDATION_MODE_DIRECT
    )

    defaults = {
        "name": "Test Step",
        "weather_file": weather_file_id,
        "validation_mode": default_mode,
        "idf_checks": [],
        "run_simulation": False,
        "case_sensitive": True,
        "remove_template": False,
    }

    defaults.update(data)

    form = EnergyPlusStepConfigForm(
        data=defaults,
        files=files or {},
        step=step,
        org=org,
        validator=validator,
    )
    return form


# ══════════════════════════════════════════════════════════════════════════════
# build_energyplus_config() — template upload
# ══════════════════════════════════════════════════════════════════════════════


class TestBuildConfigWithTemplateUpload:
    """Tests for ``build_energyplus_config`` when a template file is uploaded.

    This verifies the pipeline: uploaded file → ``validate_idf_template()``
    → ``scan_idf_template_variables()`` → template variable dicts returned
    to the caller.  The scan/validation is tested exhaustively in
    ``test_idf_template.py``; here we test the integration with the form
    and config builder.
    """

    def test_upload_populates_template_variables(self):
        """A valid template upload returns one dict per detected variable.

        The returned ``template_vars`` list contains auto-populated metadata
        from the IDF annotation, ready for ``_sync_template_step_io()``
        to persist as ``StepIODefinition`` rows.
        """
        validator = _make_energyplus_validator()
        upload = _make_template_upload()
        form = _make_form(
            validator=validator,
            files={"template_file": upload},
        )
        assert form.is_valid(), form.errors
        _config, template_vars = build_energyplus_config(form)

        assert len(template_vars) == 3  # noqa: PLR2004
        names = [v["name"] for v in template_vars]
        assert names == ["U_FACTOR", "SHGC", "VISIBLE_TRANSMITTANCE"]

    def test_upload_preserves_annotation_metadata(self):
        """Auto-populated descriptions and units from IDF annotations are
        stored in the returned variable dicts.

        The ``!- U-Factor {W/m2-K}`` annotation should produce
        ``description='U-Factor'`` and ``units='W/m2-K'``.
        """
        validator = _make_energyplus_validator()
        upload = _make_template_upload()
        form = _make_form(
            validator=validator,
            files={"template_file": upload},
        )
        assert form.is_valid(), form.errors
        _config, template_vars = build_energyplus_config(form)

        u_factor = template_vars[0]
        assert u_factor["description"] == "U-Factor"
        assert u_factor["units"] == "W/m2-K"

    def test_upload_stores_case_sensitive_setting(self):
        """The ``case_sensitive`` form value is included in the config.

        This setting controls how the scanner detects variables and must
        be persisted so future scans use the same mode.
        """
        validator = _make_energyplus_validator()
        upload = _make_template_upload()
        form = _make_form(
            validator=validator,
            data={"case_sensitive": False},
            files={"template_file": upload},
        )
        assert form.is_valid(), form.errors
        config, _template_vars = build_energyplus_config(form)

        assert config["case_sensitive"] is False

    def test_upload_initializes_display_step_outputs_empty(self):
        """Displayed step outputs start empty until the author opts in.

        An empty list means no step outputs are exposed to submitters.
        """
        validator = _make_energyplus_validator()
        upload = _make_template_upload()
        form = _make_form(
            validator=validator,
            files={"template_file": upload},
        )
        assert form.is_valid(), form.errors
        config, _template_vars = build_energyplus_config(form)

        assert config["display_step_outputs"] == []

    def test_upload_invalid_file_raises_validation_error(self):
        """An invalid template file causes ``ValidationError``.

        ``build_energyplus_config()`` delegates to ``validate_idf_template()``,
        which produces blocking errors for non-IDF files.
        """
        validator = _make_energyplus_validator()
        upload = _make_template_upload(
            content=b'{"not": "idf"}',
            filename="not_idf.epjson",
        )
        form = _make_form(
            validator=validator,
            files={"template_file": upload},
        )
        assert form.is_valid(), form.errors

        with pytest.raises(ValidationError):
            build_energyplus_config(form)

    def test_upload_attaches_warnings_to_form(self):
        """Non-blocking warnings from the scan are attached to the form.

        The view layer can then display these to the author (e.g., "This
        template is 600 KB. Consider using ##include.").
        """
        # Use a template with a bare $ to trigger a warning
        content = b"""\
WindowMaterial:SimpleGlazingSystem,
    Glazing System,          !- Name
    $U_FACTOR,               !- U-Factor {W/m2-K}
    $ SHGC,                  !- Solar Heat Gain Coefficient
    $VISIBLE_TRANSMITTANCE;  !- Visible Transmittance
"""
        validator = _make_energyplus_validator()
        upload = _make_template_upload(content=content)
        form = _make_form(
            validator=validator,
            files={"template_file": upload},
        )
        assert form.is_valid(), form.errors
        build_energyplus_config(form)

        assert hasattr(form, "template_warnings")
        assert len(form.template_warnings) > 0

    def test_upload_variable_defaults_are_text_type(self):
        """Auto-detected variables default to ``variable_type='text'``.

        The author refines types (number, choice) in the Template Variable
        Editor (Phase 3).  Until then, 'text' is the safest default because
        it accepts any value.
        """
        validator = _make_energyplus_validator()
        upload = _make_template_upload()
        form = _make_form(
            validator=validator,
            files={"template_file": upload},
        )
        assert form.is_valid(), form.errors
        _config, template_vars = build_energyplus_config(form)

        for var in template_vars:
            assert var["variable_type"] == "text"
            assert var["default"] == ""
            assert var["choices"] == []


# ══════════════════════════════════════════════════════════════════════════════
# build_energyplus_config() — validation_mode routing
# ══════════════════════════════════════════════════════════════════════════════


class TestBuildConfigValidationMode:
    """Tests for the ``validation_mode`` field that routes config building.

    The form's ``validation_mode`` radio selector determines which config keys
    are populated by ``build_energyplus_config()``.  Direct mode stores IDF
    check/simulation settings and clears template metadata.  Template mode
    stores template variables, case sensitivity, and displayed step outputs.
    """

    def test_duplicate_name_check_is_not_offered(self):
        """IDD-governed duplicate validation should be delegated to EnergyPlus."""

        choices = dict(EnergyPlusStepConfigForm.base_fields["idf_checks"].choices)

        assert "duplicate-names" not in choices
        assert set(choices) == {"hvac-sizing", "schedule-coverage"}

    def test_direct_mode_stores_idf_settings(self):
        """Direct mode populates ``idf_checks`` and ``run_simulation``.

        These are the settings relevant when submitters upload a complete IDF
        file for validation.
        """
        validator = _make_energyplus_validator()
        form = _make_form(
            validator=validator,
            data={
                "validation_mode": EnergyPlusStepConfigForm.VALIDATION_MODE_DIRECT,
                "idf_checks": ["hvac-sizing"],
                "run_simulation": True,
            },
        )
        assert form.is_valid(), form.errors
        config, _template_vars = build_energyplus_config(form)

        assert config["validation_mode"] == "direct"
        assert config["idf_checks"] == ["hvac-sizing"]
        assert config["run_simulation"] is True

    def test_direct_mode_clears_template_metadata(self):
        """Direct mode returns empty template vars and clears displayed outputs.

        Even if template data existed before, switching to direct mode should
        produce empty template metadata so the step no longer expects JSON
        parameter submissions.
        """
        validator = _make_energyplus_validator()
        form = _make_form(
            validator=validator,
            data={
                "validation_mode": EnergyPlusStepConfigForm.VALIDATION_MODE_DIRECT,
            },
        )
        assert form.is_valid(), form.errors
        config, template_vars = build_energyplus_config(form)

        assert template_vars == []
        assert config["display_step_outputs"] == []
        assert config["case_sensitive"] is True
        assert "template_variables" not in config

    def test_template_mode_stores_template_settings(self):
        """Template mode returns template vars and populates config keys.

        When a template file is uploaded, the returned ``template_vars``
        list contains the scanned variables, and the config includes the
        author's ``case_sensitive`` and ``display_step_outputs`` preferences.
        """
        validator = _make_energyplus_validator()
        upload = _make_template_upload()
        form = _make_form(
            validator=validator,
            data={
                "validation_mode": EnergyPlusStepConfigForm.VALIDATION_MODE_TEMPLATE,
            },
            files={"template_file": upload},
        )
        assert form.is_valid(), form.errors
        config, template_vars = build_energyplus_config(form)

        assert config["validation_mode"] == "template"
        expected_var_count = 3
        assert len(template_vars) == expected_var_count
        assert "template_variables" not in config
        assert config["idf_checks"] == []
        assert config["run_simulation"] is True

    def test_direct_mode_marks_template_for_removal(self):
        """Direct mode sets ``remove_template`` in cleaned_data.

        This tells ``_sync_energyplus_resources()`` to delete any existing
        template file resource from a previous template-mode configuration.
        """
        validator = _make_energyplus_validator()
        form = _make_form(
            validator=validator,
            data={
                "validation_mode": EnergyPlusStepConfigForm.VALIDATION_MODE_DIRECT,
            },
        )
        assert form.is_valid(), form.errors
        build_energyplus_config(form)

        assert form.cleaned_data["remove_template"] is True

    def test_validation_mode_persisted_in_config(self):
        """The chosen validation mode is stored in the config dict.

        This allows the step details card and other views to display the
        correct mode label without inspecting resource files.
        """
        validator = _make_energyplus_validator()
        for mode in ("direct", "template"):
            data = {"validation_mode": mode}
            if mode == "template":
                files = {"template_file": _make_template_upload()}
            else:
                files = None
            form = _make_form(validator=validator, data=data, files=files)
            assert form.is_valid(), form.errors
            config, _template_vars = build_energyplus_config(form)
            assert config["validation_mode"] == mode


# ══════════════════════════════════════════════════════════════════════════════
# build_energyplus_config() — template removal
# ══════════════════════════════════════════════════════════════════════════════


class TestBuildConfigWithTemplateRemoval:
    """Tests for ``build_energyplus_config`` when the template is removed.

    Removal means the author is switching from template mode back to direct
    IDF submission.  All template metadata should be cleared from config.
    """

    def test_remove_clears_template_step_io(self):
        """Switching to direct mode returns an empty template vars list.

        Even if the step had template StepIODefinition rows before, the
        returned list should be empty because the author selected direct
        mode. ``_sync_template_step_io()`` will clear the StepIODefinition
        rows.
        """
        validator = _make_energyplus_validator()
        step = WorkflowStepFactory(
            validator=validator,
            config={
                "idf_checks": [],
                "run_simulation": False,
                "case_sensitive": True,
            },
        )
        form = _make_form(
            validator=validator,
            step=step,
            data={
                "validation_mode": EnergyPlusStepConfigForm.VALIDATION_MODE_DIRECT,
                "remove_template": True,
            },
        )
        assert form.is_valid(), form.errors
        config, template_vars = build_energyplus_config(form, step)

        assert template_vars == []
        assert config["case_sensitive"] is True
        assert config["display_step_outputs"] == []
        assert "template_variables" not in config

    def test_remove_preserves_non_template_config(self):
        """Simulation settings are preserved when switching to direct mode.

        ``idf_checks`` and ``run_simulation`` are independent of the
        template feature and should survive template removal.
        """
        validator = _make_energyplus_validator()
        step = WorkflowStepFactory(
            validator=validator,
            config={"idf_checks": ["hvac-sizing"], "run_simulation": True},
        )
        form = _make_form(
            validator=validator,
            step=step,
            data={
                "validation_mode": EnergyPlusStepConfigForm.VALIDATION_MODE_DIRECT,
                "remove_template": True,
                "idf_checks": ["hvac-sizing"],
                "run_simulation": True,
            },
        )
        assert form.is_valid(), form.errors
        config, _template_vars = build_energyplus_config(form, step)

        assert config["idf_checks"] == ["hvac-sizing"]
        assert config["run_simulation"] is True


# ══════════════════════════════════════════════════════════════════════════════
# build_energyplus_config() — no upload, no removal
# ══════════════════════════════════════════════════════════════════════════════


class TestBuildConfigPreservesExisting:
    """Tests for ``build_energyplus_config`` when no template change occurs.

    If the author edits other settings (idf_checks, run_simulation) without
    touching the template, existing template metadata should be preserved.
    """

    def test_preserves_existing_config_on_settings_edit(self):
        """Existing config settings survive a settings-only edit.

        When no template upload or removal occurs in template mode,
        ``build_energyplus_config()`` preserves existing config as-is
        (case_sensitive, display_step_outputs).  Template variable data lives
        in ``StepIODefinition`` rows and is not affected by a
        settings-only re-save.
        """
        validator = _make_energyplus_validator()
        step = WorkflowStepFactory(
            validator=validator,
            config={
                "validation_mode": "template",
                "idf_checks": [],
                "run_simulation": True,
                "case_sensitive": True,
            },
            # display_step_outputs is cosmetic — it lives in the display bucket,
            # which is where build_energyplus_config's keep-read now looks
            # (ADR-2026-06-18).
            display_settings={"display_step_outputs": ["some_output"]},
        )
        form = _make_form(
            validator=validator,
            step=step,
            data={
                "validation_mode": EnergyPlusStepConfigForm.VALIDATION_MODE_TEMPLATE,
            },
        )
        assert form.is_valid(), form.errors
        config, template_vars = build_energyplus_config(form, step)

        # No template upload — returns empty list (existing
        # StepIODefinition rows are left unchanged by the caller).
        assert template_vars == []
        assert "template_variables" not in config
        assert config["case_sensitive"] is True
        assert config["display_step_outputs"] == ["some_output"]

    def test_no_step_no_template_keys(self):
        """A new step (no existing step) in direct mode has no template data.

        When ``step=None`` and no template is uploaded, the config should
        contain simulation settings without template data (direct mode
        returns an empty template vars list).
        """
        validator = _make_energyplus_validator()
        form = _make_form(validator=validator)
        assert form.is_valid(), form.errors
        config, template_vars = build_energyplus_config(form, step=None)

        assert template_vars == []
        assert "template_variables" not in config
        assert config["display_step_outputs"] == []

    def test_step_without_template_has_no_template_keys(self):
        """An existing step in direct mode has no template input definitions.

        Pre-template steps have ``config={"idf_checks": [], "run_simulation": False}``
        with no template keys.  Re-saving in direct mode returns an empty
        template vars list and creates no StepIODefinition rows.
        """
        validator = _make_energyplus_validator()
        step = WorkflowStepFactory(
            validator=validator,
            config={"idf_checks": [], "run_simulation": False},
        )
        form = _make_form(validator=validator, step=step)
        assert form.is_valid(), form.errors
        config, template_vars = build_energyplus_config(form, step)

        assert template_vars == []
        assert "template_variables" not in config
        assert config["display_step_outputs"] == []


# ══════════════════════════════════════════════════════════════════════════════
# _sync_energyplus_resources() — MODEL_TEMPLATE resource management
# ══════════════════════════════════════════════════════════════════════════════


class TestSyncResourcesTemplate:
    """Tests for ``_sync_energyplus_resources()`` MODEL_TEMPLATE handling.

    The template file is stored as a step-owned ``WorkflowStepResource``
    (mode 2: ``step_resource_file`` populated, ``validator_resource_file``
    NULL).  These tests verify create, replace, and delete operations.
    """

    def test_upload_creates_step_owned_resource(self):
        """Uploading a template creates a ``WorkflowStepResource`` with
        the template file stored directly on the record.

        The resource should use ``role=MODEL_TEMPLATE`` and store the
        original filename and resource type.
        """
        validator = _make_energyplus_validator()
        step = WorkflowStepFactory(validator=validator)
        upload = _make_template_upload()

        form = _make_form(
            validator=validator,
            step=step,
            files={"template_file": upload},
        )
        assert form.is_valid(), form.errors

        _sync_energyplus_resources(step, form)

        resource = step.step_resources.filter(
            role=WorkflowStepResource.MODEL_TEMPLATE,
        ).first()
        assert resource is not None
        assert resource.filename == "template.idf"
        assert resource.resource_type == ENERGYPLUS_MODEL_TEMPLATE
        assert resource.validator_resource_file is None  # step-owned, not catalog
        assert resource.step_resource_file  # file field is populated

    def test_upload_replaces_existing_template(self):
        """A new upload replaces any existing template resource.

        The old resource row (and its file) should be deleted before
        creating the new one.
        """
        validator = _make_energyplus_validator()
        step = WorkflowStepFactory(validator=validator)

        # Create initial template resource
        first_upload = _make_template_upload(filename="first.idf")
        form1 = _make_form(
            validator=validator,
            step=step,
            files={"template_file": first_upload},
        )
        assert form1.is_valid(), form1.errors
        _sync_energyplus_resources(step, form1)

        assert (
            step.step_resources.filter(
                role=WorkflowStepResource.MODEL_TEMPLATE,
            ).count()
            == 1
        )

        # Upload replacement template
        second_upload = _make_template_upload(filename="second.idf")
        form2 = _make_form(
            validator=validator,
            step=step,
            files={"template_file": second_upload},
        )
        assert form2.is_valid(), form2.errors
        _sync_energyplus_resources(step, form2)

        # Should still be exactly one template resource
        resources = step.step_resources.filter(
            role=WorkflowStepResource.MODEL_TEMPLATE,
        )
        assert resources.count() == 1
        assert resources.first().filename == "second.idf"

    def test_remove_deletes_template_resource(self):
        """``remove_template=True`` deletes the template resource row."""
        validator = _make_energyplus_validator()
        step = WorkflowStepFactory(validator=validator)

        # Create template resource
        upload = _make_template_upload()
        form1 = _make_form(
            validator=validator,
            step=step,
            files={"template_file": upload},
        )
        assert form1.is_valid(), form1.errors
        _sync_energyplus_resources(step, form1)

        assert (
            step.step_resources.filter(
                role=WorkflowStepResource.MODEL_TEMPLATE,
            ).count()
            == 1
        )

        # Remove template
        form2 = _make_form(
            validator=validator,
            step=step,
            data={"remove_template": True},
        )
        assert form2.is_valid(), form2.errors
        _sync_energyplus_resources(step, form2)

        assert (
            step.step_resources.filter(
                role=WorkflowStepResource.MODEL_TEMPLATE,
            ).count()
            == 0
        )

    def test_no_upload_preserves_existing_resource(self):
        """When no file is uploaded and no removal, existing template
        resource is untouched.

        This is the normal case when the author edits simulation settings
        without touching the template.
        """
        validator = _make_energyplus_validator()
        step = WorkflowStepFactory(validator=validator)

        # Create template resource
        upload = _make_template_upload()
        form1 = _make_form(
            validator=validator,
            step=step,
            files={"template_file": upload},
        )
        assert form1.is_valid(), form1.errors
        _sync_energyplus_resources(step, form1)

        resource_pk = (
            step.step_resources.filter(
                role=WorkflowStepResource.MODEL_TEMPLATE,
            )
            .first()
            .pk
        )

        # Submit form without template changes
        form2 = _make_form(validator=validator, step=step)
        assert form2.is_valid(), form2.errors
        _sync_energyplus_resources(step, form2)

        # Same resource should still exist with the same PK
        resources = step.step_resources.filter(
            role=WorkflowStepResource.MODEL_TEMPLATE,
        )
        assert resources.count() == 1
        assert resources.first().pk == resource_pk

    def test_template_does_not_affect_weather_file(self):
        """Template operations don't interfere with weather file resources.

        Weather files use ``role=WEATHER_FILE`` and catalog references.
        Template operations only touch ``role=MODEL_TEMPLATE`` rows.
        The weather file resource created by ``_sync_energyplus_resources``
        (from the auto-selected weather file) should survive template
        operations unchanged.
        """
        validator = _make_energyplus_validator()
        step = WorkflowStepFactory(validator=validator)

        # Create template resource (also syncs weather file)
        upload = _make_template_upload()
        form = _make_form(
            validator=validator,
            step=step,
            files={"template_file": upload},
        )
        assert form.is_valid(), form.errors
        _sync_energyplus_resources(step, form)

        # Both resource types should exist independently
        weather_count = step.step_resources.filter(
            role=WorkflowStepResource.WEATHER_FILE,
        ).count()
        template_count = step.step_resources.filter(
            role=WorkflowStepResource.MODEL_TEMPLATE,
        ).count()
        assert weather_count == 1  # Auto-selected weather file
        assert template_count == 1


# ══════════════════════════════════════════════════════════════════════════════
# save_workflow_step() — file type enforcement
# ══════════════════════════════════════════════════════════════════════════════


class TestFileTypeEnforcement:
    """Tests for JSON file type enforcement on parameterized template steps.

    Parameterized templates require JSON submissions because the submitter
    sends variable values as a JSON payload.  The enforcement check in
    ``save_workflow_step()`` rejects template activation on workflows that
    don't allow JSON file types.
    """

    def test_template_allowed_when_json_in_file_types(self):
        """Template activation succeeds when the workflow allows JSON.

        This is the normal case — the factory default includes JSON in
        ``allowed_file_types``.  Template variables are stored as
        StepIODefinition rows (not in config JSON).
        """
        validator = _make_energyplus_validator()
        workflow = WorkflowFactory(
            allowed_file_types=[SubmissionFileType.JSON],
        )
        upload = _make_template_upload()
        form = _make_form(
            validator=validator,
            files={"template_file": upload},
        )
        assert form.is_valid(), form.errors

        step = save_workflow_step(workflow, validator, form)

        # Template variables are now stored as StepIODefinition rows,
        # not in config JSON.
        assert "template_variables" not in step.config
        from validibot.validations.constants import StepIOOriginKind

        expected_io_definition_count = 3
        assert (
            step.step_io_definitions.filter(
                origin_kind=StepIOOriginKind.TEMPLATE,
            ).count()
            == expected_io_definition_count
        )

    def test_template_rejected_when_json_not_in_file_types(self):
        """Template activation fails when the workflow doesn't allow JSON.

        The author must add JSON to ``allowed_file_types`` before
        activating a parameterized template.
        """
        validator = _make_energyplus_validator()
        workflow = WorkflowFactory(
            allowed_file_types=[SubmissionFileType.XML],
        )
        upload = _make_template_upload()
        form = _make_form(
            validator=validator,
            files={"template_file": upload},
        )
        assert form.is_valid(), form.errors

        with pytest.raises(ValidationError, match="JSON"):
            save_workflow_step(workflow, validator, form)

    def test_no_template_no_enforcement(self):
        """Steps without templates don't trigger file type enforcement.

        Non-template EnergyPlus steps accept any file type — the
        enforcement is only for parameterized templates.
        """
        validator = _make_energyplus_validator()
        workflow = WorkflowFactory(
            allowed_file_types=[SubmissionFileType.XML],
        )
        form = _make_form(validator=validator)
        assert form.is_valid(), form.errors

        # Should not raise — direct mode has no template variables
        step = save_workflow_step(workflow, validator, form)
        assert "template_variables" not in step.config


# ══════════════════════════════════════════════════════════════════════════════
# EnergyPlusStepConfigForm — template state
# ══════════════════════════════════════════════════════════════════════════════


class TestFormTemplateState:
    """Tests for template-related state on the form instance.

    The form exposes ``has_template`` and ``template_filename`` so that
    the Django template can render the appropriate UI: "upload a template"
    vs "current template: glazing_template.idf [Remove]".
    """

    def test_new_step_has_no_template(self):
        """A form for a new step reports no template."""
        validator = _make_energyplus_validator()
        form = _make_form(validator=validator)

        assert form.has_template is False
        assert form.template_filename == ""

    def test_step_with_template_resource_reports_template(self):
        """A form for an existing step with a template resource shows it.

        The ``has_template`` flag and ``template_filename`` are populated
        from the ``WorkflowStepResource`` query in ``__init__``.
        """
        validator = _make_energyplus_validator()
        step = WorkflowStepFactory(validator=validator)

        # Create a template resource for the step
        upload = _make_template_upload(filename="glazing.idf")
        form1 = _make_form(
            validator=validator,
            step=step,
            files={"template_file": upload},
        )
        assert form1.is_valid(), form1.errors
        _sync_energyplus_resources(step, form1)

        # Now create a fresh form for the same step
        form2 = _make_form(validator=validator, step=step)

        assert form2.has_template is True
        assert form2.template_filename == "glazing.idf"

    def test_case_sensitive_initial_from_config(self):
        """The ``case_sensitive`` field initial value comes from step config.

        When editing an existing step, the form should show the current
        case sensitivity setting, not the default.
        """
        validator = _make_energyplus_validator()
        step = WorkflowStepFactory(
            validator=validator,
            config={
                "idf_checks": [],
                "run_simulation": False,
                "case_sensitive": False,
            },
        )
        form = _make_form(validator=validator, step=step)

        assert form.fields["case_sensitive"].initial is False


# ══════════════════════════════════════════════════════════════════════════════
# TemplateVariableAnnotationForm — dynamic form fields
# ══════════════════════════════════════════════════════════════════════════════


def _make_annotation_form(step):
    """Create a ``TemplateVariableAnnotationForm`` bound to the given step.

    This is the standalone form that renders the per-variable annotation
    card on the step detail page.  It reads from step-owned
    ``StepIODefinition`` rows and creates dynamic ``tplvar_*`` fields.
    """
    return TemplateVariableAnnotationForm(step=step)


class TestVariableEditorDynamicFields:
    """Tests for the dynamic per-variable fields on TemplateVariableAnnotationForm.

    When a step has template ``StepIODefinition`` rows, the standalone
    annotation form creates dynamic fields (``tplvar_0_description``,
    ``tplvar_0_variable_type``, etc.) for each variable.  These fields
    let the author annotate each variable with a type, constraints,
    default value, and label.
    """

    def test_dynamic_fields_created_for_template_variables(self):
        """Dynamic ``tplvar_*`` fields are created when template variables exist.

        Each detected variable gets nine fields: description, default, units,
        variable_type, min_value, min_exclusive, max_value, max_exclusive,
        and choices.  The form reads from step-owned StepIODefinition rows.
        """
        from validibot.validations.services.template_step_io import (
            sync_step_template_io_definitions,
        )

        validator = _make_energyplus_validator()
        step = WorkflowStepFactory(validator=validator)
        sync_step_template_io_definitions(
            step,
            [
                {"name": "U_FACTOR", "description": "U-Factor"},
                {"name": "SHGC", "description": "SHGC"},
            ],
        )
        form = _make_annotation_form(step)

        # 9 fields per variable x 2 variables = 18 dynamic fields
        tplvar_fields = [f for f in form.fields if f.startswith("tplvar_")]
        assert len(tplvar_fields) == 18  # noqa: PLR2004

        # Verify specific field names
        assert "tplvar_0_description" in form.fields
        assert "tplvar_0_variable_type" in form.fields
        assert "tplvar_1_choices" in form.fields

    def test_no_dynamic_fields_without_template(self):
        """No ``tplvar_*`` fields when the step has no template variables.

        This is the backward-compatible case: pre-template EnergyPlus steps
        have no template-related config keys.
        """
        validator = _make_energyplus_validator()
        step = WorkflowStepFactory(
            validator=validator,
            config={"idf_checks": [], "run_simulation": False},
        )
        form = _make_annotation_form(step)

        tplvar_fields = [f for f in form.fields if f.startswith("tplvar_")]
        assert tplvar_fields == []

    def test_dynamic_fields_have_correct_initial_values(self):
        """Dynamic field initial values match the step I/O definition data.

        When editing an existing step, each variable's fields should be
        pre-populated with the saved annotation data from StepIODefinition.
        """
        from validibot.validations.services.template_step_io import (
            sync_step_template_io_definitions,
        )

        validator = _make_energyplus_validator()
        step = WorkflowStepFactory(validator=validator)
        sync_step_template_io_definitions(
            step,
            [
                {
                    "name": "U_FACTOR",
                    "description": "Window U-Factor",
                    "default": "2.0",
                    "units": "W/m2-K",
                    "variable_type": "number",
                    "min_value": 0.1,
                    "min_exclusive": False,
                    "max_value": 7.0,
                    "max_exclusive": True,
                    "choices": [],
                },
            ],
        )
        form = _make_annotation_form(step)

        assert form.fields["tplvar_0_description"].initial == "Window U-Factor"
        assert form.fields["tplvar_0_default"].initial == "2.0"
        assert form.fields["tplvar_0_units"].initial == "W/m2-K"
        assert form.fields["tplvar_0_variable_type"].initial == "number"
        assert form.fields["tplvar_0_min_value"].initial == "0.1"
        assert form.fields["tplvar_0_min_exclusive"].initial is False
        assert form.fields["tplvar_0_max_value"].initial == "7.0"
        assert form.fields["tplvar_0_max_exclusive"].initial is True

    def test_new_step_no_dynamic_fields(self):
        """A form for a brand-new step (step=None) has no dynamic fields.

        Template variables only exist after the first template upload and
        save cycle.
        """
        form = TemplateVariableAnnotationForm(step=None)

        tplvar_fields = [f for f in form.fields if f.startswith("tplvar_")]
        assert tplvar_fields == []


# ══════════════════════════════════════════════════════════════════════════════
# TemplateVariableAnnotationForm.template_variable_fields property
# ══════════════════════════════════════════════════════════════════════════════


class TestTemplateVariableFieldsProperty:
    """Tests for ``TemplateVariableAnnotationForm.template_variable_fields``.

    This property returns a list of dicts, each containing a variable's
    name, index, required/optional badge state, and BoundField objects.
    The template partial iterates over this list to render per-variable
    annotation cards.
    """

    def test_returns_correct_structure(self):
        """Each item has the expected keys for template rendering.

        The ``name`` and ``index`` are strings/ints for display; the field
        keys are BoundField objects that render as HTML form controls.
        """
        from validibot.validations.services.template_step_io import (
            sync_step_template_io_definitions,
        )

        validator = _make_energyplus_validator()
        step = WorkflowStepFactory(validator=validator)
        sync_step_template_io_definitions(
            step,
            [{"name": "U_FACTOR", "description": "U-Factor"}],
        )
        form = _make_annotation_form(step)

        fields = form.template_variable_fields
        assert len(fields) == 1

        var = fields[0]
        assert var["name"] == "U_FACTOR"
        assert var["index"] == 0
        assert "description" in var
        assert "variable_type" in var
        assert "choices" in var

    def test_is_required_when_no_default(self):
        """Variable is marked required when default is empty.

        The required/optional badge is derived from the default value:
        empty default = required, non-empty default = optional.
        """
        from validibot.validations.services.template_step_io import (
            sync_step_template_io_definitions,
        )

        validator = _make_energyplus_validator()
        step = WorkflowStepFactory(validator=validator)
        sync_step_template_io_definitions(
            step,
            [{"name": "U_FACTOR", "default": ""}],
        )
        form = _make_annotation_form(step)

        assert form.template_variable_fields[0]["is_required"] is True

    def test_is_optional_when_default_set(self):
        """Variable is marked optional when default is non-empty."""
        from validibot.validations.services.template_step_io import (
            sync_step_template_io_definitions,
        )

        validator = _make_energyplus_validator()
        step = WorkflowStepFactory(validator=validator)
        sync_step_template_io_definitions(
            step,
            [{"name": "U_FACTOR", "default": "2.0"}],
        )
        form = _make_annotation_form(step)

        assert form.template_variable_fields[0]["is_required"] is False

    def test_empty_when_no_template_variables(self):
        """Returns empty list when no template variables exist."""
        form = TemplateVariableAnnotationForm(step=None)

        assert form.template_variable_fields == []


# ══════════════════════════════════════════════════════════════════════════════
# merge_template_variable_annotations() — annotation merge
# ══════════════════════════════════════════════════════════════════════════════


class TestAnnotationMerge:
    """Tests for ``merge_template_variable_annotations()``.

    This extracted helper takes existing template variable dicts and
    form data with ``tplvar_*`` keys, then returns a new list of
    variable dicts with author annotations merged in.  It is called by
    ``WorkflowStepTemplateVariablesView`` when saving annotations from
    the step detail page's template variables card.
    """

    def test_annotations_persist_on_save(self):
        """Author annotations from form data are merged into the variable dicts.

        When the author fills in description, default, and type fields,
        those values should appear in the merged output.
        """
        existing_vars = [
            {"name": "U_FACTOR", "description": "U-Factor", "units": "W/m2-K"},
        ]
        form_data = {
            "tplvar_0_description": "Window U-Factor",
            "tplvar_0_default": "2.0",
            "tplvar_0_variable_type": "number",
            "tplvar_0_units": "W/m2-K",
            "tplvar_0_min_value": "0.1",
            "tplvar_0_min_exclusive": False,
            "tplvar_0_max_value": "7.0",
            "tplvar_0_max_exclusive": False,
            "tplvar_0_choices": "",
        }
        result = merge_template_variable_annotations(existing_vars, form_data)

        var = result[0]
        assert var["name"] == "U_FACTOR"
        assert var["description"] == "Window U-Factor"
        assert var["default"] == "2.0"
        assert var["variable_type"] == "number"
        assert var["min_value"] == 0.1  # noqa: PLR2004
        assert var["max_value"] == 7.0  # noqa: PLR2004

    def test_name_is_immutable(self):
        """Variable names cannot be changed via form data.

        The merge logic always uses the name from the existing variable
        dict.  Even if someone tampers with form data to include a name
        field, it is ignored.
        """
        existing_vars = [{"name": "U_FACTOR"}]
        form_data = {
            "tplvar_0_description": "",
            "tplvar_0_default": "",
            "tplvar_0_units": "",
            "tplvar_0_variable_type": "text",
            "tplvar_0_min_value": "",
            "tplvar_0_min_exclusive": False,
            "tplvar_0_max_value": "",
            "tplvar_0_max_exclusive": False,
            "tplvar_0_choices": "",
        }
        result = merge_template_variable_annotations(existing_vars, form_data)

        assert result[0]["name"] == "U_FACTOR"

    def test_min_max_parsing(self):
        """String min/max values are parsed to floats; empty strings become None.

        The form fields use CharField (not FloatField) so we can distinguish
        "empty" from "0".  The ``_parse_optional_float`` helper converts them.
        """
        existing_vars = [{"name": "U_FACTOR"}]
        form_data = {
            "tplvar_0_description": "",
            "tplvar_0_default": "",
            "tplvar_0_units": "",
            "tplvar_0_variable_type": "number",
            "tplvar_0_min_value": "0.5",
            "tplvar_0_min_exclusive": False,
            "tplvar_0_max_value": "",
            "tplvar_0_max_exclusive": False,
            "tplvar_0_choices": "",
        }
        result = merge_template_variable_annotations(existing_vars, form_data)

        var = result[0]
        assert var["min_value"] == 0.5  # noqa: PLR2004
        assert var["max_value"] is None

    def test_choices_parsing(self):
        """Multiline choices are parsed into a list of non-empty strings.

        The textarea value is split by newlines, each line is stripped of
        whitespace, and empty lines are filtered out.
        """
        existing_vars = [{"name": "ROUGHNESS"}]
        form_data = {
            "tplvar_0_description": "",
            "tplvar_0_default": "",
            "tplvar_0_units": "",
            "tplvar_0_variable_type": "choice",
            "tplvar_0_min_value": "",
            "tplvar_0_min_exclusive": False,
            "tplvar_0_max_value": "",
            "tplvar_0_max_exclusive": False,
            "tplvar_0_choices": "VerySmooth\nSmooth\n\nMediumSmooth\n",
        }
        result = merge_template_variable_annotations(existing_vars, form_data)

        var = result[0]
        assert var["choices"] == ["VerySmooth", "Smooth", "MediumSmooth"]

    def test_type_change_preserves_all_fields(self):
        """Changing variable_type stores all annotation fields.

        Even when the author changes from 'number' to 'text', min/max
        values are still stored.  The UI hides them, but the data is
        preserved in case the author switches back.
        """
        existing_vars = [
            {
                "name": "U_FACTOR",
                "variable_type": "number",
                "min_value": 0.1,
                "max_value": 7.0,
            },
        ]
        form_data = {
            "tplvar_0_description": "",
            "tplvar_0_default": "",
            "tplvar_0_units": "",
            "tplvar_0_variable_type": "text",
            "tplvar_0_min_value": "0.1",
            "tplvar_0_min_exclusive": False,
            "tplvar_0_max_value": "7.0",
            "tplvar_0_max_exclusive": False,
            "tplvar_0_choices": "",
        }
        result = merge_template_variable_annotations(existing_vars, form_data)

        var = result[0]
        assert var["variable_type"] == "text"
        assert var["min_value"] == 0.1  # noqa: PLR2004
        assert var["max_value"] == 7.0  # noqa: PLR2004

    def test_multiple_variables_merge_independently(self):
        """Each variable's annotations are merged independently.

        Annotations for variable 0 don't bleed into variable 1.
        """
        existing_vars = [
            {"name": "U_FACTOR"},
            {"name": "SHGC"},
        ]
        form_data = {
            "tplvar_0_description": "Window U-Factor",
            "tplvar_0_default": "",
            "tplvar_0_units": "",
            "tplvar_0_variable_type": "number",
            "tplvar_0_min_value": "",
            "tplvar_0_min_exclusive": False,
            "tplvar_0_max_value": "",
            "tplvar_0_max_exclusive": False,
            "tplvar_0_choices": "",
            "tplvar_1_description": "Solar Heat Gain Coefficient",
            "tplvar_1_default": "",
            "tplvar_1_units": "",
            "tplvar_1_variable_type": "text",
            "tplvar_1_min_value": "",
            "tplvar_1_min_exclusive": False,
            "tplvar_1_max_value": "",
            "tplvar_1_max_exclusive": False,
            "tplvar_1_choices": "",
        }
        result = merge_template_variable_annotations(existing_vars, form_data)

        assert result[0]["description"] == "Window U-Factor"
        assert result[0]["variable_type"] == "number"
        assert result[1]["description"] == "Solar Heat Gain Coefficient"
        assert result[1]["variable_type"] == "text"


# ══════════════════════════════════════════════════════════════════════════════
# Phase 3: Displayed step outputs
# ══════════════════════════════════════════════════════════════════════════════


class TestDisplayStepOutputs:
    """Tests for the ``DisplayStepOutputsForm`` output selection modal.

    Step output selection lets the author choose which output values
    to display in submission results.  This is now a cross-validator
    feature using a standalone form in a modal, not inline in the step
    config form.
    """

    def test_display_step_outputs_not_on_step_config_form(self):
        """The ``display_step_outputs`` field was moved off the step config form.

        It's now edited via a standalone modal (``DisplayStepOutputsForm``),
        not inline in the EnergyPlus step config form.
        """
        validator = _make_energyplus_validator()
        form = _make_form(validator=validator)

        assert "display_step_outputs" not in form.fields

    def test_display_step_outputs_form_choices_empty_without_step_io_definitions(self):
        """When the validator has no output value definitions, choices are empty.

        This is the typical case for a freshly created test validator.
        """
        from validibot.workflows.forms import DisplayStepOutputsForm

        validator = _make_energyplus_validator()
        form = DisplayStepOutputsForm(validator=validator)

        assert form.fields["display_step_outputs"].choices == []


# ══════════════════════════════════════════════════════════════════════════════
# Unified step I/O — build_step_io_context() helper
# ══════════════════════════════════════════════════════════════════════════════
# The unified "Inputs and Outputs" card merges validator-owned definitions
# (from StepIODefinition rows) with step-owned template/FMU I/O definitions into
# a single view. ``build_step_io_context()`` is the
# view-layer helper that builds this merged representation.
# ══════════════════════════════════════════════════════════════════════════════


class TestBuildStepIOContext(TestCase):
    """Tests for ``build_step_io_context()`` — the view helper
    that queries ``StepIODefinition`` and ``StepInputBinding`` rows to build
    unified input/output value lists.

    The helper produces four keys: ``input_values``, ``output_values``,
    ``has_inputs``, ``has_outputs``.  Step inputs come from two ownership
    levels: step-owned ``StepIODefinition`` rows (e.g. template variables)
    and validator-owned ``StepIODefinition`` rows (catalog step I/O).
    When step-owned inputs exist, validator-owned inputs are excluded.
    Step outputs come from both levels and are always merged.
    Each output is annotated with ``show_to_user`` based on the step's
    ``display_step_outputs`` config.
    """

    def test_template_variables_only(self):
        """Steps with template-origin step I/O definitions still show inputs.

        This is the typical case for an EnergyPlus template-mode step:
        step-owned StepIODefinition rows with origin_kind="template" and
        StepInputBinding rows controlling defaults and required status.
        """
        from validibot.validations.tests.factories import StepInputBindingFactory
        from validibot.validations.tests.factories import StepIODefinitionFactory
        from validibot.workflows.views_helpers import build_step_io_context

        step = WorkflowStepFactory(config={})

        # First variable: no default → required
        sig0 = StepIODefinitionFactory(
            workflow_step=step,
            validator=None,
            contract_key="u_factor",
            native_name="$U_FACTOR",
            label="U-Factor",
            direction="input",
            origin_kind="template",
            order=0,
        )
        StepInputBindingFactory(
            workflow_step=step,
            io_definition=sig0,
            source_data_path="u_factor",
            default_value=None,
            is_required=True,
        )

        # Second variable: has default → not required
        sig1 = StepIODefinitionFactory(
            workflow_step=step,
            validator=None,
            contract_key="shgc",
            native_name="$SHGC",
            label="",
            direction="input",
            origin_kind="template",
            order=1,
        )
        StepInputBindingFactory(
            workflow_step=step,
            io_definition=sig1,
            source_data_path="shgc",
            default_value="0.4",
            is_required=False,
        )

        result = build_step_io_context(step=step)

        assert result["has_inputs"] is True
        assert result["has_outputs"] is False
        assert len(result["input_values"]) == 2  # noqa: PLR2004

        # First variable: no default → required
        inp0 = result["input_values"][0]
        assert inp0["slug"] == "u_factor"
        assert inp0["label"] == "U-Factor"
        assert inp0["source"] == "template"
        assert inp0["required"] is True

        # Second variable: has default → not required; label falls back
        # to native_name when label is empty.
        inp1 = result["input_values"][1]
        assert inp1["slug"] == "shgc"
        assert inp1["source"] == "template"
        assert inp1["required"] is False

    def test_validator_step_io_definitions_only(self):
        """Steps may have validator-owned definitions but no step-owned ones.

        This is the typical case for validators like FMU or THERM that
        define their inputs and outputs entirely through validator-level
        ``StepIODefinition`` rows.
        """
        from validibot.validations.tests.factories import StepIODefinitionFactory
        from validibot.workflows.views_helpers import build_step_io_context

        validator = _make_energyplus_validator()
        step = WorkflowStepFactory(config={}, validator=validator)

        StepIODefinitionFactory(
            validator=validator,
            workflow_step=None,
            contract_key="weather-file",
            label="Weather File",
            direction="input",
            origin_kind="catalog",
        )
        StepIODefinitionFactory(
            validator=validator,
            workflow_step=None,
            contract_key="total-energy",
            label="Total Energy",
            direction="output",
            origin_kind="catalog",
        )

        result = build_step_io_context(step=step)

        assert result["has_inputs"] is True
        assert result["has_outputs"] is True
        assert len(result["input_values"]) == 1
        assert len(result["output_values"]) == 1

        assert result["input_values"][0]["slug"] == "weather-file"
        assert result["input_values"][0]["source"] == "catalog"
        assert result["output_values"][0]["slug"] == "total-energy"
        assert result["output_values"][0]["show_to_user"] is False

    def test_template_vars_merge_with_catalog_inputs(self):
        """Step-owned template variables merge with validator-owned catalog inputs.

        Per the May 2026 P1/P2 review: template-mode EnergyPlus steps
        legitimately need BOTH groups visible in the Step Inputs table:

        - Step-owned rows expose the template variables the submitter
          provides (height, U-factor, setpoint, etc.).
        - Validator-owned catalog rows expose the parser facts the
          validator extracts from the resolved IDF (i.zone_count,
          i.idf_version, i.north_axis_deg, etc.).

        Both populate ``i.*`` at input stage and can be promoted to
        ``s.<name>``. Authors need to see and promote both. The old
        behaviour — "hide validator inputs whenever step inputs exist" —
        cut template-mode users off from parser facts in the UI even
        though autocomplete and runtime still knew about them.

        Dedup is by ``contract_key``: if a step-owned row exists for a
        key, the validator-owned row of the same key is skipped (the
        step-owned row is the more specific declaration). Different
        keys both render.
        """
        from validibot.validations.tests.factories import StepInputBindingFactory
        from validibot.validations.tests.factories import StepIODefinitionFactory
        from validibot.workflows.views_helpers import build_step_io_context

        validator = _make_energyplus_validator()
        step = WorkflowStepFactory(config={}, validator=validator)

        # Validator-owned input (parser fact — must appear).
        StepIODefinitionFactory(
            validator=validator,
            workflow_step=None,
            contract_key="zone_count",
            label="Zone Count",
            direction="input",
            origin_kind="catalog",
        )
        # Validator-owned output (should appear).
        StepIODefinitionFactory(
            validator=validator,
            workflow_step=None,
            contract_key="total-energy",
            label="Total Energy",
            direction="output",
            origin_kind="catalog",
        )

        # Step-owned template input (different key — appears alongside).
        io_definition = StepIODefinitionFactory(
            workflow_step=step,
            validator=None,
            contract_key="u_factor",
            native_name="$U_FACTOR",
            label="U-Factor",
            direction="input",
            origin_kind="template",
        )
        StepInputBindingFactory(
            workflow_step=step,
            io_definition=io_definition,
            source_data_path="u_factor",
            is_required=True,
        )

        result = build_step_io_context(step=step)

        slugs = {row["slug"] for row in result["input_values"]}
        # Both step-owned and validator-owned inputs render.
        assert slugs == {"u_factor", "zone_count"}, (
            f"Expected both template var u_factor AND catalog parser "
            f"fact zone_count, got {slugs}. The Step Inputs table must "
            f"surface validator-owned parser facts so authors can "
            f"promote them in template-mode steps."
        )
        # Step outputs from validator still present.
        assert result["has_outputs"] is True
        assert result["output_values"][0]["slug"] == "total-energy"

    def test_step_owned_input_overrides_validator_owned_same_key(self):
        """When step-owned and validator-owned rows share a contract_key,
        the step-owned row wins (more specific declaration).

        Regression guard for the dedup logic in
        ``build_step_io_context``: only one row per
        contract_key, with the step-owned row taking precedence.
        """
        from validibot.validations.tests.factories import StepIODefinitionFactory
        from validibot.workflows.views_helpers import build_step_io_context

        validator = _make_energyplus_validator()
        step = WorkflowStepFactory(config={}, validator=validator)

        StepIODefinitionFactory(
            validator=validator,
            workflow_step=None,
            contract_key="shared_key",
            label="Validator label",
            direction="input",
            origin_kind="catalog",
        )
        step_row = StepIODefinitionFactory(
            workflow_step=step,
            validator=None,
            contract_key="shared_key",
            native_name="$SHARED_KEY",
            label="Step label",
            direction="input",
            origin_kind="template",
        )

        result = build_step_io_context(step=step)
        rows = [r for r in result["input_values"] if r["slug"] == "shared_key"]
        assert len(rows) == 1
        assert rows[0]["io_definition"].pk == step_row.pk

    def test_empty_step_produces_no_step_io(self):
        """A step with no step I/O definitions at all is empty.

        This happens for newly created steps before any definitions have
        been synced from a catalog, template scan, or FMU probe.
        """
        from validibot.workflows.views_helpers import build_step_io_context

        step = WorkflowStepFactory(config={})

        result = build_step_io_context(step=step)

        assert result["has_inputs"] is False
        assert result["has_outputs"] is False
        assert result["input_values"] == []
        assert result["output_values"] == []

    def test_output_display_step_outputs_filtering(self):
        """Step outputs respect the step's ``display_step_outputs`` config.

        When ``display_step_outputs`` lists specific contract_keys, only those
        outputs have ``show_to_user=True``.  Others are still returned
        but marked hidden.
        """
        from validibot.validations.tests.factories import StepIODefinitionFactory
        from validibot.workflows.views_helpers import build_step_io_context

        validator = _make_energyplus_validator()
        step = WorkflowStepFactory(
            display_settings={"display_step_outputs": ["total-energy"]},
            validator=validator,
        )

        StepIODefinitionFactory(
            validator=validator,
            workflow_step=None,
            contract_key="total-energy",
            label="Total Energy",
            direction="output",
            origin_kind="catalog",
            order=0,
        )
        StepIODefinitionFactory(
            validator=validator,
            workflow_step=None,
            contract_key="peak-load",
            label="Peak Load",
            direction="output",
            origin_kind="catalog",
            order=1,
        )

        result = build_step_io_context(step=step)

        shown = [s for s in result["output_values"] if s["show_to_user"]]
        hidden = [s for s in result["output_values"] if not s["show_to_user"]]
        assert len(shown) == 1
        assert shown[0]["slug"] == "total-energy"
        assert len(hidden) == 1
        assert hidden[0]["slug"] == "peak-load"

    def test_no_outputs_shown_when_display_step_outputs_empty(self):
        """When ``display_step_outputs`` is absent, outputs are hidden.

        Output exposure is opt-in, so the default cannot leak validator
        results to submitters.
        """
        from validibot.validations.tests.factories import StepIODefinitionFactory
        from validibot.workflows.views_helpers import build_step_io_context

        validator = _make_energyplus_validator()
        step = WorkflowStepFactory(config={}, validator=validator)

        StepIODefinitionFactory(
            validator=validator,
            workflow_step=None,
            contract_key="total-energy",
            label="Total Energy",
            direction="output",
            origin_kind="catalog",
        )

        result = build_step_io_context(step=step)

        assert result["output_values"][0]["show_to_user"] is False

    def test_validator_owned_input_values_shown_when_no_step_inputs(self):
        """Validator-owned step inputs appear when no step-owned inputs exist.

        This replaces the old "input derivations included" test.  In the
        unified model, derivations are in the separate ``Derivation`` model
        and are not returned by ``build_step_io_context()``.
        Instead, this test verifies that validator-level step inputs
        (origin_kind="catalog") appear when the step has no step-owned
        step inputs, and that definitions without a binding default to
        required.
        """
        from validibot.validations.tests.factories import StepIODefinitionFactory
        from validibot.workflows.views_helpers import build_step_io_context

        validator = _make_energyplus_validator()
        step = WorkflowStepFactory(config={}, validator=validator)

        StepIODefinitionFactory(
            validator=validator,
            workflow_step=None,
            contract_key="window-ratio",
            label="Window-to-Wall Ratio",
            direction="input",
            origin_kind="catalog",
        )

        result = build_step_io_context(step=step)

        assert len(result["input_values"]) == 1
        assert result["input_values"][0]["slug"] == "window-ratio"
        assert result["input_values"][0]["source"] == "catalog"
        # No binding → defaults to required
        assert result["input_values"][0]["required"] is True


# ══════════════════════════════════════════════════════════════════════════════
# SingleTemplateVariableForm — per-variable modal editing
# ══════════════════════════════════════════════════════════════════════════════
# The per-variable edit form replaces the old "save all at once" annotation
# form.  Each template variable gets its own modal with this form.
# ══════════════════════════════════════════════════════════════════════════════


class TestSingleTemplateVariableForm:
    """Tests for ``SingleTemplateVariableForm`` — the per-variable edit form.

    This form is rendered in a modal when the user clicks "Edit" on a
    template-source step input.  It populates initial values from the
    step's ``StepIODefinition`` rows (origin_kind=TEMPLATE).
    """

    def test_initial_values_from_variable(self):
        """The form pre-populates fields from the variable dict.

        This ensures the modal shows the current annotations when opened,
        not blank fields.
        """
        from validibot.workflows.forms import SingleTemplateVariableForm

        variable = {
            "name": "U_FACTOR",
            "description": "U-Factor value",
            "default": "3.5",
            "units": "W/m2-K",
            "variable_type": "number",
            "min_value": 0.1,
            "min_exclusive": False,
            "max_value": 10.0,
            "max_exclusive": True,
            "choices": [],
        }

        form = SingleTemplateVariableForm(variable=variable)

        assert form.fields["description"].initial == "U-Factor value"
        assert form.fields["default"].initial == "3.5"
        assert form.fields["units"].initial == "W/m2-K"
        assert form.fields["variable_type"].initial == "number"
        assert form.fields["min_value"].initial == "0.1"
        assert form.fields["min_exclusive"].initial is False
        assert form.fields["max_value"].initial == "10.0"
        assert form.fields["max_exclusive"].initial is True
        assert form.fields["choices"].initial == ""

    def test_default_initial_values_without_variable(self):
        """Without a variable dict, the form has sensible defaults.

        This covers the edge case where the form is instantiated
        without context (shouldn't happen in practice but keeps
        the form robust).
        """
        from validibot.workflows.forms import SingleTemplateVariableForm

        form = SingleTemplateVariableForm()

        assert form.fields["variable_type"].initial == "text"
        assert form.fields["description"].initial is None

    def test_valid_submission(self):
        """A complete valid form submission passes validation.

        All fields are optional except ``variable_type`` which defaults
        to "text", so even minimal data should validate.
        """
        from validibot.workflows.forms import SingleTemplateVariableForm

        form = SingleTemplateVariableForm(
            data={
                "description": "Solar heat gain",
                "default": "0.4",
                "units": "",
                "variable_type": "number",
                "min_value": "0",
                "min_exclusive": False,
                "max_value": "1",
                "max_exclusive": False,
                "choices": "",
            },
        )

        assert form.is_valid()

    def test_minimal_submission_valid(self):
        """A submission with just the required radio field validates.

        This verifies that all text fields are truly optional — a user
        can open the modal, leave everything blank, and save.
        """
        from validibot.workflows.forms import SingleTemplateVariableForm

        form = SingleTemplateVariableForm(
            data={
                "description": "",
                "default": "",
                "units": "",
                "variable_type": "text",
                "min_value": "",
                "max_value": "",
                "choices": "",
            },
        )

        assert form.is_valid()

    def test_choices_initial_from_list(self):
        """A variable with choices gets them joined as newline-separated text.

        The form stores choices as a textarea, one per line.
        """
        from validibot.workflows.forms import SingleTemplateVariableForm

        variable = {
            "name": "GLAZING_TYPE",
            "choices": ["single", "double", "triple"],
        }

        form = SingleTemplateVariableForm(variable=variable)

        assert form.fields["choices"].initial == "single\ndouble\ntriple"


# ══════════════════════════════════════════════════════════════════════════════
# WorkflowStepTemplateVariableEditView — per-variable edit endpoint
# ══════════════════════════════════════════════════════════════════════════════
# The view handles GET (render modal content) and POST (save annotations
# for a single variable).  It's an HTMx endpoint that returns modal content
# or a 204 with refresh trigger.
# ══════════════════════════════════════════════════════════════════════════════


class TestTemplateVariableEditView:
    """Tests for ``WorkflowStepTemplateVariableEditView`` — the per-variable
    modal edit endpoint.

    GET returns the modal form content for a specific template variable.
    POST saves the annotations and triggers a page reload.
    """

    def _make_step_with_variables(self):
        """Create a workflow step with template step I/O definitions for testing.

        Returns a (workflow, step) tuple.  Uses ``with_owner=True`` so
        the factory auto-creates an org membership for the workflow user.
        Step I/O definitions are created via sync_step_template_io_definitions.
        """
        from validibot.validations.services.template_step_io import (
            sync_step_template_io_definitions,
        )

        workflow = WorkflowFactory(with_owner=True)
        step = WorkflowStepFactory(workflow=workflow)
        sync_step_template_io_definitions(
            step,
            [
                {
                    "name": "U_FACTOR",
                    "description": "U-Factor",
                    "default": "",
                    "units": "W/m2-K",
                    "variable_type": "number",
                },
                {
                    "name": "SHGC",
                    "description": "Solar Heat Gain",
                    "default": "0.4",
                    "units": "",
                    "variable_type": "text",
                },
            ],
        )
        return workflow, step

    def _login(self, client, workflow):
        """Log in as the workflow user with session org set."""
        client.force_login(workflow.user)
        session = client.session
        session["active_org_id"] = workflow.org_id
        session.save()

    def _url(self, workflow, step, var_index):
        """Build the template variable edit URL."""
        return (
            f"/app/workflows/{workflow.pk}"
            f"/steps/{step.pk}/template-variable/{var_index}/"
        )

    def test_get_renders_modal_content(self, client):
        """GET returns the modal form pre-populated with variable data.

        The response should contain the form fields and the variable's
        current name (which is immutable and shown as a header).
        """
        workflow, step = self._make_step_with_variables()
        self._login(client, workflow)

        response = client.get(self._url(workflow, step, 0))

        assert response.status_code == HTTPStatus.OK
        content = response.content.decode()
        assert "U_FACTOR" in content

    def test_get_response_renders_complete_modal(self, client):
        """The modal response includes the full form structure and JS toggle.

        This test verifies the template renders correctly end-to-end:
        the form with all expected fields, the modal footer with
        buttons, and the type-toggle script.  This catches template
        rendering errors (e.g. malformed Django comments leaking into
        ``<script>`` blocks) that would silently break the modal in
        the browser.
        """
        workflow, step = self._make_step_with_variables()
        self._login(client, workflow)

        response = client.get(self._url(workflow, step, 0))
        content = response.content.decode()

        # Modal structure
        assert "modal-header" in content
        assert "modal-body" in content
        assert "modal-footer" in content

        # Form fields present
        assert 'name="description"' in content
        assert 'name="default"' in content
        assert 'name="variable_type"' in content

        # Type-toggle JS renders cleanly (no Django template syntax leaks)
        assert "tplvar-number-fields" in content
        assert "tplvar-choice-fields" in content
        # The IIFE that toggles type-specific fields
        assert "addEventListener" in content

    def test_post_saves_single_variable(self, client):
        """POST updates only the targeted variable, preserving others.

        After saving, the variable at index 0 should have the new
        description and units, while the variable at index 1 is unchanged.
        """
        workflow, step = self._make_step_with_variables()
        self._login(client, workflow)

        response = client.post(
            self._url(workflow, step, 0),
            {
                "description": "Updated U-Factor Label",
                "default": "2.5",
                "units": "BTU/h-ft2-F",
                "variable_type": "number",
                "min_value": "0.1",
                "min_exclusive": "",
                "max_value": "10",
                "max_exclusive": "",
                "choices": "",
            },
        )

        assert response.status_code == HTTPStatus.NO_CONTENT

        # Verify the step I/O definition was updated
        from validibot.validations.constants import StepIOOriginKind
        from validibot.validations.models import StepInputBinding

        bindings = list(
            StepInputBinding.objects.filter(
                workflow_step=step,
                io_definition__origin_kind=StepIOOriginKind.TEMPLATE,
            )
            .select_related("io_definition")
            .order_by(
                "io_definition__order",
                "io_definition__contract_key",
            )
        )
        sig0 = bindings[0].io_definition
        assert sig0.native_name == "U_FACTOR"  # Name preserved
        assert sig0.label == "Updated U-Factor Label"
        assert bindings[0].default_value == "2.5"
        assert sig0.unit == "BTU/h-ft2-F"

        # Second variable untouched
        sig1 = bindings[1].io_definition
        assert sig1.native_name == "SHGC"
        assert sig1.label == "Solar Heat Gain"

    def test_invalid_index_returns_404(self, client):
        """Requesting a variable index beyond the list returns 404.

        This prevents index-out-of-range errors if the template
        variables change between when the page loaded and when the
        user clicks edit.
        """
        workflow, step = self._make_step_with_variables()
        self._login(client, workflow)

        response = client.get(self._url(workflow, step, 99))

        assert response.status_code == HTTPStatus.NOT_FOUND

    def test_post_preserves_variable_name(self, client):
        """POST cannot change the variable name — it's immutable.

        The name comes from the IDF template and must remain stable
        for parameter substitution to work.  Even if a crafted POST
        tries to change it, the view ignores the form and uses the
        stored name.
        """
        workflow, step = self._make_step_with_variables()
        self._login(client, workflow)

        response = client.post(
            self._url(workflow, step, 0),
            {
                "description": "Hacked name attempt",
                "default": "",
                "units": "",
                "variable_type": "text",
                "min_value": "",
                "max_value": "",
                "choices": "",
            },
        )

        assert response.status_code == HTTPStatus.NO_CONTENT
        # Name (native_name) is preserved from the original, not from form data
        from validibot.validations.constants import StepIOOriginKind
        from validibot.validations.models import StepIODefinition

        io_definition = (
            StepIODefinition.objects.filter(
                workflow_step=step,
                origin_kind=StepIOOriginKind.TEMPLATE,
            )
            .order_by("order", "contract_key")
            .first()
        )
        assert io_definition.native_name == "U_FACTOR"


# ===========================================================================
# Phase 4: Launcher integration tests
# ===========================================================================
# These tests exercise `launch_energyplus_validation()` with real Django
# models (factories create actual DB rows) but mock the external I/O
# layer (GCS uploads, Cloud Run job triggering, callback URL building).
#
# Architecture note: Template preprocessing (parameter merging, validation,
# substitution) now happens *before* the launcher is called, in the shared
# `AdvancedValidator.validate()` → `EnergyPlusValidator.preprocess_
# submission()` pipeline.  By the time `launch_energyplus_validation()` is
# called, `submission.get_content()` already returns a fully resolved IDF.
# Template-specific tests (merge, validate, substitute) now live in
# `test_energyplus_preprocessing.py`.
#
# The launcher tests here verify the upload and job-trigger wiring only.
# ===========================================================================

# Common mock targets — all live in the launcher module's namespace.
_PATCH_PREFIX = "validibot.validations.services.cloud_run.launcher"


def _uploaded_file_identity(*, content, uri, content_type=None):
    """Return the strict GCS identity assigned by mocked launcher uploads."""
    del content_type
    content_bytes = content.encode() if isinstance(content, str) else content
    return FileIdentity(
        uri=uri,
        size_bytes=len(content_bytes),
        sha256=hashlib.sha256(content_bytes).hexdigest(),
        storage_version="1700000000000000",
    )


def _stored_resource_identity(step_resource):
    """Return GCS metadata for a reusable weather resource outside the attempt."""
    source = step_resource.validator_resource_file
    return FileIdentity(
        uri=f"gs://test-bucket/validator-resources/{source.pk}/weather.epw",
        size_bytes=1,
        sha256=source.content_hash or "a" * 64,
        storage_version="1700000000000001",
    )


def _copied_resource_identity(
    *,
    source_uri,
    source_generation,
    destination_uri,
    expected_size_bytes,
    expected_sha256,
):
    """Model the immutable generation copy into the attempt-scoped prefix."""
    del source_uri, source_generation
    return FileIdentity(
        uri=destination_uri,
        size_bytes=expected_size_bytes,
        sha256=expected_sha256,
        storage_version="1700000000000002",
    )


def _launcher_mocks():
    """Return patch objects for the launcher's external provider I/O.

    Usage::

        with _launcher_mocks() as mocks:
            mocks["run_validator_job"].return_value = "exec-abc"
            result = launch_energyplus_validation(...)
    """
    return {
        "upload_file": patch(f"{_PATCH_PREFIX}.upload_file"),
        "upload_envelope": patch(f"{_PATCH_PREFIX}.upload_envelope"),
        "stored_resource_identity": patch(
            "validibot.validations.services.resolved_files."
            "_stored_workflow_resource_identity",
            side_effect=_stored_resource_identity,
        ),
        "copy_gcs_file_generation": patch(
            "validibot.validations.services.cloud_run.gcs_runtime_capabilities."
            "copy_gcs_file_generation",
            side_effect=_copied_resource_identity,
        ),
        "issue_gcs_capability": patch(
            "validibot.validations.services.cloud_run.gcs_runtime_capabilities."
            "issue_attempt_gcs_runtime_capability",
            return_value=object(),
        ),
        "run_validator_job": patch(
            f"{_PATCH_PREFIX}.run_validator_job",
            return_value="executions/test-exec-001",
        ),
        "build_callback_url": patch(
            f"{_PATCH_PREFIX}.build_validation_callback_url",
            return_value="https://worker.test/api/v1/validation-callbacks/",
        ),
    }


class _LauncherMocks:
    """Context manager that patches all external I/O for launcher tests.

    Provides attribute access to each mock so tests can inspect call args.
    Also patches Django settings required by the launcher (GCS bucket,
    Cloud Run job name, GCP project/region).
    """

    def __enter__(self):
        self._patchers = []
        # The launcher resolves the Cloud Run Job name via
        # ValidatorConfig.cloud_run_job_name (May 2026 standardisation),
        # not via a GCS_ENERGYPLUS_JOB_NAME setting. The EnergyPlus
        # validator config registered at startup yields
        # ``validibot-validator-backend-energyplus``, which is what
        # the launcher uses in this test — no setting patch needed
        # for the job name.
        self._settings_patcher = patch.multiple(
            "django.conf.settings",
            GCS_VALIDATION_BUCKET="test-bucket",
            GCP_PROJECT_ID="test-project",
            GCP_REGION="us-central1",
        )
        self._settings_patcher.start()
        self._patchers.append(self._settings_patcher)

        targets = _launcher_mocks()
        for name, patcher in targets.items():
            mock_obj = patcher.start()
            setattr(self, name, mock_obj)
            self._patchers.append(patcher)

        # Default return value for job execution name
        self.upload_file.side_effect = _uploaded_file_identity
        self.run_validator_job.return_value = "executions/test-exec-001"
        self.build_callback_url.return_value = (
            "https://worker.test/api/v1/validation-callbacks/"
        )
        return self

    def __exit__(self, *exc):
        for patcher in reversed(self._patchers):
            patcher.stop()


def _make_launcher_fixtures(
    *,
    template_content: str | None = None,
    submission_content: str = "{}",
    step_config: dict | None = None,
):
    """Create the full model graph needed by ``launch_energyplus_validation()``.

    Creates: Validator → WorkflowStep → WorkflowStepResource(WEATHER_FILE)
    → Submission → ValidationRun → ValidationStepRun(PENDING).

    If ``template_content`` is provided, also creates a
    ``WorkflowStepResource(MODEL_TEMPLATE)`` with that IDF content as the
    step-owned file.

    Returns a dict with keys: ``run``, ``validator``, ``submission``,
    ``step``, ``step_run``.
    """
    from validibot.submissions.constants import SubmissionDataFormat
    from validibot.submissions.tests.factories import SubmissionFactory
    from validibot.validations.constants import ArtifactKind
    from validibot.validations.constants import BindingSourceScope
    from validibot.validations.constants import CatalogValueType
    from validibot.validations.constants import EnvelopeChannel
    from validibot.validations.constants import ResourceFileType
    from validibot.validations.constants import StepIODirection
    from validibot.validations.constants import StepIOMedium
    from validibot.validations.constants import StepIOSourceKind
    from validibot.validations.tests.factories import StepInputBindingFactory
    from validibot.validations.tests.factories import StepIODefinitionFactory

    # 1. Validator (EnergyPlus type)
    validator = ValidatorFactory(
        validation_type=ValidationType.ENERGYPLUS,
    )

    # 2. Workflow and step — step must belong to the run's workflow,
    #    so we create the workflow first, then wire everything through it.
    workflow = WorkflowFactory(org=validator.org or WorkflowFactory().org)
    step = WorkflowStepFactory(
        workflow=workflow,
        validator=validator,
        config=step_config or {},
    )

    # 3. Weather file resource (required by the envelope builder)
    weather_vrf = ValidatorResourceFileFactory(validator=validator)
    WorkflowStepResourceFactory(
        step=step,
        role=WorkflowStepResource.WEATHER_FILE,
        validator_resource_file=weather_vrf,
    )

    # Launcher fixtures use the same validator-owned catalog and per-step
    # bindings as a synced installation. This keeps the integration test on
    # the single declared-port path used in production.
    primary_model_port = StepIODefinitionFactory(
        validator=validator,
        workflow_step=None,
        contract_key="primary_model",
        native_name="primary_model",
        direction=StepIODirection.INPUT,
        source_kind=StepIOSourceKind.PAYLOAD_PATH,
        data_type=CatalogValueType.ARTIFACT_REF,
        io_medium=StepIOMedium.ARTIFACT,
        artifact_kind=ArtifactKind.FILE,
        media_type="application/vnd.energyplus.idf",
        data_format=SubmissionDataFormat.ENERGYPLUS_IDF,
        accepted_data_formats=[
            SubmissionDataFormat.ENERGYPLUS_IDF,
            SubmissionDataFormat.ENERGYPLUS_EPJSON,
        ],
        accepted_media_types=[
            "application/vnd.energyplus.idf",
            "application/vnd.energyplus.epjson",
        ],
        metadata={"accepted_extensions": ["idf", "epjson", "json"]},
        envelope_channel=EnvelopeChannel.INPUT_FILES,
        role="primary-model",
        min_items=1,
        max_items=1,
        allowed_source_scopes=[
            BindingSourceScope.SUBMISSION_FILE,
            BindingSourceScope.UPSTREAM_ARTIFACT,
        ],
    )
    weather_file_port = StepIODefinitionFactory(
        validator=validator,
        workflow_step=None,
        contract_key="weather_file",
        native_name="weather_file",
        direction=StepIODirection.INPUT,
        source_kind=StepIOSourceKind.PAYLOAD_PATH,
        data_type=CatalogValueType.ARTIFACT_REF,
        io_medium=StepIOMedium.ARTIFACT,
        artifact_kind=ArtifactKind.FILE,
        media_type="application/vnd.energyplus.epw",
        data_format=ResourceFileType.ENERGYPLUS_WEATHER,
        accepted_data_formats=[ResourceFileType.ENERGYPLUS_WEATHER],
        accepted_media_types=["application/vnd.energyplus.epw"],
        metadata={"accepted_extensions": ["epw"]},
        envelope_channel=EnvelopeChannel.RESOURCE_FILES,
        resource_type=ResourceFileType.ENERGYPLUS_WEATHER,
        role="weather",
        min_items=1,
        max_items=1,
        allowed_source_scopes=[BindingSourceScope.WORKFLOW_RESOURCE],
    )
    StepInputBindingFactory(
        workflow_step=step,
        io_definition=primary_model_port,
        source_scope=BindingSourceScope.SUBMISSION_FILE,
        source_data_path="primary_file_uri",
    )
    StepInputBindingFactory(
        workflow_step=step,
        io_definition=weather_file_port,
        source_scope=BindingSourceScope.WORKFLOW_RESOURCE,
        source_data_path=ResourceFileType.ENERGYPLUS_WEATHER,
    )

    # 4. Optionally add a template resource (step-owned file)
    if template_content is not None:
        WorkflowStepResourceFactory(
            step=step,
            role=WorkflowStepResource.MODEL_TEMPLATE,
            validator_resource_file=None,
            step_resource_file=SimpleUploadedFile(
                "template.idf",
                template_content.encode("utf-8"),
                content_type="text/plain",
            ),
            filename="template.idf",
            resource_type="energyplus_model_template",
        )

    # 5. Submission with specified content
    submission = SubmissionFactory(
        workflow=workflow,
        org=workflow.org,
        content=submission_content,
    )

    # 6. ValidationRun + StepRun + durable attempt (all PENDING)
    step_run = ValidationStepRunFactory(
        validation_run__workflow=workflow,
        validation_run__org=workflow.org,
        validation_run__submission=submission,
        workflow_step=step,
    )
    run = step_run.validation_run
    attempt = ExecutionAttemptFactory(step_run=step_run)

    return {
        "run": run,
        "validator": validator,
        "submission": submission,
        "step": step,
        "step_run": step_run,
        "attempt": attempt,
    }


class TestLauncherDirectMode:
    """Tests for complete IDF or epJSON submissions that need no template."""

    def test_direct_mode_uploads_submission_and_returns_pending(self):
        """When no MODEL_TEMPLATE resource exists, the submission is uploaded
        as-is to GCS and the launcher returns a pending result.

        Direct submissions and resolved templates share this launch boundary.
        """
        from validibot.validations.services.cloud_run.launcher import (
            launch_energyplus_validation,
        )

        fixtures = _make_launcher_fixtures(
            submission_content='{"version": "24.2"}',
        )

        with _LauncherMocks() as mocks:
            result = launch_energyplus_validation(
                run=fixtures["run"],
                validator=fixtures["validator"],
                submission=fixtures["submission"],
                ruleset=None,
                step=fixtures["step"],
            )

        # Should return pending (passed=None, no issues).
        # job_name is resolved at launch time via
        # ValidatorConfig.cloud_run_job_name → ``image_name`` →
        # ``validibot-validator-backend-energyplus`` (May 2026
        # standardisation; the legacy GCS_ENERGYPLUS_JOB_NAME env
        # var was removed).
        assert result.passed is None
        assert result.issues == []
        assert result.stats["job_name"] == "validibot-validator-backend-energyplus"
        assert result.stats["execution_name"] == "executions/test-exec-001"

        # Template metadata should NOT be present
        assert "template_parameters_used" not in result.stats
        assert "template_warnings" not in result.stats

        # upload_file should have been called with the raw submission content
        upload_call = mocks.upload_file.call_args
        assert b'{"version": "24.2"}' in upload_call.kwargs.get(
            "content", upload_call[1].get("content", b"")
        )

    def test_direct_mode_no_template_metadata_in_stats(self):
        """Direct mode stats contain job info but no template-related keys.

        Template metadata is added by ``AdvancedValidator.validate()`` after
        preprocessing, not by the launcher.  Direct-mode submissions skip
        preprocessing entirely, so no template keys should appear.
        """
        from validibot.validations.services.cloud_run.launcher import (
            launch_energyplus_validation,
        )

        fixtures = _make_launcher_fixtures()

        with _LauncherMocks():
            result = launch_energyplus_validation(
                run=fixtures["run"],
                validator=fixtures["validator"],
                submission=fixtures["submission"],
                ruleset=None,
                step=fixtures["step"],
            )

        # Core stats present
        assert "job_status" in result.stats
        assert "execution_bundle_uri" in result.stats

        # No template keys
        for key in (
            "template_parameters_used",
            "template_warnings",
            "template_original_uri",
        ):
            assert key not in result.stats


class TestLauncherWithPreprocessedSubmission:
    """Tests for the launcher when the submission has been preprocessed.

    After the template preprocessing refactor, template resolution (parameter
    merging, validation, substitution) happens in
    ``EnergyPlusValidator.preprocess_submission()`` **before** the launcher
    is called.  By that point, ``submission.content`` has been set to the
    resolved IDF and ``submission.original_filename`` to ``resolved_model.idf``.

    These tests verify the launcher correctly handles such pre-processed
    submissions — uploading the resolved content with the proper file
    extension and MIME type.
    """

    def test_preprocessed_submission_uploads_resolved_idf(self):
        """When a submission has been preprocessed (content set to resolved IDF),
        the launcher uploads the resolved content with ``.idf`` extension.

        This verifies the launcher's filename-based extension detection works
        correctly with ``submission.original_filename = 'resolved_model.idf'``
        set by the preprocessing step.
        """
        from validibot.validations.services.cloud_run.launcher import (
            launch_energyplus_validation,
        )

        resolved_idf_content = (
            "Version,\n    24.2;\n\n"
            "WindowMaterial:SimpleGlazingSystem,\n"
            "    Glazing System,\n    2.5,\n    0.4;\n"
        )

        # Create fixtures WITHOUT a template resource — the launcher doesn't
        # need to know about templates.  Simulate preprocessing by setting
        # the submission content to the resolved IDF.
        fixtures = _make_launcher_fixtures(
            submission_content=resolved_idf_content,
        )

        # Simulate what preprocessing does: set original_filename to .idf
        fixtures["submission"].original_filename = "resolved_model.idf"
        fixtures["submission"].save(update_fields=["original_filename"])

        with _LauncherMocks() as mocks:
            result = launch_energyplus_validation(
                run=fixtures["run"],
                validator=fixtures["validator"],
                submission=fixtures["submission"],
                ruleset=None,
                step=fixtures["step"],
            )

        assert result.passed is None
        assert result.issues == []

        # The upload should use .idf extension and text/plain content type
        upload_call = mocks.upload_file.call_args
        uploaded_uri = upload_call.kwargs.get("uri", upload_call[1].get("uri", ""))
        uploaded_content_type = upload_call.kwargs.get(
            "content_type", upload_call[1].get("content_type", "")
        )
        uploaded_content = upload_call.kwargs.get(
            "content", upload_call[1].get("content", b"")
        ).decode("utf-8")

        assert uploaded_uri.endswith("/model.idf")
        assert uploaded_content_type == "text/plain"
        assert "2.5" in uploaded_content
        assert "0.4" in uploaded_content

    def test_preprocessed_submission_no_template_metadata_in_launcher_stats(self):
        """The launcher itself no longer produces template metadata in its stats.

        Template metadata (``template_parameters_used``, ``template_warnings``)
        is now added by ``AdvancedValidator.validate()`` after preprocessing
        completes, not by the launcher.  The launcher stats contain only job
        tracking info (job_name, execution_name, etc.).
        """
        from validibot.validations.services.cloud_run.launcher import (
            launch_energyplus_validation,
        )

        fixtures = _make_launcher_fixtures(
            submission_content="Version,\n    24.2;\n",
        )
        fixtures["submission"].original_filename = "resolved_model.idf"
        fixtures["submission"].save(update_fields=["original_filename"])

        with _LauncherMocks():
            result = launch_energyplus_validation(
                run=fixtures["run"],
                validator=fixtures["validator"],
                submission=fixtures["submission"],
                ruleset=None,
                step=fixtures["step"],
            )

        # Launcher stats contain job info only — no template metadata
        assert "job_status" in result.stats
        assert "job_name" in result.stats
        assert "template_parameters_used" not in result.stats
        assert "template_warnings" not in result.stats
        assert "template_original_uri" not in result.stats
