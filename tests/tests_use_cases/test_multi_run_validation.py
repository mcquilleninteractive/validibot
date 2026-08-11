"""
Multi-run validation stress tests (serial).

These tests submit many validation runs in series via the API and verify:

1. All runs reach a terminal status (no runs lost)
2. Each run has a unique ID (no collisions)
3. Each run produces correct step results
4. No database integrity errors occur

Runs are submitted sequentially because CELERY_TASK_ALWAYS_EAGER=True
causes each run to complete synchronously during the POST call. For
true concurrent stress tests with parallel HTTP requests and real Celery
workers, see tests/tests_e2e/.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import pytest
from django.urls import reverse

from tests.helpers.payloads import invalid_product_payload
from tests.helpers.payloads import valid_product_payload
from tests.helpers.polling import extract_issues
from tests.helpers.polling import normalize_poll_url
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

# How many validation runs to submit per test
NUM_RUNS = 10

pytestmark = pytest.mark.django_db


def _get_poll_url(resp, org_slug: str) -> str:
    """Extract the polling URL from a workflow start response."""
    loc = resp.headers.get("Location") or resp.headers.get("location") or ""
    poll_url = normalize_poll_url(loc)
    if not poll_url:
        try:
            data = resp.json()
        except Exception:
            data = {}
        run_id = data.get("id")
        if run_id:
            try:
                poll_url = reverse(
                    "api:org-runs-detail",
                    kwargs={"org_slug": org_slug, "pk": run_id},
                )
            except Exception:
                poll_url = f"/api/v1/orgs/{org_slug}/runs/{run_id}/"
    return poll_url


def _submit_run(
    client: Any,
    workflow: Any,
    org: Any,
    payload: dict,
) -> dict:
    """
    Submit a single validation run and return the result.

    With CELERY_TASK_ALWAYS_EAGER=True the run completes synchronously
    during the POST, so no polling is needed - the response already
    contains the final state. We still fetch the run detail to confirm.
    """
    url = start_workflow_url(workflow)
    resp = client.post(
        url,
        data=json.dumps(payload),
        content_type="application/json",
    )

    if resp.status_code not in (200, 201, 202):
        return {
            "error": f"Start failed: {resp.status_code} {resp.content!r}",
            "run_id": None,
            "status": None,
            "data": {},
            "issues": [],
        }

    poll_url = _get_poll_url(resp, org.slug)

    # With eager Celery the run is already done, but fetch the detail
    # endpoint to verify it's accessible and returns correct data.
    detail_resp = client.get(poll_url)
    try:
        data = detail_resp.json()
    except Exception:
        data = {}

    return {
        "run_id": data.get("id"),
        "status": (data.get("status") or "").upper(),
        "data": data,
        "issues": extract_issues(data),
        "error": None,
    }


@pytest.fixture
def json_schema_setup(load_json_asset, api_client, system_validator_for):
    """
    Create the shared test objects: org, user, validator, ruleset, workflow, step.

    Uses the api_client fixture (DRF APIClient) so all runs share the same
    authenticated client in the main thread.
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
        "workflow": workflow,
        "client": api_client,
    }


def _run_many(setup: dict, payloads: list[dict]) -> list[dict]:
    """Submit multiple validation runs in rapid succession."""
    client = setup["client"]
    workflow = setup["workflow"]
    org = setup["org"]
    return [_submit_run(client, workflow, org, p) for p in payloads]


class TestMultiRunValidation:
    """
    Stress tests that submit many validation runs in series and verify
    no runs are lost, duplicated, or produce incorrect results.
    """

    def test_many_valid_payloads_all_succeed(self, json_schema_setup):
        """All runs with valid payloads should succeed."""
        payloads = [valid_product_payload() for _ in range(NUM_RUNS)]
        results = _run_many(json_schema_setup, payloads)

        # No start errors
        errors = [r for r in results if r["error"]]
        assert not errors, f"Some runs failed to start: {errors}"

        # All reached terminal status
        for r in results:
            assert r["status"] == ValidationRunStatus.SUCCEEDED, (
                f"Run {r['run_id']} has unexpected status: {r['status']}"
            )

        # All have unique IDs (no collisions)
        run_ids = [r["run_id"] for r in results]
        assert len(set(run_ids)) == NUM_RUNS, (
            f"Expected {NUM_RUNS} unique IDs, got {len(set(run_ids))}: {run_ids}"
        )

        # All should have zero issues
        for r in results:
            assert len(r["issues"]) == 0, (
                f"Run {r['run_id']} should have no issues but got: {r['issues']}"
            )

    def test_many_invalid_payloads_all_fail(self, json_schema_setup):
        """All runs with invalid payloads should fail with issues."""
        payloads = [invalid_product_payload() for _ in range(NUM_RUNS)]
        results = _run_many(json_schema_setup, payloads)

        # No start errors
        errors = [r for r in results if r["error"]]
        assert not errors, f"Some runs failed to start: {errors}"

        # All reached FAILED status
        for r in results:
            assert r["status"] == ValidationRunStatus.FAILED, (
                f"Run {r['run_id']} has unexpected status: {r['status']}"
            )

        # All have unique IDs
        run_ids = [r["run_id"] for r in results]
        assert len(set(run_ids)) == NUM_RUNS, (
            f"Expected {NUM_RUNS} unique IDs, got {len(set(run_ids))}"
        )

        # All should have at least one issue about rating/max
        for r in results:
            assert len(r["issues"]) >= 1, (
                f"Run {r['run_id']} should have issues but got none"
            )

    def test_mixed_payloads_correct_results(self, json_schema_setup):
        """Mixed valid/invalid payloads should produce correct per-run results."""
        payloads = []
        expected_statuses = []
        for i in range(NUM_RUNS):
            if i % 2 == 0:
                payloads.append(valid_product_payload())
                expected_statuses.append(ValidationRunStatus.SUCCEEDED)
            else:
                payloads.append(invalid_product_payload())
                expected_statuses.append(ValidationRunStatus.FAILED)

        results = _run_many(json_schema_setup, payloads)

        # No start errors
        errors = [r for r in results if r["error"]]
        assert not errors, f"Some runs failed to start: {errors}"

        # All have unique IDs
        run_ids = [r["run_id"] for r in results]
        assert len(set(run_ids)) == NUM_RUNS

        # Each result matches expected status
        zipped = zip(results, expected_statuses, strict=True)
        for i, (result, expected) in enumerate(zipped):
            assert result["status"] == expected, (
                f"Run {i} (ID={result['run_id']}): "
                f"expected {expected}, got {result['status']}"
            )
