"""Verify HTTP request-mode detection at the workflow launch boundary.

These tests ensure supported MIME types enter the expected parser path while
malformed JSON, missing headers, and unregistered media types fail explicitly.
"""

from types import SimpleNamespace

from validibot.workflows.request_utils import SubmissionRequestMode
from validibot.workflows.request_utils import detect_mode
from validibot.workflows.request_utils import extract_request_basics


def make_request(*, body: bytes, content_type: str):
    """Build the minimal request shape consumed by the detection helpers."""
    return SimpleNamespace(body=body, content_type=content_type)


def test_detect_mode_raw_json():
    """A plain JSON document must not be mistaken for a JSON envelope."""
    req = make_request(body=b'{"hello": "world"}', content_type="application/json")
    content_type, body = extract_request_basics(req)
    result = detect_mode(req, content_type, body)
    assert result.mode is SubmissionRequestMode.RAW_BODY
    assert not result.has_error


def test_detect_mode_json_envelope():
    """A JSON object with content selects the structured envelope path."""
    req = make_request(
        body=b'{"content": "<root/>", "content_type": "application/xml"}',
        content_type="application/json",
    )
    content_type, body = extract_request_basics(req)
    result = detect_mode(req, content_type, body)
    assert result.mode is SubmissionRequestMode.JSON_ENVELOPE
    assert result.parsed_envelope["content"] == "<root/>"


def test_detect_mode_invalid_json_records_error():
    """Malformed JSON must fail during mode detection with useful context."""
    req = make_request(body=b"{invalid-json", content_type="application/json")
    content_type, body = extract_request_basics(req)
    result = detect_mode(req, content_type, body)
    assert result.mode is SubmissionRequestMode.UNKNOWN
    assert result.has_error
    assert "Invalid JSON payload" in (result.error or "")


def test_detect_mode_multipart():
    """Multipart uploads are routed without parsing their body as JSON."""
    req = make_request(
        body=b"",
        content_type="multipart/form-data; boundary=abc123",
    )
    content_type, body = extract_request_basics(req)
    result = detect_mode(req, content_type, body)
    assert result.mode is SubmissionRequestMode.MULTIPART


def test_detect_mode_pdf_raw_body():
    """PDF has a first-class MIME mapping and enters the raw-body path."""
    req = make_request(body=b"%PDF-2.0", content_type="application/pdf")
    content_type, body = extract_request_basics(req)
    result = detect_mode(req, content_type, body)
    assert result.mode is SubmissionRequestMode.RAW_BODY
    assert not result.has_error


def test_detect_mode_unsupported_content_type():
    """Unregistered MIME types remain outside every submission mode."""
    req = make_request(body=b"", content_type="application/x-unsupported")
    content_type, body = extract_request_basics(req)
    result = detect_mode(req, content_type, body)
    assert result.mode is SubmissionRequestMode.UNKNOWN
    assert result.has_error
    assert "Unsupported Content-Type" in (result.error or "")


def test_detect_mode_missing_content_type():
    """A request without a MIME type cannot select a safe parser path."""
    req = make_request(body=b"", content_type="")
    content_type, body = extract_request_basics(req)
    result = detect_mode(req, content_type, body)
    assert result.mode is SubmissionRequestMode.UNKNOWN
    assert result.has_error
    assert "Missing Content-Type" in (result.error or "")
