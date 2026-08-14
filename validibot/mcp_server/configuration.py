"""Fail-closed production configuration checks for the embedded MCP server."""

from __future__ import annotations

from urllib.parse import urlsplit

from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey
from cryptography.hazmat.primitives.serialization import load_pem_private_key
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

_MINIMUM_RSA_KEY_SIZE = 2048


def validate_production_mcp_configuration() -> None:
    """Reject an activated production MCP surface with unsafe configuration."""

    if not bool(getattr(settings, "MCP_STRICT_CONFIGURATION", False)):
        return

    failures: list[str] = []
    site_url = str(getattr(settings, "SITE_URL", "")).rstrip("/")
    parsed = urlsplit(site_url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        failures.append("SITE_URL must be one canonical HTTPS origin")

    expected_audience = f"{site_url}/mcp"
    configured_audience = str(
        getattr(settings, "IDP_OIDC_MCP_RESOURCE_AUDIENCE", ""),
    ).rstrip("/")
    if configured_audience != expected_audience:
        failures.append(
            "IDP_OIDC_MCP_RESOURCE_AUDIENCE must equal SITE_URL plus /mcp",
        )

    private_key = str(getattr(settings, "IDP_OIDC_PRIVATE_KEY", ""))
    try:
        parsed_key = load_pem_private_key(private_key.encode(), password=None)
    except (TypeError, ValueError):
        parsed_key = None
    if (
        not isinstance(parsed_key, RSAPrivateKey)
        or parsed_key.key_size < _MINIMUM_RSA_KEY_SIZE
    ):
        failures.append("IDP_OIDC_PRIVATE_KEY_B64 must contain an RSA-2048+ key")

    file_hosts = [
        str(host).strip()
        for host in getattr(settings, "MCP_FILE_ALLOWED_HOSTS", [])
        if str(host).strip()
    ]
    if not file_hosts or any(
        "*" in host or "/" in host or ":" in host for host in file_hosts
    ):
        failures.append("MCP_FILE_ALLOWED_HOSTS must list exact DNS hostnames")

    if "*" in getattr(settings, "ALLOWED_HOSTS", []):
        failures.append("DJANGO_ALLOWED_HOSTS must not contain a wildcard")

    positive_settings = (
        "MCP_FILE_MAX_BYTES",
        "MCP_FILE_DOWNLOAD_TOTAL_TIMEOUT_SECONDS",
        "MCP_FILE_DOWNLOAD_MAX_ADDRESSES",
        "MCP_MAX_REQUEST_BODY_BYTES",
        "MCP_MAX_RESPONSE_BYTES",
        "MCP_READS_PER_MINUTE",
        "MCP_STARTS_PER_MINUTE",
        "MCP_REQUESTS_PER_IP_PER_MINUTE",
        "MCP_FAILED_AUTH_PER_IP_PER_MINUTE",
        "MCP_GLOBAL_REQUESTS_PER_MINUTE",
        "IDP_OIDC_TOKEN_REQUESTS_PER_IP_PER_MINUTE",
        "IDP_OIDC_REVOKE_REQUESTS_PER_IP_PER_MINUTE",
        "IDP_OIDC_ENDPOINT_GLOBAL_REQUESTS_PER_MINUTE",
        "IDP_OIDC_ACCESS_TOKEN_EXPIRES_IN",
        "IDP_OIDC_REFRESH_TOKEN_EXPIRES_IN",
    )
    for name in positive_settings:
        try:
            value = float(getattr(settings, name))
        except (AttributeError, TypeError, ValueError):
            value = 0
        if value <= 0:
            failures.append(f"{name} must be greater than zero")

    if failures:
        detail = "; ".join(failures)
        raise ImproperlyConfigured(f"Unsafe MCP production configuration: {detail}")
