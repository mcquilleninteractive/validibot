"""Bounded retrieval of temporary files supplied through OpenAI tool calls."""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from http import HTTPStatus
from typing import TYPE_CHECKING
from urllib.parse import SplitResult
from urllib.parse import urljoin
from urllib.parse import urlsplit
from urllib.parse import urlunsplit

import httpx
from django.conf import settings

from validibot.mcp_server.constants import MCP_DEFAULT_FILE_MAX_BYTES
from validibot.mcp_server.constants import MCP_FILE_DOWNLOAD_MAX_REDIRECTS
from validibot.mcp_server.constants import MCP_FILE_DOWNLOAD_TIMEOUT_SECONDS
from validibot.mcp_server.constants import MCPErrorCode
from validibot.mcp_server.exceptions import MCPApplicationError

if TYPE_CHECKING:
    from validibot.mcp_server.schemas import OpenAIFileInput

_MAX_METADATA_LENGTH = 255


@dataclass(frozen=True, slots=True)
class DownloadedFile:
    """Validated file content and metadata ready for the launch service."""

    file_name: str
    content_type: str
    content: bytes


def download_openai_file(
    file: OpenAIFileInput,
    *,
    transport: httpx.BaseTransport | None = None,
) -> DownloadedFile:
    """Download one temporary OpenAI file without forwarding user credentials."""

    file_name = _validated_file_name(file.file_name)
    url = file.download_url
    limit = int(getattr(settings, "MCP_FILE_MAX_BYTES", MCP_DEFAULT_FILE_MAX_BYTES))
    try:
        with httpx.Client(
            follow_redirects=False,
            timeout=MCP_FILE_DOWNLOAD_TIMEOUT_SECONDS,
            transport=transport,
            trust_env=False,
            # Redirects may change the TLS hostname while resolving to the same
            # address. Do not let the IP-address origin key cause a connection
            # authenticated for one hostname to be reused for another.
            limits=httpx.Limits(max_keepalive_connections=0),
        ) as client:
            response = _request_with_safe_redirects(client=client, url=url)
            try:
                content = _read_bounded_content(response=response, limit=limit)
                content_type = _validated_content_type(
                    file.mime_type or response.headers.get("Content-Type"),
                )
            finally:
                response.close()
    except MCPApplicationError:
        raise
    except (httpx.HTTPError, OSError) as exc:
        raise MCPApplicationError(
            MCPErrorCode.TEMPORARILY_UNAVAILABLE,
            "The attached file could not be downloaded. Retry with a new attachment.",
        ) from exc
    return DownloadedFile(
        file_name=file_name,
        content_type=content_type,
        content=content,
    )


def _request_with_safe_redirects(*, client: httpx.Client, url: str) -> httpx.Response:
    """Fetch a URL while reapplying the SSRF policy at every redirect hop."""

    current_url = url
    redirect_statuses = {
        HTTPStatus.MOVED_PERMANENTLY,
        HTTPStatus.FOUND,
        HTTPStatus.SEE_OTHER,
        HTTPStatus.TEMPORARY_REDIRECT,
        HTTPStatus.PERMANENT_REDIRECT,
    }
    for redirect_count in range(MCP_FILE_DOWNLOAD_MAX_REDIRECTS + 1):
        parsed, addresses = _validated_public_https_destination(current_url)
        response = _send_to_validated_address(
            client=client,
            parsed=parsed,
            addresses=addresses,
        )
        if response.status_code == HTTPStatus.OK:
            return response
        if response.status_code not in redirect_statuses:
            response.close()
            code = (
                MCPErrorCode.TEMPORARILY_UNAVAILABLE
                if response.status_code >= HTTPStatus.INTERNAL_SERVER_ERROR
                else MCPErrorCode.INVALID_INPUT
            )
            raise MCPApplicationError(
                code,
                "The attached file download was rejected.",
            )
        location = response.headers.get("Location")
        response.close()
        if not location or redirect_count == MCP_FILE_DOWNLOAD_MAX_REDIRECTS:
            raise MCPApplicationError(
                MCPErrorCode.INVALID_INPUT,
                "The attached file download redirected unexpectedly.",
            )
        current_url = urljoin(current_url, location)
    raise AssertionError("The bounded redirect loop must return or raise")


def _validated_public_https_destination(
    url: str,
) -> tuple[
    SplitResult,
    tuple[ipaddress.IPv4Address | ipaddress.IPv6Address, ...],
]:
    """Resolve one safe HTTPS destination exactly once for a pinned request."""

    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise MCPApplicationError(
            MCPErrorCode.INVALID_INPUT,
            "The attached file URL is invalid.",
        ) from exc
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or parsed.fragment
    ):
        raise MCPApplicationError(
            MCPErrorCode.INVALID_INPUT,
            "The attached file URL must be a public HTTPS URL.",
        )
    addresses = _resolved_addresses(parsed.hostname)
    if not addresses or any(not address.is_global for address in addresses):
        raise MCPApplicationError(
            MCPErrorCode.INVALID_INPUT,
            "The attached file URL must resolve to a public address.",
        )
    ordered = tuple(sorted(addresses, key=lambda item: (item.version, int(item))))
    return parsed, ordered


def _send_to_validated_address(
    *,
    client: httpx.Client,
    parsed: SplitResult,
    addresses: tuple[ipaddress.IPv4Address | ipaddress.IPv6Address, ...],
) -> httpx.Response:
    """Connect only to validated addresses while authenticating the URL host.

    Replacing the request URL's host prevents the HTTP transport from doing a
    second, attacker-influenced DNS lookup. The original hostname remains the
    HTTP Host header and TLS SNI name, so certificate verification still proves
    the caller reached the server authorized for that URL.
    """

    hostname = parsed.hostname
    if hostname is None:  # pragma: no cover - validated by the caller
        raise AssertionError("A validated HTTPS URL always has a hostname")
    tls_hostname = hostname.encode("idna").decode("ascii")
    host_header = f"[{tls_hostname}]" if ":" in tls_hostname else tls_hostname
    last_error: httpx.ConnectError | httpx.ConnectTimeout | OSError | None = None
    for address in addresses:
        authority = (
            f"[{address.compressed}]"
            if isinstance(address, ipaddress.IPv6Address)
            else str(address)
        )
        pinned_url = urlunsplit(
            ("https", authority, parsed.path, parsed.query, ""),
        )
        request = client.build_request(
            "GET",
            pinned_url,
            headers={
                "Accept": "*/*",
                "Connection": "close",
                "Host": host_header,
            },
            extensions={"sni_hostname": tls_hostname},
        )
        try:
            return client.send(request, stream=True)
        except (httpx.ConnectError, httpx.ConnectTimeout, OSError) as exc:
            last_error = exc
    if last_error is not None:
        raise last_error
    raise AssertionError("A validated destination always has an address")


def _resolved_addresses(
    hostname: str,
) -> set[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """Resolve all candidate addresses used by the public-address policy."""

    try:
        return {ipaddress.ip_address(hostname)}
    except ValueError:
        pass
    try:
        results = socket.getaddrinfo(hostname, 443, type=socket.SOCK_STREAM)
        return {ipaddress.ip_address(str(result[4][0])) for result in results}
    except (OSError, ValueError) as exc:
        raise MCPApplicationError(
            MCPErrorCode.TEMPORARILY_UNAVAILABLE,
            "The attached file host could not be resolved.",
        ) from exc


def _read_bounded_content(*, response: httpx.Response, limit: int) -> bytes:
    """Reject oversized content before or during the streamed response body."""

    content_length = response.headers.get("Content-Length")
    if content_length:
        try:
            declared_size = int(content_length)
        except ValueError:
            declared_size = 0
        if declared_size > limit:
            raise _file_too_large(limit)
    chunks: list[bytes] = []
    total = 0
    for chunk in response.iter_bytes():
        total += len(chunk)
        if total > limit:
            raise _file_too_large(limit)
        chunks.append(chunk)
    content = b"".join(chunks)
    if not content:
        raise MCPApplicationError(
            MCPErrorCode.INVALID_INPUT,
            "The attached file is empty.",
        )
    return content


def _validated_file_name(value: str | None) -> str:
    """Return safe leaf metadata without interpreting server response headers."""

    file_name = (value or "upload.bin").strip()
    if (
        not file_name
        or len(file_name) > _MAX_METADATA_LENGTH
        or "/" in file_name
        or "\\" in file_name
        or not file_name.isprintable()
    ):
        raise MCPApplicationError(
            MCPErrorCode.INVALID_INPUT,
            "The attached file name is invalid.",
        )
    return file_name


def _validated_content_type(value: str | None) -> str:
    """Return bounded MIME metadata with a conservative binary fallback."""

    content_type = (value or "application/octet-stream").split(";", 1)[0].strip()
    if (
        not content_type
        or len(content_type) > _MAX_METADATA_LENGTH
        or not content_type.isprintable()
    ):
        raise MCPApplicationError(
            MCPErrorCode.INVALID_INPUT,
            "The attached file MIME type is invalid.",
        )
    return content_type


def _file_too_large(limit: int) -> MCPApplicationError:
    """Build the stable oversize error shared by both download checks."""

    return MCPApplicationError(
        MCPErrorCode.FILE_TOO_LARGE,
        f"The attached file exceeds the {limit}-byte MCP limit.",
    )
