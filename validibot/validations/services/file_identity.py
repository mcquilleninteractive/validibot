"""Provider-neutral immutable identity for validator file contracts.

The trusted application commits these four values before dispatch. Storage
adapters decide how ``storage_version`` is obtained, while envelope builders
only consume this value object and therefore cannot accidentally fall back to
a bare mutable URI.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import TYPE_CHECKING
from typing import Any
from typing import TypedDict

if TYPE_CHECKING:
    from pathlib import Path

FILE_IDENTITY_CHUNK_SIZE = 1024 * 1024
LOCAL_STORAGE_VERSION_PREFIX = "sha256:"


class FileIdentityEnvelopeFields(TypedDict):
    """Keyword fields shared by strict input and resource envelope items."""

    uri: str
    size_bytes: int
    sha256: str
    storage_version: str


@dataclass(frozen=True, slots=True)
class FileIdentity:
    """Exact bytes and provider version committed to one storage URI."""

    uri: str
    size_bytes: int
    sha256: str
    storage_version: str

    @classmethod
    def from_envelope_item(cls, item: Any) -> FileIdentity:
        """Copy identity fields from a strict shared envelope item."""
        return cls(
            uri=str(item.uri),
            size_bytes=int(item.size_bytes),
            sha256=str(item.sha256),
            storage_version=str(item.storage_version),
        )

    @classmethod
    def from_artifact_ref(cls, artifact_ref: dict[str, Any]) -> FileIdentity:
        """Build identity from a validated cross-step artifact reference."""
        return cls(
            uri=str(artifact_ref.get("uri") or ""),
            size_bytes=int(artifact_ref.get("size_bytes", -1)),
            sha256=str(artifact_ref.get("sha256") or ""),
            storage_version=str(artifact_ref.get("storage_version") or ""),
        )

    def envelope_fields(self) -> FileIdentityEnvelopeFields:
        """Return the shared fields used by every file-bearing item."""
        return {
            "uri": self.uri,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "storage_version": self.storage_version,
        }


def local_file_identity(*, path: Path, uri: str) -> FileIdentity:
    """Hash a local file once and bind its version to the resulting digest."""
    digest = hashlib.sha256()
    size_bytes = 0
    with path.open("rb") as source:
        while chunk := source.read(FILE_IDENTITY_CHUNK_SIZE):
            size_bytes += len(chunk)
            digest.update(chunk)
    sha256 = digest.hexdigest()
    return FileIdentity(
        uri=uri,
        size_bytes=size_bytes,
        sha256=sha256,
        storage_version=f"{LOCAL_STORAGE_VERSION_PREFIX}{sha256}",
    )


def local_bytes_identity(*, content: bytes, uri: str) -> FileIdentity:
    """Return the content-addressed identity for bytes written locally."""
    sha256 = hashlib.sha256(content).hexdigest()
    return FileIdentity(
        uri=uri,
        size_bytes=len(content),
        sha256=sha256,
        storage_version=f"{LOCAL_STORAGE_VERSION_PREFIX}{sha256}",
    )


__all__ = [
    "FILE_IDENTITY_CHUNK_SIZE",
    "LOCAL_STORAGE_VERSION_PREFIX",
    "FileIdentity",
    "FileIdentityEnvelopeFields",
    "local_bytes_identity",
    "local_file_identity",
]
