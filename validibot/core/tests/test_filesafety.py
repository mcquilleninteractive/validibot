"""
Tests for core file safety utilities.

Covers:
- sanitize_filename: path traversal, control chars, dotfiles, length, unicode
- force_extension: extension replacement based on content type
- build_safe_filename: end-to-end sanitization + extension enforcement
- detect_suspicious_magic: binary format detection
- sha256_hexdigest: checksum computation
"""

from __future__ import annotations

import hashlib

from validibot.core.filesafety import build_safe_filename
from validibot.core.filesafety import detect_suspicious_magic
from validibot.core.filesafety import force_extension
from validibot.core.filesafety import sanitize_filename
from validibot.core.filesafety import sha256_hexdigest

# ---------------------------------------------------------------------------
# sanitize_filename
# ---------------------------------------------------------------------------


class TestSanitizeFilename:
    """Tests for sanitize_filename()."""

    def test_simple_name_unchanged(self):
        """A simple alphanumeric filename passes through unchanged."""
        assert sanitize_filename("report.csv") == "report.csv"

    def test_strips_directory_components(self):
        """Path traversal attempts are reduced to basename only."""
        assert sanitize_filename("../../etc/passwd") == "passwd"
        assert sanitize_filename("/var/data/secret.txt") == "secret.txt"

    def test_strips_windows_directory_components(self):
        """Windows-style backslashes are replaced with underscores on Unix.

        On macOS/Linux, Path() doesn't treat backslashes as separators, so
        the unsafe-char regex replaces them with underscores instead. The key
        guarantee is that no directory traversal occurs.
        """
        result = sanitize_filename("C:\\Users\\evil\\malware.exe")
        # No path traversal — result is a flat filename
        assert "/" not in result
        assert "\\" not in result
        assert result.endswith(".exe")

    def test_removes_control_characters(self):
        """ASCII control characters (< 0x20, 0x7F) are stripped."""
        assert sanitize_filename("file\x00name.txt") == "filename.txt"
        assert sanitize_filename("file\x1fname.txt") == "filename.txt"
        # DEL (0x7F) is also removed
        assert sanitize_filename("file\x7fname.txt") == "filename.txt"

    def test_replaces_unsafe_characters(self):
        """Characters outside the safe set are replaced with underscores."""
        assert sanitize_filename("file;name&here.txt") == "file_name_here.txt"
        assert sanitize_filename("file'name\".txt") == "file_name_.txt"

    def test_collapses_whitespace(self):
        """Multiple spaces collapse to a single space; tabs become underscores."""
        assert sanitize_filename("file   name.txt") == "file name.txt"
        # Tabs are allowed through the control-char filter but the safe-char
        # regex replaces them with underscores (tabs aren't in the safe set)
        assert sanitize_filename("file\t\tname.txt") == "file_name.txt"

    def test_removes_leading_dots(self):
        """Leading dots (hidden files/dotfile exploits) are stripped."""
        assert sanitize_filename(".hidden") == "hidden"
        assert sanitize_filename("...sneaky.txt") == "sneaky.txt"

    def test_removes_trailing_dots(self):
        """Trailing dots (invalid on Windows) are stripped."""
        assert sanitize_filename("file...") == "file"

    def test_truncates_long_names(self):
        """Filenames longer than the max length are truncated."""
        max_length = 100
        long_name = "a" * 150 + ".txt"
        result = sanitize_filename(long_name)
        assert len(result) <= max_length

    def test_empty_string_returns_fallback(self):
        """Empty string returns the fallback name."""
        assert sanitize_filename("") == "document"
        assert sanitize_filename("", fallback="upload") == "upload"

    def test_none_returns_fallback(self):
        """None returns the fallback name."""
        assert sanitize_filename(None) == "document"

    def test_all_dots_returns_fallback(self):
        """A filename of only dots returns the fallback."""
        assert sanitize_filename("...") == "document"

    def test_all_unsafe_chars_returns_fallback(self):
        """A filename of only unsafe chars returns the fallback after stripping."""
        # All chars below 0x20 are stripped, leaving empty -> fallback
        assert sanitize_filename("\x01\x02\x03") == "document"

    def test_unicode_normalization(self):
        """Unicode is normalized to NFKC form."""
        # NFKC normalizes full-width A (U+FF21) to regular A
        assert sanitize_filename("\uff21file.txt") == "Afile.txt"

    def test_preserves_extension(self):
        """The file extension is preserved through sanitization."""
        assert sanitize_filename("my report (2).csv") == "my report (2).csv"

    def test_custom_fallback(self):
        """Custom fallback is used when name sanitizes to empty."""
        assert sanitize_filename("", fallback="weather") == "weather"


# ---------------------------------------------------------------------------
# force_extension
# ---------------------------------------------------------------------------


class TestForceExtension:
    """Tests for force_extension()."""

    def test_matching_extension_unchanged(self):
        """Correct extension is not replaced."""
        assert (
            force_extension("data.json", content_type="application/json") == "data.json"
        )

    def test_mismatched_extension_replaced(self):
        """Wrong extension is replaced with the correct one."""
        assert (
            force_extension("data.txt", content_type="application/json") == "data.json"
        )

    def test_case_insensitive_match(self):
        """Extension matching is case-insensitive."""
        assert (
            force_extension("data.JSON", content_type="application/json") == "data.JSON"
        )

    def test_no_extension_gets_one(self):
        """A filename with no extension gets the correct one appended."""
        assert force_extension("data", content_type="application/json") == "data.json"

    def test_unknown_content_type_uses_default(self):
        """Unknown content type falls back to default_ext or .txt."""
        assert (
            force_extension("file.dat", content_type="application/weird") == "file.txt"
        )

    def test_unknown_content_type_with_custom_default(self):
        """Unknown content type uses the provided default_ext."""
        result = force_extension(
            "file.dat",
            content_type="application/weird",
            default_ext=".bin",
        )
        assert result == "file.bin"

    def test_xml_content_type(self):
        """XML content type maps to .xml extension."""
        assert (
            force_extension("schema.txt", content_type="application/xml")
            == "schema.xml"
        )

    def test_idf_content_type(self):
        """IDF content type maps to .idf extension."""
        assert force_extension("model.txt", content_type="text/x-idf") == "model.idf"

    def test_rdf_content_types_preserve_rdf_extensions(self):
        """RDF MIME types keep RDF-specific extensions instead of becoming .txt."""
        assert force_extension("model.txt", content_type="text/turtle") == "model.ttl"
        assert (
            force_extension("model.txt", content_type="application/ld+json")
            == "model.jsonld"
        )
        assert (
            force_extension("model.xml", content_type="application/rdf+xml")
            == "model.rdf"
        )

    def test_pdf_content_type_uses_pdf_extension(self):
        """PDF uploads retain the extension required by the isolated parser."""
        assert (
            force_extension("package.bin", content_type="application/pdf")
            == "package.pdf"
        )


# ---------------------------------------------------------------------------
# build_safe_filename
# ---------------------------------------------------------------------------


class TestBuildSafeFilename:
    """Tests for build_safe_filename()."""

    def test_sanitizes_and_enforces_extension(self):
        """Combines sanitization with extension enforcement."""
        result = build_safe_filename(
            "../../evil.exe",
            content_type="application/json",
        )
        assert result == "evil.json"

    def test_traversal_plus_wrong_extension(self):
        """Path traversal and wrong extension are both fixed."""
        result = build_safe_filename(
            "/var/../uploads/hack.php",
            content_type="text/plain",
        )
        assert result == "hack.txt"

    def test_empty_name_uses_fallback_with_extension(self):
        """Empty name uses fallback, then gets correct extension."""
        result = build_safe_filename(
            "",
            content_type="application/json",
            fallback="upload",
        )
        assert result == "upload.json"

    def test_preserves_correct_extension(self):
        """A clean filename with the right extension passes through."""
        result = build_safe_filename(
            "weather_data.json",
            content_type="application/json",
        )
        assert result == "weather_data.json"

    def test_preserves_turtle_upload_extension(self):
        """Turtle uploads must not be renamed to .txt before SHACL parsing."""
        result = build_safe_filename(
            "223p_example_building.ttl",
            content_type="text/turtle",
        )
        assert result == "223p_example_building.ttl"


# ---------------------------------------------------------------------------
# detect_suspicious_magic
# ---------------------------------------------------------------------------


class TestDetectSuspiciousMagic:
    """Tests for detect_suspicious_magic()."""

    def test_zip_archive_detected(self):
        """ZIP magic bytes (PK\\x03\\x04) are detected."""
        assert detect_suspicious_magic(b"PK\x03\x04rest of zip") is True

    def test_pdf_detected(self):
        """PDF magic bytes (%PDF) are detected."""
        assert detect_suspicious_magic(b"%PDF-1.5 rest of pdf") is True

    def test_elf_detected(self):
        """ELF magic bytes (\\x7fELF) are detected."""
        assert detect_suspicious_magic(b"\x7fELF\x02\x01\x01") is True

    def test_windows_pe_detected(self):
        """Windows PE magic bytes (MZ) are detected."""
        assert detect_suspicious_magic(b"MZ\x90\x00\x03\x00") is True

    def test_macho_detected(self):
        """Mach-O magic bytes are detected."""
        # 64-bit little-endian
        assert detect_suspicious_magic(b"\xcf\xfa\xed\xfe") is True
        # Universal binary / Java class
        assert detect_suspicious_magic(b"\xca\xfe\xba\xbe") is True

    def test_shebang_detected(self):
        """Shell script shebang (#!) is detected."""
        assert detect_suspicious_magic(b"#!/bin/bash\necho hi") is True
        assert detect_suspicious_magic(b"#!/usr/bin/env python") is True

    def test_plain_text_not_detected(self):
        """Plain text content is not flagged."""
        assert detect_suspicious_magic(b"Hello, world!") is False

    def test_json_not_detected(self):
        """JSON content is not flagged."""
        assert detect_suspicious_magic(b'{"key": "value"}') is False

    def test_xml_not_detected(self):
        """XML content is not flagged."""
        assert detect_suspicious_magic(b"<?xml version='1.0'?>") is False

    def test_epw_not_detected(self):
        """EnergyPlus weather file content is not flagged."""
        assert detect_suspicious_magic(b"LOCATION,San Francisco") is False

    def test_idf_not_detected(self):
        """EnergyPlus IDF content is not flagged."""
        assert detect_suspicious_magic(b"!- EnergyPlus model") is False

    def test_empty_bytes_not_detected(self):
        """Empty bytes are not flagged."""
        assert detect_suspicious_magic(b"") is False

    def test_short_input_handled(self):
        """Input shorter than magic prefix length is handled safely."""
        # "MZ" is only 2 bytes; should still detect
        assert detect_suspicious_magic(b"MZ") is True
        # Single byte that starts like a prefix but is too short
        assert detect_suspicious_magic(b"P") is False


# ---------------------------------------------------------------------------
# sha256_hexdigest
# ---------------------------------------------------------------------------


class TestSha256Hexdigest:
    """Tests for sha256_hexdigest()."""

    def test_known_hash(self):
        """Matches the known SHA-256 of 'hello'."""
        expected = "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
        assert sha256_hexdigest(b"hello") == expected

    def test_empty_bytes(self):
        """Hash of empty bytes matches hashlib directly."""
        expected = hashlib.sha256(b"").hexdigest()
        assert sha256_hexdigest(b"") == expected

    def test_returns_lowercase_hex(self):
        """Result is always lowercase hex, 64 characters."""
        sha256_hex_length = 64
        result = sha256_hexdigest(b"test data")
        assert len(result) == sha256_hex_length
        assert result == result.lower()
        assert all(c in "0123456789abcdef" for c in result)

    def test_different_inputs_different_hashes(self):
        """Different inputs produce different hashes."""
        assert sha256_hexdigest(b"a") != sha256_hexdigest(b"b")
