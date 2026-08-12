"""Tests for canonical and bounded submission metadata serialization.

These tests deliberately avoid the database so the exact untrusted-data rules
can be checked independently of Django models and every submission channel can
reuse the same deterministic representation.
"""

import hashlib

import pytest

from validibot.core.metadata import MetadataPolicyError
from validibot.core.metadata import canonical_metadata_bytes


@pytest.mark.parametrize(
    ("metadata", "expected"),
    [
        ({}, b"{}"),
        ({"nested": {"items": [1, None, True]}}, b'{"nested":{"items":[1,null,true]}}'),
        ({"name": "Café 東京"}, '{"name":"Café 東京"}'.encode()),
        ({"integer": 12, "decimal": 3.5}, b'{"decimal":3.5,"integer":12}'),
    ],
)
def test_supported_json_values_have_exact_canonical_bytes(metadata, expected):
    """All documented JSON value classes serialize compactly and predictably."""

    assert canonical_metadata_bytes(metadata) == expected


def test_object_key_order_does_not_change_content_identity():
    """Equivalent metadata produces one digest regardless of caller key order."""

    first = canonical_metadata_bytes({"z": 2, "a": {"b": 1}})
    second = canonical_metadata_bytes({"a": {"b": 1}, "z": 2})

    assert first == second
    assert hashlib.sha256(first).digest() == hashlib.sha256(second).digest()


@pytest.mark.parametrize("metadata", [[], "value", 1, None])
def test_top_level_metadata_must_be_an_object(metadata):
    """Every channel receives the same object-only metadata contract."""

    with pytest.raises(MetadataPolicyError, match="must be a JSON object"):
        canonical_metadata_bytes(metadata)


@pytest.mark.parametrize("number", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_numbers_are_never_serialized(number):
    """Canonical JSON cannot contain Python's non-standard numeric tokens."""

    with pytest.raises(MetadataPolicyError, match="finite JSON numbers"):
        canonical_metadata_bytes({"number": number})


def test_maximum_depth_counts_the_top_level_object():
    """A child container beyond the configured nesting ceiling is rejected."""

    with pytest.raises(MetadataPolicyError, match="maximum depth of 2"):
        canonical_metadata_bytes({"a": {"b": {}}}, max_depth=2)


def test_cycle_detection_precedes_json_serialization():
    """Internal object cycles fail deterministically instead of walking forever."""

    metadata = {}
    metadata["self"] = metadata

    with pytest.raises(MetadataPolicyError, match="must not contain cycles"):
        canonical_metadata_bytes(metadata)
