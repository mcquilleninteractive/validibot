"""Security-contract tests for the standalone MCP production container.

The MCP service is a public OAuth boundary deployed independently from Django.
These tests keep its image reproducible and ensure a future Dockerfile edit
cannot silently restore floating build tools, unlocked dependencies, or a root
runtime user.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MCP_DOCKERFILE = REPO_ROOT / "compose" / "production" / "mcp" / "Dockerfile"
MCP_DOCKERIGNORE = REPO_ROOT / "mcp" / ".dockerignore"
EXPECTED_UV_IMAGE = (
    "ghcr.io/astral-sh/uv:0.11.19@"
    "sha256:b46b03ddfcfbf8f547af7e9eaefdf8a39c8cebcba7c98858d3162bd28cf536f6"
)
EXPECTED_PYTHON_IMAGE = (
    "python:3.13-slim@"
    "sha256:6771159cd4fa5d9bba1258caf0b82e6b73458c694d178ad97c5e925c2d0e1a91"
)
EXPECTED_PYTHON_STAGE_COUNT = 2


def test_mcp_image_uses_pinned_build_and_runtime_inputs():
    """The MCP image must consume immutable images and its committed lockfile."""
    dockerfile = MCP_DOCKERFILE.read_text(encoding="utf-8")
    python_stage_count = dockerfile.count(f"FROM {EXPECTED_PYTHON_IMAGE}")

    assert "ghcr.io/astral-sh/uv:latest" not in dockerfile
    assert f"FROM {EXPECTED_UV_IMAGE} AS uv" in dockerfile
    assert python_stage_count == EXPECTED_PYTHON_STAGE_COUNT
    assert "COPY pyproject.toml uv.lock ./" in dockerfile
    assert "uv sync --frozen --no-dev --no-install-project" in dockerfile
    assert "uv pip install --system" not in dockerfile


def test_mcp_runtime_is_minimal_and_non_root():
    """Build tooling and root authority must not reach the serving process."""
    dockerfile = MCP_DOCKERFILE.read_text(encoding="utf-8")
    runtime = dockerfile.split(" AS runtime", maxsplit=1)[1]

    assert "COPY --from=builder" in runtime
    assert "COPY --from=uv" not in runtime
    assert "groupadd --gid 1000 validibot" in runtime
    assert "useradd --uid 1000 --gid validibot" in runtime
    assert "USER validibot:validibot" in runtime
    assert runtime.index("USER validibot:validibot") < runtime.index('CMD ["uvicorn"')


def test_mcp_build_context_excludes_local_and_development_state():
    """Local environments, caches, tests, and bytecode must stay out of builds."""
    dockerignore = MCP_DOCKERIGNORE.read_text(encoding="utf-8")

    effective_rules = [
        line for line in dockerignore.splitlines() if line and not line.startswith("#")
    ]

    assert effective_rules == [
        "**",
        "!pyproject.toml",
        "!uv.lock",
        "!src/",
        "!src/**/",
        "!src/**/*.py",
    ]
