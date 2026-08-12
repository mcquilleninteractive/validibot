"""
Tests for platform-wide SiteSettings.

Verifies the singleton loading pattern, default field values, and the
metadata policy enforcement logic that protects the submission API from
oversized or overly-nested metadata payloads.
"""

import pytest

from validibot.core.models import MetadataPolicyError
from validibot.core.models import SiteSettings
from validibot.core.site_settings import get_site_settings

pytestmark = pytest.mark.django_db

DEFAULT_MAX_BYTES = 4096
DEFAULT_MAX_DEPTH = 8


class TestGetSiteSettings:
    """Verify the singleton loading helper."""

    def test_creates_default_record(self):
        """First call should create the singleton row with field defaults."""
        SiteSettings.objects.all().delete()
        obj = get_site_settings()
        assert obj.metadata_max_bytes == DEFAULT_MAX_BYTES
        assert obj.metadata_max_depth == DEFAULT_MAX_DEPTH
        assert obj.metadata_key_value_only is False
        assert SiteSettings.objects.count() == 1

    def test_returns_existing_record(self):
        """Subsequent calls should return the same row, not create a new one."""
        SiteSettings.objects.all().delete()
        first = get_site_settings()
        second = get_site_settings()
        assert first.pk == second.pk
        assert SiteSettings.objects.count() == 1


class TestEnforceMetadataPolicy:
    """Verify the metadata policy enforcement on the model."""

    def test_scalar_only_blocks_nested_dict(self):
        """When key-value-only is enabled, a nested dict should be rejected."""
        obj = SiteSettings(metadata_key_value_only=True)
        with pytest.raises(MetadataPolicyError):
            obj.enforce_metadata_policy({"nested": {"oops": True}})

    def test_scalar_only_blocks_nested_list(self):
        """When key-value-only is enabled, a list value should be rejected."""
        obj = SiteSettings(metadata_key_value_only=True)
        with pytest.raises(MetadataPolicyError):
            obj.enforce_metadata_policy({"tags": ["a", "b"]})

    def test_scalar_only_allows_scalars(self):
        """Scalar values (str, int, bool) should pass when enforcement is on."""
        obj = SiteSettings(metadata_key_value_only=True)
        obj.enforce_metadata_policy({"name": "test", "count": 5, "ok": True})

    def test_max_bytes_enforced(self):
        """Metadata exceeding the byte limit should be rejected."""
        obj = SiteSettings(metadata_max_bytes=10)
        with pytest.raises(MetadataPolicyError):
            obj.enforce_metadata_policy({"big": "x" * 50})

    def test_max_bytes_zero_disables_limit(self):
        """A zero byte limit should disable the size check entirely."""
        obj = SiteSettings(metadata_max_bytes=0)
        obj.enforce_metadata_policy({"big": "x" * 10000})

    def test_default_settings_allow_reasonable_metadata(self):
        """Default settings (4096 bytes, no key-value restriction) should
        allow normal metadata payloads."""
        obj = SiteSettings()
        obj.enforce_metadata_policy({"source": "api", "version": "1.0"})

    def test_max_depth_allows_a_value_at_the_limit(self):
        """A bounded nested object remains usable through the configured depth."""
        obj = SiteSettings(metadata_max_depth=3)

        obj.enforce_metadata_policy({"asset": {"properties": "present"}})

    def test_max_depth_rejects_a_deeper_container(self):
        """Deep metadata is rejected before validators can consume excessive work."""
        obj = SiteSettings(metadata_max_depth=2)

        with pytest.raises(MetadataPolicyError, match="maximum depth of 2"):
            obj.enforce_metadata_policy({"asset": {"properties": {"height": 3}}})

    def test_zero_max_depth_disables_only_the_policy_limit(self):
        """Operators can lift the depth ceiling without weakening JSON checks."""
        obj = SiteSettings(metadata_max_depth=0)

        obj.enforce_metadata_policy({"a": {"b": {"c": {"d": True}}}})

    @pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
    def test_non_finite_numbers_are_rejected(self, value):
        """Non-standard numeric tokens never enter canonical metadata JSON."""
        obj = SiteSettings()

        with pytest.raises(MetadataPolicyError, match="finite JSON numbers"):
            obj.enforce_metadata_policy({"measurements": [value]})

    def test_byte_limit_uses_compact_unicode_preserving_canonical_bytes(self):
        """Admission size accounting matches the runtime virtual document bytes."""
        obj = SiteSettings(metadata_max_bytes=len('{"name":"Café"}'.encode()))

        obj.enforce_metadata_policy({"name": "Café"})

    def test_cyclic_internal_metadata_is_rejected_safely(self):
        """Trusted internal callers cannot trigger an unbounded structural walk."""
        metadata = {}
        metadata["self"] = metadata
        obj = SiteSettings(metadata_max_depth=0)

        with pytest.raises(MetadataPolicyError, match="must not contain cycles"):
            obj.enforce_metadata_policy(metadata)
