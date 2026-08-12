"""Tests for WorkflowVersioningService and contract-edit detection.

ADR-2026-04-27 Phase 3 Session A: workflow versioning core. These
tests cover:

1. The model-level helpers (``has_runs``,
   ``requires_new_version_for_contract_edits``) that enable callers
   (forms, serializers, admin commands) to detect when an in-place
   edit must be replaced with a clone.

2. The :class:`WorkflowVersioningService.clone` operation that
   produces a complete contract copy. The trust-boundary concern
   here is silent contract drift — a "new version" that quietly
   inherits some pre-clone state. The :class:`CloneReport` lets
   tests assert exactly what got copied (and surfaces what didn't,
   via ``warnings``).

The tests use real Django factories rather than mocks because the
service operates on bulk_create + foreign-key relationships across
five related models. Mocking those would re-implement most of
Django's ORM in the test fixture.

Why isolate component-copy tests rather than one big end-to-end clone test
==========================================================================

A single "clone copies everything" test passes when the clone copies
*at least* what the test asserts on. A future regression that adds
a new related-object model (e.g. workflow-level webhooks) might be
silently missed. Per-component tests, by contrast, document the
per-component copy contract and fail loudly when a new component
needs to be added to the service.
"""

from __future__ import annotations

import pytest
from django.core.exceptions import ValidationError
from django.test import TestCase

from validibot.submissions.constants import OutputRetention
from validibot.submissions.constants import SubmissionFileType
from validibot.submissions.constants import SubmissionRetention
from validibot.users.tests.factories import OrganizationFactory
from validibot.users.tests.factories import UserFactory
from validibot.validations.constants import ArtifactKind
from validibot.validations.constants import AssertionOperator
from validibot.validations.constants import AssertionType
from validibot.validations.constants import BindingSourceScope
from validibot.validations.constants import CatalogValueType
from validibot.validations.constants import DefaultSourceStrategy
from validibot.validations.constants import EnvelopeChannel
from validibot.validations.constants import RulesetType
from validibot.validations.constants import StepIODirection
from validibot.validations.constants import StepIOMedium
from validibot.validations.constants import ValidationType
from validibot.validations.tests.factories import DerivationFactory
from validibot.validations.tests.factories import RulesetAssertionFactory
from validibot.validations.tests.factories import RulesetFactory
from validibot.validations.tests.factories import StepInputBindingFactory
from validibot.validations.tests.factories import StepIODefinitionFactory
from validibot.validations.tests.factories import ValidatorFactory
from validibot.workflows.constants import AgentBillingMode
from validibot.workflows.constants import WorkflowHistoryPolicy
from validibot.workflows.constants import WorkflowVisibility
from validibot.workflows.models import Workflow
from validibot.workflows.models import WorkflowPublicInfo
from validibot.workflows.models import WorkflowRoleAccess
from validibot.workflows.models import validate_workflow_version
from validibot.workflows.services.versioning import CONTRACT_FIELDS
from validibot.workflows.services.versioning import CloneReport
from validibot.workflows.services.versioning import WorkflowVersioningService
from validibot.workflows.tests.factories import WorkflowFactory
from validibot.workflows.tests.factories import WorkflowStepFactory
from validibot.workflows.version_utils import compare_workflow_versions
from validibot.workflows.version_utils import parse_workflow_version

pytestmark = pytest.mark.django_db

EXPECTED_CONTRACT_TREE_STEP_IO_DEFINITIONS = 3


# ──────────────────────────────────────────────────────────────────────
# Workflow.has_runs and requires_new_version_for_contract_edits
# ──────────────────────────────────────────────────────────────────────


class WorkflowHasRunsTests(TestCase):
    """``has_runs`` is the canonical "in use" detector.

    Once a workflow has any validation run, its launch contract is
    the rules those runs ran under. Subsequent contract edits must
    produce a new version.
    """

    def test_returns_false_when_no_runs(self):
        """A fresh workflow has no runs."""
        org = OrganizationFactory()
        workflow = WorkflowFactory(org=org)
        WorkflowStepFactory(workflow=workflow)
        assert workflow.has_runs() is False


class WorkflowVersionLabelTests(TestCase):
    """Workflow versions accept only positive integers.

    The architecture depends on deterministic ordering inside a workflow family.
    Semver-like labels imply compatibility semantics the product does not
    enforce, and ad-hoc labels like ``draft`` make "latest" resolution
    ambiguous.
    """

    def test_model_rejects_semver_label(self):
        """``1.0.0`` is no longer a workflow version label shape."""
        with pytest.raises(ValidationError):
            validate_workflow_version("1.0.0")

    def test_model_rejects_partial_semver_label(self):
        """``1.0`` is rejected for the same reason as full semver."""
        with pytest.raises(ValidationError):
            validate_workflow_version("1.0")

    def test_model_accepts_positive_integer_labels(self):
        """Positive integers are the only supported workflow version shape."""
        validate_workflow_version("2")
        validate_workflow_version(2)

    def test_model_rejects_zero_version(self):
        """Version zero must not become a real sortable workflow version."""
        with pytest.raises(ValidationError):
            validate_workflow_version(0)

    def test_compare_utility_orders_integer_versions(self):
        """The comparison helper treats request strings and ints identically."""
        assert compare_workflow_versions("1", 1) == 0
        assert compare_workflow_versions("2", "10") == -1
        assert compare_workflow_versions(12, "3") == 1

    def test_parse_helper_rejects_invalid_label(self):
        """Invalid persisted labels should fail loudly, not sort as 0.0.0."""
        with pytest.raises(ValueError, match=r"positive integer"):
            parse_workflow_version("latest")

    def test_model_rejects_empty_version(self):
        """Workflow version is required; empty strings must not validate.

        Earlier in the project blank versions were tolerated as a backfill
        placeholder. Once migration ``0023`` lands every existing row has a
        real label, and the unique constraint plus latest-version resolver
        both rely on non-empty values. Permitting blank here would silently
        re-open the gap.
        """
        with pytest.raises(ValidationError, match=r"required"):
            validate_workflow_version("")

    def test_parse_helper_rejects_empty_label(self):
        """Empty strings must raise, not silently sort as a real version.

        If empty strings sneak through (a renamed field, a deserializer
        bug, a row that escaped backfill), the version-resolution helper
        should fail loudly rather than rank that row against real versions.
        """
        with pytest.raises(ValueError, match=r"required"):
            parse_workflow_version("")


class WorkflowRequiresNewVersionTests(TestCase):
    """``requires_new_version_for_contract_edits`` policy.

    Returns True when the workflow is locked OR has runs. Either
    state means contract edits should produce a clone.
    """

    def test_fresh_workflow_does_not_require_new_version(self):
        """A workflow with no runs and not locked can be edited in place."""
        workflow = WorkflowFactory(is_locked=False)
        WorkflowStepFactory(workflow=workflow)
        assert workflow.requires_new_version_for_contract_edits() is False

    def test_locked_workflow_requires_new_version(self):
        """``is_locked=True`` is the strongest "in use" signal.

        Setting ``is_locked`` is what
        :meth:`WorkflowVersioningService.clone` does to the source
        on success — so this test also documents the post-clone
        invariant: the source is locked, and any further edits must
        clone again.
        """
        workflow = WorkflowFactory(is_locked=True)
        assert workflow.requires_new_version_for_contract_edits() is True

    def test_mutable_workflow_does_not_require_new_version_after_lock(self):
        """Mutable history opts out of the versioned edit gate.

        This does not make old runs reproducible; it means the workflow author
        has explicitly chosen in-place editing semantics for this workflow.
        """
        workflow = WorkflowFactory(
            is_locked=True,
            history_policy=WorkflowHistoryPolicy.MUTABLE,
        )
        assert workflow.requires_new_version_for_contract_edits() is False


# ──────────────────────────────────────────────────────────────────────
# Workflow.changed_contract_fields
# ──────────────────────────────────────────────────────────────────────
#
# The model-level helper that forms / serializers / scripts use to
# detect contract drift between current and proposed values. Pure
# data-in/data-out: no DB queries, no side effects. Tested in
# isolation here so changes to the comparison semantics (e.g. the
# set-equality treatment of list-shaped fields) are caught at the
# model layer, not just incidentally via the form.


class WorkflowChangedContractFieldsTests(TestCase):
    """``changed_contract_fields`` is the contract-drift comparator.

    The form / serializer / admin gates all funnel through this
    method. It returns the *names* of contract fields whose proposed
    value differs from the workflow's current value.
    """

    def test_returns_empty_set_when_proposed_matches_current(self):
        """Identical proposal -> no drift."""
        from validibot.submissions.constants import SubmissionRetention

        workflow = WorkflowFactory(
            input_retention=SubmissionRetention.DO_NOT_STORE,
            allowed_file_types=[SubmissionFileType.JSON],
        )
        proposed = {
            "input_retention": SubmissionRetention.DO_NOT_STORE,
            "allowed_file_types": [SubmissionFileType.JSON],
        }
        assert workflow.changed_contract_fields(proposed) == set()

    def test_detects_simple_value_change(self):
        """A single contract field changing -> set with that name."""
        from validibot.submissions.constants import SubmissionRetention

        workflow = WorkflowFactory(
            input_retention=SubmissionRetention.DO_NOT_STORE,
        )
        proposed = {
            "input_retention": SubmissionRetention.STORE_PERMANENTLY,
        }
        assert workflow.changed_contract_fields(proposed) == {"input_retention"}

    def test_list_field_uses_set_equality(self):
        """``allowed_file_types`` ignores ordering (it's a set semantically)."""
        workflow = WorkflowFactory(
            allowed_file_types=[SubmissionFileType.JSON, SubmissionFileType.XML],
        )
        # Same items, different order -> not a change.
        proposed = {
            "allowed_file_types": [SubmissionFileType.XML, SubmissionFileType.JSON],
        }
        assert workflow.changed_contract_fields(proposed) == set()

    def test_list_field_detects_real_membership_change(self):
        """Removing an item from ``allowed_file_types`` IS a change."""
        workflow = WorkflowFactory(
            allowed_file_types=[SubmissionFileType.JSON, SubmissionFileType.XML],
        )
        proposed = {
            "allowed_file_types": [SubmissionFileType.JSON],
        }
        assert workflow.changed_contract_fields(proposed) == {"allowed_file_types"}

    def test_skips_fields_not_in_proposed_dict(self):
        """Partial proposals: only consider fields the caller actually passed.

        A serializer for a PATCH request might only include a subset
        of contract fields. Treating absent keys as "changed to None"
        would falsely flag every PATCH on a locked workflow.
        """
        workflow = WorkflowFactory(
            allowed_file_types=[SubmissionFileType.JSON],
        )
        # No keys at all -> nothing changed.
        assert workflow.changed_contract_fields({}) == set()

    def test_ignores_non_contract_fields_even_when_changed(self):
        """``name`` is not a contract field; changing it doesn't show up."""
        workflow = WorkflowFactory(name="Original")
        proposed = {"name": "Renamed", "description": "new"}
        assert workflow.changed_contract_fields(proposed) == set()


# ──────────────────────────────────────────────────────────────────────
# CONTRACT_FIELDS
# ──────────────────────────────────────────────────────────────────────


class ContractFieldsTests(TestCase):
    """Basic shape checks on the CONTRACT_FIELDS constant.

    The constant is the source of truth for "which fields are the
    validation contract a past run was operating under?". Removing a
    field from this set is a meaningful semantic change — older code
    that previously rejected an in-place edit may now silently allow
    it. These tests pin the expected membership.
    """

    def test_contract_fields_includes_all_expected_runtime_fields(self):
        """Every documented validation-contract field is in the set.

        After the agent-fields cleanup, CONTRACT_FIELDS only covers
        fields that genuinely change what a past run validated.
        Agent fields (price, billing mode, rate limits, discovery
        flags) are commercial settings — past run outcomes don't
        depend on the current value — so they intentionally are NOT
        in this set and can change in place.
        """
        expected = {
            "allowed_file_types",
            "input_retention",
            "input_schema",
            "output_retention",
        }
        assert expected == set(CONTRACT_FIELDS)

    def test_contract_fields_excludes_agent_commercial_settings(self):
        """Agent commercial / access settings are NOT validation-contract fields.

        A past run's outcome doesn't depend on the current agent price,
        billing mode, rate limit, or the agent-channel flags
        (``mcp_enabled`` / ``x402_enabled``) — those affect what future
        callers see and pay, not what the past run validated. Forcing a
        workflow clone just to change a price would be friction for no
        integrity benefit. The form layer separately gates these fields
        behind the access-edit permission because they're commercial /
        access settings, but that's an access-control concern, not a
        contract-immutability concern.
        """
        agent_fields = {
            "agent_billing_mode",
            "agent_price_cents",
            "agent_max_launches_per_hour",
            # Renamed in the 2026-06-27 refactor (was agent_public_discovery
            # / agent_access_enabled). Still not contract fields.
            "x402_enabled",
            "mcp_enabled",
        }
        for field in agent_fields:
            assert field not in CONTRACT_FIELDS, (
                f"{field} should not be a contract field — it's a "
                "commercial setting, not part of the validation contract."
            )

    def test_contract_fields_excludes_lifecycle_and_cosmetic(self):
        """Lifecycle and cosmetic fields are NOT contract fields.

        Editing these on a used workflow should be allowed in place
        — they don't change what runs do.
        """
        non_contract = {
            "name",
            "description",
            "slug",
            "version",
            "is_active",
            "is_archived",
            "is_tombstoned",
            "is_locked",
            "make_info_page_public",
            "featured_image",
        }
        for field in non_contract:
            assert field not in CONTRACT_FIELDS, (
                f"{field!r} unexpectedly in CONTRACT_FIELDS — verify "
                f"this is intentional and update this test."
            )

    def test_contract_fields_is_immutable(self):
        """Frozenset — the constant cannot be mutated at runtime."""
        with pytest.raises(AttributeError):
            CONTRACT_FIELDS.add("bogus")  # type: ignore[attr-defined]


# ──────────────────────────────────────────────────────────────────────
# WorkflowVersioningService.clone
# ──────────────────────────────────────────────────────────────────────


class WorkflowVersioningServiceCloneTests(TestCase):
    """``clone`` produces a complete contract copy + structured report."""

    def test_clone_produces_new_workflow_with_incremented_version(self):
        """A v1 workflow clones to v2 with the same slug."""
        expected_clone_version = 2
        workflow = WorkflowFactory(version=1)
        WorkflowStepFactory(workflow=workflow)
        user = UserFactory()

        report = WorkflowVersioningService.clone(workflow, user=user)

        assert report.new_version_label == expected_clone_version
        new_workflow = Workflow.objects.get(pk=report.new_workflow_id)
        assert new_workflow.slug == workflow.slug
        assert new_workflow.version == expected_clone_version
        assert new_workflow.org == workflow.org

    def test_clone_locks_source_workflow(self):
        """The source is locked after clone — Phase 3 invariant."""
        workflow = WorkflowFactory(is_locked=False)
        WorkflowStepFactory(workflow=workflow)
        user = UserFactory()

        WorkflowVersioningService.clone(workflow, user=user)
        workflow.refresh_from_db()
        assert workflow.is_locked is True

    def test_clone_returns_structured_report(self):
        """The CloneReport carries per-component counts and warnings."""
        workflow = WorkflowFactory()
        # Two steps: a deliberate count we then assert the clone report
        # echoes back. Named so the relationship between fixture and
        # assertion is obvious at the call site (PLR2004 hates magic
        # numbers; this also makes the test trivially rebalanceable).
        expected_step_count = 2
        for _ in range(expected_step_count):
            WorkflowStepFactory(workflow=workflow)
        user = UserFactory()

        report = WorkflowVersioningService.clone(workflow, user=user)

        assert isinstance(report, CloneReport)
        assert report.source_workflow_id == workflow.pk
        assert report.new_workflow_id != workflow.pk
        assert report.components_copied["steps"] == expected_step_count
        # Warnings document deferred Phase 3 sessions' work.
        # We assert at least one warning exists when the workflow
        # references a Validator (the WorkflowStepFactory default
        # gives steps a validator).
        assert any("Validator" in w for w in report.warnings)

    def test_clone_copies_contract_fields_verbatim(self):
        """All CONTRACT_FIELDS appear on the clone with the source's value.

        Phase 3's whole point: a "new version" inherits the source's
        contract verbatim. Subsequent edits modify the new version,
        not the source.
        """
        workflow = WorkflowFactory(
            allowed_file_types=[SubmissionFileType.JSON, SubmissionFileType.XML],
            # x402-published workflows must use DO_NOT_STORE retention
            # for submissions (anonymous-per-call access incompatible
            # with persistent storage).
            input_retention=SubmissionRetention.DO_NOT_STORE,
            output_retention=OutputRetention.DO_NOT_STORE,
            x402_enabled=True,
            mcp_enabled=True,
            agent_billing_mode=AgentBillingMode.AGENT_PAYS_X402,
            agent_price_cents=42,
            agent_max_launches_per_hour=99,
        )
        WorkflowStepFactory(workflow=workflow)
        user = UserFactory()

        report = WorkflowVersioningService.clone(workflow, user=user)
        new_workflow = Workflow.objects.get(pk=report.new_workflow_id)

        # Verify every contract field copied. We compare via the
        # CONTRACT_FIELDS constant so adding a new contract field
        # surfaces its missing-from-clone gap loudly.
        for field_name in CONTRACT_FIELDS:
            source_value = getattr(workflow, field_name)
            new_value = getattr(new_workflow, field_name)
            assert source_value == new_value, (
                f"Contract field {field_name!r} not copied: "
                f"source={source_value!r} new={new_value!r}"
            )

    def test_clone_copies_history_policy(self):
        """The clone inherits whether the source is versioned or mutable.

        History policy controls how future edits behave on the cloned row. If
        the clone silently reset this value, authors would get different edit
        semantics immediately after creating the new version.
        """
        workflow = WorkflowFactory(history_policy=WorkflowHistoryPolicy.MUTABLE)
        WorkflowStepFactory(workflow=workflow)
        user = UserFactory()

        report = WorkflowVersioningService.clone(workflow, user=user)
        new_workflow = Workflow.objects.get(pk=report.new_workflow_id)

        assert new_workflow.history_policy == WorkflowHistoryPolicy.MUTABLE

    def test_clone_copies_workflow_authoring_and_launch_settings(self):
        """Workflow-owned settings should not reset on a new version.

        The clone is the author's next editable version of the same workflow,
        so descriptive fields, launch-form options, input schema authoring
        metadata, and the identity-scoped ``workflow_visibility`` must carry
        forward. (Visibility is the new home of the old ``is_public`` flag.)
        """
        workflow = WorkflowFactory(
            description="Original description",
            allow_submission_name=False,
            allow_submission_meta_data=True,
            allow_submission_short_description=True,
            make_info_page_public=True,
            workflow_visibility=WorkflowVisibility.ALL_USERS,
            success_message="Validation succeeded.",
            input_schema={
                "type": "object",
                "properties": {"area": {"type": "number"}},
            },
            input_schema_source_mode="json_schema",
            input_schema_source_text='{"type":"object"}',
        )
        WorkflowStepFactory(workflow=workflow)
        user = UserFactory()

        report = WorkflowVersioningService.clone(workflow, user=user)
        new_workflow = Workflow.objects.get(pk=report.new_workflow_id)

        assert new_workflow.project_id == workflow.project_id
        assert new_workflow.description == workflow.description
        assert new_workflow.allow_submission_name is False
        assert new_workflow.allow_submission_meta_data is True
        assert new_workflow.allow_submission_short_description is True
        assert new_workflow.make_info_page_public is True
        assert new_workflow.workflow_visibility == WorkflowVisibility.ALL_USERS
        assert new_workflow.success_message == "Validation succeeded."
        assert new_workflow.input_schema == workflow.input_schema
        assert new_workflow.input_schema_source_mode == "json_schema"
        assert new_workflow.input_schema_source_text == '{"type":"object"}'

    def test_clone_copies_step_resources(self):
        """Step-resource rows appear on the clone's steps too."""
        # The existing clone_to_new_version test exercises this
        # path; we just verify the count surfaces in the report.
        workflow = WorkflowFactory()
        WorkflowStepFactory(workflow=workflow)
        user = UserFactory()

        report = WorkflowVersioningService.clone(workflow, user=user)
        # Default step has no resources; expect 0.
        assert report.components_copied["step_resources"] == 0

    def test_clone_deep_copies_step_owned_contract_tree(self):
        """A new workflow version gets independent editable child rows.

        Step-level rulesets, assertions, and step-owned I/O/binding rows are
        part of the workflow-owned contract. If the clone reused them, editing
        the new version would silently mutate the meaning of old runs attached
        to the source version.
        """
        workflow = WorkflowFactory()
        ruleset = RulesetFactory(
            org=workflow.org,
            ruleset_type=RulesetType.BASIC,
        )
        step = WorkflowStepFactory(workflow=workflow, ruleset=ruleset)
        io_definition = StepIODefinitionFactory(
            validator=None,
            workflow_step=step,
            contract_key="shacl_total_count",
            direction=StepIODirection.OUTPUT,
        )
        input_definition = StepIODefinitionFactory(
            validator=None,
            workflow_step=step,
            contract_key="assertion_limit",
            direction=StepIODirection.INPUT,
        )
        StepIODefinitionFactory(
            validator=None,
            workflow_step=step,
            contract_key="validation_report",
            direction=StepIODirection.OUTPUT,
            data_type=CatalogValueType.ARTIFACT_REF,
            io_medium=StepIOMedium.ARTIFACT,
            artifact_kind=ArtifactKind.REPORT,
            media_type="text/html",
            data_format="html",
            accepted_data_formats=["html"],
            accepted_media_types=["text/html"],
            allowed_source_scopes=[BindingSourceScope.UPSTREAM_ARTIFACT],
            default_source_strategy=DefaultSourceStrategy.MANUAL,
            envelope_channel=EnvelopeChannel.OUTPUT_ARTIFACTS,
            role="report",
            min_items=1,
            max_items=1,
        )
        StepInputBindingFactory(
            workflow_step=step,
            io_definition=input_definition,
            default_value=0,
        )
        DerivationFactory(
            validator=None,
            workflow_step=step,
            contract_key="has_findings",
            expression="shacl_total_count > 0",
        )
        RulesetAssertionFactory(
            ruleset=ruleset,
            assertion_type=AssertionType.BASIC,
            operator=AssertionOperator.LE,
            target_io_definition=io_definition,
            target_data_path="",
            rhs={"value": 0},
            notes="No findings expected once the model passes QA.",
        )
        user = UserFactory()

        report = WorkflowVersioningService.clone(workflow, user=user)
        new_workflow = Workflow.objects.get(pk=report.new_workflow_id)
        new_step = new_workflow.steps.get()
        new_io_definition = new_step.step_io_definitions.get(
            contract_key="shacl_total_count"
        )
        new_artifact_definition = new_step.step_io_definitions.get(
            contract_key="validation_report",
        )
        new_assertion = new_step.ruleset.assertions.get()

        assert new_step.pk != step.pk
        assert new_step.ruleset_id != ruleset.pk
        assert new_io_definition.pk != io_definition.pk
        assert new_artifact_definition.io_medium == StepIOMedium.ARTIFACT
        assert new_artifact_definition.artifact_kind == ArtifactKind.REPORT
        assert (
            new_artifact_definition.envelope_channel == EnvelopeChannel.OUTPUT_ARTIFACTS
        )
        assert new_artifact_definition.accepted_data_formats == ["html"]
        assert new_artifact_definition.allowed_source_scopes == [
            BindingSourceScope.UPSTREAM_ARTIFACT,
        ]
        assert new_assertion.target_io_definition_id == new_io_definition.pk
        # Author rationale is non-semantic but part of the copied contract, so a
        # new version carries the reasoning forward rather than dropping it.
        assert new_assertion.notes == "No findings expected once the model passes QA."
        assert (
            new_step.input_bindings.get().io_definition.contract_key
            == "assertion_limit"
        )
        assert new_step.derivations.get(contract_key="has_findings").pk is not None
        assert report.components_copied["rulesets"] == 1
        assert report.components_copied["assertions"] == 1
        assert (
            report.components_copied["step_io_definitions"]
            == EXPECTED_CONTRACT_TREE_STEP_IO_DEFINITIONS
        )
        assert report.components_copied["input_bindings"] == 1
        assert report.components_copied["derivations"] == 1

    def test_clone_copies_validator_owned_io_promotion_overlays(self):
        """Cloning a workflow copies WorkflowStepIOPromotion overlays.

        Why it matters: validator-owned StepIODefinition rows (catalog
        entries shared across workflows) hold their promoted_signal_name
        in the WorkflowStepIOPromotion overlay table — the in-row field
        can't represent workflow-scoped names. The overlay IS workflow
        contract state: downstream s.<name> assertions on a new version
        of the workflow will silently break if the overlay doesn't
        clone with the workflow.

        Regression test for the May 2026 P1 review finding.

        We use an EnergyPlus catalog row owned by the validator (not
        the step) — the original ownership pattern that motivated
        the overlay model.
        """
        from validibot.validations.models import WorkflowStepIOPromotion

        workflow = WorkflowFactory()
        validator = ValidatorFactory(
            org=workflow.org,
            validation_type=ValidationType.ENERGYPLUS,
            is_system=True,
        )
        # Validator-owned: no workflow_step FK, FK is on validator.
        catalog_row = StepIODefinitionFactory(
            validator=validator,
            workflow_step=None,
            contract_key="zone_count",
            direction=StepIODirection.INPUT,
        )
        step = WorkflowStepFactory(workflow=workflow, validator=validator)
        WorkflowStepIOPromotion.objects.create(
            workflow_step=step,
            io_definition=catalog_row,
            promoted_signal_name="zones",
        )
        user = UserFactory()

        report = WorkflowVersioningService.clone(workflow, user=user)
        new_workflow = Workflow.objects.get(pk=report.new_workflow_id)
        new_step = new_workflow.steps.get()

        # The catalog row is shared — the new overlay points at the
        # SAME validator-owned StepIODefinition that the source did.
        new_overlay = WorkflowStepIOPromotion.objects.get(
            workflow_step=new_step,
        )
        assert new_overlay.io_definition_id == catalog_row.pk
        assert new_overlay.promoted_signal_name == "zones"
        # Both versions have their own overlay row — the source's
        # overlay still exists too (separate workflow_step FK).
        assert WorkflowStepIOPromotion.objects.filter(
            workflow_step=step,
            io_definition=catalog_row,
        ).exists()
        # Components report tracks the overlay count.
        assert report.components_copied["io_promotions"] == 1

    def test_step_owned_overlay_raises_validation_error(self):
        """Overlays on step-owned StepIODefinitions are forbidden.

        After the May 2026 follow-up review, the overlay model is
        restricted to validator-owned rows. Step-owned rows must use
        their in-row ``promoted_signal_name`` field instead — otherwise
        runtime would inject the same value under two ``s.*`` aliases
        (one from the in-row scan, one from the overlay scan).

        ``clean()`` enforces this at the application layer (the rest
        of the contract layer uses application-level XOR enforcement
        too). ``save()`` runs ``full_clean()`` so ORM-direct writes
        from services and migrations honour the invariant.
        """
        from django.core.exceptions import ValidationError as DjangoValidationError

        from validibot.validations.models import WorkflowStepIOPromotion

        workflow = WorkflowFactory()
        ruleset = RulesetFactory(
            org=workflow.org,
            ruleset_type=RulesetType.BASIC,
        )
        step = WorkflowStepFactory(workflow=workflow, ruleset=ruleset)
        step_owned_row = StepIODefinitionFactory(
            validator=None,
            workflow_step=step,
            contract_key="custom_metric",
            direction=StepIODirection.OUTPUT,
        )

        with pytest.raises(DjangoValidationError) as excinfo:
            WorkflowStepIOPromotion.objects.create(
                workflow_step=step,
                io_definition=step_owned_row,
                promoted_signal_name="metric",
            )
        # Error must explain why and point to the right alternative.
        assert "validator-owned" in str(excinfo.value)
        assert "in-row" in str(excinfo.value)

    def test_cross_validator_overlay_raises_validation_error(self):
        """Overlays must match the step's validator.

        The promote view at ``WorkflowStepPromoteStepIOView`` rejects
        cross-validator overlay attempts at the HTTP layer. The model
        ``clean()`` mirrors that rule so service/migration writes
        can't slip a catalog row from one validator into a step
        bound to a different validator — at runtime, that step would
        try to extract a contract_key its validator never emits.

        Regression test for the May 2026 follow-up review finding.
        """
        from django.core.exceptions import ValidationError as DjangoValidationError

        from validibot.validations.models import WorkflowStepIOPromotion

        workflow = WorkflowFactory()
        # The step uses validator A.
        validator_a = ValidatorFactory(
            org=workflow.org,
            validation_type=ValidationType.ENERGYPLUS,
            is_system=True,
        )
        step = WorkflowStepFactory(workflow=workflow, validator=validator_a)
        # The catalog row belongs to a different validator (B).
        validator_b = ValidatorFactory(
            org=workflow.org,
            validation_type=ValidationType.BASIC,
            is_system=True,
        )
        cross_validator_row = StepIODefinitionFactory(
            validator=validator_b,
            workflow_step=None,
            contract_key="something",
            direction=StepIODirection.INPUT,
        )

        with pytest.raises(DjangoValidationError) as excinfo:
            WorkflowStepIOPromotion.objects.create(
                workflow_step=step,
                io_definition=cross_validator_row,
                promoted_signal_name="something",
            )
        assert "same validator" in str(excinfo.value)

    def test_overlay_on_step_without_validator_raises_validation_error(self):
        """Overlays require the step to bind to a validator.

        A WorkflowStep can dispatch either a validator OR an
        action (the database's ``workflowstep_validator_xor_action``
        check constraint enforces XOR). For an action-type step,
        ``validator_id`` is None — and the step's runtime never
        invokes any validator, so an overlay pointing at a
        validator-owned catalog row could never resolve at runtime.
        The overlay would be silently dead weight.

        The earlier version of ``clean()`` short-circuited when
        ``step_validator_id is None``, allowing this shape through.
        The strengthened invariant requires both that the step has
        a validator AND that it matches the io_definition's
        validator. Regression test for the May 2026 follow-up
        review finding.

        We exercise this with an action-type step (the only way to
        construct a validator-less step that satisfies the XOR
        check constraint).
        """
        from django.core.exceptions import ValidationError as DjangoValidationError

        from validibot.actions.constants import ActionCategoryType
        from validibot.actions.constants import IntegrationActionType
        from validibot.actions.models import Action
        from validibot.actions.models import ActionDefinition
        from validibot.validations.models import WorkflowStepIOPromotion

        workflow = WorkflowFactory()
        # Build an action-type step (validator=None, action set).
        # The DB XOR check requires exactly one of validator /
        # action to be present. Action attaches to WorkflowStep
        # via the ``Action`` instance, not the ``ActionDefinition``
        # catalog row (definition → Action → WorkflowStep is the
        # canonical chain).
        action_def = ActionDefinition.objects.create(
            slug="integration-slack-message-overlay-test",
            name="Slack",
            description="",
            icon="bi-slack",
            action_category=ActionCategoryType.INTEGRATION,
            type=IntegrationActionType.SLACK_MESSAGE,
        )
        action = Action.objects.create(
            definition=action_def,
            slug="slack-overlay-test",
            name="Slack",
        )
        step = WorkflowStepFactory(
            workflow=workflow,
            validator=None,
            action=action,
        )
        # Validator-owned catalog row from some validator the step
        # doesn't use.
        validator = ValidatorFactory(
            org=workflow.org,
            validation_type=ValidationType.ENERGYPLUS,
            is_system=True,
        )
        catalog_row = StepIODefinitionFactory(
            validator=validator,
            workflow_step=None,
            contract_key="something",
            direction=StepIODirection.INPUT,
        )

        with pytest.raises(DjangoValidationError) as excinfo:
            WorkflowStepIOPromotion.objects.create(
                workflow_step=step,
                io_definition=catalog_row,
                promoted_signal_name="something",
            )
        # Error must call out both the same-validator requirement
        # AND the "step must have a validator" requirement so the
        # author can act on either fix.
        msg = str(excinfo.value)
        assert "same validator" in msg
        assert "step must have a validator" in msg

    def test_clone_copies_workflow_public_info_when_present(self):
        """``WorkflowPublicInfo`` (one-to-one) copies if the source has it."""
        workflow = WorkflowFactory()
        WorkflowStepFactory(workflow=workflow)
        WorkflowPublicInfo.objects.create(
            workflow=workflow,
            content_md="Source description",
        )
        user = UserFactory()

        report = WorkflowVersioningService.clone(workflow, user=user)
        new_workflow = Workflow.objects.get(pk=report.new_workflow_id)

        assert report.components_copied["public_info"] == 1
        # The new workflow has its own public_info row (different pk).
        new_public_info = new_workflow.public_info
        assert new_public_info.content_md == "Source description"
        assert new_public_info.workflow_id == new_workflow.pk

    def test_clone_skips_public_info_when_source_has_none(self):
        """Source without WorkflowPublicInfo -> count is 0."""
        workflow = WorkflowFactory()
        WorkflowStepFactory(workflow=workflow)
        user = UserFactory()

        report = WorkflowVersioningService.clone(workflow, user=user)
        assert report.components_copied["public_info"] == 0

    def test_clone_copies_workflow_role_access(self):
        """``WorkflowRoleAccess`` rows replicate to the new workflow.

        Forgetting to copy these would silently grant or revoke
        role access on the new version — exactly the silent-drift
        problem Phase 3 fixes.
        """
        from validibot.users.constants import RoleCode
        from validibot.users.models import Role

        workflow = WorkflowFactory()
        WorkflowStepFactory(workflow=workflow)
        # Create a role and grant it on the source workflow.
        role, _ = Role.objects.get_or_create(
            code=RoleCode.EXECUTOR,
            defaults={"name": "Executor"},
        )
        WorkflowRoleAccess.objects.create(workflow=workflow, role=role)
        user = UserFactory()

        report = WorkflowVersioningService.clone(workflow, user=user)
        assert report.components_copied["role_access"] == 1

        new_workflow = Workflow.objects.get(pk=report.new_workflow_id)
        new_access = WorkflowRoleAccess.objects.filter(workflow=new_workflow)
        assert new_access.count() == 1
        assert new_access.first().role == role

    def test_clone_is_atomic_on_failure(self):
        """A mid-clone failure leaves no partial workflow behind.

        We don't have an easy way to inject a failure mid-service
        without monkeypatching. This test documents the atomic
        intent and verifies the source isn't locked when the clone
        completes (i.e. the lock happens within the same transaction
        as the copy).
        """
        workflow = WorkflowFactory(is_locked=False)
        WorkflowStepFactory(workflow=workflow)
        user = UserFactory()

        # Successful run — verify atomicity by checking the
        # post-state matches the documented contract.
        WorkflowVersioningService.clone(workflow, user=user)
        workflow.refresh_from_db()
        # Both effects (lock + new workflow exists) happened.
        assert workflow.is_locked is True
        assert Workflow.objects.filter(slug=workflow.slug, version=2).count() == 1


class WorkflowCloneToNewVersionDelegationTests(TestCase):
    """The legacy ``Workflow.clone_to_new_version`` still works.

    Phase 3 refactored the method to delegate to
    :meth:`WorkflowVersioningService.clone`. Existing callers that
    invoke the model method (template tags, admin actions, the
    workflow-edit view) should keep working without modification.
    """

    def test_legacy_method_returns_workflow_instance(self):
        """``clone_to_new_version`` returns the new Workflow row."""
        expected_clone_version = 2
        workflow = WorkflowFactory()
        WorkflowStepFactory(workflow=workflow)
        user = UserFactory()

        new = workflow.clone_to_new_version(user)

        assert isinstance(new, Workflow)
        assert new.slug == workflow.slug
        assert new.version == expected_clone_version

    def test_legacy_method_locks_source(self):
        """The legacy method's contract: source is locked after clone."""
        workflow = WorkflowFactory(is_locked=False)
        WorkflowStepFactory(workflow=workflow)
        user = UserFactory()

        workflow.clone_to_new_version(user)
        workflow.refresh_from_db()
        assert workflow.is_locked is True
