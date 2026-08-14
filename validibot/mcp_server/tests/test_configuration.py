"""Tests for fail-closed MCP production configuration validation.

Community deployments may leave MCP dormant, but an activated production MCP
server must refuse startup when its origin, JWT key, audience, download-host
allowlist, or abuse budgets are unsafe.
"""

from __future__ import annotations

import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import Encoding
from cryptography.hazmat.primitives.serialization import NoEncryption
from cryptography.hazmat.primitives.serialization import PrivateFormat
from django.core.exceptions import ImproperlyConfigured

from validibot.mcp_server.configuration import validate_production_mcp_configuration


def _private_key() -> str:
    """Create a disposable RSA-2048 key for startup-policy tests."""

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        encoding=Encoding.PEM,
        format=PrivateFormat.PKCS8,
        encryption_algorithm=NoEncryption(),
    ).decode("ascii")


def _configure_valid_production(settings) -> None:
    """Install a complete strict configuration before focused mutations."""

    settings.MCP_STRICT_CONFIGURATION = True
    settings.SITE_URL = "https://app.validibot.com"
    settings.IDP_OIDC_MCP_RESOURCE_AUDIENCE = "https://app.validibot.com/mcp"
    settings.IDP_OIDC_PRIVATE_KEY = _private_key()
    settings.MCP_FILE_ALLOWED_HOSTS = ["files.oaiusercontent.com"]
    settings.ALLOWED_HOSTS = ["app.validibot.com"]


def test_dormant_non_strict_deployments_do_not_require_mcp_credentials(
    settings,
) -> None:
    """Community deployments must not need an OAuth key for an inactive feature."""

    settings.MCP_STRICT_CONFIGURATION = False
    settings.IDP_OIDC_PRIVATE_KEY = ""

    validate_production_mcp_configuration()


def test_complete_strict_configuration_is_accepted(settings) -> None:
    """A production MCP server with exact origins and positive budgets may start."""

    _configure_valid_production(settings)

    validate_production_mcp_configuration()


@pytest.mark.parametrize(
    ("setting_name", "unsafe_value"),
    [
        ("SITE_URL", "http://app.validibot.com"),
        ("IDP_OIDC_MCP_RESOURCE_AUDIENCE", "https://other.example/mcp"),
        ("IDP_OIDC_PRIVATE_KEY", "not-a-key"),
        ("MCP_FILE_ALLOWED_HOSTS", []),
        ("MCP_FILE_ALLOWED_HOSTS", ["*.oaiusercontent.com"]),
        ("MCP_MAX_RESPONSE_BYTES", 0),
        ("IDP_OIDC_REFRESH_TOKEN_EXPIRES_IN", 0),
    ],
)
def test_strict_configuration_rejects_each_unsafe_boundary(
    settings,
    setting_name: str,
    unsafe_value: object,
) -> None:
    """Any missing trust anchor or disabled safety budget must fail startup."""

    _configure_valid_production(settings)
    setattr(settings, setting_name, unsafe_value)

    with pytest.raises(ImproperlyConfigured, match="Unsafe MCP"):
        validate_production_mcp_configuration()
