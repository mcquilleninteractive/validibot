"""Security and reliability tests for OpenAI temporary-file retrieval.

ChatGPT supplies a short-lived URL rather than embedding file bytes in an MCP
request. These tests prove the downloader remains bounded, credential-free,
redirect-aware, and resistant to obvious server-side request forgery targets.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
from dns import asyncresolver

from validibot.mcp_server.constants import MCPErrorCode
from validibot.mcp_server.exceptions import MCPApplicationError
from validibot.mcp_server.file_downloads import download_openai_file
from validibot.mcp_server.schemas import OpenAIFileInput


@pytest.fixture(autouse=True)
def _resolve_example_hosts_to_public_addresses(
    monkeypatch: pytest.MonkeyPatch,
    settings,
) -> None:
    """Keep tests offline while exercising the production DNS policy."""

    settings.MCP_FILE_ALLOWED_HOSTS = ["files.openai.example"]

    class Answers:
        """Expose the small dnspython answer interface used by production."""

        def __init__(self, *addresses: str) -> None:
            self.values = addresses

        def addresses(self):
            """Yield configured address strings like dnspython HostAnswers."""

            return iter(self.values)

    async def public_address(*args: object, **kwargs: object) -> Answers:
        """Return one documentation-only public IPv4 address for any host."""

        del args, kwargs
        return Answers("93.184.216.34")

    monkeypatch.setattr(asyncresolver, "resolve_name", public_address)


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
        assert request.headers["Host"] == "files.openai.example"
        assert request.url.host == "93.184.216.34"
        assert request.extensions["sni_hostname"] == "files.openai.example"
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
    settings,
) -> None:
    """A public first hop must not be allowed to redirect into a private network."""

    settings.MCP_FILE_ALLOWED_HOSTS = ["files.openai.example", "internal.example"]

    class Answers:
        """Expose one address through dnspython's production interface."""

        def __init__(self, address: str) -> None:
            self.address = address

        def addresses(self):
            """Yield the configured address."""

            return iter([self.address])

    async def mixed_dns(
        host: str,
        *args: object,
        **kwargs: object,
    ) -> Answers:
        """Resolve the redirect hostname to a private address only."""

        del args, kwargs
        address = "10.0.0.5" if host == "internal.example" else "93.184.216.34"
        return Answers(address)

    monkeypatch.setattr(asyncresolver, "resolve_name", mixed_dns)
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


def test_download_pins_the_single_validated_dns_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DNS rebinding must not create a second lookup at connection time."""

    lookups = 0

    class Answers:
        """Expose one address through dnspython's production interface."""

        def __init__(self, address: str) -> None:
            self.address = address

        def addresses(self):
            """Yield the configured address."""

            return iter([self.address])

    async def rebinding_dns(
        *args: object,
        **kwargs: object,
    ) -> Answers:
        """Return public DNS once and a forbidden address on any later lookup."""

        nonlocal lookups
        del args, kwargs
        lookups += 1
        address = "93.184.216.34" if lookups == 1 else "169.254.169.254"
        return Answers(address)

    def handler(request: httpx.Request) -> httpx.Response:
        """Prove the transport received the validated IP, not the DNS hostname."""

        assert request.url.host == "93.184.216.34"
        assert request.headers["Host"] == "files.openai.example"
        assert request.extensions["sni_hostname"] == "files.openai.example"
        return httpx.Response(200, content=b"{}", request=request)

    monkeypatch.setattr(asyncresolver, "resolve_name", rebinding_dns)

    downloaded = download_openai_file(
        _file(),
        transport=httpx.MockTransport(handler),
    )

    assert downloaded.content == b"{}"
    assert lookups == 1


def test_download_retries_only_other_prevalidated_public_addresses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A connection failure may fail over without re-resolving the hostname."""

    lookups = 0
    attempted_hosts: list[str] = []

    class Answers:
        """Expose multiple addresses through dnspython's answer interface."""

        def addresses(self):
            """Yield deliberately reversed public candidates."""

            return iter(["93.184.216.35", "93.184.216.34"])

    async def two_public_addresses(
        *args: object,
        **kwargs: object,
    ) -> Answers:
        """Return two fixed public candidates in deliberately reversed order."""

        nonlocal lookups
        del args, kwargs
        lookups += 1
        return Answers()

    def handler(request: httpx.Request) -> httpx.Response:
        """Fail the first pinned address and accept the second one."""

        attempted_hosts.append(request.url.host)
        if request.url.host == "93.184.216.34":
            raise httpx.ConnectError("first candidate unavailable", request=request)
        return httpx.Response(200, content=b"{}", request=request)

    monkeypatch.setattr(asyncresolver, "resolve_name", two_public_addresses)

    downloaded = download_openai_file(
        _file(),
        transport=httpx.MockTransport(handler),
    )

    assert downloaded.content == b"{}"
    assert attempted_hosts == ["93.184.216.34", "93.184.216.35"]
    assert lookups == 1


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


def test_download_rejects_hosts_outside_the_exact_allowlist(settings) -> None:
    """A signed-in model cannot turn attachment retrieval into a public proxy."""

    settings.MCP_FILE_ALLOWED_HOSTS = ["files.openai.example"]
    with pytest.raises(MCPApplicationError) as rejected:
        download_openai_file(
            _file(download_url="https://unapproved.example/file"),
            transport=httpx.MockTransport(lambda request: httpx.Response(200)),
        )

    assert rejected.value.code == MCPErrorCode.INVALID_INPUT


def test_download_total_deadline_includes_dns(
    monkeypatch: pytest.MonkeyPatch,
    settings,
) -> None:
    """A deliberately slow resolver must not strand attachment capacity."""

    settings.MCP_FILE_DOWNLOAD_TOTAL_TIMEOUT_SECONDS = 0.01

    async def slow_resolution(*args: object, **kwargs: object):
        """Outlive the complete operation deadline without blocking a thread."""

        del args, kwargs
        await asyncio.sleep(1)

    monkeypatch.setattr(asyncresolver, "resolve_name", slow_resolution)

    with pytest.raises(MCPApplicationError) as unavailable:
        download_openai_file(
            _file(),
            transport=httpx.MockTransport(lambda request: httpx.Response(200)),
        )

    assert unavailable.value.code == MCPErrorCode.TEMPORARILY_UNAVAILABLE


def test_download_caps_connection_attempts_after_validating_all_dns_answers(
    monkeypatch: pytest.MonkeyPatch,
    settings,
) -> None:
    """A host with many public records cannot multiply outbound work unboundedly."""

    settings.MCP_FILE_DOWNLOAD_MAX_ADDRESSES = 2
    attempts: list[str] = []

    class Answers:
        """Represent an unusually large but entirely public DNS answer."""

        def addresses(self):
            """Yield four public documentation addresses."""

            return iter(
                [
                    "93.184.216.34",
                    "93.184.216.35",
                    "93.184.216.36",
                    "93.184.216.37",
                ],
            )

    async def many_addresses(*args: object, **kwargs: object) -> Answers:
        """Return more candidates than the configured connection budget."""

        del args, kwargs
        return Answers()

    def fail(request: httpx.Request) -> httpx.Response:
        """Record each bounded connection attempt."""

        attempts.append(request.url.host)
        raise httpx.ConnectError("unavailable", request=request)

    monkeypatch.setattr(asyncresolver, "resolve_name", many_addresses)

    with pytest.raises(MCPApplicationError):
        download_openai_file(_file(), transport=httpx.MockTransport(fail))

    assert attempts == ["93.184.216.34", "93.184.216.35"]
