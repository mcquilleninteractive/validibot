"""Regression tests for code-backed application data used by every runtime.

Deployment and local startup synchronize the validator catalog before
reconciling bundled EnergyPlus weather files. Validator version history is
intentionally retained, so setup commands must bind new resources and
workflows to the exact contract declared by the running code rather than
assuming one row per validation type. These tests preserve that contract as
validator versions accumulate.
"""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from django.core.files.base import ContentFile
from django.core.management import call_command
from django.core.management.base import CommandError

from validibot.core.management.commands.seed_weather_files import WEATHER_FILES
from validibot.core.management.commands.setup_e2e_workflows import EP_WEATHER_FILENAME
from validibot.core.management.commands.setup_e2e_workflows import EP_WORKFLOW_SLUG
from validibot.core.management.commands.setup_e2e_workflows import Command
from validibot.projects.tests.factories import ProjectFactory
from validibot.users.tests.factories import OrganizationFactory
from validibot.users.tests.factories import UserFactory
from validibot.validations.constants import ResourceFileType
from validibot.validations.constants import StepIODirection
from validibot.validations.constants import StepIOOriginKind
from validibot.validations.constants import ValidationType
from validibot.validations.models import RulesetAssertion
from validibot.validations.models import StepInputBinding
from validibot.validations.models import StepIODefinition
from validibot.validations.models import ValidatorResourceFile
from validibot.validations.tests.factories import ValidatorFactory
from validibot.validations.validators.energyplus.config import (
    config as energyplus_config,
)
from validibot.workflows.tests.factories import WorkflowFactory

pytestmark = pytest.mark.django_db

EXPECTED_E2E_INPUT_BINDING_COUNT = 5


def _energyplus_validator(*, version: int):
    """Create one historical or current row in the EnergyPlus family."""
    return ValidatorFactory(
        slug=energyplus_config.slug,
        name=f"EnergyPlus contract v{version}",
        validation_type=ValidationType.ENERGYPLUS,
        version=version,
        is_system=True,
    )


def test_seed_weather_files_uses_current_energyplus_contract(tmp_path):
    """Weather seeding must tolerate retained validator version history.

    A version bump leaves the old row in place for existing workflows. Local
    startup must therefore complete without ``MultipleObjectsReturned`` and
    attach newly seeded files only to the contract shipped by the running code.
    """
    historical = _energyplus_validator(version=energyplus_config.version - 1)
    current = _energyplus_validator(version=energyplus_config.version)
    filename, _display_name = WEATHER_FILES[0]
    (tmp_path / filename).write_bytes(b"minimal EPW fixture")

    call_command("seed_weather_files", source_dir=str(tmp_path))
    call_command("seed_weather_files", source_dir=str(tmp_path))

    resources = ValidatorResourceFile.objects.filter(filename=filename)
    assert resources.count() == 1
    assert resources.get().validator == current
    assert not resources.filter(validator=historical).exists()


def test_strict_weather_reconciliation_populates_a_new_energyplus_version(tmp_path):
    """A semantic EnergyPlus version bump must receive its own complete catalogue.

    Historical resources remain attached to the contract that existing workflows
    used. Deployment reconciliation must create the five current-version rows
    even when one-time application initialization completed long ago.
    """
    historical = _energyplus_validator(version=energyplus_config.version - 1)
    current = _energyplus_validator(version=energyplus_config.version)
    historical_filename, historical_name = WEATHER_FILES[0]
    ValidatorResourceFile.objects.create(
        validator=historical,
        org=None,
        resource_type=ResourceFileType.ENERGYPLUS_WEATHER,
        name=historical_name,
        filename=historical_filename,
        file=ContentFile(b"historical weather", name=historical_filename),
    )
    for filename, _display_name in WEATHER_FILES:
        (tmp_path / filename).write_bytes(f"weather:{filename}".encode())

    call_command("seed_weather_files", source_dir=str(tmp_path), strict=True)
    call_command("seed_weather_files", source_dir=str(tmp_path), check=True)

    assert ValidatorResourceFile.objects.filter(validator=current).count() == len(
        WEATHER_FILES
    )
    assert ValidatorResourceFile.objects.filter(validator=historical).count() == 1


def test_strict_weather_reconciliation_rejects_an_incomplete_runtime_image(tmp_path):
    """A production image missing any bundled EPW must fail before service cutover."""
    _energyplus_validator(version=energyplus_config.version)
    filename, _display_name = WEATHER_FILES[0]
    (tmp_path / filename).write_bytes(b"only one weather asset")

    with pytest.raises(CommandError, match="catalogue is incomplete"):
        call_command("seed_weather_files", source_dir=str(tmp_path), strict=True)

    assert not ValidatorResourceFile.objects.exists()


def test_weather_check_detects_stored_object_drift(tmp_path):
    """Release preflight must hash durable bytes instead of trusting DB metadata.

    Normal startup can use the persisted content hash for an inexpensive
    idempotency check. Before provider routes change, ``--check`` must still
    read storage and catch an object that drifted independently of its row.
    """
    current = _energyplus_validator(version=energyplus_config.version)
    for filename, _display_name in WEATHER_FILES:
        (tmp_path / filename).write_bytes(f"weather:{filename}".encode())
    call_command("seed_weather_files", source_dir=str(tmp_path), strict=True)
    resource = ValidatorResourceFile.objects.get(
        validator=current,
        filename=WEATHER_FILES[0][0],
    )
    original_hash = resource.content_hash
    storage = resource.file.storage
    object_name = resource.file.name
    storage.delete(object_name)
    saved_name = storage.save(object_name, ContentFile(b"drifted weather"))
    assert saved_name == object_name

    with pytest.raises(CommandError, match="differs from the bundled asset"):
        call_command("seed_weather_files", source_dir=str(tmp_path), check=True)

    resource.refresh_from_db()
    assert resource.content_hash == original_hash


def test_setup_e2e_workflow_uses_current_energyplus_contract():
    """New E2E workflows must never bind to a retained historical contract.

    Default model ordering does not represent contract currency. Pinning this
    selection protects the generated workflow, its weather resource, and its
    assertions from silently using stale step I/O definitions.
    """
    historical = _energyplus_validator(version=energyplus_config.version - 1)
    current = _energyplus_validator(version=energyplus_config.version)
    current_weather = ValidatorResourceFile.objects.create(
        validator=current,
        org=None,
        resource_type=ResourceFileType.ENERGYPLUS_WEATHER,
        name="San Francisco weather",
        filename=EP_WEATHER_FILENAME,
        file=ContentFile(b"weather", name=EP_WEATHER_FILENAME),
    )
    ValidatorResourceFile.objects.create(
        validator=current,
        org=None,
        resource_type=ResourceFileType.ENERGYPLUS_WEATHER,
        name="Chicago weather",
        filename="USA_IL_Chicago-OHare.Intl.AP.725300_TMY3.epw",
        file=ContentFile(b"other weather", name="chicago.epw"),
    )
    ValidatorResourceFile.objects.create(
        validator=historical,
        org=None,
        resource_type=ResourceFileType.ENERGYPLUS_WEATHER,
        name="Historical weather",
        filename="historical.epw",
        file=ContentFile(b"old weather", name="historical.epw"),
    )
    org = OrganizationFactory()
    user = UserFactory()
    project = ProjectFactory(org=org)
    WorkflowFactory(
        org=org,
        user=user,
        project=project,
        slug=EP_WORKFLOW_SLUG,
        version=1,
    )
    command = Command()
    step = SimpleNamespace(ruleset=object())
    command._create_energyplus_step = Mock(return_value=step)
    command._attach_template_idf = Mock()
    command._attach_weather_file = Mock()
    command._create_output_assertions = Mock()

    workflow = command._ensure_energyplus_template_workflow(org, user, project)

    assert workflow is not None
    assert command._create_energyplus_step.call_args.args[1] == current
    command._attach_weather_file.assert_called_once_with(step, current_weather)
    assert command._create_output_assertions.call_args.args[1] == current


def test_setup_e2e_step_uses_relational_io_and_prefixed_output_expressions():
    """E2E provisioning must exercise the current architecture exclusively.

    A green E2E suite is meaningful only when its fixture uses relational
    template inputs, real bindings, and the same ``o.*`` CEL namespace authors
    use. Reintroducing the retired template JSON would otherwise let setup
    succeed while production preprocessing sees no parameters.
    """

    current = _energyplus_validator(version=energyplus_config.version)
    for contract_key in ("primary_model", "weather_file"):
        StepIODefinition.objects.create(
            validator=current,
            workflow_step=None,
            contract_key=contract_key,
            native_name=contract_key.replace("_", "-"),
            label=contract_key.replace("_", " ").title(),
            direction=StepIODirection.INPUT,
            origin_kind=StepIOOriginKind.CATALOG,
        )
    for order, contract_key in enumerate(
        ("window_heat_loss_kwh", "cooling_energy_kwh", "heating_energy_kwh"),
        start=1,
    ):
        StepIODefinition.objects.create(
            validator=current,
            workflow_step=None,
            contract_key=contract_key,
            native_name=contract_key,
            label=contract_key.replace("_", " ").title(),
            direction=StepIODirection.OUTPUT,
            origin_kind=StepIOOriginKind.CATALOG,
            order=order,
        )
    org = OrganizationFactory()
    user = UserFactory(orgs=[org])
    project = ProjectFactory(org=org)
    workflow = WorkflowFactory(org=org, user=user, project=project)
    command = Command()

    step = command._create_energyplus_step(workflow, current, org, user)
    command._create_output_assertions(step.ruleset, current)

    assert step.config == {"run_simulation": True, "case_sensitive": True}
    assert set(step.display_settings) == {"display_step_outputs"}
    template_definitions = StepIODefinition.objects.filter(
        workflow_step=step,
        origin_kind=StepIOOriginKind.TEMPLATE,
    )
    assert set(template_definitions.values_list("contract_key", flat=True)) == {
        "u_factor",
        "shgc",
        "visible_transmittance",
    }
    assert (
        StepInputBinding.objects.filter(workflow_step=step).count()
        == EXPECTED_E2E_INPUT_BINDING_COUNT
    )
    assert list(
        RulesetAssertion.objects.filter(ruleset=step.ruleset)
        .order_by("order")
        .values_list("rhs", flat=True),
    ) == [
        {"expr": "o.window_heat_loss_kwh < 800"},
        {"expr": "o.cooling_energy_kwh < o.heating_energy_kwh"},
    ]
