"""Shared fixtures and database hygiene for the public end-to-end test tree.

The system-validator resolver in this module deliberately synchronizes the
production catalog so workflow tests exercise named ports and backend identity,
while low-level unit tests remain free to construct partial model graphs.
"""

from __future__ import annotations

import json
from io import StringIO
from pathlib import Path
from typing import TYPE_CHECKING
from typing import Any

import pytest
from django.core.management import call_command
from django.db import connections
from rest_framework.test import APIClient

if TYPE_CHECKING:
    from collections.abc import Callable

    from validibot.validations.models import Validator

BASE_DIR = Path(__file__).resolve().parent


def _reset_bad_connections() -> None:
    """Reset database connections that are in a bad psycopg state.

    This keeps teardown stable for suites that use ``live_server`` and also
    helps when a local reused test database contains stale optional-app tables.
    """
    for conn in connections.all():
        if hasattr(conn, "connection") and conn.connection is not None:
            try:
                is_bad = False
                if hasattr(conn.connection, "pgconn"):
                    from psycopg.pq import ConnStatus

                    if conn.connection.pgconn.status == ConnStatus.BAD:
                        is_bad = True

                if is_bad:
                    conn.connection = None
                    conn.closed_in_transaction = False
                    conn.in_atomic_block = False
                    conn.savepoint_ids = []
                    conn.needs_rollback = False
            except Exception:
                conn.connection = None
                conn.closed_in_transaction = False
                conn.in_atomic_block = False
                conn.savepoint_ids = []
                conn.needs_rollback = False


_original_flush_handle: dict[str, Any | None] = {"handle": None}


def _patched_flush_handle(self, *args, **options):
    """Reset DB connections and allow CASCADE when flushing test tables."""
    _reset_bad_connections()
    options["allow_cascade"] = True
    original_handle = _original_flush_handle["handle"]
    if original_handle is None:  # pragma: no cover - defensive
        raise RuntimeError("Original FlushCommand.handle not set")
    return original_handle(self, *args, **options)


def pytest_configure(config):
    """Install the global flush patch for the test session."""
    from django.core.management.commands.flush import Command as FlushCommand

    _original_flush_handle["handle"] = FlushCommand.handle
    FlushCommand.handle = _patched_flush_handle


def pytest_unconfigure(config):
    """Restore Django's original flush command at session end."""
    from django.core.management.commands.flush import Command as FlushCommand

    original_handle = _original_flush_handle["handle"]
    if original_handle is not None:
        FlushCommand.handle = original_handle


def _locate_schema(rel_path: str) -> Path:
    """Return the preferred path for XML schemas, falling back to legacy dirs."""
    primary = BASE_DIR / "assets" / "xml" / "schemas" / rel_path
    if primary.exists():
        return primary
    # Legacy fallbacks kept for older fixtures/tests
    legacy_dirs = [
        BASE_DIR / "assets" / "xsd",
        BASE_DIR / "assets" / "rng",
        BASE_DIR / "assets" / "dtd",
    ]
    for candidate_root in legacy_dirs:
        candidate = candidate_root / rel_path
        if candidate.exists():
            return candidate
    return primary


@pytest.fixture
def load_xml_asset():
    """
    Load a valid XML asset relative to tests/assets.
    Usage: load_valid_xml_asset("example_product.xml")
    """

    def _loader(rel_path: str) -> str:
        path = BASE_DIR / "assets" / "xml" / rel_path
        with path.open("r", encoding="utf-8") as f:
            return f.read()

    return _loader


@pytest.fixture
def load_json_asset():
    """
    Load a JSON asset relative to tests/assets.
    Usage: load_json_asset("example_product_schema.json")
    """

    def _loader(rel_path: str):
        path = BASE_DIR / "assets" / "json" / rel_path
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)

    return _loader


@pytest.fixture
def load_xsd_asset():
    """
    Load an XSD asset relative to tests/assets.
    Usage: load_xsd_asset("example_product_schema.xsd")
    """

    def _loader(rel_path: str):
        path = _locate_schema(rel_path)
        with path.open("r", encoding="utf-8") as f:
            return f.read()

    return _loader


@pytest.fixture
def load_rng_asset():
    """
    Load an RNG asset relative to tests/assets.
    Usage: load_rng_asset("example_product_schema.rng")
    """

    def _loader(rel_path: str) -> str:
        path = _locate_schema(rel_path)
        with path.open("r", encoding="utf-8") as f:
            return f.read()

    return _loader


@pytest.fixture
def load_dtd_asset():
    """Load a DTD asset relative to tests/assets."""

    def _loader(rel_path: str) -> str:
        path = _locate_schema(rel_path)
        with path.open("r", encoding="utf-8") as f:
            return f.read()

    return _loader


@pytest.fixture
def load_shacl_asset():
    """
    Load a SHACL/Turtle asset relative to tests/assets/shacl/.
    Usage: load_shacl_asset("example_person_shapes.ttl")
    """

    def _loader(rel_path: str) -> str:
        path = BASE_DIR / "assets" / "shacl" / rel_path
        with path.open("r", encoding="utf-8") as f:
            return f.read()

    return _loader


@pytest.fixture
def load_thmx_asset():
    """Load a THMX test data file relative to tests/data/therm/."""

    def _loader(rel_path: str) -> str:
        path = BASE_DIR / "data" / "therm" / rel_path
        with path.open("r", encoding="utf-8") as f:
            return f.read()

    return _loader


@pytest.fixture
def api_client() -> APIClient:
    return APIClient()


@pytest.fixture
def system_validator_for() -> Callable[[str], Validator]:
    """Resolve production system validators from their synchronized catalogs.

    End-to-end tests must not replace a config-managed system validator with a
    generic model factory because the catalog owns semantic fields, named file
    ports, and backend identity. The command runs at most once per consuming
    test, while the returned resolver can select multiple validator families.
    """
    synchronized = False

    def _resolver(validation_type: str) -> Validator:
        nonlocal synchronized

        from validibot.validations.models import Validator
        from validibot.validations.validators.base.config import get_config

        if not synchronized:
            call_command(
                "sync_validators",
                stdout=StringIO(),
                stderr=StringIO(),
            )
            synchronized = True

        config = get_config(validation_type)
        if config is None:
            raise AssertionError(
                f"No registered system validator config for {validation_type!r}.",
            )
        return Validator.objects.get(slug=config.slug, version=config.version)

    return _resolver


@pytest.fixture(autouse=True)
def reset_connections_before_test():
    """Reset any bad DB connections before and after each test."""
    _reset_bad_connections()
    yield
    _reset_bad_connections()
