"""Tests for THERM parsing, geometry helpers, and resolved file inputs.

The parser cases protect THMX/THMZ format detection and controlled failures.
The runtime cases prove that THERM consumes its declared ``therm_model`` port,
so an earlier-step artifact is not accidentally replaced by submission bytes.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from django.test import TestCase

from validibot.validations.validators.therm.geometry import compute_bounding_box
from validibot.validations.validators.therm.models import ThermPolygon
from validibot.validations.validators.therm.parser import parse_therm_file
from validibot.validations.validators.therm.validator import ThermValidator

FIXTURES_DIR = Path(__file__).parent / "fixtures"
SAMPLE_THMX = FIXTURES_DIR / "sample_valid.thmx"


def _read_sample_thmx() -> str:
    return SAMPLE_THMX.read_text()


def _make_thmz(thmx_content: str) -> bytes:
    """Create a THMZ (ZIP) archive from THMX content."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("model.thmx", thmx_content)
    return buf.getvalue()


# ---- Parser Tests ----


class ThermParserTests(TestCase):
    """Tests for ``parse_therm_file()`` format detection and safe failures."""

    def test_parse_thmx_format_detected(self):
        """Parser correctly identifies THMX format."""
        content = _read_sample_thmx()
        model = parse_therm_file(content, filename="test.thmx")
        assert model.source_format == "thmx"

    def test_parse_thmz_format_detected(self):
        """Parser correctly identifies THMZ (ZIP) format."""
        thmx = _read_sample_thmx()
        thmz = _make_thmz(thmx)
        model = parse_therm_file(thmz, filename="test.thmz")
        assert model.source_format == "thmz"

    def test_parse_thmz_auto_detect_zip(self):
        """THMZ auto-detected by ZIP magic bytes even without filename hint."""
        thmx = _read_sample_thmx()
        thmz = _make_thmz(thmx)
        model = parse_therm_file(thmz, filename=None)
        assert model.source_format == "thmz"

    def test_parse_invalid_xml(self):
        """Malformed XML must fail predictably instead of producing a partial model."""
        with pytest.raises(ValueError, match="Invalid XML"):
            parse_therm_file("<not valid xml<<<>>>")

    def test_parse_empty_content(self):
        """An empty file cannot be represented as a valid THERM model."""
        with pytest.raises((ValueError, Exception)):
            parse_therm_file("")


class ThermResolvedFileInputTests(TestCase):
    """Tests for the validator's declared ``therm_model`` runtime input."""

    def test_resolved_model_replaces_unrelated_submission_metadata(self):
        """Cross-step THMX bytes must be parsed even when the submission is not XML."""
        validator = ThermValidator()
        resolved_file = SimpleNamespace(
            content=_read_sample_thmx().encode(),
            name="earlier-step-model.thmx",
        )
        validator.run_context = SimpleNamespace(
            resolved_file_inputs={"therm_model": resolved_file},
        )
        unrelated_submission = SimpleNamespace(file_type="json")

        assert validator.validate_file_type(unrelated_submission) is None
        parsed = validator.parse_content(unrelated_submission)

        assert parsed.source_format == "thmx"

    def test_resolved_archive_uses_its_own_filename_and_bytes(self):
        """A THMZ artifact must retain ZIP detection independent of the submission."""
        validator = ThermValidator()
        validator.run_context = SimpleNamespace(
            resolved_file_inputs={
                "therm_model": SimpleNamespace(
                    content=_make_thmz(_read_sample_thmx()),
                    name="earlier-step-model.thmz",
                ),
            },
        )

        parsed = validator.parse_content(SimpleNamespace(file_type="text"))

        assert parsed.source_format == "thmz"


# ---- Geometry Tests ----


class ThermGeometryTests(TestCase):
    """Tests for bounding-box calculations used by THERM geometry checks."""

    def test_compute_bounding_box(self):
        """A closed polygon should report its exact width and height."""
        poly = ThermPolygon(
            id="1",
            material_id="mat",
            vertices=[(0, 0), (10, 0), (10, 10), (0, 10), (0, 0)],
        )
        width, height = compute_bounding_box([poly])
        assert width == 10.0  # noqa: PLR2004
        assert height == 10.0  # noqa: PLR2004

    def test_compute_bounding_box_empty(self):
        """No geometry should produce a neutral zero-sized bounding box."""
        width, height = compute_bounding_box([])
        assert width == 0.0
        assert height == 0.0

    def test_compute_bounding_box_multiple_polygons(self):
        """The combined box must span every polygon rather than only the first."""
        p1 = ThermPolygon(
            id="1",
            material_id="m",
            vertices=[(0, 0), (10, 0), (10, 10), (0, 10), (0, 0)],
        )
        p2 = ThermPolygon(
            id="2",
            material_id="m",
            vertices=[(10, 0), (30, 0), (30, 20), (10, 20), (10, 0)],
        )
        width, height = compute_bounding_box([p1, p2])
        assert width == 30.0  # noqa: PLR2004
        assert height == 20.0  # noqa: PLR2004
