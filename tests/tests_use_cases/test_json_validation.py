"""End-to-end JSON Schema workflow tests using the synchronized system catalog.

These tests prove both passing and failing submissions through the public API.
The workflow fixture uses the declared ``json_document`` file port and its
production default binding instead of an incomplete validator model stand-in.
"""

from __future__ import annotations

import json
import logging

import pytest
from django.urls import reverse
from rest_framework.status import HTTP_200_OK

from tests.helpers.payloads import invalid_product_payload
from tests.helpers.payloads import valid_product_payload
from tests.helpers.polling import extract_issues
from tests.helpers.polling import normalize_poll_url
from tests.helpers.polling import poll_until_complete
from tests.helpers.polling import start_workflow_url
from tests.helpers.workflows import create_workflow_step_with_default_bindings
from validibot.users.models import RoleCode
from validibot.users.tests.factories import OrganizationFactory
from validibot.users.tests.factories import UserFactory
from validibot.users.tests.factories import grant_role
from validibot.validations.constants import JSONSchemaVersion
from validibot.validations.constants import ValidationRunStatus
from validibot.validations.constants import ValidationType
from validibot.validations.tests.factories import RulesetFactory
from validibot.workflows.tests.factories import WorkflowFactory

logger = logging.getLogger(__name__)


@pytest.fixture
def workflow_context(load_json_asset, api_client, system_validator_for):
    """
    Build a minimal workflow that validates a product JSON using a JSON Schema
    ruleset, and authenticate the API client with EXECUTOR permissions.
    """
    org = OrganizationFactory()
    user = UserFactory(orgs=[org])
    user.set_current_org(org)

    grant_role(user, org, RoleCode.EXECUTOR)

    validator = system_validator_for(ValidationType.JSON_SCHEMA)

    schema = load_json_asset("example_product_schema.json")
    ruleset = RulesetFactory(
        org=org,
        user=user,
        ruleset_type=ValidationType.JSON_SCHEMA,
        rules_text=json.dumps(schema),
        metadata={
            "schema_type": JSONSchemaVersion.DRAFT_2020_12.value,
        },
    )

    workflow = WorkflowFactory(org=org, user=user)
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


@pytest.mark.django_db
class TestJsonValidation:
    """
    End-to-end JSON Schema validation tests that start workflows via the API and
    poll until completion, covering both valid and invalid payloads.
    """

    def test_json_validation_happy_path(self, workflow_context):
        """
        Valid payload should succeed and return no issues from the validation run.
        """
        client = workflow_context["client"]
        workflow = workflow_context["workflow"]
        org = workflow_context["org"]

        start_url = start_workflow_url(workflow)
        payload = valid_product_payload()

        resp = client.post(
            start_url,
            data=json.dumps(payload),
            content_type="application/json",
        )
        assert resp.status_code in (200, 201, 202), resp.content

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
                try:
                    poll_url = reverse(
                        "api:org-runs-detail",
                        kwargs={"org_slug": org.slug, "pk": run_id},
                    )
                except Exception as exc:
                    logger.debug("Could not reverse org-runs-detail: %s", exc)
                    poll_url = f"/api/v1/orgs/{org.slug}/runs/{run_id}/"

        data, last_status = poll_until_complete(client, poll_url)
        assert last_status == HTTP_200_OK, f"Polling failed: {last_status} {data}"

        run_status = (data.get("status") or data.get("state") or "").upper()
        assert run_status == ValidationRunStatus.SUCCEEDED, (
            f"Unexpected status: {run_status} payload={data}"
        )
        issues = extract_issues(data)
        assert isinstance(issues, list)
        assert len(issues) == 0, f"Expected no issues, got: {issues}"

    def test_json_validation_one_field_fails(self, workflow_context):
        """
        Invalid payload should fail validation and surface the rating/max error
        in issues.
        """
        client = workflow_context["client"]
        workflow = workflow_context["workflow"]
        org = workflow_context["org"]

        start_url = start_workflow_url(workflow)
        payload = invalid_product_payload()

        resp = client.post(
            start_url,
            data=json.dumps(payload),
            content_type="application/json",
        )
        assert resp.status_code in (200, 201, 202), resp.content

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
                try:
                    poll_url = reverse(
                        "api:org-runs-detail",
                        kwargs={"org_slug": org.slug, "pk": run_id},
                    )
                except Exception as exc:
                    logger.debug("Could not reverse org-runs-detail: %s", exc)
                    poll_url = f"/api/v1/orgs/{org.slug}/runs/{run_id}/"

        data, last_status = poll_until_complete(client, poll_url)
        assert last_status == HTTP_200_OK, f"Polling failed: {last_status} {data}"

        run_status = (data.get("status") or data.get("state") or "").upper()
        assert run_status == ValidationRunStatus.FAILED, (
            f"Unexpected status: {run_status}"
        )

        issues = extract_issues(data)
        assert isinstance(issues, list)
        assert len(issues) >= 1, "Expected at least one issue for invalid payload"

        joined = " | ".join(str(issue) for issue in issues)
        assert ("rating" in joined) or ("maximum" in joined), (
            f"Expected rating/max error in issues, got: {issues}"
        )
