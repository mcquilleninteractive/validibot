"""
Doctor command — verify Validibot is set up correctly across deployment targets.

This command is the implementation behind ``just self-hosted doctor`` and
``just gcp doctor <stage>``. It runs a comprehensive set of checks against
the running Validibot instance and reports issues with severity levels and
stable check IDs that map to documented fixes.

Usage:
    python manage.py check_validibot                  # target from settings
    python manage.py check_validibot --target self_hosted
    python manage.py check_validibot --target gcp --stage prod
    python manage.py check_validibot --json                # stable JSON schema
    python manage.py check_validibot --strict              # warnings exit non-zero
    python manage.py check_validibot --verbose             # show detail blocks
    python manage.py check_validibot --fix                 # attempt auto-fix

Severity levels (5-state scale plus skipped, per ADR-2026-04-27 section 6):

    ok       — check passed
    info     — informational, no action required
    warn     — non-blocking concern, should be reviewed
    error    — blocking issue, requires action before production
    fatal    — install is fundamentally broken
    skipped  — check did not run (not applicable to this target)

Check IDs are organized by category:

    VB0xx  Settings / configuration
    VB1xx  Database
    VB2xx  Storage
    VB3xx  Docker / containers
    VB4xx  Background tasks / Celery
    VB5xx  Cache
    VB6xx  Email
    VB7xx  Validators
    VB8xx  Site / roles / permissions / initial data
    VB9xx  Network / TLS / signing (reserved for Phase 1 Session 2)

For the full ID-to-fix table, see:
    docs/operations/self-hosting/doctor-check-ids.md

The JSON output is governed by the ``validibot.doctor.v1`` schema (see
``_output_json`` below). The schema is a contract — additive changes
remain v1; removing or renaming fields requires a v2 bump.

Implementation pattern is informed by comparable projects (Sentry's
sentry-cli health checks, GitLab's gitlab-ctl status, NetBox's plugin
healthchecks). All code in this module is original work for Validibot.
"""

from __future__ import annotations

import json
import logging
import socket
import sys
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

from django.conf import settings
from django.core.management.base import BaseCommand

if TYPE_CHECKING:
    from collections.abc import Callable

    from validibot.validations.services.storage_capabilities import (
        StorageCapabilityReport,
    )

logger = logging.getLogger(__name__)

MIN_SECRET_KEY_LENGTH = 32

# JSON output schema version. Bump only on breaking changes (renames,
# removals). Additive fields stay v1.
DOCTOR_SCHEMA_VERSION = "validibot.doctor.v1"

# -- Compatibility matrix ---------------------------------------------------
#
# Minimum supported versions for the self-hosted profile. ADR
# Phase 6 will publish the official matrix; for Phase 1 Session 2 we
# hard-code reasonable minimums based on what Validibot actually
# tests against. Versions below these produce ``ERROR`` on the
# ``self_hosted`` and ``self_hosted_hardened`` profiles. Host-specific
# checks are skipped for ``local_docker_compose`` because doctor runs inside
# the application container there, not on the Docker host.
#
# Why these specific minimums:
#   - Docker 24.0 — Compose plugin v2 GA; older versions have
#     intermittent issues with named volumes and BuildKit secrets.
#   - Postgres 14 — Phase 3 backups use ``pg_dump`` with
#     ``--load-via-partition-root``; that flag is 14+. Older
#     deployments work for routine validation but fail backup tests.
#   - Ubuntu 22.04 (Jammy) — earliest LTS where the official Docker
#     repository ships the modern Compose plugin without manual
#     workarounds.
MIN_DOCKER_VERSION = (24, 0)
MIN_POSTGRES_VERSION = (14, 0)
MIN_UBUNTU_VERSION = (22, 4)  # Jammy = 22.04

# Days until a backup that hasn't been restore-tested becomes a
# warning. 90 days matches the "test restore quarterly" recommendation
# in the security model. Phase 3's backup recipe writes the marker
# file consumed by ``_check_restore_test``.
RESTORE_TEST_STALE_DAYS = 90

# Sentinel filename inside DATA_STORAGE_ROOT. The Phase 3 restore
# recipe touches this file with ``utime`` after a successful restore;
# doctor reads its mtime to compute staleness.
RESTORE_TEST_MARKER_FILENAME = ".last-restore-test"

# /proc/mounts format is "device mountpoint fstype opts ..." — we
# need at least the first two fields to identify a mount entry.
MIN_MOUNT_FIELDS = 2


class CheckStatus(Enum):
    """Status of a health check.

    The 5-state severity scale (plus ``skipped``) follows ADR-2026-04-27
    section 6. Existing community-edition checks emit ``ok``, ``warn``,
    ``error``, and ``skipped`` today. ``info`` and ``fatal`` are
    reserved for future checks that need them — e.g., ``info`` for
    "telemetry is off (this is the default)" and ``fatal`` for
    "validibot-pro is named in INSTALLED_APPS but the package isn't
    installed."

    The exit-code mapping is:
        ``ok``, ``info``, ``skipped``       -> exit 0
        ``warn``                            -> exit 0 unless --strict
        ``error``, ``fatal``                -> exit non-zero
    """

    OK = "ok"
    INFO = "info"
    WARN = "warn"
    ERROR = "error"
    FATAL = "fatal"
    SKIPPED = "skipped"


# Statuses that should fail the doctor command unconditionally.
_BLOCKING_STATUSES = frozenset({CheckStatus.ERROR, CheckStatus.FATAL})

# Status that is blocking only under --strict.
_STRICT_BLOCKING_STATUS = CheckStatus.WARN


@dataclass
class CheckResult:
    """Result of a single doctor check.

    Each result carries a stable check ID (e.g. ``"VB101"``) that
    operators can look up in
    ``docs/operations/self-hosting/doctor-check-ids.md`` to find
    documented fixes. The ``category`` field groups related checks in
    summary output and JSON; it's informational, not load-bearing.

    The pair (``id``, ``category``) is the contract: callers in
    integrations, dashboards, and support tooling rely on these to
    route results. Adding new IDs is fine; renaming an existing ID is
    a breaking change.
    """

    id: str
    category: str
    name: str
    status: CheckStatus
    message: str
    details: str | None = None
    fix_hint: str | None = None


class Command(BaseCommand):
    """
    Verify Validibot installation health.

    Runs comprehensive checks on all system components and reports any
    issues found, with check IDs operators can look up in the docs.
    Use ``--verbose`` for detailed output, ``--strict`` to treat
    warnings as failures, or ``--fix`` to attempt automatic fixes for
    common issues.

    See the module docstring for the full severity/ID taxonomy.
    """

    help = (
        "Verify Validibot is healthy across the configured deployment target. "
        "Operators normally invoke this via `just self-hosted doctor` or "
        "`just gcp doctor <stage>`."
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.results: list[CheckResult] = []
        self.verbose = False
        self.fix_mode = False
        self.json_output = False
        self.strict = False
        self.target: str | None = None
        self.stage: str | None = None
        self.provider: str | None = None
        self.storage_capability: StorageCapabilityReport | None = None

    def add_arguments(self, parser):
        parser.add_argument(
            "--verbose",
            action="store_true",
            help="Show detailed output for each check",
        )
        parser.add_argument(
            "--fix",
            action="store_true",
            help="Attempt to automatically fix common issues",
        )
        parser.add_argument(
            "--json",
            action="store_true",
            help="Output results as JSON (stable schema for scripting)",
        )
        parser.add_argument(
            "--strict",
            action="store_true",
            help=(
                "Treat warnings as failures. Without --strict, only "
                "errors and fatals cause a non-zero exit."
            ),
        )
        parser.add_argument(
            "--target",
            choices=["self_hosted", "gcp", "local_docker_compose", "test"],
            default=None,
            help=(
                "Deployment target. Defaults to settings.DEPLOYMENT_TARGET "
                "(usually the right thing). Pass explicitly to run doctor "
                "for a different target's profile."
            ),
        )
        parser.add_argument(
            "--stage",
            choices=["dev", "staging", "prod"],
            default=None,
            help=(
                "Stage. Only meaningful for --target gcp; self-hosted is "
                "single-stage per VM (see ADR-2026-04-27 section 4)."
            ),
        )
        parser.add_argument(
            "--provider",
            choices=["digitalocean"],
            default=None,
            help=(
                "Provider overlay — adds provider-specific checks on top "
                "of the regular target checks. Currently supports "
                "'digitalocean'. The overlay runs after the standard "
                "checks (so DNS / volume mount / monitoring agent show "
                "up alongside settings / database / storage)."
            ),
        )

    def handle(self, *args, **options):
        self.verbose = options.get("verbose", False)
        self.fix_mode = options.get("fix", False)
        self.json_output = options.get("json", False)
        self.strict = options.get("strict", False)
        self.target = options.get("target") or self._infer_target()
        self.stage = options.get("stage")
        self.provider = options.get("provider")

        if not self.json_output:
            self.stdout.write("")
            self.stdout.write(self.style.HTTP_INFO("=" * 60))
            self.stdout.write(self.style.HTTP_INFO("  Validibot Doctor"))
            self.stdout.write(
                self.style.HTTP_INFO(
                    f"  target={self.target} stage={self.stage or '-'} "
                    f"provider={self.provider or '-'}",
                ),
            )
            self.stdout.write(self.style.HTTP_INFO("=" * 60))
            self.stdout.write("")

        # Run all checks. Order matters for readability — settings first
        # (everything depends on those), then the data layer (db, cache),
        # then the runtime layer (storage, validators), then peripheral
        # concerns (email, security), then compatibility matrix +
        # restore-test marker, then any provider overlay.
        checks: list[tuple[str, Callable]] = [
            ("Database", self._check_database),
            ("Migrations", self._check_migrations),
            ("Cache", self._check_cache),
            ("Storage", self._check_storage),
            ("Validator Storage Capability", self._check_storage_capability),
            ("Site Configuration", self._check_site),
            ("Roles & Permissions", self._check_roles_permissions),
            ("Validators", self._check_validators),
            (
                "Validator execution deployments",
                self._check_validator_execution_deployments,
            ),
            (
                "Validator backend image policy",
                self._check_validator_backend_image_policy,
            ),
            ("Background Tasks", self._check_celery),
            ("Docker", self._check_docker),
            ("Email", self._check_email),
            ("Security", self._check_security),
            ("Compatibility Matrix", self._check_compatibility_matrix),
            ("Restore Test", self._check_restore_test),
        ]
        if self.provider == "digitalocean":
            checks.append(("DigitalOcean Provider", self._check_provider_digitalocean))

        for section_name, check_func in checks:
            if not self.json_output:
                self.stdout.write(
                    self.style.MIGRATE_HEADING(f"Checking {section_name}..."),
                )
            try:
                check_func()
            except Exception as e:
                # An exception during a check should surface as an
                # error, not crash the whole doctor run. Operators
                # need to see the OTHER checks' results too.
                self._add_result(
                    "VB000",
                    "internal",
                    section_name,
                    CheckStatus.ERROR,
                    f"Check failed with exception: {e}",
                )
            if not self.json_output:
                self.stdout.write("")

        # Output results
        if self.json_output:
            self._output_json()
        else:
            self._output_summary()

        # Exit code: errors and fatals always fail; warnings fail only
        # under --strict.
        if any(r.status in _BLOCKING_STATUSES for r in self.results):
            sys.exit(1)
        if self.strict and any(
            r.status is _STRICT_BLOCKING_STATUS for r in self.results
        ):
            sys.exit(1)

    def _infer_target(self) -> str:
        """Read the deployment target from settings.

        ``settings.DEPLOYMENT_TARGET`` is the canonical source — it's
        set by every settings module (local, production, etc.). The
        --target flag is an override for the rare case where an
        operator wants to run doctor against a different profile than
        the running app's settings.
        """
        return getattr(settings, "DEPLOYMENT_TARGET", "self_hosted")

    def _add_result(
        self,
        check_id: str,
        category: str,
        name: str,
        status: CheckStatus,
        message: str,
        *,
        details: str | None = None,
        fix_hint: str | None = None,
    ) -> None:
        """Add a check result.

        Each result must have a stable check ID (``VB...``) and a
        category. The pair is the contract that
        ``docs/operations/self-hosting/doctor-check-ids.md`` documents
        and that integrations rely on. The first two args are
        positional to make missing them a syntax error rather than a
        silent typo.
        """
        result = CheckResult(
            id=check_id,
            category=category,
            name=name,
            status=status,
            message=message,
            details=details,
            fix_hint=fix_hint,
        )
        self.results.append(result)

        if self.json_output:
            return

        # Display result with severity-appropriate icon and color.
        if status == CheckStatus.OK:
            icon = self.style.SUCCESS("✓")
            msg = self.style.SUCCESS(message)
        elif status == CheckStatus.INFO:
            icon = self.style.HTTP_INFO("i")
            msg = self.style.HTTP_INFO(message)
        elif status == CheckStatus.WARN:
            icon = self.style.WARNING("!")
            msg = self.style.WARNING(message)
        elif status == CheckStatus.ERROR:
            icon = self.style.ERROR("✗")
            msg = self.style.ERROR(message)
        elif status == CheckStatus.FATAL:
            icon = self.style.ERROR("✗✗")
            msg = self.style.ERROR(message)
        else:  # SKIPPED
            icon = self.style.NOTICE("-")
            msg = self.style.NOTICE(message)

        self.stdout.write(f"  {icon} [{check_id}] {msg}")

        if self.verbose and details:
            for line in details.split("\n"):
                self.stdout.write(f"      {line}")

        if (
            status in (CheckStatus.WARN, CheckStatus.ERROR, CheckStatus.FATAL)
            and fix_hint
        ):
            self.stdout.write(f"      Fix: {fix_hint}")

    # =========================================================================
    # Database Checks (VB1xx)
    # =========================================================================

    def _check_database(self):
        """Check database connectivity and basic health."""
        from django.db import connection

        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                cursor.fetchone()

            db_settings = settings.DATABASES.get("default", {})
            engine = db_settings.get("ENGINE", "unknown")
            name = db_settings.get("NAME", "unknown")

            if "postgresql" in engine:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT version()")
                    version = cursor.fetchone()[0]
                    details = f"Engine: {engine}\nDatabase: {name}\nVersion: {version}"
            else:
                details = f"Engine: {engine}\nDatabase: {name}"

            self._add_result(
                "VB101",
                "database",
                "Database connection",
                CheckStatus.OK,
                "Database is accessible",
                details=details if self.verbose else None,
            )

        except Exception as e:
            self._add_result(
                "VB101",
                "database",
                "Database connection",
                CheckStatus.ERROR,
                f"Cannot connect to database: {e}",
                fix_hint="Check DATABASE_URL or DATABASES settings",
            )

    def _check_migrations(self):
        """Check that all migrations have been applied."""
        from django.db import connection
        from django.db.migrations.executor import MigrationExecutor

        try:
            executor = MigrationExecutor(connection)
            plan = executor.migration_plan(executor.loader.graph.leaf_nodes())

            if plan:
                pending = len(plan)
                self._add_result(
                    "VB102",
                    "database",
                    "Migrations",
                    CheckStatus.WARN,
                    f"{pending} unapplied migration(s)",
                    details="\n".join(f"  - {m[0]}" for m in plan[:10]),
                    fix_hint="Run: python manage.py migrate",
                )

                if self.fix_mode:
                    from django.core.management import call_command

                    self.stdout.write("      Applying migrations...")
                    call_command("check_migration_history", verbosity=0)
                    call_command("migrate", verbosity=0)
                    self._add_result(
                        "VB102",
                        "database",
                        "Migrations (fixed)",
                        CheckStatus.OK,
                        "Migrations applied successfully",
                    )
            else:
                self._add_result(
                    "VB102",
                    "database",
                    "Migrations",
                    CheckStatus.OK,
                    "All migrations applied",
                )

        except Exception as e:
            self._add_result(
                "VB103",
                "database",
                "Migrations",
                CheckStatus.ERROR,
                f"Cannot check migrations: {e}",
            )

    # =========================================================================
    # Cache Checks (VB5xx)
    # =========================================================================

    def _check_cache(self):
        """Check cache/Redis connectivity."""
        from django.core.cache import cache

        test_key = "validibot_health_check"
        test_value = "ok"

        try:
            cache.set(test_key, test_value, timeout=10)
            result = cache.get(test_key)

            if result == test_value:
                cache_backend = settings.CACHES.get("default", {}).get(
                    "BACKEND",
                    "unknown",
                )
                location = settings.CACHES.get("default", {}).get("LOCATION", "")

                details = f"Backend: {cache_backend}"
                if location:
                    if "@" in str(location):
                        location = location.split("@")[-1]
                    details += f"\nLocation: {location}"

                self._add_result(
                    "VB501",
                    "cache",
                    "Cache",
                    CheckStatus.OK,
                    "Cache is working",
                    details=details if self.verbose else None,
                )

                cache.delete(test_key)
            else:
                self._add_result(
                    "VB502",
                    "cache",
                    "Cache",
                    CheckStatus.ERROR,
                    "Cache read/write failed",
                    fix_hint="Check REDIS_URL or CACHES settings",
                )

        except Exception as e:
            self._add_result(
                "VB501",
                "cache",
                "Cache",
                CheckStatus.ERROR,
                f"Cannot connect to cache: {e}",
                fix_hint="Check REDIS_URL or CACHES settings",
            )

    # =========================================================================
    # Storage Checks (VB2xx)
    # =========================================================================

    def _check_storage(self):
        """Check file storage configuration."""
        from django.core.files.storage import default_storage

        try:
            storage_class = default_storage.__class__.__name__
            details = f"Storage backend: {storage_class}"

            if hasattr(default_storage, "bucket_name"):
                # GCS storage
                bucket = getattr(default_storage, "bucket_name", "unknown")
                details += f"\nGCS Bucket: {bucket}"

                try:
                    list(default_storage.listdir(""))[:1]
                    self._add_result(
                        "VB201",
                        "storage",
                        "Storage",
                        CheckStatus.OK,
                        f"GCS storage configured ({bucket})",
                        details=details if self.verbose else None,
                    )
                except Exception as e:
                    self._add_result(
                        "VB201",
                        "storage",
                        "Storage",
                        CheckStatus.ERROR,
                        f"Cannot access GCS bucket: {e}",
                        fix_hint="Check GS_BUCKET_NAME and GCP credentials",
                    )

            elif hasattr(default_storage, "location"):
                location = default_storage.location
                details += f"\nLocation: {location}"

                if Path(location).exists():
                    test_file = Path(location) / ".validibot_health_check"
                    try:
                        with test_file.open("w") as f:
                            f.write("test")
                        test_file.unlink()
                        self._add_result(
                            "VB203",
                            "storage",
                            "Storage",
                            CheckStatus.OK,
                            "Local storage is writable",
                            details=details if self.verbose else None,
                        )
                    except OSError as e:
                        self._add_result(
                            "VB203",
                            "storage",
                            "Storage",
                            CheckStatus.ERROR,
                            f"Storage directory not writable: {e}",
                            fix_hint=f"Check permissions on {location}",
                        )
                else:
                    self._add_result(
                        "VB202",
                        "storage",
                        "Storage",
                        CheckStatus.ERROR,
                        f"Storage directory does not exist: {location}",
                        fix_hint=f"Create directory: mkdir -p {location}",
                    )
            else:
                self._add_result(
                    "VB204",
                    "storage",
                    "Storage",
                    CheckStatus.OK,
                    f"Storage configured ({storage_class})",
                    details=details if self.verbose else None,
                )

        except Exception as e:
            self._add_result(
                "VB200",
                "storage",
                "Storage",
                CheckStatus.ERROR,
                f"Storage check failed: {e}",
            )

    def _check_storage_capability(self) -> None:
        """Report the effective validator I/O integrity and isolation mode.

        This check is intentionally separate from the generic storage
        reachability check above. Being able to list a bucket or write a local
        directory does not prove that one validator execution receives only its
        own bytes. VB205 describes the paired data-storage + runner contract and
        keeps integrity claims separate from cross-attempt confidentiality.
        """
        from validibot.validations.constants import RuntimeStorageIsolation
        from validibot.validations.services.storage_capabilities import (
            get_storage_capability_report,
        )

        report = get_storage_capability_report()
        self.storage_capability = report

        if report.isolation is RuntimeStorageIsolation.ATTEMPT_SCOPED:
            status = CheckStatus.OK
        else:
            status = CheckStatus.ERROR

        details = "\n".join(
            [
                f"Data storage backend: {report.data_storage_backend}",
                f"Validator runner: {report.validator_runner}",
                f"Integrity enforced: {str(report.integrity_enforced).lower()}",
                f"Create-only writes: {str(report.create_only_writes).lower()}",
                f"Immutable reads: {str(report.immutable_reads).lower()}",
                (
                    "Attempt-scoped authority: "
                    f"{str(report.attempt_scoped_authority).lower()}"
                ),
                *[f"Limitation: {item}" for item in report.limitations],
            ],
        )
        self._add_result(
            "VB205",
            "storage",
            "Validator storage capability",
            status,
            (
                f"mode={report.mode.value}; "
                f"isolation={report.isolation.value}. {report.summary}"
            ),
            details=details,
            fix_hint=report.operator_action,
        )

    # =========================================================================
    # Site Configuration Checks (VB8xx)
    # =========================================================================

    def _check_site(self):
        """Check Django Sites framework configuration."""
        from django.contrib.sites.models import Site

        try:
            site = Site.objects.get(id=settings.SITE_ID)

            if site.domain in ("example.com", "localhost"):
                self._add_result(
                    "VB801",
                    "site",
                    "Site domain",
                    CheckStatus.WARN,
                    f"Site domain is '{site.domain}' (default/development value)",
                    fix_hint=(
                        "Run: python manage.py setup_validibot --domain yourdomain.com"
                    ),
                )
            else:
                self._add_result(
                    "VB801",
                    "site",
                    "Site domain",
                    CheckStatus.OK,
                    f"Site domain: {site.domain}",
                )

            self._add_result(
                "VB802",
                "site",
                "Site name",
                CheckStatus.OK,
                f"Site name: {site.name}",
            )

        except Site.DoesNotExist:
            self._add_result(
                "VB800",
                "site",
                "Site configuration",
                CheckStatus.ERROR,
                f"Site with ID {settings.SITE_ID} does not exist",
                fix_hint="Run: python manage.py setup_validibot",
            )

    # =========================================================================
    # Roles & Permissions Checks (VB8xx)
    # =========================================================================

    def _check_roles_permissions(self):
        """Check that required roles and permissions exist."""
        from django.contrib.auth.models import Permission

        from validibot.users.constants import RoleCode
        from validibot.users.models import Role

        # Check roles
        expected_roles = {r.value for r in RoleCode}
        existing_roles = set(Role.objects.values_list("code", flat=True))
        missing_roles = expected_roles - existing_roles

        if missing_roles:
            self._add_result(
                "VB810",
                "site",
                "Roles",
                CheckStatus.ERROR,
                f"Missing roles: {', '.join(missing_roles)}",
                fix_hint="Run: python manage.py setup_validibot",
            )

            if self.fix_mode:
                for code in missing_roles:
                    label = RoleCode(code).label
                    Role.objects.create(code=code, name=label)
                self._add_result(
                    "VB810",
                    "site",
                    "Roles (fixed)",
                    CheckStatus.OK,
                    f"Created {len(missing_roles)} missing roles",
                )
        else:
            self._add_result(
                "VB810",
                "site",
                "Roles",
                CheckStatus.OK,
                f"{len(existing_roles)} roles configured",
            )

        # Check custom permissions
        from validibot.core.management.commands.setup_validibot import (
            DEFAULT_PERMISSIONS,
        )

        expected_perms = {p[0] for p in DEFAULT_PERMISSIONS}
        existing_perms = set(
            Permission.objects.filter(codename__in=expected_perms).values_list(
                "codename",
                flat=True,
            ),
        )
        missing_perms = expected_perms - existing_perms

        if missing_perms:
            self._add_result(
                "VB811",
                "site",
                "Permissions",
                CheckStatus.ERROR,
                f"Missing permissions: {', '.join(list(missing_perms)[:5])}...",
                fix_hint="Run: python manage.py setup_validibot",
            )
        else:
            self._add_result(
                "VB811",
                "site",
                "Permissions",
                CheckStatus.OK,
                f"{len(existing_perms)} custom permissions configured",
            )

    # =========================================================================
    # Validators Checks (VB7xx)
    # =========================================================================

    def _check_validator_backend_image_policy(self):
        """Surface the deployment's image-pinning policy.

        Operators choose between ``tag`` (default community),
        ``digest`` (production recommended), and ``signed-digest``
        (high-trust).  The doctor flags configurations that are
        loose for the deployment's target stage and inconsistent
        ``signed-digest`` setups where cosign verification isn't
        enabled.

        Catches ``ImproperlyConfigured`` from
        :func:`get_current_policy` and surfaces it as a check
        failure rather than letting it crash the whole doctor run —
        an unrecognised setting value is exactly what the doctor is
        meant to find, not stop on.
        """
        from django.core.exceptions import ImproperlyConfigured

        from validibot.validations.services.image_policy import (
            ValidatorBackendImagePolicy,
        )
        from validibot.validations.services.image_policy import get_current_policy

        try:
            policy = get_current_policy()
        except ImproperlyConfigured as exc:
            self._add_result(
                "VB711",
                "validators",
                "Validator backend image policy",
                CheckStatus.ERROR,
                f"Invalid VALIDATOR_BACKEND_IMAGE_POLICY: {exc}",
                fix_hint=(
                    "Set VALIDATOR_BACKEND_IMAGE_POLICY to one of: "
                    "tag, digest, signed-digest.  Leave empty for "
                    "the 'tag' default."
                ),
            )
            return
        cosign_enabled = bool(
            getattr(settings, "COSIGN_VERIFY_VALIDATOR_BACKEND_IMAGES", False),
        )

        # Stage-aware advisory: ``tag`` is fine for self-hosted
        # quick-start but a warn for production. We don't know the
        # deployment's intent here precisely, so pick a conservative
        # mapping: tag in any non-self_hosted target → warn. Target
        # is a string literal, not an enum (see ``self.target``).
        is_production_target = self.target not in (
            "self_hosted",
            "self_hosted_hardened",
        )

        if policy == ValidatorBackendImagePolicy.TAG:
            severity = CheckStatus.WARN if is_production_target else CheckStatus.INFO
            self._add_result(
                "VB712",
                "validators",
                "Validator backend image policy",
                severity,
                f"Policy is '{policy.value}' (floating tags permitted)",
                fix_hint=(
                    "Set VALIDATOR_BACKEND_IMAGE_POLICY=digest for "
                    "production self-hosted deployments. Pin validator "
                    "backend images via '@sha256:<hex>' in the deployment "
                    "config."
                )
                if is_production_target
                else None,
            )
            return

        # digest / signed-digest
        if policy == ValidatorBackendImagePolicy.SIGNED_DIGEST and not cosign_enabled:
            self._add_result(
                "VB713",
                "validators",
                "Validator backend image policy",
                CheckStatus.ERROR,
                (
                    "Policy is 'signed-digest' but "
                    "COSIGN_VERIFY_VALIDATOR_BACKEND_IMAGES is False. "
                    "Every launch will be refused."
                ),
                fix_hint=(
                    "Set COSIGN_VERIFY_VALIDATOR_BACKEND_IMAGES=True and "
                    "configure COSIGN_VERIFY_PUBLIC_KEY_PATH, or relax "
                    "VALIDATOR_BACKEND_IMAGE_POLICY to 'digest'."
                ),
            )
            return

        self._add_result(
            "VB712",
            "validators",
            "Validator backend image policy",
            CheckStatus.OK,
            f"Policy is '{policy.value}'",
        )

    def _check_validators(self):
        """Check that default validators exist."""
        from validibot.validations.models import Validator

        validator_count = Validator.objects.filter(is_system=True).count()

        if validator_count == 0:
            self._add_result(
                "VB701",
                "validators",
                "System validators",
                CheckStatus.ERROR,
                "No system validators found",
                fix_hint="Run: python manage.py setup_validibot",
            )
        else:
            self._add_result(
                "VB701",
                "validators",
                "System validators",
                CheckStatus.OK,
                f"{validator_count} system validators configured",
            )

        # The Validator model uses ``is_enabled`` (not ``is_active`` —
        # that was a stale field name in the original check_validibot
        # before this refactor; see VB702 in doctor-check-ids.md).
        enabled_count = Validator.objects.filter(is_enabled=True).count()
        if enabled_count == 0:
            self._add_result(
                "VB702",
                "validators",
                "Enabled validators",
                CheckStatus.WARN,
                "No enabled validators found",
            )

    def _check_validator_execution_deployments(self):
        """Report safe GCP backend queue, route, identity, and capacity facts."""
        if self.target != "gcp":
            self._add_result(
                "VB720",
                "validators",
                "Validator backend queue",
                CheckStatus.SKIPPED,
                "Managed validator Service routing is specific to GCP.",
            )
            return

        from validibot.validations.constants import ExecutionDeploymentKind
        from validibot.validations.constants import ExecutionDeploymentReadiness
        from validibot.validations.constants import ExecutionDeploymentRoutingRole
        from validibot.validations.constants import ValidationType
        from validibot.validations.constants import ValidatorAvailabilityState
        from validibot.validations.constants import ValidatorReleaseState
        from validibot.validations.models import Validator
        from validibot.validations.models import ValidatorExecutionDeployment

        eligible = list(
            Validator.objects.filter(
                validation_type__in=(
                    ValidationType.ENERGYPLUS,
                    ValidationType.FMU,
                    ValidationType.SHACL,
                    ValidationType.SCHEMATRON,
                    ValidationType.PORTFOLIO_MANAGER,
                ),
                is_enabled=True,
                release_state=ValidatorReleaseState.PUBLISHED,
                availability_state=ValidatorAvailabilityState.AVAILABLE,
            ).order_by("slug")
        )
        routes = list(
            ValidatorExecutionDeployment.objects.filter(
                validator__in=eligible,
                routing_role__in=(
                    ExecutionDeploymentRoutingRole.PRIMARY,
                    ExecutionDeploymentRoutingRole.LONG_RUNNING,
                ),
            ).select_related("validator")
        )
        routes_by_validator: dict[object, dict[str, ValidatorExecutionDeployment]] = {}
        for route in routes:
            routes_by_validator.setdefault(route.validator_id, {})[
                route.routing_role
            ] = route
        problems: list[str] = []
        service_primaries: list[ValidatorExecutionDeployment] = []
        for validator in eligible:
            active = routes_by_validator.get(validator.pk, {})
            primary = active.get(ExecutionDeploymentRoutingRole.PRIMARY)
            if primary is None:
                problems.append(f"{validator.slug}: no primary route")
                continue
            if (
                primary.readiness_state != ExecutionDeploymentReadiness.READY
                or primary.emergency_blocked
            ):
                problems.append(f"{validator.slug}: primary is not launchable")
            if primary.deployment_kind == ExecutionDeploymentKind.CLOUD_RUN_SERVICE:
                service_primaries.append(primary)
                compatibility = active.get(ExecutionDeploymentRoutingRole.LONG_RUNNING)
                if (
                    compatibility is None
                    or compatibility.deployment_kind
                    != ExecutionDeploymentKind.CLOUD_RUN_JOB
                    or compatibility.readiness_state
                    != ExecutionDeploymentReadiness.READY
                    or compatibility.emergency_blocked
                ):
                    problems.append(
                        f"{validator.slug}: no ready long-running Job route"
                    )

        if problems:
            self._add_result(
                "VB721",
                "validators",
                "Validator deployment routes",
                CheckStatus.ERROR,
                f"{len(problems)} managed route problem(s)",
                details="\n".join(problems) if self.verbose else None,
                fix_hint=(
                    "Run 'just gcp validator-deployments-sync <stage>', inspect "
                    "'just gcp validator-deployments-list <stage>', and only "
                    "then activate Services."
                ),
            )
        elif eligible:
            self._add_result(
                "VB721",
                "validators",
                "Validator deployment routes",
                CheckStatus.OK,
                f"{len(eligible)} validator route(s) are launchable",
            )
        else:
            self._add_result(
                "VB721",
                "validators",
                "Validator deployment routes",
                CheckStatus.WARN,
                "No release-enabled managed validators were found.",
            )

        queue = str(getattr(settings, "GCP_VALIDATOR_TASK_QUEUE_NAME", "")).strip()
        invoker = str(
            getattr(
                settings,
                "GCP_VALIDATOR_TASK_INVOKER_SERVICE_ACCOUNT",
                "",
            )
        ).strip()
        provider_configured = bool(queue and invoker)
        self._add_result(
            "VB720",
            "validators",
            "Validator backend queue",
            (
                CheckStatus.OK
                if provider_configured
                else CheckStatus.ERROR
                if service_primaries
                else CheckStatus.WARN
            ),
            (
                f"Validator backend queue '{queue}' uses dedicated invoker '{invoker}'"
                if provider_configured
                else "Validator backend queue or dedicated invoker is not configured."
            ),
            fix_hint=(
                "Set GCP_VALIDATOR_TASK_QUEUE_NAME and "
                "GCP_VALIDATOR_TASK_INVOKER_SERVICE_ACCOUNT, upload secrets, "
                "and rerun 'just gcp init-stage <stage>'."
                if not provider_configured
                else None
            ),
        )

        allowlist = {
            str(email).strip().lower()
            for email in (
                getattr(settings, "TASK_OIDC_ALLOWED_SERVICE_ACCOUNTS", [])
                or [getattr(settings, "CLOUD_TASKS_SERVICE_ACCOUNT", "")]
            )
            if str(email).strip()
        }
        missing_identities = sorted(
            {
                route.expected_runtime_identity
                for route in routes
                if route.expected_runtime_identity.lower() not in allowlist
            }
        )
        self._add_result(
            "VB722",
            "validators",
            "Validator callback identities",
            CheckStatus.ERROR if missing_identities else CheckStatus.OK,
            (
                f"{len(missing_identities)} active runtime identity/identities "
                "are absent from the worker OIDC allowlist."
                if missing_identities
                else "Every active validator runtime identity is allowlisted."
            ),
            details=(
                "\n".join(missing_identities)
                if missing_identities and self.verbose
                else None
            ),
            fix_hint=(
                "Add the listed validator runtime service accounts to "
                "TASK_OIDC_ALLOWED_SERVICE_ACCOUNTS and redeploy the worker."
                if missing_identities
                else None
            ),
        )

        capacities = [
            f"{route.validator.slug}: min={route.minimum_instances}, "
            f"max={route.maximum_instances}, concurrency={route.concurrency}"
            for route in service_primaries
        ]
        self._add_result(
            "VB723",
            "validators",
            "Validator Service capacity",
            CheckStatus.OK if service_primaries else CheckStatus.INFO,
            (
                f"{len(service_primaries)} Service primary route(s) have "
                "explicit capacity."
                if service_primaries
                else "No validator Services are primary."
            ),
            details="\n".join(capacities) if capacities and self.verbose else None,
        )

    # =========================================================================
    # Background Tasks Checks (VB4xx)
    # =========================================================================

    def _check_celery(self):
        """Check Celery/background task system."""
        if self.target == "gcp":
            self._add_result(
                "VB401",
                "tasks",
                "Celery broker",
                CheckStatus.SKIPPED,
                "Celery is not used on GCP; background work uses Cloud Tasks.",
            )
            self._add_result(
                "VB402",
                "tasks",
                "Celery Beat schedules",
                CheckStatus.SKIPPED,
                "Celery Beat is not used on GCP; schedules use Cloud Scheduler.",
            )
            return

        broker_url = getattr(settings, "CELERY_BROKER_URL", None)

        if not broker_url:
            self._add_result(
                "VB401",
                "tasks",
                "Celery broker",
                CheckStatus.SKIPPED,
                "CELERY_BROKER_URL not configured (using sync mode or Cloud Tasks)",
            )
            return

        try:
            from celery import Celery

            app = Celery()
            app.config_from_object("django.conf:settings", namespace="CELERY")

            conn = app.connection()
            conn.ensure_connection(max_retries=1, timeout=5)
            conn.close()

            self._add_result(
                "VB401",
                "tasks",
                "Celery broker",
                CheckStatus.OK,
                "Celery broker is accessible",
            )

        except Exception as e:
            self._add_result(
                "VB401",
                "tasks",
                "Celery broker",
                CheckStatus.ERROR,
                f"Cannot connect to Celery broker: {e}",
                fix_hint="Check CELERY_BROKER_URL setting",
            )

        # Check Celery Beat schedules
        try:
            from django_celery_beat.models import PeriodicTask

            schedule_count = PeriodicTask.objects.filter(enabled=True).count()
            if schedule_count == 0:
                self._add_result(
                    "VB402",
                    "tasks",
                    "Celery Beat schedules",
                    CheckStatus.WARN,
                    "No periodic tasks configured",
                    fix_hint="Run: python manage.py setup_validibot",
                )
            else:
                self._add_result(
                    "VB402",
                    "tasks",
                    "Celery Beat schedules",
                    CheckStatus.OK,
                    f"{schedule_count} periodic tasks configured",
                )

        except ImportError:
            self._add_result(
                "VB403",
                "tasks",
                "Celery Beat",
                CheckStatus.SKIPPED,
                "django_celery_beat not installed",
            )

    # =========================================================================
    # Docker Checks (VB3xx)
    # =========================================================================

    def _check_docker(self):
        """Check the configured validator runner's Docker API access.

        Local and self-hosted Compose deployments deliberately use the
        Python Docker SDK through a mounted socket. The application container
        does not need the Docker CLI, so checking ``shutil.which("docker")``
        produces a false warning even when advanced validators can run.
        Asking the configured runner exercises the same dependency and socket
        path that real validation jobs use.
        """
        from validibot.validations.services.runners.registry import get_validator_runner

        try:
            runner = get_validator_runner()
            runner_type = runner.get_runner_type()
        except Exception as e:
            self._add_result(
                "VB304",
                "docker",
                "Docker",
                CheckStatus.WARN,
                f"Cannot initialize configured validator runner: {e}",
                fix_hint=(
                    "Check VALIDATOR_RUNNER and VALIDATOR_RUNNER_OPTIONS, then "
                    "restart the application."
                ),
            )
            return

        if runner_type != "docker":
            self._add_result(
                "VB301",
                "docker",
                "Docker",
                CheckStatus.SKIPPED,
                f"Docker is not required by configured runner '{runner_type}'.",
            )
            return

        if runner.is_available():
            self._add_result(
                "VB302",
                "docker",
                "Docker",
                CheckStatus.OK,
                "Docker runner can reach Docker Engine through the Docker API",
            )
            self._check_validator_images()
        else:
            self._add_result(
                "VB302",
                "docker",
                "Docker",
                CheckStatus.WARN,
                "Configured Docker runner cannot reach Docker Engine",
                details=(
                    "Advanced validators such as EnergyPlus and FMU require "
                    "access to the Docker API."
                ),
                fix_hint=(
                    "Start the container engine and verify that the host socket "
                    "selected by VALIDATOR_CONTAINER_SOCKET is mounted into "
                    "the worker container at /var/run/docker.sock with usable "
                    "permissions."
                ),
            )

    def _check_validator_images(self):
        """Check validator images through the same SDK used by the runner."""
        import docker

        expected_images = [
            "validibot-validator-backend-energyplus",
            "validibot-validator-backend-fmu",
        ]

        try:
            client = docker.from_env()
            available_images = {
                tag.rsplit(":", 1)[0]
                # Sparse listing avoids one inspect request (and one DEBUG log
                # line) per host image. Repository tags are already present in
                # Docker's list response, which is all this inventory needs.
                for image in client.images.list(sparse=True)
                for tag in image.tags
            }
            found = [img for img in expected_images if img in available_images]
            missing = [img for img in expected_images if img not in available_images]

            if found:
                self._add_result(
                    "VB310",
                    "docker",
                    "Validator images",
                    CheckStatus.OK,
                    f"{len(found)} validator image(s) available",
                    details="\n".join(f"  - {img}" for img in found)
                    if self.verbose
                    else None,
                )

            if missing and self.verbose:
                self._add_result(
                    "VB311",
                    "docker",
                    "Validator images (optional)",
                    CheckStatus.SKIPPED,
                    f"{len(missing)} validator image(s) not installed",
                    details="\n".join(f"  - {img}" for img in missing),
                    fix_hint=(
                        "Build images: (in ../validibot-validator-backends) "
                        "just build energyplus fmu"
                    ),
                )

        except Exception:  # noqa: S110
            # Non-critical: missing image listing shouldn't fail the
            # whole doctor run. VB302 already verified runner availability;
            # this is only an optional inventory hint.
            pass

    # =========================================================================
    # Email Checks (VB6xx)
    # =========================================================================

    def _check_email(self):
        """Check email configuration."""
        email_backend = getattr(settings, "EMAIL_BACKEND", "")

        if "console" in email_backend.lower() or "dummy" in email_backend.lower():
            self._add_result(
                "VB601",
                "email",
                "Email",
                CheckStatus.WARN,
                f"Using development email backend: {email_backend.split('.')[-1]}",
                details="Emails will not be sent to real addresses",
                fix_hint="Configure EMAIL_HOST, EMAIL_PORT, etc. for production",
            )
            return

        if "smtp" in email_backend.lower() or "ses" in email_backend.lower():
            email_host = getattr(settings, "EMAIL_HOST", None)
            email_port = getattr(settings, "EMAIL_PORT", 587)

            if not email_host:
                self._add_result(
                    "VB602",
                    "email",
                    "Email",
                    CheckStatus.ERROR,
                    "EMAIL_HOST not configured",
                    fix_hint="Set EMAIL_HOST in settings or environment",
                )
                return

            try:
                sock = socket.create_connection((email_host, email_port), timeout=5)
                sock.close()
                self._add_result(
                    "VB603",
                    "email",
                    "Email",
                    CheckStatus.OK,
                    f"SMTP server reachable ({email_host}:{email_port})",
                )
            except (OSError, TimeoutError) as e:
                self._add_result(
                    "VB603",
                    "email",
                    "Email",
                    CheckStatus.WARN,
                    f"Cannot connect to SMTP server: {e}",
                    fix_hint="Check EMAIL_HOST and EMAIL_PORT settings",
                )
        else:
            self._add_result(
                "VB604",
                "email",
                "Email",
                CheckStatus.OK,
                f"Email backend: {email_backend.split('.')[-1]}",
            )

    # =========================================================================
    # Security Checks (VB0xx)
    # =========================================================================

    def _check_security(self):
        """Check security-related settings.

        Each issue gets its own check ID rather than aggregating into
        one ``VB000 Security`` result. That way operators can look up
        each finding independently in the doctor-check-ids docs.
        """
        # Map of (id, predicate, message, fix_hint)
        # Each tuple is one independent finding.
        findings: list[tuple[str, bool, str, str]] = []

        # VB001 — DEBUG mode
        if settings.DEBUG:
            findings.append(
                (
                    "VB001",
                    True,
                    "DEBUG mode enabled",
                    "Set DEBUG=False in production",
                ),
            )

        # VB002 — Weak SECRET_KEY
        secret_key = getattr(settings, "SECRET_KEY", "")
        if "changeme" in secret_key.lower() or len(secret_key) < MIN_SECRET_KEY_LENGTH:
            findings.append(
                (
                    "VB002",
                    True,
                    "Weak SECRET_KEY",
                    "Generate a strong random SECRET_KEY",
                ),
            )

        # VB003 — ALLOWED_HOSTS misconfigured
        allowed_hosts = getattr(settings, "ALLOWED_HOSTS", [])
        if "*" in allowed_hosts:
            findings.append(
                (
                    "VB003",
                    True,
                    "ALLOWED_HOSTS contains '*'",
                    "Set specific allowed hosts",
                ),
            )
        elif not allowed_hosts:
            findings.append(
                (
                    "VB003",
                    True,
                    "ALLOWED_HOSTS is empty",
                    "Add your domain to ALLOWED_HOSTS",
                ),
            )

        # VB004 — CSRF_TRUSTED_ORIGINS missing in production
        csrf_trusted = getattr(settings, "CSRF_TRUSTED_ORIGINS", [])
        if not csrf_trusted and not settings.DEBUG:
            findings.append(
                (
                    "VB004",
                    True,
                    "CSRF_TRUSTED_ORIGINS not set",
                    "Add your domain to CSRF_TRUSTED_ORIGINS",
                ),
            )

        # VB005 — DJANGO_ADMIN_URL still default
        admin_url = getattr(settings, "ADMIN_URL", "admin/")
        if admin_url == "admin/" and not settings.DEBUG:
            findings.append(
                (
                    "VB005",
                    True,
                    "DJANGO_ADMIN_URL is the default 'admin/'",
                    "Set DJANGO_ADMIN_URL to a random path: "
                    'python -c "import secrets; print(secrets.token_urlsafe(16))"',
                ),
            )

        # VB006 — SSL redirect off in production
        if not settings.DEBUG and not getattr(settings, "SECURE_SSL_REDIRECT", False):
            findings.append(
                (
                    "VB006",
                    True,
                    "SECURE_SSL_REDIRECT is False",
                    "Enable HTTPS redirect",
                ),
            )

        # VB007 — Insecure session cookies in production
        if not settings.DEBUG and not getattr(
            settings,
            "SESSION_COOKIE_SECURE",
            False,
        ):
            findings.append(
                (
                    "VB007",
                    True,
                    "SESSION_COOKIE_SECURE is False",
                    "Enable secure cookies",
                ),
            )

        # VB008 — API key digest secret missing or coupled to SECRET_KEY
        api_key_digest_key = getattr(settings, "API_KEY_DIGEST_KEY", "")
        if not settings.DEBUG and not api_key_digest_key:
            findings.append(
                (
                    "VB008",
                    True,
                    "DJANGO_API_KEY_DIGEST_KEY is not set",
                    "Generate a separate digest key with: "
                    'python -c "import secrets; print(secrets.token_urlsafe(32))"',
                ),
            )
        elif (
            not settings.DEBUG
            and api_key_digest_key
            and api_key_digest_key == secret_key
        ):
            findings.append(
                (
                    "VB008",
                    True,
                    "DJANGO_API_KEY_DIGEST_KEY reuses SECRET_KEY",
                    "Set DJANGO_API_KEY_DIGEST_KEY to a separate random value "
                    "so API-key digest rotation is independent of Django "
                    "session/signing key rotation.",
                ),
            )

        if findings:
            for finding_id, _present, issue, fix in findings:
                # In DEBUG mode (dev), security findings are warnings
                # not errors — developers don't need HTTPS locally. In
                # production, they're errors.
                status = CheckStatus.WARN if settings.DEBUG else CheckStatus.ERROR
                self._add_result(
                    finding_id,
                    "settings",
                    "Security",
                    status,
                    issue,
                    fix_hint=fix,
                )
        else:
            self._add_result(
                "VB000",
                "settings",
                "Security",
                CheckStatus.OK,
                "Security settings look good",
            )

    # =========================================================================
    # Compatibility Matrix Checks (VB0xx for OS, VB1xx for db, VB3xx for docker)
    # =========================================================================

    def _check_compatibility_matrix(self):
        """Check that runtime versions meet documented minimums.

        Phase 1 Session 2 hard-codes minimums. Phase 6 will publish the
        official supported deployment matrix and these constants will
        move to a single registry that doctor consults.

        Severity is target-aware: ``error`` for ``self_hosted`` /
        ``self_hosted_hardened`` (production deployments must run
        supported versions); ``warn`` for developer/test runtime services;
        and ``info`` for GCP because Cloud Run / Cloud SQL versions are
        externally managed. Host-only checks skip local Compose because the
        command runs inside an application container there.
        """
        # The error-vs-warn decision varies by target. We compute it
        # once here and reuse for each finding below.
        unsupported_status = self._unsupported_version_status()

        self._check_postgres_version(unsupported_status)
        self._check_docker_version(unsupported_status)
        self._check_docker_rootless()
        self._check_docker_not_from_snap()
        self._check_os_version(unsupported_status)

    def _unsupported_version_status(self) -> CheckStatus:
        """Decide what severity to use for an unsupported version finding.

        Production self-hosted profiles get ``error`` because running
        unsupported versions is a real reliability risk. Developer /
        test profiles get ``warn`` because devs sometimes need to
        reproduce issues on older toolchains. GCP is ``info`` because
        we don't control Cloud Run / Cloud SQL versions directly.
        """
        if self.target in ("self_hosted", "self_hosted_hardened"):
            return CheckStatus.ERROR
        if self.target == "gcp":
            return CheckStatus.INFO
        return CheckStatus.WARN

    def _check_postgres_version(self, unsupported_status: CheckStatus) -> None:
        """Verify Postgres >= ``MIN_POSTGRES_VERSION``.

        Reads the version via ``SHOW server_version_num`` (an integer
        like ``180001`` for 18.0.1). This is more reliable than parsing
        ``SELECT version()``'s string, which varies by build.
        """
        from django.db import connection

        if "postgresql" not in connection.settings_dict.get("ENGINE", ""):
            self._add_result(
                "VB120",
                "database",
                "Postgres version",
                CheckStatus.SKIPPED,
                "Database is not PostgreSQL — version check skipped.",
            )
            return

        try:
            with connection.cursor() as cursor:
                cursor.execute("SHOW server_version_num")
                raw = int(cursor.fetchone()[0])
                # server_version_num encoding: VVVVNNN in pre-10, then
                # VVNNNN starting in 10. We just compare major.
                # Postgres 14 = 140000, 18 = 180000.
                major = raw // 10000
                minor = (raw % 10000) // 100
        except Exception as e:
            self._add_result(
                "VB120",
                "database",
                "Postgres version",
                CheckStatus.WARN,
                f"Could not determine Postgres version: {e}",
            )
            return

        min_major, min_minor = MIN_POSTGRES_VERSION
        if (major, minor) < (min_major, min_minor):
            self._add_result(
                "VB120",
                "database",
                "Postgres version",
                unsupported_status,
                f"Postgres {major}.{minor} is below minimum {min_major}.{min_minor}",
                fix_hint=(
                    f"Upgrade Postgres to at least {min_major}.{min_minor}. "
                    "See docs/operations/self-hosting/upgrades.md once "
                    "Phase 4 ships."
                ),
            )
        else:
            self._add_result(
                "VB120",
                "database",
                "Postgres version",
                CheckStatus.OK,
                f"Postgres {major}.{minor} meets minimum {min_major}.{min_minor}",
            )

    def _check_docker_version(self, unsupported_status: CheckStatus) -> None:
        """Verify the Docker-compatible engine meets the minimum version.

        Skipped on GCP (Cloud Run doesn't expose a host Docker) and on
        ``test`` profile (Django test runner has no Docker dependency).
        The version is read through the Python Docker SDK, which is the same
        API path validator execution uses. The application image deliberately
        does not include the Docker CLI.
        """
        if self.target in ("gcp", "test", "local_docker_compose"):
            self._add_result(
                "VB320",
                "docker",
                "Docker version",
                CheckStatus.SKIPPED,
                f"Docker version check not applicable to target={self.target}.",
            )
            return

        try:
            import docker

            version_data = docker.from_env().version()
            version_str = str(version_data.get("Version") or "").strip()
            platform_name = str(
                (version_data.get("Platform") or {}).get("Name") or ""
            ).strip()
        except Exception as e:
            self._add_result(
                "VB320",
                "docker",
                "Docker version",
                CheckStatus.WARN,
                f"Could not query the container engine through the Docker API: {e}",
                fix_hint=(
                    "Run doctor in the worker container and verify the "
                    "VALIDATOR_CONTAINER_SOCKET mount."
                ),
            )
            return

        if "podman" in platform_name.lower():
            self._add_result(
                "VB320",
                "docker",
                "Docker version",
                CheckStatus.INFO,
                f"{platform_name} {version_str or 'unknown version'} detected",
                details=(
                    "Docker's minimum version is not numerically comparable "
                    "with Podman's release series."
                ),
                fix_hint=(
                    "Podman support is experimental. Run the full advanced "
                    "validator acceptance suite against this exact release."
                ),
            )
            return

        # Parse "24.0.7" -> (24, 0). Be defensive about extra suffixes
        # like rc / beta tags.
        parts = version_str.split(".")
        try:
            major = int(parts[0])
            minor = int(parts[1].split("-")[0]) if len(parts) > 1 else 0
        except (ValueError, IndexError):
            self._add_result(
                "VB320",
                "docker",
                "Docker version",
                CheckStatus.WARN,
                f"Could not parse Docker version string: {version_str!r}",
            )
            return

        min_major, min_minor = MIN_DOCKER_VERSION
        if (major, minor) < (min_major, min_minor):
            self._add_result(
                "VB320",
                "docker",
                "Docker version",
                unsupported_status,
                (
                    f"Container engine {major}.{minor} is below the "
                    f"Docker-compatible minimum {min_major}.{min_minor}"
                ),
                fix_hint=(
                    "Upgrade Docker to at least "
                    f"{min_major}.{min_minor}. Use the official Docker "
                    "repository, not the OS package manager — those tend "
                    "to lag and miss the Compose plugin."
                ),
            )
        else:
            self._add_result(
                "VB320",
                "docker",
                "Docker version",
                CheckStatus.OK,
                (
                    f"Container engine {major}.{minor} meets the "
                    f"Docker-compatible minimum {min_major}.{min_minor}"
                ),
            )

    def _check_docker_rootless(self) -> None:
        """Report whether the worker's container engine is rootless.

        Rootless operation is recommended hardening for self-hosted installs,
        but it is not a requirement. Validibot's intended operators run known
        backends on networks they control, so a rootful engine is an
        informational trade-off rather than an installation failure.
        """
        if self.target in ("gcp", "test", "local_docker_compose"):
            self._add_result(
                "VB322",
                "docker",
                "Rootless container engine",
                CheckStatus.SKIPPED,
                f"Rootless check not applicable to target={self.target}.",
            )
            return

        try:
            import docker

            engine_info = docker.from_env().info()
        except Exception as e:
            self._add_result(
                "VB322",
                "docker",
                "Rootless container engine",
                CheckStatus.WARN,
                f"Could not determine whether the container engine is rootless: {e}",
                fix_hint=(
                    "Run doctor in the worker container and verify the "
                    "VALIDATOR_CONTAINER_SOCKET mount."
                ),
            )
            return

        security_options = [
            str(option).lower() for option in (engine_info.get("SecurityOptions") or [])
        ]
        host_security = (
            (engine_info.get("host") or {}).get("security") or {}
            if isinstance(engine_info.get("host"), dict)
            else {}
        )
        is_rootless = (
            bool(engine_info.get("Rootless"))
            or bool(engine_info.get("rootless"))
            or bool(host_security.get("rootless"))
            or any("rootless" in option for option in security_options)
        )

        if is_rootless:
            self._add_result(
                "VB322",
                "docker",
                "Rootless container engine",
                CheckStatus.OK,
                "Worker is connected to a rootless container engine",
                details=(
                    "SecurityOptions: " + ", ".join(security_options)
                    if self.verbose and security_options
                    else None
                ),
            )
            return

        self._add_result(
            "VB322",
            "docker",
            "Rootless container engine",
            CheckStatus.INFO,
            "Worker is connected to a rootful container engine",
            details=(
                "This is supported for trusted, operator-selected validator "
                "backends, but the engine socket gives the worker broad host "
                "authority."
            ),
            fix_hint=(
                "Optional hardening: configure rootless Docker, set "
                "VALIDATOR_CONTAINER_SOCKET in .envs/.production/"
                ".self-hosted/.build to its user socket, redeploy, and rerun "
                "doctor."
            ),
        )

    def _check_docker_not_from_snap(self) -> None:
        """Detect Docker installed via Ubuntu snap.

        Snap-installed Docker is a known cause of issues with Compose
        named volumes (it sandboxes /var/lib/docker into the snap
        confinement) and BuildKit secrets. Operators who installed via
        ``apt install docker.io`` instead of the official repo also
        hit this. We detect by checking whether ``which docker``
        resolves to ``/snap/bin/docker``.
        """
        if self.target in ("gcp", "test", "local_docker_compose"):
            return  # Not applicable

        import shutil

        docker_path = shutil.which("docker")
        if docker_path is None:
            return  # Runner availability is covered by VB302.

        if "/snap/" in docker_path:
            self._add_result(
                "VB321",
                "docker",
                "Docker installation source",
                CheckStatus.WARN,
                f"Docker is installed from snap ({docker_path})",
                details=(
                    "Snap-installed Docker has known compatibility issues "
                    "with Compose named volumes and BuildKit secrets. "
                    "Validibot uses both."
                ),
                fix_hint=(
                    "Reinstall Docker from the official Docker repository: "
                    "https://docs.docker.com/engine/install/ubuntu/. "
                    "The bootstrap-host script does this automatically."
                ),
            )
        else:
            self._add_result(
                "VB321",
                "docker",
                "Docker installation source",
                CheckStatus.OK,
                f"Docker installed at {docker_path} (not snap)",
            )

    def _check_os_version(self, unsupported_status: CheckStatus) -> None:
        """Verify OS major version meets minimum (Linux only).

        Only enforces on Ubuntu for now — that's the supported host OS
        per the deployment matrix. Other Linux distros (Debian, RHEL,
        Alpine) produce an informational finding for self-hosted installs;
        macOS / Windows hosts running Docker Desktop skip. Local Compose also
        skips because ``/etc/os-release`` describes the application image,
        not the developer's Docker host.

        ADR Phase 6 will expand the matrix to cover other distros.
        """
        if self.target in ("gcp", "test"):
            return

        if self.target == "local_docker_compose":
            self._add_result(
                "VB030",
                "settings",
                "OS version",
                CheckStatus.SKIPPED,
                "Host OS check is not applicable inside local Docker Compose.",
            )
            return

        os_release = Path("/etc/os-release")
        if not os_release.exists():
            self._add_result(
                "VB030",
                "settings",
                "OS version",
                CheckStatus.SKIPPED,
                "Not on a Linux host — OS version check skipped.",
            )
            return

        try:
            content = os_release.read_text(encoding="utf-8")
            os_id = ""
            os_version_id = ""
            for line in content.splitlines():
                if line.startswith("ID="):
                    os_id = line.split("=", 1)[1].strip().strip('"')
                elif line.startswith("VERSION_ID="):
                    os_version_id = line.split("=", 1)[1].strip().strip('"')
        except OSError as e:
            self._add_result(
                "VB030",
                "settings",
                "OS version",
                CheckStatus.WARN,
                f"Could not read /etc/os-release: {e}",
            )
            return

        if os_id != "ubuntu":
            self._add_result(
                "VB030",
                "settings",
                "OS version",
                CheckStatus.INFO,
                f"OS is {os_id} {os_version_id} — outside the Phase 1 "
                "supported matrix. Validibot may work but isn't tested "
                "on this distro.",
            )
            return

        try:
            parts = os_version_id.split(".")
            major = int(parts[0])
            minor = int(parts[1]) if len(parts) > 1 else 0
        except (ValueError, IndexError):
            self._add_result(
                "VB030",
                "settings",
                "OS version",
                CheckStatus.WARN,
                f"Could not parse Ubuntu version: {os_version_id!r}",
            )
            return

        min_major, min_minor = MIN_UBUNTU_VERSION
        if (major, minor) < (min_major, min_minor):
            self._add_result(
                "VB030",
                "settings",
                "OS version",
                unsupported_status,
                f"Ubuntu {major}.{minor} is below minimum {min_major}.{min_minor} LTS",
                fix_hint=(
                    "Upgrade to Ubuntu 22.04 LTS or 24.04 LTS. Older "
                    "versions ship outdated Docker packages and miss the "
                    "Compose plugin."
                ),
            )
        else:
            self._add_result(
                "VB030",
                "settings",
                "OS version",
                CheckStatus.OK,
                f"Ubuntu {major}.{minor} meets minimum {min_major}.{min_minor}",
            )

    # =========================================================================
    # Restore-test marker (VB4xx)
    # =========================================================================

    def _check_restore_test(self):
        """Verify a restore drill has been performed recently.

        ADR section 5: "A backup that has never been restored is not
        considered valid." Doctor surfaces this by checking for a
        sentinel file (``.last-restore-test``) that the Phase 3
        restore recipe writes after a successful drill. If the file
        is missing or its mtime is older than ``RESTORE_TEST_STALE_DAYS``,
        we warn the operator to run a restore drill.

        Local Compose data is intentionally disposable, so requiring a restore
        drill there would add noise without improving operational readiness.
        Self-hosted and hosted deployments retain the warning because operators
        relying on persistent data need evidence that backups can be restored.
        """
        if self.target == "local_docker_compose":
            self._add_result(
                "VB411",
                "backups",
                "Restore test",
                CheckStatus.SKIPPED,
                "Restore drills are not required for disposable local Compose data.",
            )
            return

        if self.target == "gcp":
            self._add_result(
                "VB411",
                "backups",
                "Restore test",
                CheckStatus.SKIPPED,
                (
                    "Cloud Run has no durable local data root; hosted Cloud SQL "
                    "and GCS restore-drill evidence is tracked in the GCP "
                    "operator runbook."
                ),
            )
            return

        data_root = getattr(settings, "DATA_STORAGE_ROOT", None)
        if not data_root:
            self._add_result(
                "VB411",
                "backups",
                "Restore test",
                CheckStatus.SKIPPED,
                "DATA_STORAGE_ROOT not configured — cannot check marker.",
            )
            return

        marker = Path(data_root) / RESTORE_TEST_MARKER_FILENAME
        if not marker.exists():
            self._add_result(
                "VB411",
                "backups",
                "Restore test",
                CheckStatus.WARN,
                f"No restore drill recorded ({marker} missing).",
                fix_hint=(
                    "Run: just self-hosted backup, then test restore on a "
                    "clean volume with: "
                    "just self-hosted restore <backup-path>. The restore "
                    "recipe writes the marker file."
                ),
            )
            return

        # File exists — check staleness
        try:
            mtime = marker.stat().st_mtime
        except OSError as e:
            self._add_result(
                "VB411",
                "backups",
                "Restore test",
                CheckStatus.WARN,
                f"Cannot read restore-test marker: {e}",
            )
            return

        from time import time

        age_seconds = time() - mtime
        age_days = age_seconds / 86400
        if age_days > RESTORE_TEST_STALE_DAYS:
            self._add_result(
                "VB411",
                "backups",
                "Restore test",
                CheckStatus.WARN,
                f"Restore drill is stale ({int(age_days)} days old, "
                f"max {RESTORE_TEST_STALE_DAYS}).",
                fix_hint="Run another restore drill with the latest backup.",
            )
        else:
            self._add_result(
                "VB411",
                "backups",
                "Restore test",
                CheckStatus.OK,
                f"Restore drill recorded {int(age_days)} days ago.",
            )

    # =========================================================================
    # DigitalOcean Provider Overlay (VB9xx)
    # =========================================================================

    def _check_provider_digitalocean(self):
        """DigitalOcean-specific health checks.

        The ADR (section 15) names DigitalOcean as the first supported
        provider. Operators running on DO get this overlay by passing
        ``--provider digitalocean`` to doctor (or via the DO-specific
        bootstrap script's invocation). Checks here verify the
        DigitalOcean primitives are correctly configured against the
        running Validibot instance.

        Phase 1 Session 2 implements the most useful checks (DNS,
        volume mount, monitoring agent). Cloud Firewall verification
        from the host is genuinely hard (the host can't see its own
        firewall rules without a DO API token, which the security
        model says shouldn't be on the production server). We surface
        an info-level reminder instead.
        """
        self._check_do_dns_resolves_to_host()
        self._check_do_volume_mount()
        self._check_do_monitoring_agent()
        self._check_do_firewall_reminder()

    def _check_do_dns_resolves_to_host(self):
        """SITE_URL hostname should resolve to this host's public IP.

        Pre-flight for Caddy / Let's Encrypt — if DNS isn't right
        before TLS issuance, the ACME challenge fails AND counts
        against the LE rate limit. Same logic as ``just self-hosted
        check-dns``, but inside doctor so it shows up alongside
        other findings.
        """
        site_url = getattr(settings, "SITE_URL", "")
        if not site_url:
            self._add_result(
                "VB910",
                "network",
                "DigitalOcean DNS",
                CheckStatus.SKIPPED,
                "SITE_URL not set — cannot verify DNS.",
            )
            return

        # Strip protocol, port, path
        hostname = (
            site_url.removeprefix("https://")
            .removeprefix("http://")
            .split("/", 1)[0]
            .split(":", 1)[0]
        )
        if not hostname:
            self._add_result(
                "VB910",
                "network",
                "DigitalOcean DNS",
                CheckStatus.WARN,
                f"Could not extract hostname from SITE_URL={site_url!r}",
            )
            return

        try:
            resolved_ip = socket.gethostbyname(hostname)
        except socket.gaierror:
            self._add_result(
                "VB910",
                "network",
                "DigitalOcean DNS",
                CheckStatus.ERROR,
                f"{hostname} does not resolve to any IP address",
                fix_hint=(
                    f"Add a DNS A-record for {hostname} pointing at this "
                    "Droplet's public IPv4."
                ),
            )
            return

        # Detecting the host's public IP from inside a container
        # without an outbound HTTPS call is tricky. We don't make
        # outbound calls from doctor (that violates the telemetry-off
        # principle). Instead, we report what DNS says and note the
        # comparison must be done by ``just self-hosted check-dns``,
        # which does the public-IP detection from outside the doctor
        # process boundary.
        self._add_result(
            "VB910",
            "network",
            "DigitalOcean DNS",
            CheckStatus.INFO,
            f"DNS resolves: {hostname} -> {resolved_ip}",
            details=(
                "Doctor cannot detect this host's public IP from inside "
                "the web container (no outbound calls allowed). To "
                "verify the resolved IP matches this host, run "
                "``just self-hosted check-dns`` from the host shell."
            ),
        )

    def _check_do_volume_mount(self):
        """DATA_STORAGE_ROOT should be on the attached volume mount.

        DigitalOcean's recommended setup mounts a block-storage volume
        at /srv/validibot. If DATA_STORAGE_ROOT points there but the
        mount is wrong (or the boot disk got used by accident), the
        operator loses data on Droplet rebuild without realizing.
        """
        data_root = getattr(settings, "DATA_STORAGE_ROOT", "")
        if not data_root:
            return  # Covered by VB202 / VB204

        if not data_root.startswith("/srv/validibot"):
            self._add_result(
                "VB911",
                "storage",
                "DigitalOcean volume mount",
                CheckStatus.INFO,
                f"DATA_STORAGE_ROOT={data_root} (not on the recommended "
                "/srv/validibot path). This is fine if your volume is "
                "mounted elsewhere — just confirm it's not on the boot "
                "disk.",
            )
            return

        # Read /proc/mounts to verify /srv/validibot is a real mount
        # point, not just a directory on the boot disk.
        proc_mounts = Path("/proc/mounts")
        if not proc_mounts.exists():
            self._add_result(
                "VB911",
                "storage",
                "DigitalOcean volume mount",
                CheckStatus.SKIPPED,
                "Cannot read /proc/mounts (not running on Linux?).",
            )
            return

        try:
            mounts = proc_mounts.read_text(encoding="utf-8")
        except OSError as e:
            self._add_result(
                "VB911",
                "storage",
                "DigitalOcean volume mount",
                CheckStatus.WARN,
                f"Could not read /proc/mounts: {e}",
            )
            return

        is_mount_point = any(
            line.split()[1] == "/srv/validibot"
            for line in mounts.splitlines()
            if len(line.split()) >= MIN_MOUNT_FIELDS
        )
        if is_mount_point:
            self._add_result(
                "VB911",
                "storage",
                "DigitalOcean volume mount",
                CheckStatus.OK,
                "/srv/validibot is on a separate mount (likely the "
                "DigitalOcean block-storage volume).",
            )
        else:
            self._add_result(
                "VB911",
                "storage",
                "DigitalOcean volume mount",
                CheckStatus.WARN,
                "/srv/validibot exists but is NOT a mount point — your "
                "data is on the boot disk and will be lost on Droplet "
                "rebuild.",
                fix_hint=(
                    "Attach a DigitalOcean volume and mount it at "
                    "/srv/validibot. See "
                    "docs/operations/self-hosting/providers/digitalocean.md "
                    "step 2."
                ),
            )

    def _check_do_monitoring_agent(self):
        """DigitalOcean monitoring agent presence (info-level).

        The DO monitoring agent is optional but operationally useful.
        We detect by checking for the systemd service or the agent
        binary. Result is informational — operators may have chosen
        not to install it for telemetry reasons.
        """
        agent_binary = Path("/opt/digitalocean/bin/do-agent")
        if agent_binary.exists():
            self._add_result(
                "VB912",
                "network",
                "DigitalOcean monitoring agent",
                CheckStatus.INFO,
                "DigitalOcean monitoring agent is installed.",
            )
        else:
            self._add_result(
                "VB912",
                "network",
                "DigitalOcean monitoring agent",
                CheckStatus.INFO,
                "DigitalOcean monitoring agent is not installed (optional).",
            )

    def _check_do_firewall_reminder(self):
        """Cloud Firewall reminder (informational only).

        The host cannot reliably introspect its own DO Cloud Firewall
        rules without a DO API token, which the security model
        deliberately keeps off the production server (section 15
        step 7). Instead, we surface a reminder for the operator to
        verify firewall rules from their workstation using ``doctl``.
        """
        self._add_result(
            "VB913",
            "network",
            "DigitalOcean Cloud Firewall",
            CheckStatus.INFO,
            "Cloud Firewall rules cannot be verified from inside the host.",
            details=(
                "From your operator workstation (not the Droplet):\n"
                "  doctl compute firewall list\n"
                "  doctl compute firewall get <firewall-id>\n"
                "Verify allow rules: 22/tcp from operator IP, 80/tcp + "
                "443/tcp from internet, deny everything else inbound."
            ),
        )

    # =========================================================================
    # Output
    # =========================================================================

    def _output_summary(self):
        """Output summary of all checks (human-readable)."""
        self.stdout.write(self.style.HTTP_INFO("=" * 60))
        self.stdout.write(self.style.HTTP_INFO("  Summary"))
        self.stdout.write(self.style.HTTP_INFO("=" * 60))
        self.stdout.write("")

        counts = dict.fromkeys(CheckStatus, 0)
        for r in self.results:
            counts[r.status] += 1

        if counts[CheckStatus.OK]:
            self.stdout.write(
                f"  {self.style.SUCCESS('✓')} Passed:   {counts[CheckStatus.OK]}",
            )
        if counts[CheckStatus.INFO]:
            self.stdout.write(
                f"  {self.style.HTTP_INFO('i')} Info:     {counts[CheckStatus.INFO]}",
            )
        if counts[CheckStatus.WARN]:
            self.stdout.write(
                f"  {self.style.WARNING('!')} Warnings: {counts[CheckStatus.WARN]}",
            )
        if counts[CheckStatus.ERROR]:
            self.stdout.write(
                f"  {self.style.ERROR('✗')} Errors:   {counts[CheckStatus.ERROR]}",
            )
        if counts[CheckStatus.FATAL]:
            self.stdout.write(
                f"  {self.style.ERROR('✗✗')} Fatal:    {counts[CheckStatus.FATAL]}",
            )
        if counts[CheckStatus.SKIPPED]:
            self.stdout.write(
                f"  {self.style.NOTICE('-')} Skipped:  {counts[CheckStatus.SKIPPED]}",
            )

        self.stdout.write("")

        # Every finding carries a stable VBnnn ID; point operators at the
        # lookup table both as a repo path (works offline) and as a URL
        # (clickable from most terminals).
        lookup_path = "docs/operations/self-hosting/doctor-check-ids.md"
        lookup_url = (
            "https://github.com/mcquilleninteractive/validibot/blob/main/" + lookup_path
        )

        if counts[CheckStatus.ERROR] > 0 or counts[CheckStatus.FATAL] > 0:
            self.stdout.write(
                self.style.ERROR(
                    "  Some checks failed. Please fix the errors above.",
                ),
            )
            self.stdout.write(f"  Look up any check ID in: {lookup_path}")
            self.stdout.write(f"  ({lookup_url})")
            self.stdout.write("  Or try: python manage.py check_validibot --fix")
        elif counts[CheckStatus.WARN] > 0:
            warn_msg = "  Validibot is working but some warnings were found."
            if self.strict:
                self.stdout.write(
                    self.style.ERROR(warn_msg + " (--strict: failing)"),
                )
            else:
                self.stdout.write(self.style.WARNING(warn_msg))
            self.stdout.write(f"  Look up any check ID in: {lookup_path}")
        else:
            self.stdout.write(
                self.style.SUCCESS("  All checks passed! Validibot is healthy."),
            )

        self.stdout.write("")

    def _output_json(self):
        """Output results as JSON (stable schema = ``validibot.doctor.v1``).

        Schema contract:

            {
              "schema_version": "validibot.doctor.v1",
              "validibot_version": "<package version>",
              "target": "self_hosted" | "gcp" | "local_docker_compose" | "test",
              "stage": "dev" | "staging" | "prod" | null,
              "provider": "digitalocean" | null,
              "storage_capability": {
                "mode": "local_attempt_mount" | "gcs_downscoped_token" | ...,
                "isolation": "attempt_scoped" | "unsupported",
                ...
              } | null,
              "ran_at": "<ISO 8601 UTC timestamp>",
              "summary": {
                "ok": int, "info": int, "warn": int,
                "error": int, "fatal": int, "skipped": int
              },
              "checks": [
                {
                  "id": "VB101",
                  "category": "database",
                  "name": "Database connection",
                  "status": "ok" | "info" | "warn" | "error" | "fatal" | "skipped",
                  "message": "...",
                  "details": "..." | null,
                  "fix_hint": "..." | null
                },
                ...
              ]
            }

        Additive changes (new fields, new severities, new IDs) stay v1.
        ``provider`` was added in Phase 1 Session 2 alongside the
        ``--provider digitalocean`` overlay; consumers built before
        Session 2 should treat unknown fields as null.

        Renaming or removing fields requires a v2 schema bump and a
        migration window. Integrations should ignore unknown fields
        gracefully.
        """
        validibot_version = self._get_validibot_version()

        output = {
            "schema_version": DOCTOR_SCHEMA_VERSION,
            "validibot_version": validibot_version,
            "target": self.target,
            "stage": self.stage,
            "provider": self.provider,
            "storage_capability": (
                self.storage_capability.as_dict()
                if self.storage_capability is not None
                else None
            ),
            "ran_at": datetime.now(tz=UTC).isoformat(),
            "summary": {
                status.value: sum(1 for r in self.results if r.status == status)
                for status in CheckStatus
            },
            "checks": [
                {
                    "id": r.id,
                    "category": r.category,
                    "name": r.name,
                    "status": r.status.value,
                    "message": r.message,
                    "details": r.details,
                    "fix_hint": r.fix_hint,
                }
                for r in self.results
            ],
        }

        self.stdout.write(json.dumps(output, indent=2))

    def _get_validibot_version(self) -> str:
        """Return the running Validibot version.

        Delegates to :func:`get_validibot_runtime_version`, which is
        the single source of truth across operator surfaces. That
        helper prefers ``settings.VALIDIBOT_VERSION`` (stamped by the
        deploy recipes from the latest release tag) and falls back to
        package metadata only when the deployment-side stamp is
        missing.

        Earlier this implementation read package metadata directly,
        which meant doctor's ``versions`` block could disagree with
        backup manifests, support bundles, and the OCI image label —
        all of which use ``get_validibot_runtime_version()``. Now they
        all agree.
        """
        from validibot.core.deployment import get_validibot_runtime_version

        return get_validibot_runtime_version()
