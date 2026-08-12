"""Tests for the ``BackupManifest`` Pydantic schema.

The manifest is the trust root for every operator backup — it
records what version produced the backup, what migrations were
applied, and what files are present with their checksums. The
restore path consumes this directly. These tests pin the schema's
contract:

1. **Required vs optional fields** — required fields fail
   construction without a value; optional fields default cleanly.
2. **Cross-target round-trip** — a manifest built for ``gcp``
   serializes and deserializes identically; same for ``self_hosted``.
3. **Schema-version literal** — ``schema_version`` is a Literal
   pinned to ``v1``; consumers reject unknown values.
4. **Checksum field independence** — sha256 (writer-computed) and
   md5 / crc32c (GCS-supplied) are independent fields, so a media
   file can have md5+crc32c without sha256, and a DB dump can have
   sha256 without md5+crc32c.
5. **Frozen + ``extra='forbid'``** — accidental field additions or
   mutations raise rather than silently propagating.

Why these tests live alongside the schema rather than alongside the
``write_backup_manifest`` command:

The schema is the *contract*; the command is one *producer*.
Future restore code, future audit code, and future cross-tool
backups will all depend on this schema. Keeping the schema tests
focused on shape — independent of any specific producer — means a
regression in the contract surfaces here, not buried in a
producer's integration test.
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from validibot.core.backup_manifest import BACKUP_MANIFEST_SCHEMA_VERSION
from validibot.core.backup_manifest import BackupCompatibility
from validibot.core.backup_manifest import BackupConfigComponent
from validibot.core.backup_manifest import BackupDataComponent
from validibot.core.backup_manifest import BackupFileEntry
from validibot.core.backup_manifest import BackupManifest

# ──────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────


def _minimal_compatibility(**overrides) -> BackupCompatibility:
    """A minimal valid ``BackupCompatibility`` for tests that don't override it."""
    defaults = {
        "validibot_version": "abc1234",
        "python_version": "3.13.1",
        "postgres_server_version": "16.0",
        "migration_head": {"workflows": "0018_add_workflow_publish_invariants"},
    }
    defaults.update(overrides)
    return BackupCompatibility(**defaults)


def _minimal_data(**overrides) -> BackupDataComponent:
    """A minimal valid ``BackupDataComponent`` with one DB dump and no media."""
    defaults = {
        "db_dump": BackupFileEntry(
            path="db.sql.gz",
            size_bytes=1024,
            content_type="application/gzip",
            checksum_sha256="a" * 64,
        ),
        "media_files": [],
    }
    defaults.update(overrides)
    return BackupDataComponent(**defaults)


def _minimal_manifest_kwargs(**overrides):
    """Kwargs for a minimal valid ``BackupManifest``."""
    defaults = {
        "backup_id": "20260504T143022Z",
        "created_at": "2026-05-04T14:30:22Z",
        "target": "gcp",
        "stage": "prod",
        "backup_uri": "gs://bucket/20260504T143022Z/",
        "compatibility": _minimal_compatibility(),
        "data": _minimal_data(),
        "restore_command_hint": "just gcp restore prod gs://bucket/20260504T143022Z/",
    }
    defaults.update(overrides)
    return defaults


# ──────────────────────────────────────────────────────────────────────
# Schema-version contract
# ──────────────────────────────────────────────────────────────────────


class TestSchemaVersion:
    """The schema version is pinned and rejects unknown values."""

    def test_default_schema_version_is_v1(self):
        """Restore tooling depends on newly created manifests using the v1 contract."""
        manifest = BackupManifest(**_minimal_manifest_kwargs())
        assert manifest.schema_version == "validibot.backup.v1"
        assert manifest.schema_version == BACKUP_MANIFEST_SCHEMA_VERSION

    def test_rejects_other_schema_versions(self):
        """Future-proofing: a v2 schema deserialized with this code fails fast."""
        with pytest.raises(ValidationError):
            BackupManifest(
                **_minimal_manifest_kwargs(),
                schema_version="validibot.backup.v2",
            )


# ──────────────────────────────────────────────────────────────────────
# Required and optional fields
# ──────────────────────────────────────────────────────────────────────


class TestRequiredFields:
    """Required fields must be supplied; their absence raises ValidationError."""

    @pytest.mark.parametrize(
        "missing_field",
        [
            "backup_id",
            "created_at",
            "target",
            "backup_uri",
            "compatibility",
            "data",
            "restore_command_hint",
        ],
    )
    def test_missing_required_field_raises(self, missing_field):
        """An incomplete manifest must fail before an operator attempts a restore."""
        kwargs = _minimal_manifest_kwargs()
        del kwargs[missing_field]
        with pytest.raises(ValidationError):
            BackupManifest(**kwargs)


class TestOptionalFields:
    """Optional fields default cleanly when omitted."""

    def test_stage_optional_for_self_hosted(self):
        """Self-hosted backups have no ``stage``; the field is None."""
        manifest = BackupManifest(
            **{
                **_minimal_manifest_kwargs(),
                "target": "self_hosted",
                "stage": None,
            },
        )
        assert manifest.stage is None

    def test_config_optional(self):
        """Config component absent — manifest serializes cleanly without it."""
        manifest = BackupManifest(**_minimal_manifest_kwargs())
        assert manifest.config is None

    def test_config_when_present(self):
        """Config component populated — secret-version map is preserved."""
        config = BackupConfigComponent(
            secret_manager_versions={"django-env": "17", "worker-env": "4"},
        )
        manifest = BackupManifest(
            **_minimal_manifest_kwargs(),
            config=config,
        )
        assert manifest.config is not None
        assert manifest.config.secret_manager_versions["django-env"] == "17"
        assert manifest.config.secret_manager_versions["worker-env"] == "4"


# ──────────────────────────────────────────────────────────────────────
# Cross-target round-trip
# ──────────────────────────────────────────────────────────────────────


class TestRoundTrip:
    """Manifests survive serialization → deserialization unchanged.

    Round-trip is the property restore tooling depends on:
    ``BackupManifest.model_validate_json(written_json) == original``.
    """

    def test_gcp_round_trip(self):
        """GCP manifests must survive storage and parsing without semantic drift."""
        original = BackupManifest(**_minimal_manifest_kwargs())
        as_json = original.model_dump_json()
        reparsed = BackupManifest.model_validate_json(as_json)
        assert reparsed == original

    def test_self_hosted_round_trip(self):
        """Self-hosted manifests preserve their target-specific nullable stage."""
        original = BackupManifest(
            **{
                **_minimal_manifest_kwargs(),
                "target": "self_hosted",
                "stage": None,
                "backup_uri": "file:///var/backups/validibot/20260504T143022Z/",
                "restore_command_hint": (
                    "just self-hosted restore /var/backups/validibot/20260504T143022Z/"
                ),
            },
        )
        as_json = original.model_dump_json()
        reparsed = BackupManifest.model_validate_json(as_json)
        assert reparsed == original
        assert reparsed.target == "self_hosted"
        assert reparsed.stage is None


# ──────────────────────────────────────────────────────────────────────
# Checksum field independence
# ──────────────────────────────────────────────────────────────────────


class TestChecksumFields:
    """Checksum fields are independent — any combination is legal."""

    def test_db_dump_uses_sha256_only(self):
        """The DB dump entry typically carries sha256 + size, no md5/crc32c.

        The writer computes sha256 by streaming the dump through
        ``sha256sum``; GCS-native md5/crc32c are not used for the
        DB dump because we want our own checksum chain of custody.
        """
        entry = BackupFileEntry(
            path="db.sql.gz",
            size_bytes=1024,
            content_type="application/gzip",
            checksum_sha256="a" * 64,
        )
        assert entry.checksum_sha256 == "a" * 64
        assert entry.checksum_md5 is None
        assert entry.checksum_crc32c is None

    def test_media_file_uses_gcs_native_checksums(self):
        """Media file entries typically carry md5 + crc32c, no sha256.

        GCS supplies md5/crc32c for free as object metadata; making
        the manifest writer recompute sha256 across thousands of
        media files would be expensive and pointless when GCS
        already vouches for integrity via crc32c.
        """
        entry = BackupFileEntry(
            path="media/uploads/foo.png",
            size_bytes=2048,
            content_type="image/png",
            checksum_md5="bbbbbbbbbbbbbbbbbbbbbb==",
            checksum_crc32c="cccccccc",
        )
        assert entry.checksum_sha256 is None
        assert entry.checksum_md5 == "bbbbbbbbbbbbbbbbbbbbbb=="
        assert entry.checksum_crc32c == "cccccccc"

    def test_no_checksums_legal(self):
        """A file entry with no checksums is legal (e.g., self-hosted media).

        Self-hosted backups produced by ``find`` + ``tar`` may not
        have computed checksums per file; the manifest still
        records size + path so restore can verify presence and
        sequence even if it can't verify integrity per-file.
        """
        entry = BackupFileEntry(path="something.txt", size_bytes=42)
        assert entry.checksum_sha256 is None
        assert entry.checksum_md5 is None
        assert entry.checksum_crc32c is None


# ──────────────────────────────────────────────────────────────────────
# Frozen + extra='forbid' contract
# ──────────────────────────────────────────────────────────────────────


class TestStrictShape:
    """Manifests are frozen and reject unknown fields."""

    def test_unknown_top_level_field_rejected(self):
        """Rejecting unknown fields prevents silent writer/reader contract drift."""
        with pytest.raises(ValidationError):
            BackupManifest(
                **_minimal_manifest_kwargs(),
                surprise_field="this should not be accepted",
            )

    def test_manifest_is_frozen(self):
        """``frozen=True`` means re-assignment fails — manifests are
        immutable post-construction so callers can hash / cache them.
        """
        manifest = BackupManifest(**_minimal_manifest_kwargs())
        with pytest.raises(ValidationError):
            manifest.backup_id = "tampered"  # type: ignore[misc]


# ──────────────────────────────────────────────────────────────────────
# Migration head
# ──────────────────────────────────────────────────────────────────────


class TestMigrationHead:
    """Migration head captures Django's view of the live database."""

    def test_multi_app_migration_head(self):
        """Multiple apps each contribute their highest migration."""
        compat = _minimal_compatibility(
            migration_head={
                "workflows": "0018_add_workflow_publish_invariants",
                "validations": "0047_alter_validationrun_source_choices",
                "users": "0003_wipe_pre_encryption_authenticators",
            },
        )
        expected_app_count = 3
        assert len(compat.migration_head) == expected_app_count
        assert (
            compat.migration_head["workflows"] == "0018_add_workflow_publish_invariants"
        )

    def test_empty_migration_head_legal(self):
        """A fresh deployment with no migrations applied yet is legal.

        Edge case but worth pinning: backups can be taken at any
        point, including immediately after a fresh install before
        the first migrate has run. Restore would be a no-op at that
        point but the manifest format must still accept it.
        """
        compat = _minimal_compatibility(migration_head={})
        assert compat.migration_head == {}


# ──────────────────────────────────────────────────────────────────────
# JSON shape stability (operator-readable output)
# ──────────────────────────────────────────────────────────────────────


class TestJsonShape:
    """The serialized manifest's keys + nesting are operator-readable.

    Operators read ``manifest.json`` with ``cat`` and ``jq``.  This
    test pins that the field names match what the CLI helpers
    expect (e.g. ``jq .compatibility.migration_head``).  A rename
    here breaks any tooling operators have built around the
    manifest, so the regression must surface loudly.
    """

    def test_top_level_keys(self):
        """Operator scripts rely on a stable, documented top-level JSON shape."""
        manifest = BackupManifest(**_minimal_manifest_kwargs())
        as_dict = json.loads(manifest.model_dump_json())
        assert set(as_dict.keys()) == {
            "schema_version",
            "backup_id",
            "created_at",
            "target",
            "stage",
            "backup_uri",
            "compatibility",
            "data",
            "config",
            "restore_command_hint",
        }

    def test_data_component_shape(self):
        """Restore tooling needs stable database and media entry nesting."""
        # ``_minimal_manifest_kwargs`` already populates ``data`` with a
        # one-file default; override it here by mutating the dict so we
        # don't pass duplicate kwargs to the constructor.
        kwargs = _minimal_manifest_kwargs()
        kwargs["data"] = BackupDataComponent(
            db_dump=BackupFileEntry(
                path="db.sql.gz",
                size_bytes=1024,
                checksum_sha256="a" * 64,
            ),
            media_files=[
                BackupFileEntry(
                    path="media/foo.png",
                    size_bytes=2048,
                    content_type="image/png",
                    checksum_md5="abc==",
                ),
            ],
        )
        manifest = BackupManifest(**kwargs)
        as_dict = json.loads(manifest.model_dump_json())
        assert "db_dump" in as_dict["data"]
        assert "media_files" in as_dict["data"]
        assert as_dict["data"]["db_dump"]["path"] == "db.sql.gz"
        assert as_dict["data"]["media_files"][0]["path"] == "media/foo.png"
