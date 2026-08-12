"""Write a ``BackupManifest`` for an operator backup.

Invoked by ``just gcp backup <stage>`` (and eventually ``just
self-hosted backup``) after the data artifacts have been produced.
The command's job is to:

1. Query Django for the current migration head.
2. Read Validibot version + Python version + Postgres server version.
3. Walk the artifact files (a DB dump in GCS or on local disk, plus
   the media inventory) and gather their metadata.
4. Serialize the resulting ``BackupManifest`` as JSON to a target
   location (stdout, a local file, or a GCS object).

This command does NOT itself produce the data artifacts — that's
delegated to the just recipe so operators can use familiar gcloud /
docker commands. The command is the single place that knows how to
*describe* a backup, which keeps cross-target backup writers
consistent.

Designed to run in the deployed environment (Cloud Run Job for GCP,
local Django process for self-hosted) so the migration head and
Postgres version reflect what's actually live, not what the
operator's laptop has cached.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC
from datetime import datetime
from pathlib import Path

from django.core.management.base import BaseCommand
from django.core.management.base import CommandError
from django.db import connection
from django.db.migrations.recorder import MigrationRecorder

from validibot.core.backup_manifest import BACKUP_MANIFEST_SCHEMA_VERSION
from validibot.core.backup_manifest import BackupCompatibility
from validibot.core.backup_manifest import BackupConfigComponent
from validibot.core.backup_manifest import BackupDataComponent
from validibot.core.backup_manifest import BackupFileEntry
from validibot.core.backup_manifest import BackupManifest
from validibot.core.deployment import get_validibot_runtime_version


class Command(BaseCommand):
    """Write a ``BackupManifest`` describing an operator backup.

    Usage::

        python manage.py write_backup_manifest \\
            --backup-id 20260504T143022Z \\
            --target gcp \\
            --stage prod \\
            --backup-uri gs://my-bucket/backups/20260504T143022Z/ \\
            --db-file gs://my-bucket/backups/20260504T143022Z/db.sql.gz \\
            --db-sha256 abc123... \\
            --db-size-bytes 524288000 \\
            --output gs://my-bucket/backups/20260504T143022Z/manifest.json

    The ``--db-*`` flags are passed in by the just recipe rather than
    re-computed here because the recipe just produced the dump and
    already has the size + checksum on hand. Re-streaming a multi-GB
    dump through this command would needlessly slow backups.

    Media files are NOT enumerated by this command — for GCP backups
    that's a separate step the recipe performs (gsutil rsync logs
    the file list). The recipe passes the resulting inventory file
    via ``--media-inventory <path-to-jsonl>``.
    """

    help = "Write a BackupManifest JSON for an operator backup."

    def add_arguments(self, parser):
        parser.add_argument(
            "--backup-id",
            required=True,
            help="Backup identifier (conventionally a UTC timestamp).",
        )
        parser.add_argument(
            "--target",
            required=True,
            choices=["gcp", "self_hosted"],
            help="Deployment target that produced the backup.",
        )
        parser.add_argument(
            "--stage",
            default=None,
            help="GCP stage (dev/staging/prod). Omit for self-hosted.",
        )
        parser.add_argument(
            "--backup-uri",
            required=True,
            help="Root URI of the backup (gs:// or file:///).",
        )
        parser.add_argument(
            "--db-file",
            required=True,
            help="Path/URI of the DB dump file relative to the backup root.",
        )
        parser.add_argument(
            "--db-sha256",
            required=True,
            help="SHA-256 hex digest of the DB dump file.",
        )
        parser.add_argument(
            "--db-size-bytes",
            type=int,
            required=True,
            help="Size of the DB dump file in bytes.",
        )
        parser.add_argument(
            "--db-content-type",
            default=None,
            help=(
                "MIME type for the DB dump (e.g. application/zstd for "
                "self-hosted, application/gzip for GCP). If omitted, "
                "inferred from the --db-file extension."
            ),
        )
        parser.add_argument(
            "--media-inventory",
            default=None,
            help=(
                "Optional path to a JSONL file with one BackupFileEntry "
                "per line (typically produced by the just recipe from "
                "gsutil rsync output for GCP, or as a single-archive "
                "entry for self-hosted). Accepts ``-`` for stdin so the "
                "self-hosted recipe can pipe a one-line inventory in "
                "without copying a file into the container."
            ),
        )
        parser.add_argument(
            "--secret-manager-version",
            action="append",
            default=[],
            metavar="NAME=VERSION",
            help=(
                "Repeatable. Records a Secret Manager resource and the "
                "version active at backup time. Pass ``--secret-manager-"
                "version django-env=17 --secret-manager-version worker-env=4``."
            ),
        )
        parser.add_argument(
            "--restore-command-hint",
            required=True,
            help=(
                "Exact command an operator runs to restore this backup. "
                "Stored verbatim in the manifest."
            ),
        )
        parser.add_argument(
            "--output",
            default="-",
            help=(
                "Where to write the manifest. ``-`` (default) writes to "
                "stdout; a local path writes to disk; a ``gs://`` URI "
                "writes to GCS via the storages backend."
            ),
        )

    def handle(self, *args, **options):
        manifest = self._build_manifest(options)
        manifest_json = manifest.model_dump_json(indent=2)

        output = options["output"]
        if output == "-":
            self.stdout.write(manifest_json)
        elif output.startswith("gs://"):
            self._write_to_gcs(uri=output, payload=manifest_json)
            self.stdout.write(self.style.SUCCESS(f"Manifest written to {output}"))
        else:
            Path(output).write_text(manifest_json + "\n", encoding="utf-8")
            self.stdout.write(self.style.SUCCESS(f"Manifest written to {output}"))

    def _build_manifest(self, options: dict) -> BackupManifest:
        """Assemble the ``BackupManifest`` from CLI args + live Django state."""
        compatibility = self._capture_compatibility()
        data = self._capture_data_component(options)
        config = self._capture_config_component(options)

        return BackupManifest(
            schema_version=BACKUP_MANIFEST_SCHEMA_VERSION,
            backup_id=options["backup_id"],
            created_at=datetime.now(UTC).isoformat(),
            target=options["target"],
            stage=options["stage"],
            backup_uri=options["backup_uri"],
            compatibility=compatibility,
            data=data,
            config=config,
            restore_command_hint=options["restore_command_hint"],
        )

    def _capture_compatibility(self) -> BackupCompatibility:
        """Snapshot the version + migration state that restore must verify."""
        validibot_version = get_validibot_runtime_version()
        if validibot_version == "unknown":
            msg = (
                "Could not determine Validibot runtime version. Set "
                "VALIDIBOT_VERSION in the deployment or install the "
                "validibot package with version metadata before writing "
                "a backup manifest."
            )
            raise CommandError(msg)

        py_version = (
            f"{sys.version_info.major}."
            f"{sys.version_info.minor}."
            f"{sys.version_info.micro}"
        )

        # Postgres server_version_num is the canonical numeric form;
        # server_version is the human-readable string. We capture the
        # latter for log clarity and parse compatibility at restore.
        with connection.cursor() as cursor:
            cursor.execute("SHOW server_version")
            row = cursor.fetchone()
            postgres_version = row[0] if row else "unknown"

        # The migration head is the most recent applied migration per
        # app. We use the recorder rather than scanning files on disk
        # because recorded migrations reflect what the live database
        # actually has — which is what restore needs to verify.
        recorder = MigrationRecorder(connection)
        applied = recorder.applied_migrations()
        migration_head: dict[str, str] = {}
        for app_label, migration_name in applied:
            current = migration_head.get(app_label, "")
            # Migration names start with a 4-digit prefix, so string
            # compare gives the right ordering.
            if migration_name > current:
                migration_head[app_label] = migration_name

        return BackupCompatibility(
            validibot_version=validibot_version,
            python_version=py_version,
            postgres_server_version=postgres_version,
            migration_head=migration_head,
        )

    def _capture_data_component(self, options: dict) -> BackupDataComponent:
        """Build the data component from --db-* flags and --media-inventory.

        ``content_type`` is taken from ``--db-content-type`` if the
        recipe passed it; otherwise we infer from the file extension.
        Self-hosted backups produce ``db.sql.zst`` (zstd-compressed
        plain SQL); GCP backups produce ``db.sql.gz`` (gzip from
        ``gcloud sql export``). Each substrate's recipe should pass
        the right value explicitly, but the inference is a safe
        default that matches how real recipes name their files.
        """
        db_dump = BackupFileEntry(
            path=options["db_file"],
            size_bytes=options["db_size_bytes"],
            content_type=self._resolve_db_content_type(options),
            checksum_sha256=options["db_sha256"],
        )

        media_files: list[BackupFileEntry] = []
        media_inventory_path = options.get("media_inventory")
        if media_inventory_path:
            media_files = self._read_media_inventory(media_inventory_path)

        return BackupDataComponent(db_dump=db_dump, media_files=media_files)

    @staticmethod
    def _resolve_db_content_type(options: dict) -> str:
        """Return the explicit ``--db-content-type`` or infer from path.

        Inference is conservative: only the two extensions Validibot
        actually produces today (``.zst``, ``.gz``) are mapped.
        Anything else falls back to ``application/octet-stream``,
        which is wrong but at least honest — better than silently
        labelling a zstd dump as gzip.
        """
        explicit = options.get("db_content_type")
        if explicit:
            return explicit
        path = options.get("db_file") or ""
        path_lower = path.lower()
        if path_lower.endswith((".zst", ".zstd")):
            return "application/zstd"
        if path_lower.endswith((".gz", ".gzip")):
            return "application/gzip"
        return "application/octet-stream"

    def _capture_config_component(
        self,
        options: dict,
    ) -> BackupConfigComponent | None:
        """Build the config component from --secret-manager-version flags.

        Returns ``None`` when no config inputs are provided, so the
        manifest's ``config`` field stays absent rather than carrying
        an empty stub. Operator backups that don't track config
        (developer-machine self-hosted) get a clean manifest.
        """
        sm_pairs = options.get("secret_manager_version") or []
        if not sm_pairs:
            return None

        secret_versions: dict[str, str] = {}
        for pair in sm_pairs:
            if "=" not in pair:
                msg = f"--secret-manager-version expects NAME=VERSION, got: {pair!r}"
                raise CommandError(msg)
            name, _, version = pair.partition("=")
            name = name.strip()
            version = version.strip()
            if not name or not version:
                msg = f"--secret-manager-version pair has empty side: {pair!r}"
                raise CommandError(msg)
            secret_versions[name] = version

        return BackupConfigComponent(
            secret_manager_versions=secret_versions,
            env_file_inventory=[],  # populated by future self-hosted backup
        )

    def _read_media_inventory(self, path: str) -> list[BackupFileEntry]:
        """Read a JSONL inventory with one ``BackupFileEntry`` per line.

        ``path`` accepts ``-`` for stdin (matching the symmetric
        convention in ``verify_backup_compatibility``), a regular file
        path otherwise. The recipe is responsible for producing the
        JSONL — the manifest writer just consumes it.
        """
        if path == "-":
            raw_text = sys.stdin.read()
        else:
            raw_text = Path(path).read_text(encoding="utf-8")

        entries: list[BackupFileEntry] = []
        for raw_line in raw_text.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            payload = json.loads(line)
            entries.append(BackupFileEntry.model_validate(payload))
        return entries

    def _write_to_gcs(self, *, uri: str, payload: str) -> None:
        """Write ``payload`` to a ``gs://`` URI via google-cloud-storage.

        We use the GCS client directly rather than Django's ``storages``
        backend because the manifest's location is operator-supplied
        and may not match the deployed media bucket. Authentication
        comes from Application Default Credentials, which Cloud Run
        Jobs already have via the Job's service account.
        """
        try:
            from google.cloud import storage
        except ImportError as exc:
            msg = (
                "Writing the manifest to a gs:// URI requires the "
                "google-cloud-storage package. Pass --output as a local "
                "path or stdout if running outside the GCP runtime."
            )
            raise CommandError(msg) from exc

        # Parse "gs://bucket/path/to/object" → ("bucket", "path/to/object").
        without_scheme = uri[len("gs://") :]
        bucket_name, _, object_name = without_scheme.partition("/")
        if not bucket_name or not object_name:
            msg = f"Invalid gs:// URI: {uri!r}"
            raise CommandError(msg)

        client = storage.Client()
        bucket = client.bucket(bucket_name)
        blob = bucket.blob(object_name)
        blob.upload_from_string(
            payload,
            content_type="application/json",
        )
