"""End-to-end SHACL validation tests.

Mirrors the existing JSON Schema + XSD use-case test patterns:

1. Build a minimal workflow with a SHACL validator + ruleset.
2. POST a Turtle submission via the API.
3. Poll the run to completion.
4. Assert the run status and the issues list.

The tests exercise the full local self-hosted path — API → validation run
launch → Docker runner → isolated SHACL backend → findings. They run when the
Docker SDK, daemon, and ``validibot-validator-backend-shacl:latest`` image are
available, and skip as a group otherwise. CI installs the SDK and builds the
pinned compatible image before pytest, while ordinary contributors can run the
rest of the suite without installing the optional Docker runner.

These tests live alongside the other use-case tests for parity. The
finer-grained Django-side tests for SHACL (launch envelope construction,
finding persistence, output-value extraction, library-validator merge) live next
to the SHACL package at
``validibot/validations/tests/test_validators/test_shacl_*.py``.
"""

from __future__ import annotations

import logging

import pytest
from django.urls import reverse
from rest_framework.status import HTTP_200_OK

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
from validibot.validations.constants import AssertionOperator
from validibot.validations.constants import AssertionType
from validibot.validations.constants import RulesetType
from validibot.validations.constants import ValidationRunStatus
from validibot.validations.constants import ValidationType
from validibot.validations.tests.factories import RulesetAssertionFactory
from validibot.validations.tests.factories import RulesetFactory
from validibot.workflows.tests.factories import WorkflowFactory

logger = logging.getLogger(__name__)
pytestmark = pytest.mark.django_db
SHACL_BACKEND_IMAGE = "validibot-validator-backend-shacl:latest"


@pytest.fixture(scope="module", autouse=True)
def require_shacl_backend():
    """Skip real-container tests unless their complete runtime is available.

    A missing optional Docker SDK, stopped daemon, or absent SHACL image is an
    environment precondition, not a product failure. Checking all three before
    any workflow runs also prevents a backend-unavailable finding from making a
    negative validation test pass for the wrong reason. CI satisfies these
    prerequisites and therefore continues to exercise the complete container
    path.
    """
    docker = pytest.importorskip(
        "docker",
        reason=(
            "SHACL container tests require the docker-runner extra; "
            "run `uv sync --extra docker-runner`."
        ),
    )
    client = None
    try:
        client = docker.from_env()
        client.ping()
        client.images.get(SHACL_BACKEND_IMAGE)
    except Exception as exc:
        pytest.skip(
            f"SHACL container tests require Docker and {SHACL_BACKEND_IMAGE}: {exc}",
        )
    finally:
        if client is not None:
            client.close()


@pytest.fixture
def workflow_context(load_shacl_asset, api_client, system_validator_for):
    """Build a minimal SHACL workflow + authenticated API client.

    The workflow has a single step using the system SHACL validator
    with the person-shapes ruleset attached. Submissions accepted as
    plain text (Turtle).
    """
    org = OrganizationFactory()
    user = UserFactory(orgs=[org])
    user.set_current_org(org)
    grant_role(user, org, RoleCode.EXECUTOR)

    validator = system_validator_for(ValidationType.SHACL)

    shapes = load_shacl_asset("example_person_shapes.ttl")
    ontology = load_shacl_asset("example_person_ontology.ttl")

    ruleset = RulesetFactory(
        org=org,
        user=user,
        ruleset_type=RulesetType.SHACL,
        rules_text=shapes,
        metadata={
            "ontology_text": ontology,
            "inference_mode": "rdfs",
            "advanced_shacl": True,
            "submission_format": "auto",
            "bundled_standards": [],
        },
    )

    workflow = WorkflowFactory(
        org=org,
        user=user,
        allowed_file_types=[SubmissionFileType.TEXT],
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
    content_type: str = "text/turtle",
) -> dict:
    """POST a submission, follow the Location header, poll until done.

    Lifted from ``test_xsd_validation.py``'s ``_run_and_poll`` helper
    with the content_type defaulted to ``text/turtle`` — Turtle is the
    most common RDF serialization and matches the ``.ttl`` examples in
    ``tests/assets/shacl/``.
    """
    start_url = start_workflow_url(workflow)
    resp = client.post(start_url, data=content, content_type=content_type)
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
class TestShaclValidation:
    """End-to-end SHACL validation against ``example_person_shapes.ttl``.

    Covers the two paths a workflow author cares about: a passing
    submission (returns SUCCEEDED, no issues) and a failing submission
    (returns FAILED with a SHACL constraint violation surfaced as an
    issue).

    These tests intentionally use the system SHACL validator with a
    minimal Person shape rather than ASHRAE 223P shapes — keeping the
    fixtures small (under 30 lines of Turtle each) means the tests are
    fast, the fixtures are diff-friendly, and the test doesn't depend
    on ASHRAE-copyrighted content.
    """

    def test_shacl_valid_submission_succeeds(
        self,
        load_shacl_asset,
        workflow_context,
    ):
        """A submission satisfying the shape succeeds with no issues."""
        client = workflow_context["client"]
        workflow = workflow_context["workflow"]
        submission = load_shacl_asset("valid_person.ttl")

        data = _run_and_poll(client, workflow, submission)

        run_status = (data.get("status") or data.get("state") or "").upper()
        assert run_status == ValidationRunStatus.SUCCEEDED, (
            f"Unexpected status: {run_status} payload={data}"
        )
        issues = extract_issues(data)
        assert isinstance(issues, list)
        assert len(issues) == 0, f"Expected no issues, got: {issues}"

    def test_shacl_invalid_submission_fails_with_violation(
        self,
        load_shacl_asset,
        workflow_context,
    ):
        """A submission violating the shape fails with a constraint message."""
        client = workflow_context["client"]
        workflow = workflow_context["workflow"]
        submission = load_shacl_asset("invalid_person.ttl")

        data = _run_and_poll(client, workflow, submission)

        run_status = (data.get("status") or data.get("state") or "").upper()
        assert run_status == ValidationRunStatus.FAILED, (
            f"Unexpected status: {run_status} payload={data}"
        )
        issues = extract_issues(data)
        assert isinstance(issues, list)
        assert len(issues) >= 1, "Expected at least one issue for invalid submission"

        joined = " | ".join(str(issue) for issue in issues)
        # The shape's sh:message includes "name" — verify it propagated
        # through to the visible issue so authors can act on the failure.
        assert "name" in joined.lower(), (
            f"Expected the shape's 'name' message in issues, got: {issues}"
        )

    def test_shacl_unparseable_submission_fails_with_parse_error(
        self,
        workflow_context,
    ):
        """A submission that isn't valid Turtle/RDF fails with a parse error.

        Operators occasionally upload the wrong file type. The
        validator should fail cleanly with a parse-error finding
        rather than crashing the run.
        """
        client = workflow_context["client"]
        workflow = workflow_context["workflow"]

        data = _run_and_poll(client, workflow, "this is not turtle <<<<")

        run_status = (data.get("status") or data.get("state") or "").upper()
        assert run_status == ValidationRunStatus.FAILED, (
            f"Unexpected status: {run_status} payload={data}"
        )
        issues = extract_issues(data)
        assert isinstance(issues, list)
        assert len(issues) >= 1, "Expected at least one issue for bad RDF"


# ──────────────────────────────────────────────────────────────────────
# 223P blog-post scenario: end-to-end test exercising the exact files
# shown in the "Validating Semantic Building Data with SHACL" blog post.
#
# The blog post walks a reader through:
#   1. An ontology (declares s223:Zone)            → 223p_example_ontology.ttl
#   2. A shape   (every Zone must declare a Domain) → 223p_example_shapes.ttl
#   3. A submission (two Zones + an AHU)            → 223p_example_building.ttl
#   4. A SPARQL ASK assertion (every HVAC Zone is   → 223p_example_ask.sparql
#      served by at least one Equipment)
#
# This test stands up the same workflow in Validibot and submits the
# same building file. It confirms that:
#   - the SHACL shape catches Zone-102 (no s223:hasDomain)
#   - the SPARQL ASK passes for this submission (AHU-1 serves Zone-101,
#     which is the only HVAC Zone)
#
# If the blog example ever drifts from what the code actually does,
# this test fails — keeping the published tutorial honest.
# ──────────────────────────────────────────────────────────────────────


@pytest.fixture
def workflow_223p_context(
    load_shacl_asset,
    api_client,
    system_validator_for,
):
    """Build the workflow described in the 223P blog post.

    Uses the four blog-derived assets in ``tests/assets/shacl/``:
    ontology, shapes, building submission, and a SPARQL ASK assertion.
    The SPARQL assertion is registered as a ``RulesetAssertion`` row,
    matching the Add Assertion dialog path used in production. The form
    layer has dedicated tests; this fixture focuses on the validator
    execution path.
    """
    org = OrganizationFactory()
    user = UserFactory(orgs=[org])
    user.set_current_org(org)
    grant_role(user, org, RoleCode.EXECUTOR)

    validator = system_validator_for(ValidationType.SHACL)

    shapes = load_shacl_asset("223p_example_shapes.ttl")
    ontology = load_shacl_asset("223p_example_ontology.ttl")
    sparql_ask = load_shacl_asset("223p_example_ask.sparql")

    ruleset = RulesetFactory(
        org=org,
        user=user,
        ruleset_type=RulesetType.SHACL,
        rules_text=shapes,
        metadata={
            "ontology_text": ontology,
            "inference_mode": "rdfs",
            "advanced_shacl": False,  # the blog example does not need it
            "submission_format": "auto",
            "bundled_standards": [],
        },
    )
    RulesetAssertionFactory(
        ruleset=ruleset,
        assertion_type=AssertionType.SHACL,
        operator=AssertionOperator.SPARQL_ASK,
        target_data_path="shacl.data",
        severity="ERROR",
        rhs={
            "target_graph": "data",
            "query": sparql_ask,
            "description": ("Every HVAC Zone must be served by at least one Equipment"),
        },
        message_template="Found an HVAC Zone with no Equipment serving it.",
    )

    workflow = WorkflowFactory(
        org=org,
        user=user,
        allowed_file_types=[SubmissionFileType.TEXT],
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


@pytest.mark.django_db
class TestShacl223PBlogScenario:
    """End-to-end test for the 223P walk-through in the blog post.

    The blog post promises the reader that the workflow will catch
    Zone-102 (no Domain) while accepting Zone-101 (has Domain), and that
    a SPARQL ASK can layer an additional "every HVAC Zone is served"
    check on top. This test holds the promise honest: if either claim
    stops being true, the published tutorial is wrong and we want to
    know before a reader does.
    """

    def test_223p_blog_walkthrough_catches_missing_domain(
        self,
        load_shacl_asset,
        workflow_223p_context,
    ):
        """The blog scenario, run end-to-end through the public API.

        Steps mirror the blog post:

        1. Build a workflow with the 223P shapes + ontology + SPARQL
           ASK (done by the ``workflow_223p_context`` fixture).
        2. Submit ``223p_example_building.ttl`` — two Zones (only one
           has s223:hasDomain) and one Equipment (AHU-1) serving Zone-101.
        3. Confirm the run fails on the SHACL violation for Zone-102.
        4. Confirm the violation message is the one the shape author
           wrote (so the contractor receiving the finding sees a
           meaningful instruction, not a generic SHACL error).
        5. Confirm the SPARQL ASK does NOT add a second finding —
           AHU-1 serves the only HVAC Zone, so the assertion passes
           for this submission. (The test does not currently assert
           a SPARQL-fired finding because the blog's example file is
           deliberately written to pass the ASK; a follow-up test
           could submit a modified file with an orphan HVAC Zone.)
        """
        client = workflow_223p_context["client"]
        workflow = workflow_223p_context["workflow"]
        submission = load_shacl_asset("223p_example_building.ttl")

        data = _run_and_poll(client, workflow, submission)

        # The run should fail overall because Zone-102 is missing the
        # required s223:hasDomain — that's an ERROR finding, which
        # flips the step (and therefore the run) to FAILED.
        run_status = (data.get("status") or data.get("state") or "").upper()
        assert run_status == ValidationRunStatus.FAILED, (
            f"Expected run to fail because Zone-102 is missing a "
            f"Domain. Got status={run_status} payload={data}"
        )

        issues = extract_issues(data)
        assert isinstance(issues, list)

        # Exactly one finding is expected: Zone-101 satisfies the
        # shape (it declares s223:Domain-HVAC), and Zone-102 violates
        # it. If we see more than one finding, something has broken
        # in the shape evaluation or finding extraction. If we see
        # zero, the shape isn't firing at all.
        assert len(issues) == 1, (
            f"Expected exactly one SHACL finding (Zone-102 missing "
            f"s223:hasDomain); got {len(issues)}: {issues}"
        )
        finding = issues[0]

        # The shape's sh:message should propagate to the finding so
        # the contractor knows what to fix. We check for the
        # distinctive phrase from the blog example.
        assert "Domain" in finding["message"], (
            "Expected the shape's 'Every Zone must declare a Domain' "
            f"message to surface in the finding. Got: {finding}"
        )

        # The constraint path should be s223:hasDomain — proving SHACL
        # identified the right missing property on the right Zone.
        assert finding["path"] == ("http://data.ashrae.org/standard223#hasDomain"), (
            f"Expected the finding's path to be s223:hasDomain. Got: {finding}"
        )

        # And the constraint code should be SHACL's MinCount component
        # (because we set sh:minCount 1 on the shape), confirming the
        # finding came from SHACL and NOT from the SPARQL ASK engine.
        assert finding["severity"] == "ERROR"
        assert "MinCount" in finding["code"], (
            f"Expected a SHACL MinCountConstraintComponent code. Got: {finding}"
        )

        # The SPARQL ASK assertion passes for this submission (AHU-1
        # serves the only HVAC Zone, Zone-101), so it should NOT
        # produce any findings of its own. If the assertion had run
        # but the engine rejected it (e.g. a scrub bypass), we'd see
        # ``shacl.sparql_ask_engine_error`` or ``shacl.sparql_ask_failed``
        # in one of the codes. Their absence proves the SPARQL ran
        # successfully against the data graph.
        all_codes = [str(i.get("code", "")) for i in issues]
        assert not any("sparql_ask" in c for c in all_codes), (
            f"SPARQL ASK should have executed cleanly against this "
            f"submission. Got codes: {all_codes}"
        )
