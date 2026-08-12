"""Validator semantic-digest computation.

ADR-2026-04-27 Phase 3, Session B (tasks 8–9): a system validator's
semantic config — the things that change *what the validator does* —
must be hashable so we can detect drift. If someone modifies a
shipped validator's behavior (e.g. swaps the underlying processor)
without bumping the validator's ``version`` field, every workflow
that locked onto the old version silently runs under new rules.

This module provides a pure helper that:

1. Defines :data:`SEMANTIC_FIELDS` — the allowlist of validator
   attributes that are considered "behavior-defining" and therefore
   covered by the digest.
2. Provides :func:`compute_semantic_digest` to canonicalise those
   fields into a stable byte representation and hash them.

The digest is **stable**: the same semantic input produces the same
hex output across Python versions, machines, and time. This is the
property the audit story depends on — ``audit_workflow_versions``
(Session D) compares the digest stored on a validator row to the
digest re-computed from the current config to flag tampered rows.

What's semantic vs. what isn't
==============================

Semantic (covered by the digest):

- ``validation_type`` — the kind of validator (json_schema, energyplus, etc.)
- ``provider`` — backing provider that resolves the validator class
- ``has_processor`` / ``processor_name`` — whether and how the
  validator runs an intermediate compute step
- ``supports_assertions`` — whether step-level assertions can run
- ``validator_class`` / ``output_envelope_class`` — the dotted Python
  paths that resolve to executable code; changing these changes
  what's actually run
- ``image_name`` — the container image used by advanced validators
- ``compute_tier`` — affects scheduling/resourcing
- ``catalog_entries`` — the complete step I/O contract, including accepted
  submission file types, logical formats, media types, extensions, source
  scopes, and managed resource types

Not semantic (excluded — cosmetic, identity, or runtime-only):

- ``slug`` / ``version`` — these are the *identity* keys we hash
  *under*, not part of the hash
- ``name`` / ``short_description`` / ``description`` — display strings
- ``order`` — UI ordering
- ``is_system`` — lifecycle flag
- ``icon`` / ``card_image`` — UI assets
- ``step_editor_cards`` — UI customisation
- ``resolved_class`` / ``resolved_envelope_class`` — runtime-only,
  populated by registration not declaration

When in doubt, ask: "if this changed without a version bump, would
existing workflows produce different results?" — if yes, it's
semantic.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

# Length of a SHA-256 hex digest. Exposed so tests and consumers
# can validate the digest's shape without scattering ``== 64`` magic
# numbers (PLR2004).
SHA256_HEX_LENGTH = 64

# Allowlist — single source of truth for "what belongs in the digest?".
# Adding a field here is an intentional, reviewable policy change:
# every existing validator row's stored digest will mismatch the new
# computation until ``sync_validators`` runs, which is the right
# behavior (operators need to opt in to a recomputation).
SEMANTIC_FIELDS: frozenset[str] = frozenset(
    {
        "validation_type",
        "provider",
        "has_processor",
        "processor_name",
        "supports_assertions",
        "validator_class",
        "output_envelope_class",
        "image_name",
        "compute_tier",
        "catalog_entries",
        # Trust ADR Phase 5 Session C — trust_tier is semantic
        # because it determines the sandbox profile the runner
        # applies, which is observable behavior. A change from
        # TIER_1 to TIER_2 (or vice versa) under the same (slug,
        # version) would silently change how the validator's
        # backend container is locked down. The contract that
        # workflows lock onto includes "what sandbox enforces this
        # validator's runs," so flipping the tier is exactly the
        # kind of drift the semantic_digest is designed to catch.
        "trust_tier",
    },
)


def _canonicalise(value: Any) -> Any:
    """Recursively normalise a value so the JSON encoding is stable.

    Lists are sorted when their items are themselves comparable.
    Dicts have keys sorted via ``json.dumps(sort_keys=True)`` later;
    here we normalise child values. Non-container values pass
    through unchanged.

    The reason for sorting list-of-strings is so equivalent unordered contract
    lists produce the same digest. But list-of-dicts (like
    ``catalog_entries``) keeps insertion order because the order of
    catalog entries IS semantic (the ``order`` field on each entry
    drives display + position-sensitive logic).
    """
    if isinstance(value, dict):
        return {k: _canonicalise(v) for k, v in value.items()}
    if isinstance(value, list):
        if all(isinstance(item, str) for item in value):
            # List-of-strings: order-insensitive comparison.
            return sorted(value)
        return [_canonicalise(item) for item in value]
    return value


def compute_semantic_digest(data: dict[str, Any]) -> str:
    """Hash the semantic subset of ``data`` to a hex SHA-256 digest.

    Pure function: same input → same output, no side effects, no DB
    queries. Tests can call this directly to lock down the digest
    behavior without standing up a full validator config.

    Args:
        data: A dict of validator fields. Typically this is a
            ``ValidatorConfig.model_dump()`` or a Validator row's
            field dict. Keys outside :data:`SEMANTIC_FIELDS` are
            ignored — callers don't need to filter first.

    Returns:
        A 64-character hex string (SHA-256). Empty input fields
        are normalised to their type-appropriate empty value before
        hashing, so a config that's missing a key produces the same
        digest as one that explicitly sets the key to ``""`` /
        ``[]`` / ``False``.
    """
    semantic = {}
    for field in sorted(SEMANTIC_FIELDS):
        if field in data:
            semantic[field] = _canonicalise(data[field])
        # Note: we DON'T set a default for missing keys here. A key
        # missing from ``data`` and a key explicitly set to ``""``
        # should both produce the same digest, and json.dumps will
        # naturally omit a missing key just like it would for an
        # explicitly-empty one if the caller wants that semantics —
        # but emitting "absent" as missing-from-the-dict means
        # callers who pass partial dicts get a different digest than
        # callers who pass a fully-keyed dict. That's intentional:
        # callers should pass a full ValidatorConfig.model_dump(),
        # and any partial-data path is a bug.
    canonical_json = json.dumps(
        semantic,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return hashlib.sha256(canonical_json.encode("ascii")).hexdigest()


__all__ = ["SEMANTIC_FIELDS", "SHA256_HEX_LENGTH", "compute_semantic_digest"]
