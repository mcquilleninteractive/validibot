"""
Tests for the sync_validators management command.

This command syncs system validators (EnergyPlus, FMU, THERM) and their catalog
entries from config declarations in each validator package to the database.
"""

from io import StringIO

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command
from django.test import TestCase
from django.test import override_settings

from validibot.submissions.constants import SubmissionDataFormat
from validibot.validations.constants import FMU_MODEL_RESOURCE
from validibot.validations.constants import ArtifactKind
from validibot.validations.constants import BindingSourceScope
from validibot.validations.constants import CatalogValueType
from validibot.validations.constants import DefaultSourceStrategy
from validibot.validations.constants import EnvelopeChannel
from validibot.validations.constants import ResourceFileType
from validibot.validations.constants import StepIOMedium
from validibot.validations.constants import StepIOSourceKind
from validibot.validations.constants import ValidationType
from validibot.validations.constants import ValidatorAvailabilityState
from validibot.validations.models import Derivation
from validibot.validations.models import StepIODefinition
from validibot.validations.models import Validator
from validibot.validations.validators.base.config import discover_configs
from validibot.validations.validators.base.config import get_all_configs
from validibot.validations.validators.base.config import get_config


class SyncValidatorsCommandTests(TestCase):
    """Tests for the sync_validators management command."""

    def call_command(self, *args, **kwargs):
        """Helper to call the command and capture output."""
        out = StringIO()
        err = StringIO()
        call_command(
            "sync_validators",
            *args,
            stdout=out,
            stderr=err,
            **kwargs,
        )
        return out.getvalue(), err.getvalue()

    def test_command_creates_validators_from_configs(self):
        """Test that command creates validators from discovered configs."""
        out, _ = self.call_command()

        # Verify output mentions sync complete
        self.assertIn("Sync complete", out)

        # Verify validators were created for each discovered config
        for cfg in discover_configs():
            self.assertTrue(
                Validator.objects.filter(slug=cfg.slug).exists(),
                f"Validator {cfg.slug} should exist",
            )

    def test_command_creates_energyplus_validator(self):
        """Test that EnergyPlus validator is created with correct attributes."""
        self.call_command()

        validator = Validator.objects.get(slug="energyplus-idf-validator")

        self.assertEqual(validator.name, "EnergyPlus™ Validator")
        self.assertEqual(validator.validation_type, ValidationType.ENERGYPLUS)
        self.assertTrue(validator.is_system)
        self.assertTrue(validator.has_processor)
        self.assertTrue(validator.supports_assertions)
        self.assertEqual(validator.processor_name, "EnergyPlus™ Simulation")
        self.assertEqual(
            validator.availability_state,
            ValidatorAvailabilityState.AVAILABLE,
        )
        self.assertTrue(validator.config_provider)

    def test_command_creates_step_io_definitions(self):
        """Test that step I/O definitions are created for validators."""
        self.call_command()

        validator = Validator.objects.get(slug="energyplus-idf-validator")

        # Should have both input and output value definitions
        input_sigs = StepIODefinition.objects.filter(
            validator=validator,
            direction="input",
        )
        output_sigs = StepIODefinition.objects.filter(
            validator=validator,
            direction="output",
        )

        self.assertTrue(input_sigs.exists(), "Should have step input definitions")
        self.assertTrue(output_sigs.exists(), "Should have output value definitions")

    def test_command_creates_specific_output_values(self):
        """Test that specific output value definitions are created.

        Per ADR-2026-05-22 (validator revision 2 and later):
            - zone_count was removed from outputs (parsed-from-IDF facts
              are step inputs only, never step outputs)
            - floor_area_m2 was renamed to simulated_conditioned_area_m2
              for provenance clarity (lands with validibot-shared 0.8.0)
        """
        self.call_command()

        validator = Validator.objects.get(slug="energyplus-idf-validator")

        # Check for specific output values
        expected_outputs = [
            "energyplus_binary_version",
            "idd_version",
            "version_match",
            "completed_successfully",
            "warning_count",
            "fatal_count",
            "review_issue_count",
            "has_sql_output",
            "site_electricity_kwh",
            "site_other_fuels_kwh",
            "site_eui_kwh_m2",
            "unmet_heating_hours",
            "simulated_conditioned_area_m2",
        ]

        for key in expected_outputs:
            self.assertTrue(
                StepIODefinition.objects.filter(
                    validator=validator,
                    contract_key=key,
                ).exists(),
                f"Output definition {key} should exist",
            )

    def test_command_normalizes_energyplus_input_provider_binding(self):
        """EnergyPlus parser-extracted step inputs should not persist
        submission-source selectors in StepIODefinition.provider_binding.

        The unified step I/O model stores submission sourcing on
        StepInputBinding, so sync_validators must strip legacy
        ``source``/``path`` keys from provider_binding. For
        parser-extracted facts (per ADR-2026-05-22, validator revision 2+), the
        value is populated by EnergyPlusValidator.extract_input_values()
        and no payload-path binding is involved at all.

        We test against ``idf_version`` here — one of the three POC
        parser-extracted step inputs introduced in revision 2. The
        legacy ``expected_floor_area_m2`` and friends were removed
        because they were author-expectation values miscategorized as
        validator inputs.
        """
        self.call_command()

        validator = Validator.objects.get(slug="energyplus-idf-validator")
        io_definition = StepIODefinition.objects.get(
            validator=validator,
            contract_key="idf_version",
            direction="input",
        )

        self.assertEqual(io_definition.provider_binding, {})

    def test_command_normalizes_energyplus_output_provider_binding(self):
        """EnergyPlus output provider binding should store the canonical
        ``metric_key`` field used by the unified step I/O model.
        """
        self.call_command()

        validator = Validator.objects.get(slug="energyplus-idf-validator")
        io_definition = StepIODefinition.objects.get(
            validator=validator,
            contract_key="site_eui_kwh_m2",
            direction="output",
        )

        self.assertEqual(
            io_definition.provider_binding,
            {"metric_key": "site_eui_kwh_m2"},
        )

    def test_command_syncs_energyplus_artifact_ports(self):
        """EnergyPlus file dependencies must round-trip into StepIODefinition.

        The workflow engine should be able to discover the submitted model and
        selected weather file as typed artifact ports, not as EnergyPlus-only
        config keys. This pins the catalog-to-DB sync path for the first
        artifact-port vertical slice.
        """
        self.call_command()

        validator = Validator.objects.get(slug="energyplus-idf-validator")

        primary_model = StepIODefinition.objects.get(
            validator=validator,
            contract_key="primary_model",
            direction="input",
        )
        self.assertEqual(primary_model.io_medium, StepIOMedium.ARTIFACT)
        self.assertEqual(primary_model.data_type, CatalogValueType.ARTIFACT_REF)
        self.assertEqual(primary_model.artifact_kind, ArtifactKind.FILE)
        self.assertEqual(primary_model.role, "primary-model")
        self.assertEqual(primary_model.data_format, SubmissionDataFormat.ENERGYPLUS_IDF)
        self.assertEqual(primary_model.media_type, "application/vnd.energyplus.idf")
        self.assertEqual(primary_model.envelope_channel, EnvelopeChannel.INPUT_FILES)
        self.assertEqual(primary_model.resource_type, "")
        self.assertEqual(
            primary_model.accepted_data_formats,
            [
                SubmissionDataFormat.ENERGYPLUS_IDF,
                SubmissionDataFormat.ENERGYPLUS_EPJSON,
            ],
        )
        self.assertEqual(
            primary_model.accepted_media_types,
            [
                "application/vnd.energyplus.idf",
                "application/vnd.energyplus.epjson",
            ],
        )
        self.assertEqual(
            primary_model.allowed_source_scopes,
            [
                BindingSourceScope.SUBMISSION_FILE,
                BindingSourceScope.UPSTREAM_ARTIFACT,
                BindingSourceScope.SIGNAL,
            ],
        )
        self.assertEqual(
            primary_model.default_source_strategy,
            DefaultSourceStrategy.SUBMITTED_FILE_FIRST,
        )
        self.assertEqual(primary_model.min_items, 1)
        self.assertEqual(primary_model.max_items, 1)
        self.assertFalse(primary_model.is_collection)
        self.assertEqual(
            primary_model.provider_binding,
            {
                "envelope_channel": EnvelopeChannel.INPUT_FILES,
                "role": "primary-model",
            },
        )

        weather_file = StepIODefinition.objects.get(
            validator=validator,
            contract_key="weather_file",
            direction="input",
        )
        self.assertEqual(weather_file.io_medium, StepIOMedium.ARTIFACT)
        self.assertEqual(weather_file.data_type, CatalogValueType.ARTIFACT_REF)
        self.assertEqual(weather_file.artifact_kind, ArtifactKind.FILE)
        self.assertEqual(weather_file.role, "weather")
        self.assertEqual(weather_file.data_format, ResourceFileType.ENERGYPLUS_WEATHER)
        self.assertEqual(weather_file.media_type, "application/vnd.energyplus.epw")
        self.assertEqual(weather_file.envelope_channel, EnvelopeChannel.RESOURCE_FILES)
        self.assertEqual(
            weather_file.resource_type,
            ResourceFileType.ENERGYPLUS_WEATHER,
        )
        self.assertEqual(
            weather_file.accepted_data_formats,
            [ResourceFileType.ENERGYPLUS_WEATHER],
        )
        self.assertEqual(
            weather_file.accepted_media_types,
            ["application/vnd.energyplus.epw"],
        )
        self.assertEqual(
            weather_file.allowed_source_scopes,
            [
                BindingSourceScope.WORKFLOW_RESOURCE,
                BindingSourceScope.SUBMISSION_FILE,
                BindingSourceScope.UPSTREAM_ARTIFACT,
            ],
        )
        self.assertEqual(
            weather_file.default_source_strategy,
            DefaultSourceStrategy.SUBMITTED_FILE_THEN_DEFAULT_RESOURCE,
        )
        # Weather is conditional: required only when run_simulation=true.
        self.assertEqual(weather_file.min_items, 0)
        self.assertEqual(weather_file.max_items, 1)
        self.assertFalse(weather_file.is_collection)
        self.assertEqual(
            weather_file.provider_binding,
            {
                "envelope_channel": EnvelopeChannel.RESOURCE_FILES,
                "resource_type": ResourceFileType.ENERGYPLUS_WEATHER,
            },
        )

        eplusout_sql = StepIODefinition.objects.get(
            validator=validator,
            contract_key="eplusout_sql",
            direction="output",
        )
        self.assertEqual(eplusout_sql.io_medium, StepIOMedium.ARTIFACT)
        self.assertEqual(eplusout_sql.data_type, CatalogValueType.ARTIFACT_REF)
        self.assertEqual(eplusout_sql.artifact_kind, ArtifactKind.DATASET)
        self.assertEqual(eplusout_sql.role, "simulation-db")
        self.assertEqual(eplusout_sql.data_format, "sqlite")
        self.assertEqual(eplusout_sql.media_type, "application/x-sqlite3")
        self.assertEqual(
            eplusout_sql.envelope_channel,
            EnvelopeChannel.OUTPUT_ARTIFACTS,
        )
        self.assertEqual(eplusout_sql.metadata["accepted_extensions"], ["sql"])
        self.assertEqual(eplusout_sql.accepted_data_formats, ["sqlite"])
        self.assertEqual(
            eplusout_sql.accepted_media_types,
            ["application/x-sqlite3", "application/vnd.sqlite3"],
        )
        self.assertEqual(eplusout_sql.min_items, 0)
        self.assertEqual(eplusout_sql.max_items, 1)
        self.assertEqual(
            eplusout_sql.provider_binding,
            {"source": "output_artifact", "role": "simulation-db"},
        )

    def test_command_creates_derivations(self):
        """Test that derivation entries are created."""
        self.call_command()

        validator = Validator.objects.get(slug="energyplus-idf-validator")

        derivations = Derivation.objects.filter(
            validator=validator,
        )

        self.assertTrue(derivations.exists(), "Should have derivation entries")

        # Check for specific derivation
        total_unmet = Derivation.objects.filter(
            validator=validator,
            contract_key="total_unmet_hours",
        )
        self.assertTrue(
            total_unmet.exists(),
            "total_unmet_hours derivation should exist",
        )

    def test_command_is_idempotent(self):
        """Test that running command multiple times doesn't create duplicates."""
        # Run twice
        self.call_command()
        self.call_command()

        # Should only have one of each validator
        for cfg in discover_configs():
            count = Validator.objects.filter(slug=cfg.slug).count()
            self.assertEqual(
                count,
                1,
                f"Should have exactly 1 validator with slug {cfg.slug}, found {count}",
            )

    def test_command_marks_missing_config_managed_validator_unavailable(self):
        """Rows synced from a removed plugin are retained but made unavailable.

        Dynamic validation types mean a DB can contain a plugin validator row
        after the package that registered it has been removed. The sync command
        should preserve the row for history while preventing future launches.
        """
        stale = Validator.objects.create(
            slug="cloud-only-validator",
            version=1,
            name="Cloud-only Validator",
            validation_type="CLOUD_ONLY",
            is_system=True,
            config_provider="validibot_cloud.validators.cloud_only",
            availability_state=ValidatorAvailabilityState.AVAILABLE,
        )

        out, _ = self.call_command()

        stale.refresh_from_db()
        self.assertIn("Missing config", out)
        self.assertEqual(
            stale.availability_state,
            ValidatorAvailabilityState.MISSING_CONFIG,
        )
        self.assertIn(
            "validibot_cloud.validators.cloud_only",
            stale.availability_message,
        )
        self.assertTrue(Validator.objects.filter(pk=stale.pk).exists())

    def test_command_updates_existing_validator(self):
        """Test that command updates existing validator fields.

        Phase 3 Session B (ADR-2026-04-27 task 7): sync_validators
        keys by ``(slug, version)`` rather than slug alone, so the
        seed row must declare the same ``version`` the config
        advertises — otherwise sync would CREATE a new row alongside
        instead of updating the seed.

        EnergyPlus catalog revision history:
        - v1: original
        - v2: ADR-2026-05-22 cleanup + parser-extracted step inputs
        - v3: ADR-2026-05-22 Phase 2 added nine more parser facts
          (building_name, terrain, solar_distribution, timestep_per_hour,
          surface_count, window_count, construction_count,
          run_period_count, has_hvac)

        The catalogue versions were later reset to a clean v1 baseline. The
        artifact-port output slice then bumped the active contract to v2, and
        review-readiness evidence/metrics made the active contract v3. The seed
        row must match that advertised version to exercise the update path.
        """
        # Create a validator with different name but matching (slug, version).
        Validator.objects.create(
            slug="energyplus-idf-validator",
            version=3,
            name="Old Name",
            validation_type=ValidationType.ENERGYPLUS,
            is_system=True,
        )

        out, _ = self.call_command()

        # Verify output mentions update
        self.assertIn("Updated", out)

        # Verify name was updated
        validator = Validator.objects.get(slug="energyplus-idf-validator")
        self.assertEqual(validator.name, "EnergyPlus™ Validator")

    def test_command_syncs_supports_assertions(self):
        """Test that supports_assertions is synced from config to DB."""
        self.call_command()

        # Validators that support assertions
        for slug in (
            "basic-validator",
            "json-schema-validator",
            "xml-validator",
            "energyplus-idf-validator",
            "fmu-validator",
            "ai-assisted-validator",
            "shacl-validator",
            "therm-validator",
        ):
            validator = Validator.objects.get(slug=slug)
            self.assertTrue(
                validator.supports_assertions,
                f"{slug} should support assertions",
            )

    def test_command_creates_shacl_output_values(self):
        """SHACL output values should be synced for the step editor.

        The SHACL validator is inline, but it still emits output values
        such as ``shacl_violation_count``. The step editor and assertion
        form need those StepIODefinition rows to show default targets.
        """
        self.call_command()

        validator = Validator.objects.get(slug="shacl-validator")

        for key in (
            "parse_ok",
            "triple_count",
            "has_s223_namespace",
            "shacl_violation_count",
            "shacl_total_count",
        ):
            self.assertTrue(
                StepIODefinition.objects.filter(
                    validator=validator,
                    contract_key=key,
                    direction="output",
                ).exists(),
                f"SHACL output definition {key} should exist",
            )

    def test_command_reports_creation_counts(self):
        """Test that command reports how many validators/entries were created."""
        out, _ = self.call_command()

        # Should report validators created
        self.assertIn("validators created", out.lower())

        # Should report step I/O definitions synced
        self.assertIn("step i/o definitions synced", out.lower())

    # ── source_kind and is_path_editable ────────────────────────────
    # These tests verify that the sync command correctly persists the
    # new source metadata fields from CatalogEntrySpec to StepIODefinition.

    def test_energyplus_value_input_values_are_internal_and_not_editable(self):
        """EnergyPlus parser value inputs should be INTERNAL + non-editable.

        Parser-derived facts such as ``idf_version`` are computed internally
        after the model file is resolved. Artifact ports are separate input
        definitions that describe file dependencies, so this assertion filters
        to value-carried step inputs only.
        """
        self.call_command()

        validator = Validator.objects.get(slug="energyplus-idf-validator")
        input_sigs = StepIODefinition.objects.filter(
            validator=validator,
            direction="input",
            io_medium=StepIOMedium.VALUE,
        )

        self.assertTrue(input_sigs.exists())
        for io_definition in input_sigs:
            self.assertEqual(
                io_definition.source_kind,
                StepIOSourceKind.INTERNAL,
                "EnergyPlus step input "
                f"{io_definition.contract_key} should be INTERNAL",
            )
            self.assertFalse(
                io_definition.is_path_editable,
                f"EnergyPlus step input {io_definition.contract_key}"
                " should not be path-editable",
            )

    def test_energyplus_output_values_are_internal_and_not_editable(self):
        """EnergyPlus output values should be INTERNAL + non-editable.

        Output values come from simulation metrics extracted internally
        by the validator — the author has no control over the extraction.
        """
        self.call_command()

        validator = Validator.objects.get(slug="energyplus-idf-validator")
        output_sigs = StepIODefinition.objects.filter(
            validator=validator,
            direction="output",
        )

        self.assertTrue(output_sigs.exists())
        for io_definition in output_sigs:
            self.assertEqual(
                io_definition.source_kind,
                StepIOSourceKind.INTERNAL,
                "EnergyPlus output value "
                f"{io_definition.contract_key} should be INTERNAL",
            )
            self.assertFalse(
                io_definition.is_path_editable,
                f"EnergyPlus output value {io_definition.contract_key}"
                " should not be path-editable",
            )

    def test_therm_output_values_are_internal_and_not_editable(self):
        """THERM output values should be INTERNAL + non-editable.

        THERM extracts values directly from the THMX/THMZ XML — the
        author has no control over the extraction paths.
        """
        self.call_command()

        validator = Validator.objects.get(slug="therm-validator")
        output_sigs = StepIODefinition.objects.filter(
            validator=validator,
            direction="output",
        )

        self.assertTrue(output_sigs.exists())
        for io_definition in output_sigs:
            self.assertEqual(
                io_definition.source_kind,
                StepIOSourceKind.INTERNAL,
                f"THERM output value {io_definition.contract_key} should be INTERNAL",
            )
            self.assertFalse(
                io_definition.is_path_editable,
                "THERM output value "
                f"{io_definition.contract_key} should not be path-editable",
            )


class CreateDefaultValidatorsTests(TestCase):
    """Tests for create_default_validators() in utils.py."""

    def test_sets_supports_assertions(self):
        """create_default_validators sets supports_assertions from config."""
        from validibot.validations.utils import create_default_validators

        create_default_validators()

        # Validators that support assertions
        for slug in (
            "basic-validator",
            "json-schema-validator",
            "xml-validator",
            "energyplus-idf-validator",
            "fmu-validator",
            "ai-assisted-validator",
            "therm-validator",
        ):
            validator = Validator.objects.get(slug=slug)
            self.assertTrue(
                validator.supports_assertions,
                f"{slug} should support assertions",
            )

    def test_uses_registered_config_version_for_seeded_validators(self):
        """create_default_validators should not recreate stale catalog versions.

        ``reset_system`` and ``setup_validibot`` call this legacy seeder before
        running config sync. If the seeder hard-codes an older version for a
        config-managed validator, reset leaves duplicate rows and resource
        seeding can hit slug-only lookup failures.
        """
        from validibot.validations.utils import create_default_validators
        from validibot.validations.validators.base.config import get_config

        create_default_validators()

        cfg = get_config(ValidationType.ENERGYPLUS)
        validator = Validator.objects.get(slug="energyplus-idf-validator")
        self.assertEqual(validator.version, cfg.version)


class SystemValidatorShortDescriptionTests(TestCase):
    """Regression coverage for the validator-card short-description fight.

    Two commands wrote the same Validator rows:

      - ``create_default_validators`` (community legacy seed)
      - ``sync_validators``           (the new ADR-2026-04-27 source of truth)

    ``setup_validibot`` calls ``create_default_validators`` first and
    ``sync_validators`` second, so the sync command's view of the world wins.
    If a validator config silently omitted ``short_description``, sync would
    overwrite the seeded value with the empty Pydantic default, leaving the
    "Add workflow step" cards with blank subtitles for Basic / JSON Schema /
    XML / FMU / etc. — only SHACL survived because its config was the only
    one that declared the field.

    These tests pin every system validator config to a non-empty
    ``short_description`` so we never quietly regress to blank cards again.
    """

    def test_every_system_config_declares_short_description(self):
        """Each discovered validator config must set ``short_description``.

        This guards the source side of the bug: if anyone adds a new system
        validator or refactors an existing one and forgets the field, the
        DB row will be silently blanked the next time ``sync_validators``
        runs. Catching it in the config dump is much cheaper than chasing
        a blank-card UI bug in QA.
        """
        for cfg in get_all_configs():
            self.assertTrue(
                cfg.short_description.strip(),
                f"validator config {cfg.slug!r} is missing short_description "
                f"— sync_validators will blank the card subtitle on next run.",
            )

    def test_sync_validators_persists_short_description(self):
        """``sync_validators`` must write the config's ``short_description``.

        Guards the destination side: confirms that after a real sync run the
        Validator rows in the DB carry the same ``short_description`` as the
        config. If the sync command stops writing this field (e.g. someone
        adds it to the ``exclude`` set in ``cfg.model_dump``), this test
        fails immediately.
        """
        call_command("sync_validators", stdout=StringIO(), stderr=StringIO())

        for cfg in get_all_configs():
            validator = Validator.objects.get(slug=cfg.slug, version=cfg.version)
            self.assertEqual(
                validator.short_description,
                cfg.short_description,
                f"{cfg.slug!r}: sync_validators didn't persist short_description "
                f"(got {validator.short_description!r}, "
                f"expected {cfg.short_description!r})",
            )


class DiscoverConfigsTests(TestCase):
    """Tests for the discover_configs() function."""

    def test_discovers_system_validators(self):
        """discover_configs() finds configs for all system validators."""
        configs = discover_configs()
        slugs = {c.slug for c in configs}

        self.assertIn("energyplus-idf-validator", slugs)
        self.assertIn("therm-validator", slugs)
        self.assertIn("fmu-validator", slugs)

    def test_configs_are_sorted(self):
        """Configs are returned sorted by (order, name)."""
        configs = discover_configs()
        orders = [(c.order, c.name) for c in configs]

        self.assertEqual(orders, sorted(orders))

    def test_configs_have_required_fields(self):
        """Every config has slug, name, and validation_type."""
        for cfg in discover_configs():
            self.assertTrue(cfg.slug, f"Config {cfg} should have a slug")
            self.assertTrue(cfg.name, f"Config {cfg} should have a name")
            self.assertTrue(
                cfg.validation_type,
                f"Config {cfg} should have a validation_type",
            )

    def test_energyplus_has_catalog_entries(self):
        """EnergyPlus config has input/output values and derivations."""
        configs = discover_configs()
        ep_config = next(c for c in configs if c.slug == "energyplus-idf-validator")

        self.assertGreater(len(ep_config.catalog_entries), 0)

        entry_types = {e.entry_type for e in ep_config.catalog_entries}
        self.assertIn("io_definition", entry_types)
        self.assertIn("derivation", entry_types)

    def test_fmu_static_catalog_carries_file_port_and_parser_facts(self):
        """FMU config holds the model file port and parser-fact INPUT entries.

        Per-variable I/O definitions are still
        created dynamically per FMU upload via
        ``services/fmu._persist_variables``. The static catalog entries on
        the system FMU validator are the ``fmu_model`` artifact input port
        plus the Phase 6 parser facts (model_name, fmi_version, variable
        counts, etc.). If a future change adds dynamically-shaped entries to
        this config it would break the upload-time seeding, so the static
        catalog stays focused.
        """
        configs = discover_configs()
        fmu_config = next(c for c in configs if c.slug == "fmu-validator")

        parser_entries = [
            entry
            for entry in fmu_config.catalog_entries
            if entry.binding_config.get("source") == "parser"
        ]
        self.assertEqual(len(parser_entries), 7)
        for entry in parser_entries:
            self.assertEqual(entry.run_stage, "input")

        fmu_model = next(
            entry for entry in fmu_config.catalog_entries if entry.slug == "fmu_model"
        )
        self.assertEqual(fmu_model.data_type, CatalogValueType.ARTIFACT_REF)
        self.assertEqual(fmu_model.io_medium, StepIOMedium.ARTIFACT)
        self.assertEqual(fmu_model.artifact_kind, ArtifactKind.FILE)
        self.assertEqual(fmu_model.envelope_channel, EnvelopeChannel.INPUT_FILES)
        self.assertEqual(fmu_model.resource_type, FMU_MODEL_RESOURCE)
        self.assertEqual(fmu_model.role, "fmu")
        self.assertEqual(fmu_model.min_items, 1)
        self.assertEqual(fmu_model.max_items, 1)
        self.assertEqual(fmu_model.accepted_data_formats, [SubmissionDataFormat.FMU])
        self.assertEqual(fmu_model.accepted_media_types, ["application/vnd.fmi.fmu"])
        self.assertEqual(fmu_model.metadata["accepted_extensions"], ["fmu"])
        self.assertIn(
            BindingSourceScope.WORKFLOW_RESOURCE,
            fmu_model.allowed_source_scopes,
        )
        self.assertIn(BindingSourceScope.SYSTEM, fmu_model.allowed_source_scopes)

    def test_therm_catalog_declares_its_generic_model_file_port(self):
        """THERM must opt into cross-step files through catalog data alone.

        This contract makes the default step form, shared runtime resolver, and
        future compatible producers work without THERM-specific workflow code.
        """
        configs = discover_configs()
        therm_config = next(c for c in configs if c.slug == "therm-validator")
        therm_model = next(
            entry
            for entry in therm_config.catalog_entries
            if entry.slug == "therm_model"
        )

        self.assertEqual(therm_config.version, 2)
        self.assertEqual(therm_model.data_type, CatalogValueType.ARTIFACT_REF)
        self.assertEqual(therm_model.io_medium, StepIOMedium.ARTIFACT)
        self.assertEqual(therm_model.artifact_kind, ArtifactKind.FILE)
        self.assertEqual(therm_model.envelope_channel, EnvelopeChannel.INPUT_FILES)
        self.assertEqual(therm_model.role, "therm-model")
        self.assertEqual(
            therm_model.allowed_source_scopes,
            [
                BindingSourceScope.SUBMISSION_FILE,
                BindingSourceScope.UPSTREAM_ARTIFACT,
            ],
        )
        self.assertEqual(
            therm_model.default_source_strategy,
            DefaultSourceStrategy.SUBMITTED_FILE_FIRST,
        )
        self.assertEqual(therm_model.min_items, 1)
        self.assertEqual(therm_model.max_items, 1)

    def test_energyplus_has_file_handling_fields(self):
        """EnergyPlus config has file type and extension fields populated."""
        configs = discover_configs()
        ep_config = next(c for c in configs if c.slug == "energyplus-idf-validator")

        self.assertIn("text", ep_config.supported_file_types)
        self.assertIn("energyplus_idf", ep_config.supported_data_formats)
        self.assertIn("idf", ep_config.allowed_extensions)
        self.assertIn("energyplus_weather", ep_config.resource_types)

    def test_energyplus_declares_artifact_ports(self):
        """EnergyPlus config declares its model and weather file contracts.

        The workflow engine should learn file dependencies from validator
        catalog metadata, so the source config must include the primary model
        and weather file before any sync command touches the database.
        """
        configs = discover_configs()
        ep_config = next(c for c in configs if c.slug == "energyplus-idf-validator")

        artifact_ports = {
            entry.slug: entry
            for entry in ep_config.catalog_entries
            if entry.io_medium == StepIOMedium.ARTIFACT
        }

        self.assertEqual(
            set(artifact_ports),
            {
                "primary_model",
                "weather_file",
                "eplusout_sql",
                "eplusout_csv",
                "eplusout_err",
                "eplusout_eso",
            },
        )
        self.assertEqual(artifact_ports["primary_model"].role, "primary-model")
        self.assertEqual(
            artifact_ports["primary_model"].data_type,
            CatalogValueType.ARTIFACT_REF,
        )
        self.assertEqual(
            artifact_ports["primary_model"].data_format,
            SubmissionDataFormat.ENERGYPLUS_IDF,
        )
        self.assertEqual(
            artifact_ports["primary_model"].envelope_channel,
            EnvelopeChannel.INPUT_FILES,
        )
        self.assertEqual(
            artifact_ports["primary_model"].allowed_source_scopes,
            [
                BindingSourceScope.SUBMISSION_FILE,
                BindingSourceScope.UPSTREAM_ARTIFACT,
                BindingSourceScope.SIGNAL,
            ],
        )
        self.assertEqual(artifact_ports["weather_file"].role, "weather")
        self.assertEqual(
            artifact_ports["weather_file"].data_format,
            ResourceFileType.ENERGYPLUS_WEATHER,
        )
        self.assertEqual(
            artifact_ports["weather_file"].envelope_channel,
            EnvelopeChannel.RESOURCE_FILES,
        )
        self.assertEqual(
            artifact_ports["weather_file"].allowed_source_scopes,
            [
                BindingSourceScope.WORKFLOW_RESOURCE,
                BindingSourceScope.SUBMISSION_FILE,
                BindingSourceScope.UPSTREAM_ARTIFACT,
            ],
        )
        self.assertEqual(artifact_ports["eplusout_sql"].role, "simulation-db")
        self.assertEqual(
            artifact_ports["eplusout_sql"].envelope_channel,
            EnvelopeChannel.OUTPUT_ARTIFACTS,
        )
        self.assertEqual(
            artifact_ports["eplusout_sql"].data_type,
            CatalogValueType.ARTIFACT_REF,
        )
        self.assertEqual(artifact_ports["eplusout_sql"].data_format, "sqlite")
        self.assertEqual(artifact_ports["eplusout_sql"].min_items, 0)
        self.assertEqual(artifact_ports["eplusout_sql"].max_items, 1)

    def test_shacl_declares_data_graph_artifact_port(self):
        """SHACL config declares the submitted/upstream RDF graph contract.

        The SHACL backend still receives an input file URI, but the workflow
        engine should learn that dependency from the validator catalog as a
        first-class ``data_graph`` artifact port. This keeps SHACL aligned with
        EnergyPlus/FMU and prevents a regression back to the implicit
        ``primary_file_uri`` convention.
        """
        configs = discover_configs()
        shacl_config = next(c for c in configs if c.slug == "shacl-validator")

        artifact_ports = {
            entry.slug: entry
            for entry in shacl_config.catalog_entries
            if entry.io_medium == StepIOMedium.ARTIFACT
        }

        self.assertEqual(set(artifact_ports), {"data_graph", "shacl_report"})
        data_graph = artifact_ports["data_graph"]
        self.assertEqual(data_graph.data_type, CatalogValueType.ARTIFACT_REF)
        self.assertEqual(data_graph.artifact_kind, ArtifactKind.FILE)
        self.assertEqual(data_graph.envelope_channel, EnvelopeChannel.INPUT_FILES)
        self.assertEqual(data_graph.role, "data-graph")
        self.assertEqual(data_graph.min_items, 1)
        self.assertEqual(data_graph.max_items, 1)
        self.assertEqual(
            data_graph.default_source_strategy,
            DefaultSourceStrategy.SUBMITTED_FILE_FIRST,
        )
        self.assertEqual(
            data_graph.accepted_data_formats,
            [
                SubmissionDataFormat.TEXT,
                SubmissionDataFormat.JSON,
                SubmissionDataFormat.XML,
            ],
        )
        self.assertEqual(
            data_graph.accepted_media_types,
            [
                "text/turtle",
                "application/rdf+xml",
                "application/ld+json",
                "application/n-triples",
                "application/n-quads",
            ],
        )
        self.assertEqual(
            data_graph.metadata["accepted_extensions"],
            ["ttl", "rdf", "jsonld", "nt", "nq"],
        )
        self.assertEqual(
            data_graph.allowed_source_scopes,
            [
                BindingSourceScope.SUBMISSION_FILE,
                BindingSourceScope.UPSTREAM_ARTIFACT,
            ],
        )
        shacl_report = artifact_ports["shacl_report"]
        self.assertEqual(shacl_report.data_type, CatalogValueType.ARTIFACT_REF)
        self.assertEqual(shacl_report.artifact_kind, ArtifactKind.REPORT)
        self.assertEqual(
            shacl_report.envelope_channel,
            EnvelopeChannel.OUTPUT_ARTIFACTS,
        )
        self.assertEqual(shacl_report.role, "shacl-report")
        self.assertEqual(shacl_report.min_items, 0)
        self.assertEqual(shacl_report.max_items, 1)
        self.assertEqual(shacl_report.data_format, SubmissionDataFormat.TEXT)
        self.assertEqual(
            shacl_report.accepted_data_formats,
            [SubmissionDataFormat.TEXT],
        )
        self.assertEqual(shacl_report.accepted_media_types, ["text/turtle"])
        self.assertEqual(shacl_report.metadata["accepted_extensions"], ["ttl"])

    def test_schematron_declares_xml_document_artifact_port(self):
        """Schematron config declares the submitted/upstream XML document contract.

        Schematron rules stay inline in ``SchematronInputs`` for this slice.
        The submitted XML document is the file-like data-plane input and must be
        discoverable from the validator catalog instead of hidden behind
        ``primary_file_uri``.
        """
        configs = discover_configs()
        schematron_config = next(c for c in configs if c.slug == "schematron-validator")

        artifact_ports = {
            entry.slug: entry
            for entry in schematron_config.catalog_entries
            if entry.io_medium == StepIOMedium.ARTIFACT
        }

        self.assertEqual(set(artifact_ports), {"xml_document", "svrl_report"})
        xml_document = artifact_ports["xml_document"]
        self.assertEqual(xml_document.data_type, CatalogValueType.ARTIFACT_REF)
        self.assertEqual(xml_document.artifact_kind, ArtifactKind.FILE)
        self.assertEqual(xml_document.envelope_channel, EnvelopeChannel.INPUT_FILES)
        self.assertEqual(xml_document.role, "xml-document")
        self.assertEqual(xml_document.min_items, 1)
        self.assertEqual(xml_document.max_items, 1)
        self.assertEqual(
            xml_document.default_source_strategy,
            DefaultSourceStrategy.SUBMITTED_FILE_FIRST,
        )
        self.assertEqual(
            xml_document.accepted_data_formats,
            [SubmissionDataFormat.XML],
        )
        self.assertEqual(
            xml_document.accepted_media_types,
            ["application/xml", "text/xml"],
        )
        self.assertEqual(xml_document.metadata["accepted_extensions"], ["xml"])
        self.assertEqual(
            xml_document.allowed_source_scopes,
            [
                BindingSourceScope.SUBMISSION_FILE,
                BindingSourceScope.UPSTREAM_ARTIFACT,
            ],
        )
        svrl_report = artifact_ports["svrl_report"]
        self.assertEqual(svrl_report.data_type, CatalogValueType.ARTIFACT_REF)
        self.assertEqual(svrl_report.artifact_kind, ArtifactKind.REPORT)
        self.assertEqual(svrl_report.envelope_channel, EnvelopeChannel.OUTPUT_ARTIFACTS)
        self.assertEqual(svrl_report.role, "svrl-report")
        self.assertEqual(svrl_report.min_items, 0)
        self.assertEqual(svrl_report.max_items, 1)
        self.assertEqual(svrl_report.data_format, SubmissionDataFormat.XML)
        self.assertEqual(svrl_report.accepted_data_formats, [SubmissionDataFormat.XML])
        self.assertEqual(
            svrl_report.accepted_media_types,
            ["application/xml", "text/xml"],
        )
        self.assertEqual(svrl_report.metadata["accepted_extensions"], ["svrl"])

    def test_configs_have_display_metadata(self):
        """All discovered configs have icon and card_image set."""
        for cfg in discover_configs():
            self.assertTrue(cfg.icon, f"{cfg.slug} missing icon")
            self.assertTrue(cfg.card_image, f"{cfg.slug} missing card_image")

    def test_discovered_configs_include_provider_metadata(self):
        """Discovered validator configs should record the provider module."""

        for cfg in discover_configs():
            self.assertTrue(
                cfg.provider,
                f"{cfg.slug} should include a provider module path",
            )

    @override_settings(
        VALIDIBOT_ALLOWED_VALIDATOR_PLUGIN_PREFIXES=(
            "validibot.validations.validators.base",
        ),
    )
    def test_discover_configs_rejects_disallowed_provider_prefixes(self):
        """Discovery should fail when a validator provider is not allowlisted."""

        with pytest.raises(ImproperlyConfigured):
            discover_configs()


class ConfigRegistryTests(TestCase):
    """Tests for the config registry (populated at Django startup)."""

    def test_all_validation_types_registered(self):
        """Every ValidationType has a config in the registry."""
        for vtype in ValidationType:
            cfg = get_config(vtype.value)
            self.assertIsNotNone(cfg, f"No config registered for {vtype}")

    def test_get_config_returns_correct_type(self):
        """get_config() returns the matching config for a known type."""
        cfg = get_config(ValidationType.ENERGYPLUS)
        self.assertIsNotNone(cfg)
        self.assertEqual(cfg.slug, "energyplus-idf-validator")

    def test_get_config_nonexistent_returns_none(self):
        """get_config() returns None for an unregistered type."""
        self.assertIsNone(get_config("NONEXISTENT"))

    def test_get_all_configs_sorted(self):
        """get_all_configs() returns configs sorted by (order, name)."""
        configs = get_all_configs()
        orders = [(c.order, c.name) for c in configs]
        self.assertEqual(orders, sorted(orders))

    def test_get_all_configs_includes_builtins_and_discovered(self):
        """Registry includes both built-in and discovered configs."""
        configs = get_all_configs()
        slugs = {c.slug for c in configs}

        # Built-in validators
        self.assertIn("basic-validator", slugs)
        self.assertIn("json-schema-validator", slugs)

        # Discovered validators
        self.assertIn("energyplus-idf-validator", slugs)
        self.assertIn("therm-validator", slugs)

    def test_energyplus_has_resource_types(self):
        """EnergyPlus config declares weather resource type."""
        cfg = get_config(ValidationType.ENERGYPLUS)
        self.assertIn("energyplus_weather", cfg.resource_types)

    def test_basic_has_json_file_type(self):
        """Basic config declares JSON as supported file type."""
        cfg = get_config(ValidationType.BASIC)
        self.assertIn("json", cfg.supported_file_types)

    def test_configs_declare_supports_assertions(self):
        """Configs correctly declare assertion support."""
        assertion_slugs = {
            "basic-validator",
            "json-schema-validator",
            "xml-validator",
            "energyplus-idf-validator",
            "fmu-validator",
            "ai-assisted-validator",
            "therm-validator",
            "custom-validator",
        }

        for cfg in get_all_configs():
            if cfg.slug in assertion_slugs:
                self.assertTrue(
                    cfg.supports_assertions,
                    f"Config {cfg.slug} should declare supports_assertions=True",
                )

    def test_registry_count_covers_builtin_types(self):
        """Registry has at least one config per built-in ValidationType.

        External validator packages may register additional types, so the enum
        is no longer the exact registry boundary.
        """
        configs = get_all_configs()
        self.assertGreaterEqual(len(configs), len(ValidationType))

    def test_registry_configs_include_provider_metadata(self):
        """Registry entries should keep the resolved provider module."""

        for cfg in get_all_configs():
            self.assertTrue(
                cfg.provider,
                f"{cfg.slug} should keep provider metadata in the registry",
            )


class CreateCustomValidatorTests(TestCase):
    """Tests that create_custom_validator() sets assertion support correctly."""

    def test_custom_validator_supports_assertions(self):
        """create_custom_validator() sets supports_assertions=True."""
        from validibot.users.tests.factories import OrganizationFactory
        from validibot.users.tests.factories import UserFactory
        from validibot.validations.constants import CustomValidatorType
        from validibot.validations.utils import create_custom_validator

        org = OrganizationFactory()
        user = UserFactory()

        custom = create_custom_validator(
            org=org,
            user=user,
            name="Test Custom Validator",
            short_description="A test custom validator",
            description="Test description",
            custom_type=CustomValidatorType.SIMPLE,
        )
        self.assertTrue(
            custom.validator.supports_assertions,
            "Custom validators should have supports_assertions=True",
        )


class BasicValidatorConfigRuntimeAlignmentTests(TestCase):
    """Verify that Basic validator config file types match runtime."""

    def test_config_file_types_match_runtime(self):
        """Basic validator config should only advertise file types the runtime supports.

        The runtime (BasicValidator._SUPPORTED_FILE_TYPES) is the source
        of truth. The config should not advertise broader support, which
        would let workflows accept submissions that later fail at runtime.
        """
        from validibot.validations.validators.basic import BasicValidator

        cfg = get_config(ValidationType.BASIC)
        config_types = set(cfg.supported_file_types)
        runtime_types = BasicValidator._SUPPORTED_FILE_TYPES

        self.assertEqual(
            config_types,
            runtime_types,
            "Config file types should match runtime _SUPPORTED_FILE_TYPES",
        )
