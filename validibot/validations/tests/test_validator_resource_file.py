"""
Tests for ValidatorResourceFile model and resource file functionality.

These tests cover:
- Model creation and properties
- Scoping (system-wide vs org-specific)
- Storage URI generation
- Integration with step configuration
- Envelope builder resource file resolution
"""

from unittest.mock import MagicMock
from unittest.mock import patch

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from validibot.users.tests.factories import OrganizationFactory
from validibot.validations.constants import ResourceFileType
from validibot.validations.constants import ValidationType
from validibot.validations.models import ValidatorResourceFile
from validibot.validations.tests.factories import ValidatorFactory

pytestmark = pytest.mark.django_db


class TestValidatorResourceFileModel:
    """Tests for ValidatorResourceFile model basics."""

    def test_create_system_wide_resource(self):
        """System-wide resources have org=NULL."""
        validator = ValidatorFactory(validation_type=ValidationType.ENERGYPLUS)
        resource = ValidatorResourceFile.objects.create(
            validator=validator,
            org=None,
            resource_type=ResourceFileType.ENERGYPLUS_WEATHER,
            name="San Francisco TMY3",
            filename="USA_CA_San.Francisco.Intl.AP.724940_TMY3.epw",
        )

        assert resource.org is None
        assert resource.is_system is True
        assert resource.resource_type == ResourceFileType.ENERGYPLUS_WEATHER

    def test_create_org_specific_resource(self):
        """Org-specific resources are scoped to one organization."""
        validator = ValidatorFactory(validation_type=ValidationType.ENERGYPLUS)
        org = OrganizationFactory()
        resource = ValidatorResourceFile.objects.create(
            validator=validator,
            org=org,
            resource_type=ResourceFileType.ENERGYPLUS_WEATHER,
            name="Custom Weather File",
            filename="custom_weather.epw",
        )

        assert resource.org == org
        assert resource.is_system is False

    def test_str_representation_system(self):
        """String representation shows scope for system resources."""
        validator = ValidatorFactory(validation_type=ValidationType.ENERGYPLUS)
        resource = ValidatorResourceFile.objects.create(
            validator=validator,
            org=None,
            resource_type=ResourceFileType.ENERGYPLUS_WEATHER,
            name="Test Weather",
            filename="test.epw",
        )

        assert "system" in str(resource)
        assert "Test Weather" in str(resource)

    def test_str_representation_org(self):
        """String representation shows scope for org resources."""
        validator = ValidatorFactory(validation_type=ValidationType.ENERGYPLUS)
        org = OrganizationFactory()
        resource = ValidatorResourceFile.objects.create(
            validator=validator,
            org=org,
            resource_type=ResourceFileType.ENERGYPLUS_WEATHER,
            name="Org Weather",
            filename="org.epw",
        )

        assert f"org:{org.id}" in str(resource)

    def test_default_ordering(self):
        """Resources are ordered by is_default (desc), then name."""
        validator = ValidatorFactory(validation_type=ValidationType.ENERGYPLUS)
        r1 = ValidatorResourceFile.objects.create(
            validator=validator,
            name="Zebra Weather",
            filename="z.epw",
            resource_type=ResourceFileType.ENERGYPLUS_WEATHER,
            is_default=False,
        )
        r2 = ValidatorResourceFile.objects.create(
            validator=validator,
            name="Alpha Weather",
            filename="a.epw",
            resource_type=ResourceFileType.ENERGYPLUS_WEATHER,
            is_default=True,
        )
        r3 = ValidatorResourceFile.objects.create(
            validator=validator,
            name="Beta Weather",
            filename="b.epw",
            resource_type=ResourceFileType.ENERGYPLUS_WEATHER,
            is_default=False,
        )

        resources = list(ValidatorResourceFile.objects.filter(validator=validator))
        # Default first, then alphabetical
        assert resources[0] == r2  # Alpha (default)
        assert resources[1] == r3  # Beta
        assert resources[2] == r1  # Zebra

    def test_metadata_field(self):
        """Metadata JSON field can store arbitrary data."""
        validator = ValidatorFactory(validation_type=ValidationType.ENERGYPLUS)
        resource = ValidatorResourceFile.objects.create(
            validator=validator,
            name="Chicago Weather",
            filename="chicago.epw",
            resource_type=ResourceFileType.ENERGYPLUS_WEATHER,
            metadata={
                "location": "Chicago, IL",
                "latitude": 41.8781,
                "longitude": -87.6298,
                "source": "TMY3",
            },
        )

        resource.refresh_from_db()
        assert resource.metadata["location"] == "Chicago, IL"
        assert resource.metadata["latitude"] == 41.8781  # noqa: PLR2004


class TestValidatorResourceFileScoping:
    """Tests for resource file visibility scoping."""

    def test_query_system_and_org_resources(self):
        """Query pattern for showing both system and org resources."""
        from django.db.models import Q

        validator = ValidatorFactory(validation_type=ValidationType.ENERGYPLUS)
        org = OrganizationFactory()

        # System-wide resource
        system_resource = ValidatorResourceFile.objects.create(
            validator=validator,
            org=None,
            name="System Weather",
            filename="system.epw",
            resource_type=ResourceFileType.ENERGYPLUS_WEATHER,
        )

        # Org-specific resource
        org_resource = ValidatorResourceFile.objects.create(
            validator=validator,
            org=org,
            name="Org Weather",
            filename="org.epw",
            resource_type=ResourceFileType.ENERGYPLUS_WEATHER,
        )

        # Another org's resource (should not be visible)
        other_org = OrganizationFactory()
        ValidatorResourceFile.objects.create(
            validator=validator,
            org=other_org,
            name="Other Org Weather",
            filename="other.epw",
            resource_type=ResourceFileType.ENERGYPLUS_WEATHER,
        )

        # Query: system-wide OR this org's resources
        visible_resources = ValidatorResourceFile.objects.filter(
            Q(org__isnull=True) | Q(org=org),
            validator=validator,
            resource_type=ResourceFileType.ENERGYPLUS_WEATHER,
        )

        assert visible_resources.count() == 2  # noqa: PLR2004
        assert system_resource in visible_resources
        assert org_resource in visible_resources

    def test_system_resources_visible_to_all_orgs(self):
        """System resources (org=NULL) are visible to any organization."""
        validator = ValidatorFactory(validation_type=ValidationType.ENERGYPLUS)

        system_resource = ValidatorResourceFile.objects.create(
            validator=validator,
            org=None,
            name="Global Weather",
            filename="global.epw",
            resource_type=ResourceFileType.ENERGYPLUS_WEATHER,
        )

        # Multiple orgs should all see the system resource
        for _ in range(3):
            org = OrganizationFactory()
            from django.db.models import Q

            visible = ValidatorResourceFile.objects.filter(
                Q(org__isnull=True) | Q(org=org),
                validator=validator,
            )
            assert system_resource in visible


class TestValidatorResourceFileStorage:
    """Tests for storage URI generation."""

    def test_get_storage_uri_local_filesystem(self):
        """Local storage returns file:// URI."""
        validator = ValidatorFactory(validation_type=ValidationType.ENERGYPLUS)

        # Create a simple uploaded file
        file_content = b"LOCATION,San Francisco"
        uploaded_file = SimpleUploadedFile(
            name="test_weather.epw",
            content=file_content,
            content_type="application/octet-stream",
        )

        resource = ValidatorResourceFile.objects.create(
            validator=validator,
            name="Test Weather",
            filename="test_weather.epw",
            resource_type=ResourceFileType.ENERGYPLUS_WEATHER,
            file=uploaded_file,
        )

        uri = resource.get_storage_uri()
        assert uri.startswith("file://")
        assert "test_weather" in uri

    def test_get_storage_uri_gcs(self):
        """GCS storage returns gs:// URI with location prefix."""
        validator = ValidatorFactory(validation_type=ValidationType.ENERGYPLUS)

        # Create resource file with actual file (for DB save)
        file_content = b"LOCATION,Test"
        uploaded_file = SimpleUploadedFile(
            name="gcs_weather.epw",
            content=file_content,
            content_type="application/octet-stream",
        )

        resource = ValidatorResourceFile.objects.create(
            validator=validator,
            name="GCS Weather",
            filename="gcs_weather.epw",
            resource_type=ResourceFileType.ENERGYPLUS_WEATHER,
            file=uploaded_file,
        )

        # Mock the file field's storage to simulate GCS with location prefix
        with patch.object(resource.file, "storage") as mock_storage:
            mock_storage.__class__.__name__ = "GoogleCloudStorage"
            mock_storage.bucket_name = "validibot-media"
            mock_storage.location = "public"  # Storage location prefix

            with patch.object(resource.file, "name", "resource_files/v123/weather.epw"):
                uri = resource.get_storage_uri()

        # URI should include the location prefix
        assert uri == "gs://validibot-media/public/resource_files/v123/weather.epw"

    def test_get_storage_uri_gcs_no_location(self):
        """GCS storage returns gs:// URI without location when not set."""
        validator = ValidatorFactory(validation_type=ValidationType.ENERGYPLUS)

        file_content = b"LOCATION,Test"
        uploaded_file = SimpleUploadedFile(
            name="gcs_weather.epw",
            content=file_content,
            content_type="application/octet-stream",
        )

        resource = ValidatorResourceFile.objects.create(
            validator=validator,
            name="GCS Weather",
            filename="gcs_weather.epw",
            resource_type=ResourceFileType.ENERGYPLUS_WEATHER,
            file=uploaded_file,
        )

        # Mock GCS storage without location prefix
        with patch.object(resource.file, "storage") as mock_storage:
            mock_storage.__class__.__name__ = "GoogleCloudStorage"
            mock_storage.bucket_name = "validibot-media"
            mock_storage.location = ""  # No location prefix

            with patch.object(resource.file, "name", "resource_files/v123/weather.epw"):
                uri = resource.get_storage_uri()

        # URI should NOT have double slashes
        assert uri == "gs://validibot-media/resource_files/v123/weather.epw"


class TestStepConfigIntegration:
    """Tests for resource file integration with step configuration."""

    def test_build_energyplus_config_excludes_resource_file_ids(self):
        """``build_energyplus_config()`` returns IDF checks and simulation flag
        only — resource files are stored relationally via WorkflowStepResource,
        not in the config dict.
        """
        from validibot.workflows.views_helpers import build_energyplus_config

        mock_form = MagicMock()
        mock_form.cleaned_data = {
            "weather_file": "some-uuid",
            "idf_checks": ["check_version"],
            "run_simulation": True,
        }

        config, _template_vars = build_energyplus_config(mock_form)

        assert "resource_file_ids" not in config
        assert config["idf_checks"] == ["check_version"]
        assert config["run_simulation"] is True

    def test_build_energyplus_config_empty_form(self):
        """Empty form data produces default config without resource_file_ids."""
        from validibot.workflows.views_helpers import build_energyplus_config

        mock_form = MagicMock()
        mock_form.cleaned_data = {
            "weather_file": "",
            "idf_checks": [],
            "run_simulation": False,
        }

        config, _template_vars = build_energyplus_config(mock_form)

        assert "resource_file_ids" not in config
        assert config["idf_checks"] == []
        assert config["run_simulation"] is False


class TestFormIntegration:
    """Tests for resource file integration with step editor forms."""

    def test_form_populates_weather_choices(self):
        """EnergyPlus form populates weather file choices from database."""
        from validibot.workflows.forms import EnergyPlusStepConfigForm

        validator = ValidatorFactory(validation_type=ValidationType.ENERGYPLUS)
        org = OrganizationFactory()

        # Create system and org resources
        system_resource = ValidatorResourceFile.objects.create(
            validator=validator,
            org=None,
            name="System Weather",
            filename="system.epw",
            resource_type=ResourceFileType.ENERGYPLUS_WEATHER,
        )
        org_resource = ValidatorResourceFile.objects.create(
            validator=validator,
            org=org,
            name="Org Weather",
            filename="org.epw",
            resource_type=ResourceFileType.ENERGYPLUS_WEATHER,
        )

        form = EnergyPlusStepConfigForm(org=org, validator=validator)

        # Get choice values (excluding the empty choice)
        choice_values = [c[0] for c in form.fields["weather_file"].choices if c[0]]

        assert str(system_resource.id) in choice_values
        assert str(org_resource.id) in choice_values

    def test_form_excludes_other_org_resources(self):
        """Form excludes resources from other organizations."""
        from validibot.workflows.forms import EnergyPlusStepConfigForm

        validator = ValidatorFactory(validation_type=ValidationType.ENERGYPLUS)
        org = OrganizationFactory()
        other_org = OrganizationFactory()

        # Create resource for other org
        other_resource = ValidatorResourceFile.objects.create(
            validator=validator,
            org=other_org,
            name="Other Org Weather",
            filename="other.epw",
            resource_type=ResourceFileType.ENERGYPLUS_WEATHER,
        )

        form = EnergyPlusStepConfigForm(org=org, validator=validator)

        choice_values = [c[0] for c in form.fields["weather_file"].choices if c[0]]

        assert str(other_resource.id) not in choice_values

    def test_form_labels_org_resources(self):
        """Form adds '(org)' suffix to org-specific resource labels."""
        from validibot.workflows.forms import EnergyPlusStepConfigForm

        validator = ValidatorFactory(validation_type=ValidationType.ENERGYPLUS)
        org = OrganizationFactory()

        ValidatorResourceFile.objects.create(
            validator=validator,
            org=org,
            name="Custom Weather",
            filename="custom.epw",
            resource_type=ResourceFileType.ENERGYPLUS_WEATHER,
        )

        form = EnergyPlusStepConfigForm(org=org, validator=validator)

        # Find the choice label for org resource
        labels = [c[1] for c in form.fields["weather_file"].choices]
        org_labels = [label for label in labels if "(org)" in label]

        assert len(org_labels) == 1
        assert "Custom Weather" in org_labels[0]
