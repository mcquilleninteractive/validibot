"""Tests for atomic counters shared by security-sensitive rate limits.

MCP transport, OAuth token endpoints, and authenticated tools all depend on a
single counter primitive. These tests protect its expiry recovery, input
validation, and stable PostgreSQL advisory-lock identity without coupling the
suite to an external cache service.
"""

from __future__ import annotations

import pytest
from django.core.cache import cache
from django.core.management import call_command
from django.db import connection

from validibot.core.rate_limit_counters import _advisory_lock_id
from validibot.core.rate_limit_counters import increment_rate_limit_counter

EXPECTED_INCREMENTED_COUNT = 2


@pytest.fixture(autouse=True)
def _clear_counter_cache() -> None:
    """Prevent one fixed-window scenario from spending another one's budget."""

    cache.clear()


def test_counter_increments_and_recovers_after_external_eviction() -> None:
    """A disappeared bucket must restart at one instead of failing open or closed."""

    assert increment_rate_limit_counter(key="security:test", timeout=70) == 1
    assert (
        increment_rate_limit_counter(key="security:test", timeout=70)
        == EXPECTED_INCREMENTED_COUNT
    )

    cache.delete("security:test")

    assert increment_rate_limit_counter(key="security:test", timeout=70) == 1


@pytest.mark.parametrize(
    ("key", "timeout"),
    [("", 70), ("security:test", 0), ("security:test", -1)],
)
def test_counter_rejects_unbounded_or_ambiguous_inputs(
    key: str,
    timeout: int,
) -> None:
    """Callers must never accidentally create permanent or unnamed buckets."""

    with pytest.raises(ValueError, match="positive timeout"):
        increment_rate_limit_counter(key=key, timeout=timeout)


def test_advisory_lock_identity_is_stable_distinct_and_signed() -> None:
    """DatabaseCache locks need deterministic signed-bigint keys per bucket."""

    first = _advisory_lock_id("mcp:ip:one")
    assert first == _advisory_lock_id("mcp:ip:one")
    assert first != _advisory_lock_id("mcp:ip:two")
    assert -(2**63) <= first < 2**63


@pytest.mark.django_db
def test_postgresql_database_cache_uses_the_atomic_counter_path(settings) -> None:
    """The launch cache backend must execute the advisory-lock path successfully."""

    if connection.vendor != "postgresql":
        pytest.skip("Validibot's supported DatabaseCache deployment uses PostgreSQL")
    settings.CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.db.DatabaseCache",
            "LOCATION": "test_security_rate_limit_cache",
        },
    }
    call_command("createcachetable", verbosity=0)

    assert increment_rate_limit_counter(key="database:test", timeout=70) == 1
    assert (
        increment_rate_limit_counter(key="database:test", timeout=70)
        == EXPECTED_INCREMENTED_COUNT
    )
