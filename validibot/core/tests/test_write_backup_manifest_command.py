"""Tests for the ``write_backup_manifest`` Django management command.

These tests cover the command's argument-parsing + manifest-assembly
logic — the parts that don't require a live Cloud SQL connection or
GCS access. They focus on:

1. **Required arguments** — missing required flags fail with a
   clear error rather than silently producing a partial manifest.
2. **Output to stdout** — default output (``--output -``) writes
   valid JSON to stdout the recipe can pipe.
3. **Output to a local file** — a file path argument writes the
   JSON manifest to disk and creates the parent directory if
   needed.
4. **Secret-manager-version parsing** — the repeatable
   ``--secret-manager-version NAME=VERSION`` flag round-trips
   into the config component, and malformed values fail loudly.
5. **Media inventory parsing** — a JSONL inventory file is parsed
   one entry per line and lands in the manifest's
   ``data.media_files``.
6. **Runtime version capture** — the manifest records the application
   version from the deployment-facing setting or package metadata; validator
   backend metadata never participates in backup compatibility.

What these tests do NOT cover:

- The ``_write_to_gcs`` path. That requires
  ``google.cloud.storage`` and authentication; the just recipe
  is the integration boundary that exercises it.
- The migration-head capture. ``MigrationRecorder`` requires a
  live DB connection that the test DB provides cleanly, so we
  test it as a smoke check rather than against synthetic state.
- Multi-app migration head ordering nuances. Those are
  ``MigrationRecorder``'s concern, not ours.
"""

from __future__ import annotations

import json
from io import StringIO
from typing import TYPE_CHECKING

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings

from validibot import __version__ as validibot_package_version

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.django_db

DB_SIZE_BYTES = 1024
EXPECTED_MEDIA_FILE_COUNT = 2
PYTHON_VERSION_DOT_COUNT = 2


# ──────────────────────────────────────────────────────────────────────
# Required arguments
# ──────────────────────────────────────────────────────────────────────


class TestRequiredArguments:
    """Missing required flags fail with a clear error."""

    @pytest.mark.parametrize(
        "missing_arg",
        [
            "--backup-id",
            "--target",
            "--backup-uri",
            "--db-file",
            "--db-sha256",
            "--db-size-bytes",
            "--restore-command-hint",
        ],
    )
    def test_missing_required_arg_raises(self, missing_arg):
        """The command must reject manifests that cannot support a safe restore."""
        all_args = {
            "--backup-id": "20260504T143022Z",
            "--target": "gcp",
            "--backup-uri": "gs://bucket/20260504T143022Z/",
            "--db-file": "db.sql.gz",
            "--db-sha256": "a" * 64,
            "--db-size-bytes": "1024",
            "--restore-command-hint": "just gcp restore prod gs://bucket/...",
        }
        # Drop the missing arg.
        argv = []
        for k, v in all_args.items():
            if k == missing_arg:
                continue
            argv.extend([k, v])

        # argparse raises SystemExit on missing required flags;
        # CommandError wraps that for management-command callers.
        with pytest.raises((CommandError, SystemExit)):
            call_command("write_backup_manifest", *argv, stdout=StringIO())


# ──────────────────────────────────────────────────────────────────────
# Stdout output (default)
# ──────────────────────────────────────────────────────────────────────


class TestStdoutOutput:
    """Default output (``--output -``) writes valid JSON to stdout."""

    def test_stdout_output_is_valid_json(self):
        """Pipelines consume stdout directly, so it must contain valid schema JSON."""
        out = StringIO()
        call_command(
            "write_backup_manifest",
            "--backup-id",
            "20260504T143022Z",
            "--target",
            "gcp",
            "--stage",
            "prod",
            "--backup-uri",
            "gs://bucket/20260504T143022Z/",
            "--db-file",
            "db.sql.gz",
            "--db-sha256",
            "a" * 64,
            "--db-size-bytes",
            "1024",
            "--restore-command-hint",
            "just gcp restore prod gs://bucket/20260504T143022Z/",
            stdout=out,
        )
        as_dict = json.loads(out.getvalue())
        assert as_dict["schema_version"] == "validibot.backup.v1"
        assert as_dict["backup_id"] == "20260504T143022Z"
        assert as_dict["target"] == "gcp"
        assert as_dict["stage"] == "prod"
        assert as_dict["data"]["db_dump"]["checksum_sha256"] == "a" * 64
        assert as_dict["data"]["db_dump"]["size_bytes"] == DB_SIZE_BYTES


# ──────────────────────────────────────────────────────────────────────
# DB dump content-type resolution
# ──────────────────────────────────────────────────────────────────────


class TestDbContentTypeResolution:
    """Content-type matches the dump format, not a hard-coded string.

    Earlier the writer always recorded ``application/gzip`` for the
    DB dump regardless of the actual format. Self-hosted backups
    produce ``db.sql.zst`` (zstd-compressed), so the manifest was
    misleading restore tooling. The fix: explicit
    ``--db-content-type`` flag, with extension-based inference as
    the safe fallback.
    """

    def _common_args(self, *, db_file: str = "db.sql.gz") -> list[str]:
        return [
            "--backup-id",
            "20260504T143022Z",
            "--target",
            "gcp",
            "--stage",
            "prod",
            "--backup-uri",
            "gs://bucket/20260504T143022Z/",
            "--db-file",
            db_file,
            "--db-sha256",
            "a" * 64,
            "--db-size-bytes",
            "1024",
            "--restore-command-hint",
            "...",
        ]

    def test_explicit_content_type_wins(self):
        """An operator-provided media type is more authoritative than a suffix."""
        out = StringIO()
        call_command(
            "write_backup_manifest",
            *self._common_args(db_file="db.sql.zst"),
            "--db-content-type",
            "application/zstd",
            stdout=out,
        )
        as_dict = json.loads(out.getvalue())
        assert as_dict["data"]["db_dump"]["content_type"] == "application/zstd"

    def test_zst_extension_inferred_as_zstd(self):
        """Self-hosted zstd dumps need an accurate type for restore tooling."""
        out = StringIO()
        call_command(
            "write_backup_manifest",
            *self._common_args(db_file="db.sql.zst"),
            stdout=out,
        )
        as_dict = json.loads(out.getvalue())
        assert as_dict["data"]["db_dump"]["content_type"] == "application/zstd"

    def test_gz_extension_inferred_as_gzip(self):
        """Gzip database dumps retain their conventional content type."""
        out = StringIO()
        call_command(
            "write_backup_manifest",
            *self._common_args(db_file="db.sql.gz"),
            stdout=out,
        )
        as_dict = json.loads(out.getvalue())
        assert as_dict["data"]["db_dump"]["content_type"] == "application/gzip"

    def test_unknown_extension_falls_back_to_octet_stream(self):
        """Honest fallback — better than silently mislabeling."""
        out = StringIO()
        call_command(
            "write_backup_manifest",
            *self._common_args(db_file="db.sql.weirdformat"),
            stdout=out,
        )
        as_dict = json.loads(out.getvalue())
        assert as_dict["data"]["db_dump"]["content_type"] == "application/octet-stream"


# ──────────────────────────────────────────────────────────────────────
# Runtime version capture
# ──────────────────────────────────────────────────────────────────────


class TestRuntimeVersionCapture:
    """Backup manifests record the app runtime version used by restore checks."""

    def _write_manifest_to_dict(self) -> dict:
        """Run the command with the smallest valid argument set."""
        out = StringIO()
        call_command(
            "write_backup_manifest",
            "--backup-id",
            "20260504T143022Z",
            "--target",
            "gcp",
            "--stage",
            "prod",
            "--backup-uri",
            "gs://bucket/20260504T143022Z/",
            "--db-file",
            "db.sql.gz",
            "--db-sha256",
            "a" * 64,
            "--db-size-bytes",
            "1024",
            "--restore-command-hint",
            "just gcp restore prod gs://bucket/20260504T143022Z/",
            stdout=out,
        )
        return json.loads(out.getvalue())

    @override_settings(VALIDIBOT_VERSION="1.2.3", VALIDATOR_BACKEND_VERSION="9.9.9")
    def test_prefers_validibot_version_over_validator_backend_metadata(self):
        """The app version is the restore contract; backend metadata is separate."""
        manifest = self._write_manifest_to_dict()

        assert manifest["compatibility"]["validibot_version"] == "1.2.3"

    @override_settings(VALIDIBOT_VERSION="", VALIDATOR_BACKEND_VERSION="9.9.9")
    def test_validator_backend_version_is_not_a_validibot_fallback(self):
        """Backend image metadata must not masquerade as app compatibility."""
        manifest = self._write_manifest_to_dict()

        assert (
            manifest["compatibility"]["validibot_version"] == validibot_package_version
        )


# ──────────────────────────────────────────────────────────────────────
# Local file output
# ──────────────────────────────────────────────────────────────────────


class TestFileOutput:
    """A local path writes the manifest to disk."""

    def test_writes_to_file(self, tmp_path: Path):
        """Self-hosted recipes depend on durable local manifest output."""
        out_path = tmp_path / "manifest.json"
        call_command(
            "write_backup_manifest",
            "--backup-id",
            "20260504T143022Z",
            "--target",
            "self_hosted",
            "--backup-uri",
            f"file://{tmp_path}/",
            "--db-file",
            "db.sql.gz",
            "--db-sha256",
            "a" * 64,
            "--db-size-bytes",
            "1024",
            "--restore-command-hint",
            f"just self-hosted restore {tmp_path}/",
            "--output",
            str(out_path),
            stdout=StringIO(),
        )
        assert out_path.exists()
        as_dict = json.loads(out_path.read_text(encoding="utf-8"))
        assert as_dict["target"] == "self_hosted"
        assert as_dict["stage"] is None


# ──────────────────────────────────────────────────────────────────────
# Secret manager version parsing
# ──────────────────────────────────────────────────────────────────────


class TestSecretManagerVersionParsing:
    """``--secret-manager-version NAME=VERSION`` parses into the config component."""

    def test_single_pair(self):
        """A single retained secret version must remain recoverable by name."""
        out = StringIO()
        call_command(
            "write_backup_manifest",
            "--backup-id",
            "20260504T143022Z",
            "--target",
            "gcp",
            "--stage",
            "prod",
            "--backup-uri",
            "gs://bucket/20260504T143022Z/",
            "--db-file",
            "db.sql.gz",
            "--db-sha256",
            "a" * 64,
            "--db-size-bytes",
            "1024",
            "--restore-command-hint",
            "...",
            "--secret-manager-version",
            "django-env=17",
            stdout=out,
        )
        as_dict = json.loads(out.getvalue())
        assert as_dict["config"] is not None
        assert as_dict["config"]["secret_manager_versions"] == {"django-env": "17"}

    def test_multiple_pairs(self):
        """Independent runtime secret versions must coexist without overwriting."""
        out = StringIO()
        call_command(
            "write_backup_manifest",
            "--backup-id",
            "20260504T143022Z",
            "--target",
            "gcp",
            "--stage",
            "prod",
            "--backup-uri",
            "gs://bucket/20260504T143022Z/",
            "--db-file",
            "db.sql.gz",
            "--db-sha256",
            "a" * 64,
            "--db-size-bytes",
            "1024",
            "--restore-command-hint",
            "...",
            "--secret-manager-version",
            "django-env=17",
            "--secret-manager-version",
            "worker-env=4",
            stdout=out,
        )
        as_dict = json.loads(out.getvalue())
        assert as_dict["config"]["secret_manager_versions"] == {
            "django-env": "17",
            "worker-env": "4",
        }

    def test_no_pairs_means_no_config_block(self):
        """Without --secret-manager-version, the config block is null.

        Cleaner than emitting an empty stub object — operators
        viewing the manifest see "no config tracked" rather than an
        empty placeholder that suggests config WAS captured.
        """
        out = StringIO()
        call_command(
            "write_backup_manifest",
            "--backup-id",
            "20260504T143022Z",
            "--target",
            "gcp",
            "--stage",
            "prod",
            "--backup-uri",
            "gs://bucket/20260504T143022Z/",
            "--db-file",
            "db.sql.gz",
            "--db-sha256",
            "a" * 64,
            "--db-size-bytes",
            "1024",
            "--restore-command-hint",
            "...",
            stdout=out,
        )
        as_dict = json.loads(out.getvalue())
        assert as_dict["config"] is None

    def test_malformed_pair_raises(self):
        """A pair without ``=`` is operator error; raise loudly."""
        with pytest.raises(CommandError, match="NAME=VERSION"):
            call_command(
                "write_backup_manifest",
                "--backup-id",
                "20260504T143022Z",
                "--target",
                "gcp",
                "--stage",
                "prod",
                "--backup-uri",
                "gs://bucket/20260504T143022Z/",
                "--db-file",
                "db.sql.gz",
                "--db-sha256",
                "a" * 64,
                "--db-size-bytes",
                "1024",
                "--restore-command-hint",
                "...",
                "--secret-manager-version",
                "no-equals-here",
                stdout=StringIO(),
            )

    def test_empty_side_raises(self):
        """A pair like ``=value`` or ``name=`` is operator error."""
        with pytest.raises(CommandError, match="empty side"):
            call_command(
                "write_backup_manifest",
                "--backup-id",
                "20260504T143022Z",
                "--target",
                "gcp",
                "--stage",
                "prod",
                "--backup-uri",
                "gs://bucket/20260504T143022Z/",
                "--db-file",
                "db.sql.gz",
                "--db-sha256",
                "a" * 64,
                "--db-size-bytes",
                "1024",
                "--restore-command-hint",
                "...",
                "--secret-manager-version",
                "django-env=",
                stdout=StringIO(),
            )


# ──────────────────────────────────────────────────────────────────────
# Media inventory parsing
# ──────────────────────────────────────────────────────────────────────


class TestMediaInventoryParsing:
    """JSONL inventory files parse one BackupFileEntry per line."""

    def test_parses_inventory_file(self, tmp_path: Path):
        """Every JSONL media entry must become a restorable manifest file row."""
        inventory = tmp_path / "media-inventory.jsonl"
        # Three entries, one per line, valid JSON each.  The blank
        # line at the end is intentional — the reader should ignore it.
        inventory.write_text(
            "\n".join(
                [
                    json.dumps(
                        {
                            "path": "media/foo.png",
                            "size_bytes": 2048,
                            "content_type": "image/png",
                            "checksum_md5": "abc==",
                            "checksum_crc32c": "12345678",
                        },
                    ),
                    json.dumps(
                        {
                            "path": "media/bar.txt",
                            "size_bytes": 100,
                            "content_type": "text/plain",
                        },
                    ),
                    "",
                ],
            ),
            encoding="utf-8",
        )

        out = StringIO()
        call_command(
            "write_backup_manifest",
            "--backup-id",
            "20260504T143022Z",
            "--target",
            "gcp",
            "--stage",
            "prod",
            "--backup-uri",
            "gs://bucket/20260504T143022Z/",
            "--db-file",
            "db.sql.gz",
            "--db-sha256",
            "a" * 64,
            "--db-size-bytes",
            "1024",
            "--restore-command-hint",
            "...",
            "--media-inventory",
            str(inventory),
            stdout=out,
        )
        as_dict = json.loads(out.getvalue())
        media = as_dict["data"]["media_files"]
        assert len(media) == EXPECTED_MEDIA_FILE_COUNT
        assert media[0]["path"] == "media/foo.png"
        assert media[0]["checksum_md5"] == "abc=="
        assert media[1]["path"] == "media/bar.txt"
        assert media[1]["content_type"] == "text/plain"

    def test_missing_inventory_file_raises(self, tmp_path: Path):
        """A non-existent inventory path fails fast — we never silently
        produce an empty media list when the operator asked for one."""
        with pytest.raises(FileNotFoundError):
            call_command(
                "write_backup_manifest",
                "--backup-id",
                "20260504T143022Z",
                "--target",
                "gcp",
                "--stage",
                "prod",
                "--backup-uri",
                "gs://bucket/20260504T143022Z/",
                "--db-file",
                "db.sql.gz",
                "--db-sha256",
                "a" * 64,
                "--db-size-bytes",
                "1024",
                "--restore-command-hint",
                "...",
                "--media-inventory",
                str(tmp_path / "missing.jsonl"),
                stdout=StringIO(),
            )

    def test_inventory_from_stdin(self, monkeypatch):
        """``--media-inventory -`` reads JSONL from stdin.

        The self-hosted backup recipe pipes a one-line inventory in
        rather than copying a temp file into the web container — that
        works because both the writer and the verifier accept ``-``
        as a synonym for stdin. This test pins that contract.
        """
        single_entry = json.dumps(
            {
                "path": "data.tar.zst",
                "size_bytes": 4096,
                "content_type": "application/zstd",
                "checksum_sha256": "f" * 64,
            },
        )
        monkeypatch.setattr("sys.stdin", StringIO(single_entry + "\n"))

        out = StringIO()
        call_command(
            "write_backup_manifest",
            "--backup-id",
            "20260504T143022Z",
            "--target",
            "self_hosted",
            "--backup-uri",
            "file:///srv/validibot/backups/20260504T143022Z/",
            "--db-file",
            "db.sql.zst",
            "--db-sha256",
            "a" * 64,
            "--db-size-bytes",
            "2048",
            "--restore-command-hint",
            "just self-hosted restore backups/20260504T143022Z",
            "--media-inventory",
            "-",
            stdout=out,
        )
        as_dict = json.loads(out.getvalue())
        media = as_dict["data"]["media_files"]
        assert len(media) == 1
        assert media[0]["path"] == "data.tar.zst"
        assert media[0]["content_type"] == "application/zstd"
        assert media[0]["checksum_sha256"] == "f" * 64


# ──────────────────────────────────────────────────────────────────────
# Compatibility capture (smoke test)
# ──────────────────────────────────────────────────────────────────────


class TestCompatibilityCapture:
    """Smoke test: compatibility block populates from live Django state.

    We don't pin specific migration names because those drift as
    the codebase evolves; instead we assert structural properties
    a regression in the capture logic would break.
    """

    def test_compatibility_block_populated(self):
        """A backup without runtime compatibility evidence is unsafe to restore."""
        out = StringIO()
        call_command(
            "write_backup_manifest",
            "--backup-id",
            "20260504T143022Z",
            "--target",
            "gcp",
            "--stage",
            "prod",
            "--backup-uri",
            "gs://bucket/20260504T143022Z/",
            "--db-file",
            "db.sql.gz",
            "--db-sha256",
            "a" * 64,
            "--db-size-bytes",
            "1024",
            "--restore-command-hint",
            "...",
            stdout=out,
        )
        as_dict = json.loads(out.getvalue())
        compat = as_dict["compatibility"]

        # Validibot version is read from settings; falls back to "unknown".
        assert "validibot_version" in compat
        # Python version is always something like "3.X.Y".
        assert compat["python_version"].count(".") == PYTHON_VERSION_DOT_COUNT
        # Postgres version is the live ``server_version`` string.
        assert compat["postgres_server_version"]
        assert compat["postgres_server_version"] != "unknown"
        # Migration head is a dict; in a working test DB it should
        # have at least one entry (one of the apps has run migrations).
        assert isinstance(compat["migration_head"], dict)
        assert len(compat["migration_head"]) > 0
