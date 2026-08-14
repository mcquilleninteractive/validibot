"""Reconcile bundled EnergyPlus weather resources with the current contract.

This command creates ValidatorResourceFile records from EPW files in data/weather/,
making them available in the EnergyPlus step configuration dropdown.

These are created as system-wide resources (org=NULL) so they're visible to all
organizations.

Usage:
    python manage.py seed_weather_files

The command is idempotent - running it multiple times is safe.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

from django.conf import settings
from django.core.files import File
from django.core.management.base import BaseCommand
from django.core.management.base import CommandError

logger = logging.getLogger(__name__)

# Weather files with friendly display names
# Format: (filename, display_name)
WEATHER_FILES = [
    ("USA_CA_San.Francisco.Intl.AP.724940_TMY3.epw", "San Francisco, CA (TMY3)"),
    ("USA_CO_Golden-NREL.724666_TMY3.epw", "Golden/Denver, CO (TMY3)"),
    ("USA_FL_Tampa.Intl.AP.722110_TMY3.epw", "Tampa, FL (TMY3)"),
    ("USA_IL_Chicago-OHare.Intl.AP.725300_TMY3.epw", "Chicago O'Hare, IL (TMY3)"),
    (
        "USA_VA_Sterling-Washington.Dulles.Intl.AP.724030_TMY3.epw",
        "Washington Dulles, VA (TMY3)",
    ),
]


class Command(BaseCommand):
    """Reconcile bundled weather files for the current EnergyPlus contract."""

    help = (
        "Create ValidatorResourceFile records from EPW files in data/weather/. "
        "Creates system-wide resources visible to all organizations."
    )

    def add_arguments(self, parser) -> None:
        """Add command arguments."""
        parser.add_argument(
            "--source-dir",
            type=str,
            default=str(settings.BASE_DIR / "data" / "weather"),
            help="Directory containing weather files (default: data/weather)",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="Replace existing files with same filename",
        )
        parser.add_argument(
            "--strict",
            action="store_true",
            help=(
                "Fail when the bundled catalogue cannot be reconciled exactly. "
                "Deployment paths use this so partial initialization cannot pass."
            ),
        )
        parser.add_argument(
            "--check",
            action="store_true",
            help=(
                "Verify source assets, database rows, hashes, and stored objects "
                "without changing them. Implies --strict."
            ),
        )

    def handle(self, *args, **options) -> str | None:
        """Execute the command."""
        from validibot.validations.constants import ResourceFileType
        from validibot.validations.constants import ValidationType
        from validibot.validations.constants import ValidatorAvailabilityState
        from validibot.validations.models import Validator
        from validibot.validations.models import ValidatorResourceFile
        from validibot.validations.validators.energyplus.config import (
            config as energyplus_config,
        )

        source_dir = Path(options["source_dir"])
        force = options["force"]
        check_only = options["check"]
        strict = options["strict"] or check_only

        if check_only and force:
            raise CommandError("--check and --force cannot be used together")

        if not source_dir.exists():
            message = (
                f"Source directory does not exist: {source_dir}. "
                "The production image must include the bundled EPW catalogue."
            )
            if strict:
                raise CommandError(message)
            self.stderr.write(self.style.ERROR(message))
            return None

        # Resolve the exact validator contract declared by the current code.
        # Older versions remain in the database because existing workflows may
        # still reference them, so validation_type alone is not unique.
        energyplus_validator = Validator.objects.filter(
            slug=energyplus_config.slug,
            version=energyplus_config.version,
            validation_type=ValidationType.ENERGYPLUS,
            is_system=True,
            availability_state=ValidatorAvailabilityState.AVAILABLE,
        ).first()
        if energyplus_validator is None:
            message = (
                "Current EnergyPlus validator not found. Run sync_validators before "
                "reconciling bundled weather resources."
            )
            if strict:
                raise CommandError(message)
            self.stderr.write(self.style.ERROR(message))
            return None

        missing_source_files = [
            filename
            for filename, _display_name in WEATHER_FILES
            if not (source_dir / filename).is_file()
        ]
        if strict and missing_source_files:
            raise CommandError(
                "Bundled EnergyPlus weather catalogue is incomplete: "
                + ", ".join(missing_source_files)
            )

        self.stdout.write(f"Source directory: {source_dir}")
        self.stdout.write(f"Validator: {energyplus_validator.name}")
        self.stdout.write("")

        created = 0
        skipped = 0
        updated = 0
        missing = 0
        verified = 0

        for filename, display_name in WEATHER_FILES:
            source_file = source_dir / filename

            if not source_file.exists():
                self.stdout.write(self.style.WARNING(f"  Missing: {filename}"))
                missing += 1
                continue

            source_hash = hashlib.sha256(source_file.read_bytes()).hexdigest()
            resources = list(
                ValidatorResourceFile.objects.filter(
                    validator=energyplus_validator,
                    filename=filename,
                    org__isnull=True,  # System-wide only
                ).order_by("pk")
            )
            if len(resources) > 1:
                message = (
                    f"Duplicate system weather resources exist for {filename} and "
                    f"EnergyPlus v{energyplus_validator.version}."
                )
                if strict:
                    raise CommandError(message)
                self.stderr.write(self.style.ERROR(message))
            existing = resources[0] if resources else None

            if check_only:
                if existing is None:
                    raise CommandError(
                        f"System weather resource is missing for EnergyPlus "
                        f"v{energyplus_validator.version}: {filename}"
                    )
                self._verify_existing_resource(
                    existing,
                    source_hash=source_hash,
                    filename=filename,
                )
                verified += 1
                self.stdout.write(f"  Verified: {display_name}")
                continue

            if existing and not force:
                reconciled_action = self._reconcile_existing_resource(
                    existing,
                    source_file=source_file,
                    source_hash=source_hash,
                    display_name=display_name,
                    strict=strict,
                )
                if reconciled_action:
                    self.stdout.write(
                        self.style.SUCCESS(f"  {reconciled_action}: {display_name}")
                    )
                    updated += 1
                else:
                    self.stdout.write(f"  Skipped (exists): {display_name}")
                    skipped += 1
                continue

            if existing and force:
                self._replace_existing_resource(
                    existing,
                    source_file=source_file,
                    display_name=display_name,
                )
                action = "Replaced"
                updated += 1
            else:
                action = "Created"
                created += 1
                with source_file.open("rb") as f:
                    resource_file = ValidatorResourceFile(
                        validator=energyplus_validator,
                        org=None,  # System-wide
                        resource_type=ResourceFileType.ENERGYPLUS_WEATHER,
                        name=display_name,
                        filename=filename,
                        is_default=True,
                        description=(
                            f"EnergyPlus TMY3 weather file for {display_name}"
                        ),
                    )
                    resource_file.file.save(filename, File(f), save=True)

            self.stdout.write(self.style.SUCCESS(f"  {action}: {display_name}"))

        # Summary
        self.stdout.write("")
        if created > 0:
            self.stdout.write(
                self.style.SUCCESS(f"Created {created} weather file resource(s)")
            )
        if updated > 0:
            self.stdout.write(
                self.style.SUCCESS(f"Reconciled {updated} weather file resource(s)")
            )
        if skipped > 0:
            self.stdout.write(f"Skipped {skipped} file(s) (already exist)")
        if missing > 0:
            self.stdout.write(
                self.style.WARNING(
                    f"Missing {missing} source file(s). "
                    "Download from EnergyPlus or set --source-dir."
                )
            )
        if verified > 0:
            self.stdout.write(
                self.style.SUCCESS(
                    f"Verified {verified} bundled weather resource(s) for "
                    f"EnergyPlus v{energyplus_validator.version}"
                )
            )

        return None

    def _verify_existing_resource(
        self,
        resource,
        *,
        source_hash: str,
        filename: str,
    ) -> None:
        """Require one catalogue row to match source and durable storage."""
        from validibot.core.filesafety import sha256_field_file
        from validibot.validations.constants import ResourceFileType

        if resource.resource_type != ResourceFileType.ENERGYPLUS_WEATHER:
            raise CommandError(f"Weather resource has the wrong type: {filename}")
        if not resource.file or not resource.file.name:
            raise CommandError(f"Weather resource has no stored object: {filename}")
        if not resource.file.storage.exists(resource.file.name):
            raise CommandError(f"Stored weather object is missing: {filename}")
        stored_hash = sha256_field_file(resource.file)
        if stored_hash != source_hash:
            raise CommandError(
                f"Stored weather object differs from the bundled asset: {filename}"
            )
        if resource.content_hash != stored_hash:
            raise CommandError(f"Weather resource content hash is stale: {filename}")

    def _reconcile_existing_resource(
        self,
        resource,
        *,
        source_file: Path,
        source_hash: str,
        display_name: str,
        strict: bool,
    ) -> str | None:
        """Verify an existing row and restore only an absent matching object."""
        from validibot.core.filesafety import sha256_field_file
        from validibot.validations.constants import ResourceFileType

        filename = source_file.name
        if resource.resource_type != ResourceFileType.ENERGYPLUS_WEATHER:
            message = f"Weather resource has the wrong type: {filename}"
            if strict:
                raise CommandError(message)
            self.stderr.write(self.style.WARNING(message))
            return None
        if resource.content_hash and resource.content_hash != source_hash:
            message = (
                f"Existing weather resource differs from bundled bytes: {filename}. "
                "Bump the Validator/resource contract instead of mutating it in place."
            )
            if strict:
                raise CommandError(message)
            self.stderr.write(self.style.WARNING(message))
            return None

        object_exists = bool(
            resource.file
            and resource.file.name
            and resource.file.storage.exists(resource.file.name)
        )
        if not object_exists:
            with source_file.open("rb") as source:
                resource.file.save(filename, File(source), save=False)
            resource.name = display_name
            resource.filename = filename
            resource.is_default = True
            resource.save()
            return "Restored object"

        if not resource.content_hash:
            # Legacy rows may predate content hashing. Saving once computes the
            # durable hash from storage; normal reconciliations can then compare
            # it to the source asset without downloading every EPW on each boot.
            stored_hash = sha256_field_file(resource.file)
            if stored_hash != source_hash:
                message = (
                    f"Stored weather object differs from bundled bytes: {filename}"
                )
                if strict:
                    raise CommandError(message)
                self.stderr.write(self.style.WARNING(message))
                return None
            resource.save()
            return "Backfilled content hash"
        return None

    def _replace_existing_resource(
        self,
        resource,
        *,
        source_file: Path,
        display_name: str,
    ) -> None:
        """Replace bytes in place so workflow references keep their identity."""
        from validibot.validations.constants import ResourceFileType

        with source_file.open("rb") as source:
            resource.file.save(source_file.name, File(source), save=False)
        resource.resource_type = ResourceFileType.ENERGYPLUS_WEATHER
        resource.name = display_name
        resource.filename = source_file.name
        resource.is_default = True
        resource.description = f"EnergyPlus TMY3 weather file for {display_name}"
        resource.save()
