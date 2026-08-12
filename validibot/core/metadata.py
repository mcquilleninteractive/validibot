"""Canonical, bounded handling for untrusted submission metadata.

Submission metadata crosses admission, persistence, and validator input
boundaries.  Keeping its structural checks and byte representation here makes
those boundaries agree on what constitutes JSON and on the exact bytes used
for limits and content identities.
"""

from __future__ import annotations

import json
import math


class MetadataPolicyError(ValueError):
    """Raised when submission metadata violates the configured policy."""


def canonical_metadata_bytes(
    metadata: object,
    *,
    max_depth: int = 0,
) -> bytes:
    """Return deterministic UTF-8 JSON bytes for one metadata object.

    Container depth counts the top-level metadata object as depth one.  A
    ``max_depth`` of zero disables that policy limit, while the iterative
    structural walk and serializer still reject cycles, non-JSON values, and
    values Python's JSON encoder cannot safely represent.
    """

    if not isinstance(metadata, dict):
        raise MetadataPolicyError("Submission metadata must be a JSON object.")

    _validate_json_structure(metadata, max_depth=max_depth)
    try:
        return json.dumps(
            metadata,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (RecursionError, TypeError, ValueError) as exc:
        raise MetadataPolicyError("Submission metadata is not valid JSON.") from exc


def _validate_json_structure(metadata: dict[object, object], *, max_depth: int) -> None:
    """Validate JSON types, finite numbers, cycles, and bounded container depth."""

    stack: list[tuple[object, int, bool]] = [(metadata, 1, False)]
    active_containers: set[int] = set()

    while stack:
        value, depth, leaving = stack.pop()
        if leaving:
            active_containers.remove(id(value))
            continue

        if isinstance(value, dict):
            _enter_container(
                value,
                depth=depth,
                max_depth=max_depth,
                active_containers=active_containers,
            )
            if not all(isinstance(key, str) for key in value):
                raise MetadataPolicyError("Submission metadata keys must be strings.")
            stack.append((value, depth, True))
            stack.extend((item, depth + 1, False) for item in value.values())
            continue

        if isinstance(value, list):
            _enter_container(
                value,
                depth=depth,
                max_depth=max_depth,
                active_containers=active_containers,
            )
            stack.append((value, depth, True))
            stack.extend((item, depth + 1, False) for item in value)
            continue

        if isinstance(value, float) and not math.isfinite(value):
            raise MetadataPolicyError(
                "Submission metadata numbers must be finite JSON numbers."
            )
        if value is not None and not isinstance(value, (str, int, float, bool)):
            raise MetadataPolicyError("Submission metadata contains a non-JSON value.")


def _enter_container(
    value: object,
    *,
    depth: int,
    max_depth: int,
    active_containers: set[int],
) -> None:
    """Apply depth and cycle checks before traversing one JSON container."""

    if max_depth > 0 and depth > max_depth:
        raise MetadataPolicyError(
            f"Submission metadata exceeds the maximum depth of {max_depth}."
        )
    container_id = id(value)
    if container_id in active_containers:
        raise MetadataPolicyError("Submission metadata must not contain cycles.")
    active_containers.add(container_id)
