# ruff: noqa: INP001
"""Sync the app's composed-workflow PDF from the backend's reviewed corpus.

This is an explicit maintainer command, never a test-suite side effect. Keeping
the copy operation in reviewed source makes the cross-repository provenance and
expected digest visible instead of relying on an undocumented manual copy.
"""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

EXPECTED_SOURCE_SHA256 = (
    "661418435c058cb35f5b1166242087a62287b8b76d8b9c7e6b5cdc09609b6a1e"
)
DESTINATION = Path(__file__).resolve().parent / "composed-package.pdf"
SOURCE = (
    Path(__file__).resolve().parents[4]
    / "validibot-validator-backends"
    / "validator_backends"
    / "pdf"
    / "tests"
    / "fixtures"
    / "golden"
    / "static-text-package.pdf"
)


def main() -> None:
    """Verify the reviewed backend fixture identity, then copy its exact bytes."""
    source_bytes = SOURCE.read_bytes()
    source_sha256 = hashlib.sha256(source_bytes).hexdigest()
    if source_sha256 != EXPECTED_SOURCE_SHA256:
        raise RuntimeError(
            "Backend static-text fixture digest changed; review both repositories "
            "before updating EXPECTED_SOURCE_SHA256."
        )
    shutil.copyfile(SOURCE, DESTINATION)


if __name__ == "__main__":
    main()
