"""Keep public release verification instructions aligned with release CI.

Operators must be able to verify the exact attestation predicate that the
immutable release workflow publishes. These checks prevent a workflow or
documentation edit from leaving a plausible-looking command that cannot find
or adequately constrain the release attestation.
"""

from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
RELEASE_GUIDE = REPOSITORY_ROOT / "RELEASING.md"
SECURITY_GUIDE = REPOSITORY_ROOT / "SECURITY.md"
RELEASE_WORKFLOW = REPOSITORY_ROOT / ".github" / "workflows" / "release.yml"


def _assert_constrained_cyclonedx_verification(document: str) -> None:
    """Require the controls that identify the published SBOM attestation."""

    assert "--predicate-type https://cyclonedx.org/bom" in document
    assert (
        "--signer-workflow mcquilleninteractive/validibot/.github/workflows/release.yml"
    ) in document
    assert "--source-ref refs/tags/vX.Y.Z" in document


def test_release_guide_verifies_the_published_cyclonedx_attestation() -> None:
    """The operator command must select the actual predicate, signer, and tag."""

    guide = RELEASE_GUIDE.read_text(encoding="utf-8")

    _assert_constrained_cyclonedx_verification(guide)


def test_security_guide_verifies_the_published_cyclonedx_attestation() -> None:
    """Security guidance must not direct operators to the wrong predicate."""

    guide = SECURITY_GUIDE.read_text(encoding="utf-8")

    _assert_constrained_cyclonedx_verification(guide)


def test_release_workflow_attests_the_json_sbom_described_by_the_guide() -> None:
    """Release CI must keep creating the CycloneDX SBOM attestation we verify."""

    workflow = RELEASE_WORKFLOW.read_text(encoding="utf-8")

    assert "uses: actions/attest@" in workflow
    assert "sbom-path: release/validibot.cdx.json" in workflow
    assert "subject-path: release/validibot-*.tar.gz" in workflow
