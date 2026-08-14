"""Strict client-address selection shared by security-sensitive boundaries."""

from __future__ import annotations

import ipaddress

_MAX_FORWARDED_HEADER_LENGTH = 2_048
_MAX_FORWARDED_ADDRESSES = 32


def resolve_client_ip(
    *,
    peer_host: str,
    forwarded_for: str,
    proxy_depth: int,
) -> str:
    """Select one valid address from an exact trusted-proxy chain."""

    peer = _validated_ip(peer_host) or "unknown"
    depth = max(0, proxy_depth)
    if depth == 0:
        return peer
    forwarded = _forwarded_addresses(forwarded_for)
    if len(forwarded) < depth:
        return peer
    return _validated_ip(forwarded[-depth]) or peer


def _forwarded_addresses(raw_value: str) -> list[str]:
    """Parse one bounded X-Forwarded-For value without trusting partial input."""

    if not raw_value or len(raw_value) > _MAX_FORWARDED_HEADER_LENGTH:
        return []
    addresses = [item.strip() for item in raw_value.split(",")]
    return (
        addresses
        if all(addresses) and len(addresses) <= _MAX_FORWARDED_ADDRESSES
        else []
    )


def _validated_ip(value: str) -> str | None:
    """Normalize one IPv4 or IPv6 value, rejecting names and malformed input."""

    try:
        return ipaddress.ip_address(value).compressed
    except ValueError:
        return None
