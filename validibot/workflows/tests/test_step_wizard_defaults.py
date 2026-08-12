"""HTTP-safety tests for validator discovery in the workflow step wizard.

Validator contracts are synchronized explicitly and are never repaired while
rendering the picker. This suite guards the clean architecture boundary: a GET
may discover and serialize validators, but it cannot mutate catalog rows.
"""

from __future__ import annotations

import pytest
from django.test import RequestFactory

from validibot.validations.constants import ValidationType
from validibot.validations.tests.factories import ValidatorFactory
from validibot.workflows.tests.factories import WorkflowFactory
from validibot.workflows.views.steps import WorkflowStepWizardView

pytestmark = pytest.mark.django_db


def _wizard_view() -> WorkflowStepWizardView:
    """Return a wizard view configured for a safe GET request."""
    view = WorkflowStepWizardView()
    view.request = RequestFactory().get("/")
    return view


class TestValidatorDiscoveryHttpSafety:
    """GET discovery must remain read-only now that contracts are explicit."""

    def test_get_discovery_does_not_modify_validator(self):
        """Rendering candidates reads synchronized rows without repairing them."""

        workflow = WorkflowFactory()
        validator = ValidatorFactory(
            validation_type=ValidationType.FMU,
            is_system=True,
        )
        original_modified = validator.modified

        discovered = _wizard_view()._available_validators(workflow)

        validator.refresh_from_db()
        assert validator in discovered
        assert validator.modified == original_modified
        assert not hasattr(WorkflowStepWizardView, "_ensure_validator_defaults")
