"""EnergyPlus workflow tests for declared file-port admission failures.

The successful real-container lifecycle lives in
``tests_integration/test_docker_compose_execution.py``. This module keeps the
fast failure path deterministic: a simulation with no bound weather resource
must fail on the named ``weather_file`` contract before Docker is invoked.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from django.urls import reverse
from rest_framework.status import HTTP_200_OK
from rest_framework.status import HTTP_201_CREATED
from rest_framework.status import HTTP_202_ACCEPTED

from tests.helpers.polling import extract_issues
from tests.helpers.polling import normalize_poll_url
from tests.helpers.polling import poll_until_complete
from tests.helpers.workflows import create_workflow_step_with_default_bindings
from validibot.submissions.constants import SubmissionFileType
from validibot.users.models import RoleCode
from validibot.users.tests.factories import OrganizationFactory
from validibot.users.tests.factories import UserFactory
from validibot.users.tests.factories import grant_role
from validibot.validations.constants import RulesetType
from validibot.validations.constants import ValidationRunStatus
from validibot.validations.constants import ValidationType
from validibot.validations.tests.factories import RulesetFactory
from validibot.workflows.tests.factories import WorkflowFactory

pytestmark = pytest.mark.django_db


def load_example_epjson() -> str:
    base = Path(__file__).resolve().parent.parent / "data" / "energyplus"
    path = base / "example_epjson.json"
    return path.read_text(encoding="utf-8")


@pytest.fixture
def energyplus_workflow(api_client, system_validator_for):
    """Build a simulation step with production ports but no weather resource."""
    org = OrganizationFactory()
    user = UserFactory(orgs=[org])
    grant_role(user, org, RoleCode.EXECUTOR)
    user.set_current_org(org)

    validator = system_validator_for(ValidationType.ENERGYPLUS)

    ruleset = RulesetFactory(
        org=org,
        user=user,
        ruleset_type=RulesetType.ENERGYPLUS,
        rules_text="{}",
    )

    workflow = WorkflowFactory(
        org=org,
        user=user,
        allowed_file_types=[
            SubmissionFileType.TEXT,
            SubmissionFileType.JSON,
        ],
    )
    step = create_workflow_step_with_default_bindings(
        workflow=workflow,
        validator=validator,
        ruleset=ruleset,
        order=1,
        config={
            "run_simulation": True,
        },
    )

    api_client.force_authenticate(user=user)

    return {
        "org": org,
        "user": user,
        "validator": validator,
        "ruleset": ruleset,
        "workflow": workflow,
        "step": step,
        "client": api_client,
    }


@pytest.mark.django_db
class TestEnergyPlusValidation:
    """End-to-end failure coverage for EnergyPlus file-port resolution."""

    def test_missing_weather_resource_fails_before_dispatch(
        self,
        energyplus_workflow,
    ):
        """A required named weather port must never fall back to hidden config."""
        client = energyplus_workflow["client"]
        workflow = energyplus_workflow["workflow"]

        payload = load_example_epjson()

        # Use org-scoped route (ADR-2026-01-06)
        start_url = reverse(
            "api:org-workflows-runs",
            kwargs={"org_slug": workflow.org.slug, "pk": workflow.pk},
        )
        resp = client.post(
            start_url,
            data=payload,
            content_type="application/json",
        )

        assert resp.status_code in (HTTP_200_OK, HTTP_201_CREATED, HTTP_202_ACCEPTED), (
            resp.content
        )

        poll_url = normalize_poll_url(resp.headers.get("Location") or "")
        if not poll_url:
            run_id = resp.json().get("id")
            poll_url = reverse(
                "api:org-runs-detail",
                kwargs={"org_slug": workflow.org.slug, "pk": run_id},
            )

        data, status = poll_until_complete(client, poll_url)
        assert status == HTTP_200_OK
        assert data["status"] == ValidationRunStatus.FAILED

        issues = extract_issues(data)
        assert issues
        assert "weather_file" in " | ".join(
            str(issue.get("message", "")) for issue in issues
        )
