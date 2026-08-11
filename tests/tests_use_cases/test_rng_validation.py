"""End-to-end RELAX NG workflow tests using the XML system validator contract.

The submitted XML is bound through the synchronized ``xml_document`` file port
before the ordinary API launch and result-polling lifecycle is exercised.
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
def workflow_context(load_rng_asset, api_client, system_validator_for):
    """
    Build a workflow configured for RELAX NG XML validation and authenticate
    the API client.
    """
    org = OrganizationFactory()
    user = UserFactory(orgs=[org])
    user.set_current_org(org)

    grant_role(user, org, RoleCode.EXECUTOR)

    validator = system_validator_for(ValidationType.XML_SCHEMA)

    schema = load_rng_asset("product.rng")

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
    step = create_workflow_step_with_default_bindings(
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
        "step": step,
        "client": api_client,
    }


def _run_and_poll(
    client,
    workflow,
    content: str,
    content_type: str = "application/xml",
) -> dict:
    """
    Start the workflow, then poll until the run completes, returning the final payload.
    """
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
            # Use org-scoped route (ADR-2026-01-06)
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
class TestRelaxNGValidation:
    """
    End-to-end RELAX NG validation tests covering both valid and invalid XML
    payloads.
    """

    def test_xml_rng_happy_path(self, load_xml_asset, workflow_context):
        """
        Valid XML should pass RELAX NG validation and return a succeeded run
        with no issues.
        """
        client = workflow_context["client"]
        workflow = workflow_context["workflow"]
        valid_product_xml = load_xml_asset("valid_product.xml")
        data = _run_and_poll(client, workflow, content=valid_product_xml)
        run_status = (data.get("status") or data.get("state") or "").upper()
        assert run_status == ValidationRunStatus.SUCCEEDED, (
            f"Unexpected status: {run_status} payload={data}"
        )
        issues = extract_issues(data)
        assert isinstance(issues, list)
        assert len(issues) == 0, f"Expected no issues, got: {issues}"

    def test_xml_rng_one_field_fails(self, load_xml_asset, workflow_context):
        """
        Invalid XML should fail RELAX NG validation and report at least one issue.
        """
        client = workflow_context["client"]
        workflow = workflow_context["workflow"]
        invalid_product_xml = load_xml_asset("invalid_product.xml")
        data = _run_and_poll(client, workflow, content=invalid_product_xml)
        run_status = (data.get("status") or data.get("state") or "").upper()
        assert run_status == ValidationRunStatus.FAILED, (
            f"Unexpected status: {run_status}"
        )

        issues = extract_issues(data)
        assert isinstance(issues, list)
        assert len(issues) >= 1, "Expected at least one issue for invalid payload"
