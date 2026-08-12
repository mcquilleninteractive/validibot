"""Contracts linking local ``just`` commands to protected-main CI.

Validibot has one Python application containing Django and the embedded MCP
surface, plus generated frontend assets. These tests prevent release helpers
or the aggregate branch-protection check from silently omitting a shipped
surface or accidentally restoring the retired standalone MCP runtime.
"""

import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
JUSTFILE = REPO_ROOT / "justfile"
GCP_JUSTFILE = REPO_ROOT / "just" / "gcp" / "mod.just"
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
PRECOMMIT_CONFIG = REPO_ROOT / ".pre-commit-config.yaml"
PYPROJECT = REPO_ROOT / "pyproject.toml"
EXPECTED_RUNTIME_COUNT = 1


def test_root_check_covers_every_locally_owned_runtime():
    """The advertised gate must test the integrated backend and frontend."""
    justfile = JUSTFILE.read_text(encoding="utf-8")

    assert (
        "check: lock-check format-check lint typecheck test frontend-check" in justfile
    )
    assert "just mcp check" not in justfile
    assert "cd mcp && uv lock --check" not in justfile
    assert "ruff check --exclude '*.md' ." in justfile
    assert "ruff format --check --exclude '*.md' ." in justfile
    assert "npm test -- --passWithNoTests=false" in justfile
    assert "npm run build" in justfile


def test_local_and_precommit_ruff_versions_match():
    """Local and CI linting must enforce one deterministic Ruff policy."""
    project = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    ruff_requirement = next(
        requirement
        for requirement in project["dependency-groups"]["dev"]
        if requirement.startswith("ruff==")
    )
    ruff_version = ruff_requirement.removeprefix("ruff==")
    precommit_config = PRECOMMIT_CONFIG.read_text(encoding="utf-8")

    assert f"rev: v{ruff_version}" in precommit_config


def test_ci_exercises_mcp_inside_the_django_runtime():
    """Branch protection must test MCP without rebuilding a second runtime."""
    workflow = CI_WORKFLOW.read_text(encoding="utf-8")
    aggregate = workflow.split("\n  ci:\n", maxsplit=1)[1]

    assert "\n  mcp:\n" not in workflow
    assert "working-directory: mcp" not in workflow
    assert "uv run --frozen --group dev --extra docker-runner pytest" in workflow
    assert "      - mcp" not in aggregate
    assert "MCP_RESULT:" not in aggregate


def test_dependency_audits_consume_exported_lockfiles():
    """An isolated ``uvx`` tool must receive explicit project dependency inputs."""
    justfile = JUSTFILE.read_text(encoding="utf-8")
    workflow = CI_WORKFLOW.read_text(encoding="utf-8")

    assert justfile.count("uv export --frozen") == EXPECTED_RUNTIME_COUNT
    assert justfile.count("--from pip-audit==2.10.1") == EXPECTED_RUNTIME_COUNT
    assert workflow.count("uv export --frozen") == EXPECTED_RUNTIME_COUNT
    assert workflow.count("--from pip-audit==2.10.1") == EXPECTED_RUNTIME_COUNT
    assert justfile.count("--require-hashes") == EXPECTED_RUNTIME_COUNT
    assert workflow.count("--require-hashes") == EXPECTED_RUNTIME_COUNT
    assert "run: uvx pip-audit" not in workflow


def test_release_requires_exact_commit_ci_and_a_clean_post_check_tree():
    """A bypassed direct push cannot be tagged before its own CI succeeds."""
    justfile = JUSTFILE.read_text(encoding="utf-8")

    assert "release-check: check audit _require-release-ci" in justfile
    assert "--workflow ci.yml" in justfile
    assert '--commit "$HEAD_SHA"' in justfile
    assert "--event push" in justfile
    assert 'gh run watch "$RUN_ID"' in justfile
    assert "just release-check" in justfile
    assert "Release checks changed the working tree" in justfile


def test_gcp_automation_does_not_restore_a_standalone_mcp_build():
    """GCP must publish MCP only as part of the canonical web application."""
    gcp_justfile = GCP_JUSTFILE.read_text(encoding="utf-8")

    assert "just gcp mcp build" not in gcp_justfile
    assert "just gcp mcp build-push" not in gcp_justfile
