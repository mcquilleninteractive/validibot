"""Backup manifest — cross-target schema for operator-facing backups.

Operator-facing backups are produced by ``just self-hosted backup`` and
``just gcp backup <stage>``. Every backup directory contains a
``manifest.json`` file conforming to the schema in this module. The
manifest is the trust root for the backup: it records what version of
Validibot produced the backup, which migrations were applied, what
files are present, and (for the critical DB dump) a sha256 checksum
the restore path verifies before importing.

What the manifest commits to
============================

1. **Identity** — ``backup_id`` (timestamp-based), ``created_at``,
   ``target`` (``gcp`` / ``self_hosted``), ``stage`` (for GCP).
2. **Compatibility** — Validibot version, Python version, Postgres
   server version, full migration head (``app_label`` →
   ``last_migration_name``). The restore path refuses to import a
   backup whose migration head is ahead of current code; that
   prevents data corruption when a backup taken on v0.6 is restored
   onto code that has rolled back to v0.5.
3. **Components** — ``data`` (DB dump + media inventory) is always
   present; ``config`` is optional and records secret-manager
   versions / env-file references but never the secret values.
4. **File inventory** — every artifact with size, content-type, and
   a checksum. The DB dump's checksum is sha256 (computed by the
   writer); media files use GCS-native MD5 / CRC32C where available
   to avoid streaming the whole bucket through the writer.

What the manifest does NOT commit to
====================================

- **Secret values.** The ``config`` component records *where* secrets
  live (Secret Manager resource name + version) and what env-file
  paths existed, but never the secret bytes. Restoring config means
  re-pinning Secret Manager versions, which an operator does
  explicitly — the backup tooling will not automate it because the
  threat model differs from data restore.
- **Validator backend container images.** Those are pinned by digest
  in deployment config and tracked separately by the Phase 5 trust
  work; they are not part of an operator backup.

Schema versioning
=================

``BACKUP_MANIFEST_SCHEMA_VERSION`` is the contract string. Additive
fields (new optional fields with defaults) preserve the version;
removing or renaming fields requires a v2. Self-hosted and GCP
producers share this single schema, so cross-target restore tooling
can be written without per-target branches.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field

# Bump only on breaking changes; additive changes stay v1.
BACKUP_MANIFEST_SCHEMA_VERSION = "validibot.backup.v1"


class BackupFileEntry(BaseModel):
    """One file or directory within the backup.

    Used for both the DB dump (a single file) and media-bucket
    inventory entries. ``checksum_sha256`` is populated for files the
    backup writer reads end-to-end; ``checksum_md5`` and
    ``checksum_crc32c`` come from GCS object metadata when available.
    Restore path verifies whichever fields are present.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str = Field(
        description=(
            "Path relative to the backup root. For GCP, the root is the "
            "GCS object prefix; for self-hosted, the backup directory."
        ),
    )
    size_bytes: int = Field(
        description="File size in bytes; useful for restore-time progress reporting.",
    )
    content_type: str | None = Field(
        default=None,
        description=(
            "MIME type as recorded by the storage layer. Defaults to None "
            "for self-hosted backups where the local filesystem doesn't "
            "track content types."
        ),
    )
    checksum_sha256: str | None = Field(
        default=None,
        description=(
            "SHA-256 hex digest. Populated by the writer for the DB dump "
            "and any other end-to-end-read files. None for media files "
            "that rely on GCS-native checksums (md5 / crc32c)."
        ),
    )
    checksum_md5: str | None = Field(
        default=None,
        description=(
            "MD5 hex digest from GCS object metadata. None for self-hosted backups."
        ),
    )
    checksum_crc32c: str | None = Field(
        default=None,
        description=(
            "CRC32C hash from GCS object metadata (Google's preferred "
            "integrity check). None for self-hosted backups."
        ),
    )


class BackupDataComponent(BaseModel):
    """The ``data`` half of a backup — DB dump + media inventory.

    Always present. The restore path's ``--components data-only``
    flag instructs it to apply only this section.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    db_dump: BackupFileEntry = Field(
        description=(
            "The Postgres dump file. Always sha256-checksummed by the "
            "writer because it's small enough (typically <1 GB) to read "
            "end-to-end and large enough that integrity matters."
        ),
    )
    media_files: list[BackupFileEntry] = Field(
        default_factory=list,
        description=(
            "Inventory of every media file in the backup. Empty list is "
            "legal for a fresh deployment with no media yet."
        ),
    )


class BackupConfigComponent(BaseModel):
    """The ``config`` half of a backup — references to secrets, never values.

    Recorded so a restore operator can re-create the deployment with
    the same secret versions that were active at backup time. The
    fields here are *pointers* (Secret Manager resource names,
    env-file paths) — the secret bytes are never copied.

    Optional in the manifest because some backup workflows
    (developer-machine self-hosted snapshots) don't have a meaningful
    config layer.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    secret_manager_versions: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Map of Secret Manager resource name to the version that was "
            "active at backup time, e.g. "
            "``{'django-env': '17', 'worker-env': '4'}``. Empty for "
            "self-hosted backups."
        ),
    )
    env_file_inventory: list[BackupFileEntry] = Field(
        default_factory=list,
        description=(
            "Inventory of env-file paths that exist at backup time, with "
            "sizes (NOT contents). Useful for self-hosted backups where "
            "config files live on local disk."
        ),
    )


class BackupCompatibility(BaseModel):
    """Version + migration state that restore must verify.

    The restore path refuses an import when:

    - ``migration_head`` references a migration the current code
      doesn't know about (backup is ahead of code → would fail to
      import; tells operator to upgrade first), OR
    - The current ``Validibot`` version is more than one major
      version behind the backup's, per AC #16 of the boring
      self-hosting ADR.

    Less strict than full version-pinning so that hotfix releases and
    minor migrations don't gratuitously block restore.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    validibot_version: str = Field(
        description=(
            "Version string that produced the backup. Read from "
            "``VALIDIBOT_VERSION`` when set, then package metadata."
        ),
    )
    python_version: str = Field(
        description="Major.minor.patch of the Python interpreter (e.g. ``3.13.1``).",
    )
    postgres_server_version: str = Field(
        description="``server_version`` reported by the Postgres connection.",
    )
    migration_head: dict[str, str] = Field(
        description=(
            "Map of Django ``app_label`` → name of the most recent "
            "migration applied. Used by restore to refuse imports whose "
            "head is ahead of current code."
        ),
    )


class BackupManifest(BaseModel):
    """Top-level operator-facing backup manifest.

    Single producer per backup directory; restore reads it before
    touching any other artifact. Lives at ``manifest.json`` next to
    ``db.sql.gz`` and the media inventory.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["validibot.backup.v1"] = Field(
        default=BACKUP_MANIFEST_SCHEMA_VERSION,
        description="Pinned schema version. Restore rejects unknown values.",
    )
    backup_id: str = Field(
        description=(
            "Backup identifier, conventionally a UTC timestamp like "
            "``20260504T143022Z``. Operators paste this into the restore "
            "command."
        ),
    )
    created_at: str = Field(
        description=(
            "ISO 8601 UTC timestamp when the backup writer started. "
            "Independent of ``backup_id`` because the writer takes some "
            "amount of time after the ID is chosen."
        ),
    )
    target: Literal["gcp", "self_hosted"] = Field(
        description="Deployment target that produced the backup.",
    )
    stage: str | None = Field(
        default=None,
        description=(
            "GCP stage (``dev`` / ``staging`` / ``prod``). ``None`` for "
            "self-hosted backups, where stage is not a meaningful concept."
        ),
    )
    backup_uri: str = Field(
        description=(
            "URI of the backup root: ``gs://...`` for GCP, "
            "``file:///...`` for self-hosted. Populated by the writer so "
            "an operator viewing the manifest in isolation can find the "
            "rest of the artifacts."
        ),
    )

    compatibility: BackupCompatibility
    data: BackupDataComponent
    config: BackupConfigComponent | None = Field(
        default=None,
        description=(
            "Config component when present, ``None`` otherwise. Optional "
            "so simple backups can omit the section without polluting "
            "the schema with empty lists."
        ),
    )

    restore_command_hint: str = Field(
        description=(
            "Exact command an operator runs to restore this backup, "
            "e.g. ``just gcp restore prod gs://bucket/20260504T143022Z/``. "
            "Stored so the manifest is self-documenting."
        ),
    )


__all__ = [
    "BACKUP_MANIFEST_SCHEMA_VERSION",
    "BackupCompatibility",
    "BackupConfigComponent",
    "BackupDataComponent",
    "BackupFileEntry",
    "BackupManifest",
]
