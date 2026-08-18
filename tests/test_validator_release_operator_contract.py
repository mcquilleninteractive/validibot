"""Static tests for the public independent-validator operator interface.

The recipes ultimately invoke cloud CLIs, which these tests must never run.
Instead, they pin the safety-critical public command construction: routine
operations expose five commands, retained release records feed status, and
provider deployment creates release-specific resources from digest-selected
images without updating a stable validator Job in place.

The public test suite must remain runnable from a standalone source checkout.
Hosted production coordinates and cross-repository wrappers belong to the
private ``validibot-project`` repository and are deliberately not inspected
here.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PUBLIC_GCP_RECIPES = REPO_ROOT / "just" / "gcp" / "mod.just"
CERTIFICATION_COMMAND = (
    REPO_ROOT
    / "validibot"
    / "validations"
    / "management"
    / "commands"
    / "certify_validator_backend_release.py"
)
ROUTINE_RECIPE_HEADERS = (
    "validator-setup stage",
    "validator-status stage",
    'validator-update stage backend=""',
    'validator-rollback stage backend operation="release"',
    "validator-cleanup stage",
)
EXPECTED_STATUS_SELECTION_CALL_COUNT = 3
EXPECTED_NORMAL_ROUTE_REFERENCES = 3
EXPECTED_LATEST_ONLY_STATE_EXPORTS = 2


def _recipe(text: str, name: str, next_marker: str) -> str:
    """Return one recipe body without parsing or executing Just syntax."""

    match = re.search(rf"(?m)^{re.escape(name)}:", text)
    if match is None:
        raise AssertionError(f"Recipe not found: {name}")
    start = match.start()
    end = text.index(next_marker, start)
    return text[start:end]


def test_public_recipes_expose_complete_routine_operator_surface():
    """Standalone installations need every routine release operation."""

    text = PUBLIC_GCP_RECIPES.read_text(encoding="utf-8")

    for header in ROUTINE_RECIPE_HEADERS:
        assert re.search(
            rf"(?m)^{re.escape(header)}: _require-gcp-config$",
            text,
        )


def test_release_job_recipe_creates_one_digest_selected_named_resource():
    """A backend release must never rewrite a stable validator Job definition."""

    text = PUBLIC_GCP_RECIPES.read_text(encoding="utf-8")
    recipe = _recipe(
        text,
        'validator-job-deploy name stage release_tag=""',
        "# Deploy all managed validator Jobs",
    )

    assert 'name --backend "{{name}}" --version "$BACKEND_RELEASE"' in recipe
    assert 'IMAGE_REF="${IMAGE_REPOSITORY}@${IMAGE_DIGEST}"' in recipe
    assert 'gcloud run jobs create "$JOB_NAME"' in recipe
    assert "gcloud run jobs update" not in recipe
    assert "gcloud run jobs delete" not in recipe
    assert "VALIDIBOT_BACKEND_SLUG={{name}}" in recipe
    assert 'VALIDIBOT_SOURCE_RELEASE_TAG="$SOURCE_TAG"' in recipe
    assert 'VALIDIBOT_RELEASE_RECORD_SHA256="$RELEASE_RECORD_SHA"' in recipe
    assert 'revision="$DEPLOYMENT_REVISION"' in recipe


def test_release_service_and_job_share_release_identity_environment():
    """Both pair members must expose values the read-only importers compare."""

    text = PUBLIC_GCP_RECIPES.read_text(encoding="utf-8")
    job = _recipe(
        text,
        'validator-job-deploy name stage release_tag=""',
        "# Deploy all managed validator Jobs",
    )
    service = _recipe(
        text,
        'validator-service-deploy name stage release_tag=""',
        "# Provision all managed Services",
    )
    required = (
        "VALIDIBOT_BACKEND_SLUG={{name}}",
        'VALIDIBOT_BACKEND_IMAGE_DIGEST="$IMAGE_DIGEST"',
        'VALIDIBOT_BACKEND_RELEASE="$BACKEND_RELEASE"',
        'VALIDIBOT_SOURCE_RELEASE_TAG="$SOURCE_TAG"',
        'VALIDIBOT_RELEASE_RECORD_SHA256="$RELEASE_RECORD_SHA"',
    )

    for value in required:
        assert value in job
        assert value in service
    assert "latest" not in job
    assert "latest" not in service


def test_reusing_a_release_service_does_not_create_another_revision():
    """An idempotent update must observe an immutable Service without updating it."""
    text = PUBLIC_GCP_RECIPES.read_text(encoding="utf-8")
    service = _recipe(
        text,
        'validator-service-deploy name stage release_tag=""',
        "# Provision all managed Services",
    )
    reuse = service.split('if [ "$CREATE_SERVICE" = "1" ]', maxsplit=1)[0]

    assert "Leaving the Service definition unchanged" in reuse
    assert 'gcloud run services update "$SERVICE_NAME"' not in reuse


def test_live_transition_does_not_update_validator_service_definitions():
    """Opening a stage must not create new revisions of accepted releases."""
    text = PUBLIC_GCP_RECIPES.read_text(encoding="utf-8")
    live = _recipe(
        text,
        "mode-live stage",
        "# Report the reconciled lifecycle mode.",
    )

    assert "Restoring validator Service" not in live
    assert "desired-min-instances" not in live


def test_latest_only_retries_database_finalization_without_provider_deletions():
    """A retry must finish historical checkpoints after providers are absent.

    Provider deletion and database retirement are separate resumable phases.
    Once Cloud Run is already clean, latest-only must keep going, use the
    complete historical deployment projection, and route wholly unaccepted
    failed candidates through the explicit guarded retirement option.
    """
    text = PUBLIC_GCP_RECIPES.read_text(encoding="utf-8")
    recipe = _recipe(
        text,
        "validator-latest-only stage",
        "# Retain the exact accepted release record",
    )
    no_provider_branch = recipe.split(
        'echo "  No superseded managed validator providers found."',
        maxsplit=1,
    )[1].split("while IFS= read -r name", maxsplit=1)[0]

    assert "exit 0" not in no_provider_branch
    assert "(.deployment_history // .deployments)[]" in recipe
    assert "every historical deployment attempt to be terminal" in recipe
    assert "--deactivate-superseded" in recipe
    assert '[ "$role" = "INACTIVE" ] || continue' not in recipe
    assert (
        recipe.count("_validator-state-export {{stage}}")
        == EXPECTED_LATEST_ONLY_STATE_EXPORTS
    )
    assert "--allow-unaccepted-candidate" in recipe


def test_validator_capacity_uses_only_service_level_scaling():
    """Capacity reconciliation must not change revision-level configuration."""
    text = PUBLIC_GCP_RECIPES.read_text(encoding="utf-8")
    capacity = _recipe(
        text,
        "validator-service-capacity name stage version minimum maximum",
        "# Diagnostic compatibility alias",
    )

    assert "--min={{minimum}}" in capacity
    assert "--max={{maximum}}" in capacity
    assert "--min-instances" not in capacity
    assert "--update-labels" not in capacity


def test_status_reads_retained_accepted_release_records_for_selected_stage():
    """A standalone stage must protect releases retained in its own bucket."""

    text = PUBLIC_GCP_RECIPES.read_text(encoding="utf-8")
    recipe = _recipe(
        text,
        "_validator-status-json stage output",
        "# Retain the exact accepted release record",
    )

    assert 'if [ "{{stage}}" = "prod" ]' in recipe
    assert 'STORAGE_BUCKET="${APP_NAME}-storage"' in recipe
    assert 'STORAGE_BUCKET="${APP_NAME}-storage-{{stage}}"' in recipe
    assert "operations/validator-backend-releases/*/*.json" in recipe
    assert '--release-records-json "$WORK_DIR/records.json"' in recipe


def test_setup_and_multi_update_activate_selected_backends_as_one_group():
    """A final route failure must not leave a partially active backend set."""

    text = PUBLIC_GCP_RECIPES.read_text(encoding="utf-8")
    setup = _recipe(
        text,
        "validator-setup stage",
        "# Reconcile one backend",
    )
    update = _recipe(
        text,
        'validator-update stage backend=""',
        "# Roll back one backend",
    )
    acceptance = _recipe(
        text,
        'validator-acceptance name stage release_tag attempts="3" '
        'final_mode="normal" outgoing_version=""',
        "# Historical all-backend acceptance",
    )
    certifier = CERTIFICATION_COMMAND.read_text(encoding="utf-8")

    for recipe in (setup, update):
        assert "activate_validator_backend_release_group" in recipe
        assert "--release=${" in recipe
        assert "_validator-status-json" in recipe
        assert "activation-check" in recipe
        assert "validator-acceptance" in recipe
        assert 'management-cmd {{stage}} "$GROUP_COMMAND"' in recipe
    assert "certify_validator_backend_release" in acceptance
    assert '"sync_gcp_validator_deployments"' in certifier
    assert '"sync_gcp_validator_services"' in certifier


def test_validator_mutations_restore_the_mode_that_was_active_on_entry():
    """Release work must not turn a parked or maintenance stage into LIVE.

    Each operation records the complete lifecycle classification before its
    first mutation, rejects an already partial transition, performs isolated
    work in MAINTENANCE, and delegates restoration to the shared exact-mode
    helper on both success and cleanup paths.
    """
    text = PUBLIC_GCP_RECIPES.read_text(encoding="utf-8")
    recipes = (
        _recipe(text, "validator-setup stage", "# Reconcile one backend"),
        _recipe(
            text,
            'validator-update stage backend=""',
            "# Roll back one backend",
        ),
        _recipe(
            text,
            'validator-rollback stage backend operation="release"',
            "# Calculate an exact seven-day cleanup plan",
        ),
    )

    for recipe in recipes:
        assert "INITIAL_MODE=$(just gcp _mode-current {{stage}})" in recipe
        assert "PARTIALLY_TRANSITIONED" in recipe
        assert "trap" in recipe
        assert "EXIT INT TERM" in recipe
        assert "mode-maintenance {{stage}}" in recipe
        assert '_mode-restore {{stage}} "$INITIAL_MODE"' in recipe


def test_update_reverifies_and_round_trips_the_outgoing_release():
    """Candidate acceptance must prove the advertised rollback pair still works."""

    text = PUBLIC_GCP_RECIPES.read_text(encoding="utf-8")
    update = _recipe(
        text,
        'validator-update stage backend=""',
        "# Roll back one backend",
    )
    certifier = CERTIFICATION_COMMAND.read_text(encoding="utf-8")

    assert 'old_source_tag="${selected_backend}-v${old_version}"' in update
    assert 'validator-release-verify "$selected_backend"' in update
    assert '3 inactive "$old_version"' in update
    assert "Reverifying and round-tripping the outgoing rollback pair" in certifier
    assert 'job_name=options["outgoing_job_name"]' in certifier
    assert 'service_name=options["outgoing_service_name"]' in certifier
    assert 'release_version=options["outgoing_version"]' in certifier
    assert (
        certifier.count("ExecutionRoutingMode.NORMAL")
        >= EXPECTED_NORMAL_ROUTE_REFERENCES
    )


def test_update_migrates_missing_legacy_release_evidence_after_confirmation():
    """Older accepted releases gain retained evidence without a repair command.

    Retained release records were introduced after some installations already
    had accepted provider pairs. The routine update command must distinguish
    that one repairable blocker from unsafe drift, retain the signed public
    record only after the normal operator confirmation, and re-run status so a
    mismatched record still fails before provider deployment begins.
    """

    text = PUBLIC_GCP_RECIPES.read_text(encoding="utf-8")
    update = _recipe(
        text,
        'validator-update stage backend=""',
        "# Roll back one backend",
    )

    assert (
        'LEGACY_RECORD_BLOCKER="active accepted release record is not retained"'
        in update
    )
    assert "select(.blockers != [$legacy_record_blocker])" in update
    assert "_validator-retain-release-record" in update
    assert "status-after-record-migration.json" in update
    assert "retained release evidence did not match the active deployment" in update
    confirmation = 'read -r -p "Type $CONFIRM to continue: " REPLY'
    assert update.index(confirmation) < update.index("_validator-retain-release-record")


def test_update_stops_when_the_requested_release_is_already_reconciled():
    """Naming a healthy backend explicitly must not force a duplicate rollout."""
    text = PUBLIC_GCP_RECIPES.read_text(encoding="utf-8")
    update = _recipe(
        text,
        'validator-update stage backend=""',
        "# Roll back one backend",
    )

    assert '.backend == $backend and .recommended_action != "none"' in update
    assert "No validator release deployment is required." in update
    assert update.count("select_status_rows") >= EXPECTED_STATUS_SELECTION_CALL_COUNT


def test_management_command_failure_prints_the_django_execution_log():
    """A failed remote command must expose its useful error before cleanup."""
    text = PUBLIC_GCP_RECIPES.read_text(encoding="utf-8")
    command = _recipe(
        text,
        "management-cmd stage command",
        "# DESTRUCTIVE: completely reset",
    )

    assert 'if ! gcloud run jobs execute "$JOB_NAME"' in command
    assert '>"$EXECUTION_FILE"' in command
    assert "gcloud run jobs executions list" in command
    assert "gcloud beta run jobs executions logs read" in command


def test_update_cleanup_handles_a_preflight_failure_before_any_backend_is_touched():
    """A read-only update failure must not crash while iterating an empty array.

    macOS ships Bash 3.2, where expanding an empty array under ``set -u`` raises
    an unbound-variable error. The cleanup trap must therefore guard the route
    restoration loop when preflight stopped before provider mutation.
    """

    text = PUBLIC_GCP_RECIPES.read_text(encoding="utf-8")
    update = _recipe(
        text,
        'validator-update stage backend=""',
        "# Roll back one backend",
    )

    guard = 'if [ "${#TOUCHED_BACKENDS[@]}" -gt 0 ]; then'
    loop = 'for selected_backend in "${TOUCHED_BACKENDS[@]}"; do'
    assert guard in update
    assert loop in update
    assert update.index(guard) < update.index(loop)


def test_exact_recovery_requires_and_transports_a_recorded_repair_reason():
    """Reusing a rolled-back version must create accountable audit metadata."""

    text = PUBLIC_GCP_RECIPES.read_text(encoding="utf-8")
    rollback = _recipe(
        text,
        'validator-rollback stage backend operation="release"',
        "# Calculate an exact seven-day cleanup plan",
    )
    route = _recipe(
        text,
        (
            "validator-backend-route name stage version "
            'mode="normal" cause="SUPERSEDED_BY_ACCEPTED_RELEASE" '
            'allow_unaccepted="" reason_b64=""'
        ),
        "# Set mutable service-level capacity",
    )

    assert ".rolled_back_from[]?" in rollback
    assert "Explain what was repaired before reusing this release" in rollback
    assert "reusing a rolled-back release requires a reason" in rollback
    assert "base64 | tr -d" in rollback
    assert '"$CAUSE" "" "$RECOVERY_REASON_B64"' in rollback
    assert "--reason-b64={{reason_b64}}" in route


def test_release_lifecycle_recipes_avoid_django_reserved_version_option():
    """Operator recipes must not shadow Django's global ``--version`` flag."""

    text = PUBLIC_GCP_RECIPES.read_text(encoding="utf-8")
    route = _recipe(
        text,
        (
            "validator-backend-route name stage version "
            'mode="normal" cause="SUPERSEDED_BY_ACCEPTED_RELEASE" '
            'allow_unaccepted="" reason_b64=""'
        ),
        "# Set mutable service-level capacity",
    )
    cleanup = _recipe(
        text,
        "validator-cleanup stage",
        "# Report persisted p50/p95 timing stages",
    )

    assert "activate_validator_backend_release" in route
    assert "--release-version={{version}}" in route
    assert "--version={{version}}" not in route
    assert "retire_validator_backend_release" in cleanup
    assert "--release-version=$version" in cleanup
    assert "--version=$version" not in cleanup


def test_release_preflight_names_each_missing_publication_artifact():
    """Operators must see the exact absent trust artifact before GCP mutation."""

    text = PUBLIC_GCP_RECIPES.read_text(encoding="utf-8")
    verifier = _recipe(
        text,
        "_validator-release-verify-image name release_tag",
        "# Verify the signed release and GAR mirror",
    )

    required_messages = (
        "Missing: signed Git tag $SOURCE_TAG",
        "Missing: GHCR image",
        "Missing or invalid: image build attestation",
        "Missing: GitHub Release $SOURCE_TAG",
        '"backend release JSON"',
        '"release JSON checksum"',
        '"SPDX SBOM"',
        "Missing or invalid: attestation for backend release JSON",
        "Missing: Artifact Registry mirror",
        "No GCP resources were changed.",
    )
    for message in required_messages:
        assert message in verifier
