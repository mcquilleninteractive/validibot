"""Contract tests for fail-closed GCP secret version retention."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_django_secret_upload_applies_bounded_retention() -> None:
    """The unified application secret must prune only after creating a latest."""
    django_recipe = (REPO_ROOT / "just/gcp/django/mod.just").read_text(
        encoding="utf-8",
    )

    assert "--format=json" in django_recipe
    assert "NEW_VERSION" in django_recipe
    assert "prune-secret-versions.sh" in django_recipe
    assert "GCP_SECRET_VERSIONS_TO_KEEP:-2" in django_recipe
    assert "--new-version=" in django_recipe
    assert "--mode=apply" in django_recipe
    assert "gcloud secrets versions access" not in django_recipe


def test_umbrella_secret_upload_delegates_to_the_django_recipe() -> None:
    """The umbrella path must use the one application secret retention policy."""
    recipes = (REPO_ROOT / "just/gcp/mod.just").read_text(encoding="utf-8")
    recipe = recipes.split("secrets stage:", maxsplit=1)[1].split(
        "# Operations",
        maxsplit=1,
    )[0]

    assert 'just gcp django secrets "{{stage}}"' in recipe
    assert 'just gcp mcp secrets "{{stage}}"' not in recipe


def test_secret_retention_helper_is_fail_closed_and_never_reads_payloads() -> None:
    """The helper must protect current references and validate raw inventories."""
    helper = (REPO_ROOT / "ops/gcp/prune-secret-versions.sh").read_text(
        encoding="utf-8",
    )

    assert "gcloud run services list" in helper
    assert "gcloud run jobs list" in helper
    assert "resolve_enabled_version" in helper
    assert "collect_protected_versions" in helper
    assert "--format=json" in helper
    assert "gcloud secrets versions destroy" in helper
    assert "gcloud secrets versions access" not in helper


def test_secret_retention_helper_accepts_recipe_argument_style() -> None:
    """Upload recipes use ``--option=value`` and the helper must parse it."""
    helper = (REPO_ROOT / "ops/gcp/prune-secret-versions.sh").read_text(
        encoding="utf-8",
    )

    for option in ("project", "secret", "new-version", "keep", "mode"):
        assert f"--{option}=*)" in helper


def test_artifact_cleanup_policy_cannot_match_validator_backends() -> None:
    """Generic age/count cleanup must remain limited to the Django app."""
    policy = json.loads(
        (REPO_ROOT / "ops/gcp/artifact-cleanup-policy.json").read_text(
            encoding="utf-8",
        ),
    )

    assert policy
    for rule in policy:
        scope = rule.get("condition", rule.get("mostRecentVersions", {}))
        assert scope["packageNamePrefixes"] == ["validibot-web"]


def test_fixed_job_provider_preflight_requires_execution_drain(tmp_path) -> None:
    """The transitional fixed-Job guard must fail until executions are terminal."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fixture = tmp_path / "executions.json"
    fake_gcloud = fake_bin / "gcloud"
    fake_gcloud.write_text(
        '#!/usr/bin/env bash\nset -euo pipefail\ncat "${GCLOUD_EXECUTION_FIXTURE}"\n',
        encoding="utf-8",
    )
    fake_gcloud.chmod(0o755)
    environment = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "GCLOUD_EXECUTION_FIXTURE": str(fixture),
    }
    current_digest = "sha256:" + "1" * 64
    replacement_digest = "sha256:" + "2" * 64
    command = [
        "bash",
        str(REPO_ROOT / "ops/gcp/preflight-validator-job-update.sh"),
        "--project=test-project",
        "--region=test-region",
        "--job=validibot-validator-backend-energyplus",
        f"--current-digest={current_digest}",
        f"--replacement-digest={replacement_digest}",
    ]
    fixture.write_text(
        '[{"metadata":{"name":"still-running"},"conditions":[]}]',
        encoding="utf-8",
    )

    active_result = subprocess.run(  # noqa: S603 - fixed local test command
        command,
        check=False,
        capture_output=True,
        env=environment,
        text=True,
    )

    assert active_result.returncode != 0
    assert "still-running" in active_result.stderr

    fixture.write_text(
        '[{"metadata":{"name":"done"},"completionTime":"2026-07-24T00:00:00Z"}]',
        encoding="utf-8",
    )
    terminal_result = subprocess.run(  # noqa: S603 - fixed local test command
        command,
        check=False,
        capture_output=True,
        env=environment,
        text=True,
    )

    assert terminal_result.returncode == 0
    assert "no pending or running" in terminal_result.stdout


def test_release_job_deploy_never_mutates_a_legacy_fixed_job() -> None:
    """Immutable release Jobs must not enter the old drain-and-update path."""
    recipes = (REPO_ROOT / "just/gcp/mod.just").read_text(encoding="utf-8")
    recipe = recipes.split(
        'validator-job-deploy name stage release_tag=""',
        maxsplit=1,
    )[1].split(
        "# Deploy all managed validator Jobs without deploying Services",
        maxsplit=1,
    )[0]

    database_preflight = "preflight_validator_job_update"
    provider_preflight = "preflight-validator-job-update.sh"
    assert 'gcloud run jobs create "$JOB_NAME"' in recipe
    assert 'gcloud run jobs deploy "$JOB_NAME"' not in recipe
    assert 'gcloud run jobs update "$JOB_NAME"' not in recipe
    assert database_preflight not in recipe
    assert provider_preflight not in recipe
