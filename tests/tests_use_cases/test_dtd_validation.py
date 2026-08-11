"""End-to-end DTD validation through the declared XML document file port.

Both success and schema-failure paths use the production system validator and
default input-binding service so a clean database has the deployed contract.
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


@pytest.fixture
def workflow_context(load_dtd_asset, api_client, system_validator_for):
    """
    Build a minimal workflow configured for XML DTD
    validation and authenticate the API client.
    """
    org = OrganizationFactory()
    user = UserFactory(orgs=[org])
    grant_role(user, org, RoleCode.EXECUTOR)
    user.set_current_org(org)

    validator = system_validator_for(ValidationType.XML_SCHEMA)
    schema = load_dtd_asset("product.dtd")

    ruleset = RulesetFactory(
        org=org,
        user=user,
        ruleset_type=ValidationType.XML_SCHEMA,
        rules_text=schema,
        metadata={
            "schema_type": XMLSchemaType.DTD.name,
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
        "workflow": workflow,
        "client": api_client,
        "step": step,
    }


def _run_and_poll(client, workflow, *, content: str) -> dict:
    """
    Start a workflow via the API and poll until the
    validation run completes, returning the payload.
    """
    start_url = start_workflow_url(workflow)
    resp = client.post(start_url, data=content, content_type="application/xml")
    assert resp.status_code in (HTTP_200_OK, HTTP_201_CREATED, HTTP_202_ACCEPTED), (
        resp.content
    )

    loc = resp.headers.get("Location") or resp.headers.get("location") or ""
    poll_url = normalize_poll_url(loc)
    if not poll_url:
        data = {}
        try:
            data = resp.json()
        except Exception:
            data = {}
        run_id = data.get("id")
        if run_id:
            # Use org-scoped route (ADR-2026-01-06)
            org_slug = workflow.org.slug
            try:
                poll_url = reverse(
                    "api:org-runs-detail",
                    kwargs={"org_slug": org_slug, "pk": run_id},
                )
            except Exception:
                logger.info("Could not reverse for org-runs-detail")
                poll_url = f"/api/v1/orgs/{org_slug}/runs/{run_id}/"

    data, last_status = poll_until_complete(client, poll_url)
    assert last_status == HTTP_200_OK, f"Polling failed: {last_status} {data}"
    return data


@pytest.mark.django_db
class TestDtdValidation:
    """
    End-to-end DTD validation tests that start a workflow via the API and poll
    until completion, asserting both happy path and failure scenarios.
    """

    def test_xml_dtd_happy_path(self, load_xml_asset, workflow_context):
        """
        Valid XML payload should pass DTD validation,
        produce a succeeded run, and return no issues.
        """
        client = workflow_context["client"]
        workflow = workflow_context["workflow"]
        payload = load_xml_asset("valid_product.xml")

        data = _run_and_poll(client, workflow, content=payload)
        run_status = (data.get("status") or data.get("state") or "").upper()
        assert run_status == ValidationRunStatus.SUCCEEDED
        assert extract_issues(data) == []

    def test_xml_dtd_missing_required_elements(self, load_xml_asset, workflow_context):
        """
        Invalid XML payload missing required
        elements should fail DTD validation and return issues.
        """
        client = workflow_context["client"]
        workflow = workflow_context["workflow"]
        payload = load_xml_asset("invalid_product.xml")

        data = _run_and_poll(client, workflow, content=payload)
        run_status = (data.get("status") or data.get("state") or "").upper()
        assert run_status == ValidationRunStatus.FAILED
        issues = extract_issues(data)
        assert issues, "Expected DTD validation issues"
