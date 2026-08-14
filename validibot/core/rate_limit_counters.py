"""Atomic fixed-window counters for security-sensitive rate limits.

Django's Redis and in-process cache backends implement atomic increments, but
``DatabaseCache`` inherits a read-then-write ``incr()`` implementation. Validibot
uses DatabaseCache on PostgreSQL as its zero-extra-service production default,
so counter mutations take a transaction-scoped advisory lock for that backend.
"""

from __future__ import annotations

import hashlib
from contextlib import contextmanager
from typing import TYPE_CHECKING

from django.core.cache import caches
from django.core.cache.backends.db import DatabaseCache
from django.core.exceptions import ImproperlyConfigured
from django.db import DEFAULT_DB_ALIAS
from django.db import connections
from django.db import router
from django.db import transaction

if TYPE_CHECKING:
    from collections.abc import Iterator

    from django.core.cache.backends.base import BaseCache


def increment_rate_limit_counter(*, key: str, timeout: int) -> int:
    """Increment one expiring counter without losing concurrent updates."""

    if not key or timeout <= 0:
        raise ValueError("Rate-limit counters require a key and positive timeout")
    backend = caches["default"]
    with _counter_lock(backend=backend, key=key):
        if backend.add(key, 1, timeout=timeout):
            return 1
        try:
            return int(backend.incr(key))
        except ValueError:
            # The bucket expired between ``add`` and ``incr``. Resetting under
            # the same lock keeps DatabaseCache's recovery path atomic too.
            backend.set(key, 1, timeout=timeout)
            return 1


@contextmanager
def _counter_lock(*, backend: BaseCache, key: str) -> Iterator[None]:
    """Serialize DatabaseCache mutations while leaving atomic backends alone."""

    if not isinstance(backend, DatabaseCache):
        yield
        return

    database_alias = router.db_for_write(backend.cache_model_class) or DEFAULT_DB_ALIAS
    connection = connections[database_alias]
    if connection.vendor != "postgresql":
        raise ImproperlyConfigured(
            "Security rate limits require Redis or PostgreSQL-backed "
            "DatabaseCache; this DatabaseCache backend cannot increment "
            "counters atomically.",
        )
    with transaction.atomic(using=database_alias):
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_advisory_xact_lock(%s)",
                [_advisory_lock_id(key)],
            )
        yield


def _advisory_lock_id(key: str) -> int:
    """Map an arbitrary cache key deterministically into PostgreSQL ``bigint``."""

    digest = hashlib.blake2b(
        key.encode("utf-8"),
        digest_size=8,
        person=b"vb-rate-limit",
    ).digest()
    return int.from_bytes(digest, byteorder="big", signed=True)
