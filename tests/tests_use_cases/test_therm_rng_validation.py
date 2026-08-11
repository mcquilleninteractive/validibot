"""End-to-end tests for THERM (.thmx) file validation using RelaxNG.

These tests exercise the XML Schema validator with the THERM 8.x RNG schema
against real-world THMX files before a dedicated THERMValidator exists.
"""

from __future__ import annotations

import logging

import pytest
from django.urls import reverse
from rest_framework.status import HTTP_200_OK
from rest_framework.status import HTTP_201_CREATED
from rest_framework.status import HTTP_202_ACCEPTED

from tests.helpers.polling import extract_issues
from tests.helpers.polling import normalize_poll_url
from tests.helpers.polling import poll_until_complete
from tests.helpers.polling import start_workflow_url
from tests.helpers.workflows import create_workflow_step_with_default_bindings
from validibot.submissions.constants import SubmissionFileType
from validibot.users.models import RoleCode
from validibot.users.tests.factories import OrganizationFactory
from validibot.users.tests.factories import UserFactory
from validibot.users.tests.factories import grant_role
from validibot.validations.constants import ValidationRunStatus
from validibot.validations.constants import ValidationType
from validibot.validations.constants import XMLSchemaType
from validibot.validations.tests.factories import RulesetFactory
from validibot.workflows.tests.factories import WorkflowFactory

logger = logging.getLogger(__name__)
pytestmark = pytest.mark.django_db


@pytest.fixture
def therm_workflow_context(load_rng_asset, api_client, system_validator_for):
    """Build a workflow configured for THERM RNG validation."""
    org = OrganizationFactory()
    user = UserFactory(orgs=[org])
    user.set_current_org(org)

    grant_role(user, org, RoleCode.EXECUTOR)

    validator = system_validator_for(ValidationType.XML_SCHEMA)

    schema = load_rng_asset("therm.rng")

    ruleset = RulesetFactory(
        org=org,
        user=user,
        ruleset_type=ValidationType.XML_SCHEMA,
        rules_text=schema,
        metadata={
            "schema_type": XMLSchemaType.RELAXNG.name,
        },
    )

    workflow = WorkflowFactory(
        org=org,
        user=user,
        allowed_file_types=[SubmissionFileType.XML],
    )
    create_workflow_step_with_default_bindings(
        workflow=workflow,
        validator=validator,
        ruleset=ruleset,
        order=1,
    )

    api_client.force_authenticate(user=user)

    return {
        "org": org,
        "user": user,
        "validator": validator,
        "ruleset": ruleset,
        "workflow": workflow,
        "client": api_client,
    }


def _run_and_poll(
    client,
    workflow,
    content: str,
    content_type: str = "application/xml",
) -> dict:
    """Start the workflow, then poll until the run completes."""
    start_url = start_workflow_url(workflow)
    resp = client.post(start_url, data=content, content_type=content_type)
    assert resp.status_code in (HTTP_200_OK, HTTP_201_CREATED, HTTP_202_ACCEPTED), (
        resp.content
    )

    loc = resp.headers.get("Location") or resp.headers.get("location") or ""
    poll_url = normalize_poll_url(loc)
    if not poll_url:
        data = {}
        try:
            data = resp.json()
        except Exception as exc:
            logger.debug("Could not parse JSON response: %s", exc)
        run_id = data.get("id")
        if run_id:
            org_slug = workflow.org.slug
            try:
                poll_url = reverse(
                    "api:org-runs-detail",
                    kwargs={"org_slug": org_slug, "pk": run_id},
                )
            except Exception as exc:
                logger.debug("Could not reverse org-runs-detail: %s", exc)
                poll_url = f"/api/v1/orgs/{org_slug}/runs/{run_id}/"

    data, last_status = poll_until_complete(client, poll_url)
    assert last_status == HTTP_200_OK, f"Polling failed: {last_status} {data}"
    return data


@pytest.mark.django_db
class TestThermRngValidation:
    """THERM .thmx file validation via RelaxNG schema."""

    def test_valid_thmx_passes(self, load_thmx_asset, therm_workflow_context):
        """A well-formed THMX file should pass the THERM RNG schema."""
        client = therm_workflow_context["client"]
        workflow = therm_workflow_context["workflow"]
        content = load_thmx_asset("sample_sill_CMA.thmx")
        data = _run_and_poll(client, workflow, content=content)
        run_status = (data.get("status") or data.get("state") or "").upper()
        assert run_status == ValidationRunStatus.SUCCEEDED, (
            f"Unexpected status: {run_status} payload={data}"
        )
        issues = extract_issues(data)
        assert isinstance(issues, list)
        assert len(issues) == 0, f"Expected no issues, got: {issues}"

    def test_malformed_thmx_fails(self, load_thmx_asset, therm_workflow_context):
        """A THMX file with missing required elements/attributes should fail.

        The malformed file has two deliberate schema violations:
          1. Required <Units> element removed
          2. Required BC attribute removed from a BCPolygon
        """
        client = therm_workflow_context["client"]
        workflow = therm_workflow_context["workflow"]
        content = load_thmx_asset("sample_sill_CMA_malformed.thmx")
        data = _run_and_poll(client, workflow, content=content)
        run_status = (data.get("status") or data.get("state") or "").upper()
        assert run_status == ValidationRunStatus.FAILED, (
            f"Unexpected status: {run_status}"
        )

        issues = extract_issues(data)
        assert isinstance(issues, list)
        assert len(issues) >= 1, "Expected at least one issue for malformed THMX"
        joined = " | ".join(str(i) for i in issues)
        assert "units" in joined.lower(), (
            f"Expected error mentioning missing Units element, got: {joined}"
        )
