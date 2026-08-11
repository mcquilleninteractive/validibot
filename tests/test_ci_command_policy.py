"""Contracts linking local ``just`` commands to protected-main CI.

Validibot has two independently resolved Python applications (Django and MCP),
plus generated frontend assets. These tests prevent release helpers or the
aggregate branch-protection check from silently omitting one of those surfaces.
"""

import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
JUSTFILE = REPO_ROOT / "justfile"
MCP_JUSTFILE = REPO_ROOT / "just" / "mcp" / "mod.just"
GCP_JUSTFILE = REPO_ROOT / "just" / "gcp" / "mod.just"
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
PRECOMMIT_CONFIG = REPO_ROOT / ".pre-commit-config.yaml"
PYPROJECT = REPO_ROOT / "pyproject.toml"
EXPECTED_RUNTIME_COUNT = 2
MINIMUM_MCP_FROZEN_COMMAND_COUNT = 5


def test_root_check_covers_every_locally_owned_runtime():
    """The advertised integration gate must include Django, frontend, and MCP."""
    justfile = JUSTFILE.read_text(encoding="utf-8")

    assert (
        "check: lock-check format-check lint typecheck test frontend-check" in justfile
    )
    assert "just mcp check" in justfile
    assert "cd mcp && uv lock --check" in justfile
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


def test_mcp_check_uses_its_frozen_environment_and_container_contract():
    """MCP verification must not rely on Django's environment or skip its image."""
    justfile = MCP_JUSTFILE.read_text(encoding="utf-8")

    assert "check: lock-check format-check lint test" in justfile
    assert (
        justfile.count("uv run --frozen --extra dev")
        >= MINIMUM_MCP_FROZEN_COMMAND_COUNT
    )
    assert "pytest -xvs tests/" in justfile


def test_ci_requires_the_standalone_mcp_job():
    """Branch protection must fail if MCP code, lock, audit, or tests fail."""
    workflow = CI_WORKFLOW.read_text(encoding="utf-8")
    aggregate = workflow.split("\n  ci:\n", maxsplit=1)[1]

    assert "\n  mcp:\n" in workflow
    assert "uv sync --frozen --extra dev" in workflow
    assert "Test MCP and its production container contract" in workflow
    assert "pytest -xvs tests/" in workflow
    assert "      - mcp" in aggregate
    assert "MCP_RESULT: ${{ needs.mcp.result }}" in aggregate
    assert '"$MCP_RESULT"' in aggregate


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


def test_mcp_deployment_uses_explicit_build_and_push_semantics():
    """New automation must state when an MCP build will publish registry bytes."""
    mcp_justfile = MCP_JUSTFILE.read_text(encoding="utf-8")
    gcp_justfile = GCP_JUSTFILE.read_text(encoding="utf-8")

    assert "build-local:" in mcp_justfile
    assert "build-push: _require-gcp-config" in mcp_justfile
    assert "build: build-push" in mcp_justfile
    assert "just gcp mcp build-push" in gcp_justfile
