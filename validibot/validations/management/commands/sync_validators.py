"""
Management command to sync system validators and their step I/O definitions.

Usage:
    python manage.py sync_validators
    python manage.py sync_validators --allow-drift  # development override

All system validators — both built-in single-file validators (Basic, JSON
Schema, XML Schema, AI Assist) and package-based validators (EnergyPlus,
FMU, THERM) — declare their metadata via ``ValidatorConfig``. This command
discovers all configs and ensures the corresponding ``Validator``,
``StepIODefinition``, and ``Derivation`` rows exist in the database.

The step I/O definitions are required for the step editor UI to show separate
"Input Assertions" and "Output Assertions" sections.

ADR-2026-04-27 Phase 3, Session B (tasks 7–9): the command keys validator
rows by ``(slug, version)`` rather than ``slug`` alone, and computes a
``semantic_digest`` from the validator's behavior-defining fields. If a
config is changed in place under the same ``(slug, version)`` (e.g. a
processor name swapped without a version bump), sync raises a
``CommandError`` so the operator must either bump ``version`` to declare
a new validator row, or pass ``--allow-drift`` if they're in development
and intentionally re-syncing a mutated config.
"""

from django.core.management.base import BaseCommand
from django.core.management.base import CommandError
from django.db import transaction

from validibot.validations.constants import CatalogEntryType
from validibot.validations.constants import CatalogRunStage
from validibot.validations.constants import StepIOMedium
from validibot.validations.constants import StepIOOriginKind
from validibot.validations.constants import ValidatorAvailabilityState
from validibot.validations.models import Derivation
from validibot.validations.models import StepIODefinition
from validibot.validations.models import Validator
from validibot.validations.services.catalog_entry_normalization import (
    build_provider_binding_from_mapping,
)
from validibot.validations.services.validator_digest import compute_semantic_digest
from validibot.validations.validators.base.config import get_all_configs
from validibot.workflows.models import WorkflowStep


class Command(BaseCommand):
    help = (
        "Sync system validators and their step I/O definitions from config "
        "declarations."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--allow-drift",
            action="store_true",
            default=False,
            help=(
                "Allow re-syncing a validator whose semantic config has "
                "changed under the same (slug, version). Use only in "
                "development. In production this signals an unbumped "
                "version — bump the config's ``version`` instead."
            ),
        )
        parser.add_argument(
            "--strict-missing",
            action="store_true",
            default=False,
            help=(
                "Fail if any active workflow references a config-managed "
                "validator whose plugin config is no longer registered."
            ),
        )

    def handle(self, *args, **options):
        configs = get_all_configs()
        allow_drift = options["allow_drift"]
        strict_missing = options["strict_missing"]
        active_config_keys = {(cfg.slug, cfg.version) for cfg in configs}

        total_validators_created = 0
        total_validators_updated = 0
        total_validators_missing = 0
        total_io_definitions_synced = 0
        total_derivations_synced = 0

        for cfg in configs:
            self.stdout.write(f"Processing {cfg.slug}...")

            artifact_inputs = [
                entry
                for entry in cfg.catalog_entries
                if entry.entry_type == CatalogEntryType.IO_DEFINITION
                and entry.run_stage == CatalogRunStage.INPUT
                and entry.io_medium == StepIOMedium.ARTIFACT
            ]
            if not artifact_inputs:
                raise CommandError(
                    f"Validator {cfg.slug} v{cfg.version} has no declared "
                    "artifact input port. Every validator must publish its "
                    "input contract before it can be synchronized."
                )

            with transaction.atomic():
                # Build validator field dict from the Pydantic model,
                # excluding fields that aren't Validator model columns.
                # This must cover ALL ValidatorConfig fields that don't
                # map to a Validator DB column — if a new config field
                # is added, add it here too.
                validator_data = cfg.model_dump(
                    exclude={
                        "card_image",
                        "catalog_entries",
                        "icon",
                        "image_name",
                        "output_envelope_class",
                        "provider",
                        "resolved_class",
                        "resolved_envelope_class",
                        "step_editor_cards",
                        "step_serializer_class",
                        "validator_class",
                    },
                )
                validator_data["availability_state"] = (
                    ValidatorAvailabilityState.AVAILABLE
                )
                validator_data["availability_message"] = ""
                validator_data["config_provider"] = cfg.provider

                # Compute the semantic digest from the FULL config dump
                # (not the trimmed one above). The digest function picks
                # only SEMANTIC_FIELDS — passing the trimmed dump would
                # silently exclude fields like ``catalog_entries`` and
                # ``validator_class`` that are highly semantic.
                proposed_digest = compute_semantic_digest(cfg.model_dump())

                # ADR-2026-04-27 Phase 3 task 7: key by (slug, version),
                # not slug alone. A version bump in the config now
                # creates a new Validator row instead of mutating the
                # old one — preserving the launch contract that
                # workflows locked onto the old version were running
                # under.
                validator, created = Validator.objects.get_or_create(
                    slug=cfg.slug,
                    version=cfg.version,
                    defaults={**validator_data, "semantic_digest": proposed_digest},
                )

                if created:
                    total_validators_created += 1
                    self.stdout.write(
                        self.style.SUCCESS(f"  Created validator: {validator}"),
                    )
                else:
                    # ADR-2026-04-27 Phase 3 task 9: drift detection.
                    # If the existing row's stored digest disagrees
                    # with the proposed digest, the config changed
                    # behavior without bumping ``version``. That's
                    # a contract violation: workflows that locked
                    # onto this (slug, version) would silently start
                    # running under different rules.
                    #
                    # Empty stored digest is the migration-window
                    # case: a row created before semantic_digest
                    # existed. We allow the empty → populated
                    # transition without raising; that's the
                    # backfill the field was designed for.
                    existing_digest = validator.semantic_digest or ""
                    if (
                        existing_digest
                        and existing_digest != proposed_digest
                        and not allow_drift
                    ):
                        msg = (
                            f"Semantic drift detected on validator "
                            f"{cfg.slug} v{cfg.version}: stored digest "
                            f"{existing_digest[:12]}... differs from "
                            f"proposed {proposed_digest[:12]}.... "
                            f"Either bump the config's ``version`` to "
                            f"declare a new validator row, or re-run "
                            f"with ``--allow-drift`` to overwrite "
                            f"(development only)."
                        )
                        raise CommandError(msg)

                    # Update existing validator fields, including the
                    # newly-computed digest.
                    for key, value in validator_data.items():
                        if key not in {"slug", "version"}:
                            setattr(validator, key, value)
                    validator.semantic_digest = proposed_digest
                    validator.save()
                    total_validators_updated += 1
                    if existing_digest and existing_digest != proposed_digest:
                        # Allowed-drift path: log loudly so operators
                        # see what just got rewritten.
                        self.stdout.write(
                            self.style.WARNING(
                                f"  Updated validator (DRIFT OVERRIDDEN): {validator}",
                            ),
                        )
                    else:
                        self.stdout.write(f"  Updated validator: {validator}")

                # Sync step I/O definitions and derivations from the
                # validator config's catalog_entries spec.
                seen_io_definition_keys: set[tuple[str, str]] = set()
                seen_derivation_keys: set[str] = set()

                for entry in cfg.catalog_entries:
                    entry_data = entry.model_dump()
                    entry_slug = entry_data.pop("slug")
                    entry_type = entry_data.pop("entry_type")

                    if entry_type == "derivation":
                        Derivation.objects.update_or_create(
                            validator=validator,
                            contract_key=entry_slug,
                            defaults={
                                "expression": entry.binding_config.get(
                                    "expr",
                                    "",
                                ),
                                "data_type": entry.data_type,
                                "order": entry.order,
                            },
                        )
                        seen_derivation_keys.add(entry_slug)
                        total_derivations_synced += 1
                    elif entry_type == "io_definition":
                        provider_binding = build_provider_binding_from_mapping(
                            entry.binding_config,
                        )
                        StepIODefinition.objects.update_or_create(
                            validator=validator,
                            contract_key=entry_slug,
                            direction=entry.run_stage,
                            defaults={
                                "native_name": entry_slug,
                                "label": entry.label or "",
                                "description": entry.description or "",
                                "data_type": entry.data_type,
                                "io_medium": entry.io_medium,
                                "artifact_kind": entry.artifact_kind,
                                "media_type": entry.media_type,
                                "data_format": entry.data_format,
                                "accepted_data_formats": entry.accepted_data_formats,
                                "accepted_extensions": entry.accepted_extensions,
                                "accepted_file_types": entry.accepted_file_types,
                                "accepted_media_types": entry.accepted_media_types,
                                "allowed_source_scopes": entry.allowed_source_scopes,
                                "default_source_strategy": (
                                    entry.default_source_strategy
                                ),
                                "envelope_channel": entry.envelope_channel,
                                "resource_type": entry.resource_type,
                                "role": entry.role,
                                "is_collection": entry.is_collection,
                                "min_items": entry.min_items,
                                "max_items": entry.max_items,
                                "order": entry.order,
                                "unit": (entry.metadata or {}).get("units", ""),
                                "origin_kind": StepIOOriginKind.CATALOG,
                                "source_kind": entry.source_kind,
                                "is_path_editable": entry.is_path_editable,
                                "provider_binding": provider_binding,
                                "metadata": entry.metadata,
                                # Persist the on_missing policy from the
                                # catalog spec per ADR-2026-05-22 and the
                                # May 2026 review's P3 finding. Runtime
                                # enforcement is deferred but the value
                                # round-trips through sync so future
                                # implementation work has a stable place
                                # to read intent from.
                                "on_missing": entry.on_missing,
                            },
                        )
                        seen_io_definition_keys.add((entry_slug, entry.run_stage))
                        total_io_definitions_synced += 1

                # Prune step I/O definitions/derivations that are no longer declared
                # in the config (e.g., renamed or removed entries). Only
                # prune CATALOG-origin step I/O definitions — step-owned definitions
                # (FMU, template) are managed separately.
                if cfg.catalog_entries:
                    pruned_sigs = StepIODefinition.objects.filter(
                        validator=validator,
                        origin_kind=StepIOOriginKind.CATALOG,
                    )
                    for key, direction in seen_io_definition_keys:
                        pruned_sigs = pruned_sigs.exclude(
                            contract_key=key,
                            direction=direction,
                        )
                    pruned_count = pruned_sigs.count()
                    if pruned_count:
                        pruned_sigs.delete()
                        self.stdout.write(
                            f"  Pruned {pruned_count} stale step I/O definition(s)",
                        )

                    pruned_derivs = Derivation.objects.filter(
                        validator=validator,
                    ).exclude(contract_key__in=seen_derivation_keys)
                    pruned_d_count = pruned_derivs.count()
                    if pruned_d_count:
                        pruned_derivs.delete()
                        self.stdout.write(
                            f"  Pruned {pruned_d_count} stale derivation(s)",
                        )

                    self.stdout.write(
                        "  Step I/O definitions: "
                        f"{total_io_definitions_synced} synced, "
                        f"derivations: {total_derivations_synced} synced",
                    )

                # NOTE: We do NOT call ensure_step_input_bindings() here for
                # existing steps using this validator. This command runs on
                # startup/deploy and iterating all steps would be expensive.
                # Instead, ensure_step_input_bindings() handles binding
                # creation at step creation/update time (in save_workflow_step).
                # For backfilling existing steps, use a one-off data migration.

        missing_messages: list[str] = []
        config_managed_validators = Validator.objects.exclude(config_provider="")
        for validator in config_managed_validators:
            if (validator.slug, validator.version) in active_config_keys:
                continue
            total_validators_missing += 1
            message = (
                f"No registered ValidatorConfig for {validator.slug} "
                f"v{validator.version} from provider {validator.config_provider}."
            )
            if (
                validator.availability_state
                != ValidatorAvailabilityState.MISSING_CONFIG
                or validator.availability_message != message
            ):
                validator.availability_state = ValidatorAvailabilityState.MISSING_CONFIG
                validator.availability_message = message
                validator.save(
                    update_fields=[
                        "availability_state",
                        "availability_message",
                        "modified",
                    ],
                )
            self.stdout.write(self.style.WARNING(f"  Missing config: {validator}"))
            if WorkflowStep.objects.filter(
                workflow__is_active=True,
                validator=validator,
            ).exists():
                missing_messages.append(
                    f"{validator.slug} v{validator.version} "
                    f"({validator.validation_type})",
                )

        if strict_missing and missing_messages:
            formatted = ", ".join(sorted(missing_messages))
            raise CommandError(
                "Active workflows reference validator configs that are not "
                f"registered in this deployment: {formatted}",
            )

        self.stdout.write("")
        self.stdout.write(
            self.style.SUCCESS(
                f"Sync complete: "
                f"{total_validators_created} validators created, "
                f"{total_validators_updated} updated, "
                f"{total_validators_missing} missing. "
                f"{total_io_definitions_synced} step I/O definitions synced, "
                f"{total_derivations_synced} derivations synced."
            ),
        )
