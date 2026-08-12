"""Security and reliability tests for OpenAI temporary-file retrieval.

ChatGPT supplies a short-lived URL rather than embedding file bytes in an MCP
request. These tests prove the downloader remains bounded, credential-free,
redirect-aware, and resistant to obvious server-side request forgery targets.
"""

from __future__ import annotations

import socket

import httpx
import pytest

from validibot.mcp_server.constants import MCPErrorCode
from validibot.mcp_server.exceptions import MCPApplicationError
from validibot.mcp_server.file_downloads import download_openai_file
from validibot.mcp_server.schemas import OpenAIFileInput


@pytest.fixture(autouse=True)
def _resolve_example_hosts_to_public_addresses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep tests offline while exercising the production DNS policy."""

    def public_address(*args: object, **kwargs: object) -> list[tuple[object, ...]]:
        """Return one documentation-only public IPv4 address for any host."""

        del args, kwargs
        return [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("93.184.216.34", 443),
            ),
        ]

    monkeypatch.setattr(socket, "getaddrinfo", public_address)


def _file(**updates: str | None) -> OpenAIFileInput:
    """Build the exact OpenAI file object with focused per-test overrides."""

    values: dict[str, str | None] = {
        "download_url": "https://files.openai.example/temporary",
        "file_id": "file-validibot-test",
        "mime_type": "application/json",
        "file_name": "payload.json",
    }
    values.update(updates)
    return OpenAIFileInput.model_validate(values)


def test_download_returns_openai_metadata_without_forwarding_credentials() -> None:
    """Temporary file retrieval must preserve metadata but never application auth."""

    def handler(request: httpx.Request) -> httpx.Response:
        """Inspect the outbound request before returning representative content."""

        assert request.headers.get("Authorization") is None
        assert request.headers.get("Cookie") is None
        assert request.headers["Accept"] == "*/*"
        return httpx.Response(
            200,
            headers={"Content-Type": "text/plain; charset=utf-8"},
            content=b"{}",
        )

    downloaded = download_openai_file(
        _file(),
        transport=httpx.MockTransport(handler),
    )

    assert downloaded.file_name == "payload.json"
    assert downloaded.content_type == "application/json"
    assert downloaded.content == b"{}"


def test_download_uses_conservative_optional_metadata_fallbacks() -> None:
    """Missing optional OpenAI fields must not require trusting response filenames."""

    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            headers={"Content-Type": "text/plain; charset=utf-8"},
            content=b"content",
            request=request,
        ),
    )

    downloaded = download_openai_file(
        _file(file_name=None, mime_type=None),
        transport=transport,
    )

    assert downloaded.file_name == "upload.bin"
    assert downloaded.content_type == "text/plain"


@pytest.mark.parametrize(
    "url",
    [
        "http://files.openai.example/file",
        "https://user:password@files.openai.example/file",
        "https://files.openai.example:8443/file",
        "https://files.openai.example/file#fragment",
        "https://127.0.0.1/file",
        "https://169.254.169.254/latest/meta-data",
    ],
)
def test_download_rejects_unsafe_url_shapes(url: str) -> None:
    """Model-controlled URLs must not reach insecure or local network targets."""

    with pytest.raises(MCPApplicationError) as rejected:
        download_openai_file(
            _file(download_url=url),
            transport=httpx.MockTransport(lambda request: httpx.Response(200)),
        )

    assert rejected.value.code == MCPErrorCode.INVALID_INPUT


def test_download_rechecks_redirect_targets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A public first hop must not be allowed to redirect into a private network."""

    def mixed_dns(
        host: str,
        *args: object,
        **kwargs: object,
    ) -> list[tuple[object, ...]]:
        """Resolve the redirect hostname to a private address only."""

        del args, kwargs
        address = "10.0.0.5" if host == "internal.example" else "93.184.216.34"
        return [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                (address, 443),
            ),
        ]

    monkeypatch.setattr(socket, "getaddrinfo", mixed_dns)
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            302,
            headers={"Location": "https://internal.example/private"},
            request=request,
        ),
    )

    with pytest.raises(MCPApplicationError) as rejected:
        download_openai_file(_file(), transport=transport)

    assert rejected.value.code == MCPErrorCode.INVALID_INPUT


def test_download_rejects_declared_and_streamed_oversize_content(settings) -> None:
    """Both honest and undeclared oversized responses must stop before validation."""

    settings.MCP_FILE_MAX_BYTES = 2
    declared = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            headers={"Content-Length": "3"},
            stream=httpx.ByteStream(b"abc"),
            request=request,
        ),
    )
    streamed = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            stream=httpx.ByteStream(b"abc"),
            request=request,
        ),
    )

    for transport in (declared, streamed):
        with pytest.raises(MCPApplicationError) as oversized:
            download_openai_file(_file(), transport=transport)
        assert oversized.value.code == MCPErrorCode.FILE_TOO_LARGE


def test_download_rejects_empty_files_and_bad_metadata() -> None:
    """Empty content and unsafe leaf metadata must fail with curated errors."""

    empty_transport = httpx.MockTransport(
        lambda request: httpx.Response(200, content=b"", request=request),
    )
    with pytest.raises(MCPApplicationError) as empty:
        download_openai_file(_file(), transport=empty_transport)
    assert empty.value.code == MCPErrorCode.INVALID_INPUT

    with pytest.raises(MCPApplicationError) as unsafe_name:
        download_openai_file(
            _file(file_name="../secret.txt"),
            transport=empty_transport,
        )
    assert unsafe_name.value.code == MCPErrorCode.INVALID_INPUT


def test_download_maps_network_failures_to_retryable_safe_errors() -> None:
    """Transport failures must remain retryable without leaking internal details."""

    def fail(request: httpx.Request) -> httpx.Response:
        """Represent a failed connection without performing network I/O."""

        raise httpx.ConnectError("secret upstream detail", request=request)

    with pytest.raises(MCPApplicationError) as unavailable:
        download_openai_file(_file(), transport=httpx.MockTransport(fail))

    assert unavailable.value.code == MCPErrorCode.TEMPORARILY_UNAVAILABLE
    assert "secret upstream detail" not in unavailable.value.detail
