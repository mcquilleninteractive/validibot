"""Tests for the validator semantic-digest helper.

ADR-2026-04-27 Phase 3 Session B (tasks 8–9): the digest is the
mechanism by which we detect a system validator's behavior changing
without an explicit version bump. These tests pin down the digest's
core properties so any future refactor that breaks them gets caught.

What we care about
==================

1. **Determinism** — same input produces the same digest, today,
   tomorrow, and across machines. Without this the audit story
   collapses.
2. **Order-insensitivity for set-like port fields** — accepted carrier types
   on one catalog entry describe the same contract in either order.
3. **Order-sensitivity for list-of-dict fields** — ``catalog_entries``
   keep order because their position is semantic.
4. **Cosmetic-field exclusion** — changing ``name`` /
   ``description`` should NOT change the digest. Changing
   ``processor_name`` SHOULD.
5. **Identity-field exclusion** — ``slug`` and ``version`` are the
   keys we hash *under*, not part of the hash content. Changing them
   should not change the digest.
"""

from __future__ import annotations

from validibot.validations.services.validator_digest import SEMANTIC_FIELDS
from validibot.validations.services.validator_digest import SHA256_HEX_LENGTH
from validibot.validations.services.validator_digest import compute_semantic_digest

# ──────────────────────────────────────────────────────────────────────
# SEMANTIC_FIELDS shape
# ──────────────────────────────────────────────────────────────────────
#
# The constant declares the policy. Locking down its membership
# explicitly catches accidental drift between code and ADR docs.


class TestSemanticFieldsConstant:
    def test_includes_behavior_defining_fields(self):
        """Every behavior-defining validator field is in the set."""
        # If a field changes WHAT the validator does, it's semantic.
        # These are the fields most likely to be silently mutated.
        expected = {
            "validation_type",
            "processor_name",
            "has_processor",
            "supports_assertions",
            "validator_class",
            "image_name",
            "catalog_entries",
        }
        assert expected.issubset(SEMANTIC_FIELDS)

    def test_excludes_cosmetic_and_identity_fields(self):
        """Identity and cosmetic fields must NOT be in the digest."""
        # ``slug`` / ``version`` are the keys we hash *under*.
        # ``name`` / ``short_description`` / ``description`` / ``icon`` /
        # ``card_image`` are cosmetic. ``order`` is for UI display.
        non_semantic = {
            "slug",
            "version",
            "name",
            "short_description",
            "description",
            "order",
            "icon",
            "card_image",
            "is_system",
            "step_editor_cards",
            "resolved_class",
            "resolved_envelope_class",
        }
        for field in non_semantic:
            assert field not in SEMANTIC_FIELDS, (
                f"{field!r} unexpectedly appears in SEMANTIC_FIELDS — "
                f"if intentional, update the ADR and remove this assertion."
            )

    def test_is_immutable(self):
        """frozenset — runtime mutation is rejected."""
        # The digest depends on the set being stable. A test that
        # adds a field after import would silently re-key every
        # validator's digest.
        try:
            SEMANTIC_FIELDS.add("evil")  # type: ignore[attr-defined]
        except AttributeError:
            return  # expected
        msg = "SEMANTIC_FIELDS should be a frozenset (immutable)"
        raise AssertionError(msg)


# ──────────────────────────────────────────────────────────────────────
# compute_semantic_digest — determinism and stability
# ──────────────────────────────────────────────────────────────────────


class TestComputeSemanticDigestDeterminism:
    """Same input → same digest, every time."""

    def test_identical_input_produces_identical_digest(self):
        """Idempotency: hashing twice gives the same hex."""
        data = {
            "validation_type": "json_schema",
            "processor_name": "JSON Schema",
            "has_processor": False,
            "supports_assertions": True,
            "validator_class": "validibot.validations.foo.Validator",
            "catalog_entries": [
                {
                    "slug": "json_document",
                    "accepted_file_types": ["json"],
                    "accepted_data_formats": ["json"],
                }
            ],
            "compute_tier": "LOW",
        }
        d1 = compute_semantic_digest(data)
        d2 = compute_semantic_digest(data)
        assert d1 == d2

    def test_returns_64_char_hex(self):
        """SHA-256 always produces 64 hex chars."""
        digest = compute_semantic_digest({"validation_type": "x"})
        assert len(digest) == SHA256_HEX_LENGTH
        # Hex check.
        int(digest, 16)


# ──────────────────────────────────────────────────────────────────────
# Cosmetic / identity field independence
# ──────────────────────────────────────────────────────────────────────


class TestComputeSemanticDigestIgnoresNonSemanticFields:
    """Cosmetic / identity changes must not change the digest."""

    def _baseline(self) -> dict:
        return {
            "validation_type": "energyplus",
            "processor_name": "EnergyPlus",
            "has_processor": True,
            "validator_class": "validibot.validations.eplus.Validator",
            "catalog_entries": [
                {
                    "slug": "primary_model",
                    "accepted_file_types": ["text"],
                }
            ],
        }

    def test_name_change_does_not_affect_digest(self):
        """Display name is cosmetic, not semantic."""
        a = {**self._baseline(), "name": "EnergyPlus IDF Validator"}
        b = {**self._baseline(), "name": "Energy Modelling Validator"}
        assert compute_semantic_digest(a) == compute_semantic_digest(b)

    def test_description_change_does_not_affect_digest(self):
        """Description is cosmetic."""
        a = {**self._baseline(), "description": "Validates IDF files."}
        b = {**self._baseline(), "description": "A different blurb."}
        assert compute_semantic_digest(a) == compute_semantic_digest(b)

    def test_slug_and_version_change_does_not_affect_digest(self):
        """Identity keys aren't part of the hash content.

        The whole point: ``(slug, version)`` is the *key* we hash
        UNDER. The digest captures behavior. Making the digest
        depend on slug/version would defeat the point — every
        cloned-with-version-bump would have a different digest
        even if behavior was identical.
        """
        a = {**self._baseline(), "slug": "energyplus-idf", "version": "1.0"}
        b = {**self._baseline(), "slug": "totally-different", "version": "9.99"}
        assert compute_semantic_digest(a) == compute_semantic_digest(b)


# ──────────────────────────────────────────────────────────────────────
# Semantic field changes DO affect the digest
# ──────────────────────────────────────────────────────────────────────


class TestComputeSemanticDigestRespondsToSemanticChanges:
    """Behavior-defining changes must change the digest.

    These are the changes that, in production without drift
    detection, would silently re-write the rules of every workflow
    pinned to a (slug, version).
    """

    def _baseline(self) -> dict:
        return {
            "validation_type": "json_schema",
            "processor_name": "JSON Schema",
            "has_processor": False,
            "validator_class": "validibot.validations.json_schema.Validator",
            "catalog_entries": [
                {"slug": "json_document", "accepted_file_types": ["json"]}
            ],
        }

    def test_processor_name_change_changes_digest(self):
        """Swapping processor swaps semantics."""
        a = self._baseline()
        b = {**a, "processor_name": "Different Processor"}
        assert compute_semantic_digest(a) != compute_semantic_digest(b)

    def test_validator_class_change_changes_digest(self):
        """Changing the executable Python class is HUGE — it's the code that runs."""
        a = self._baseline()
        b = {**a, "validator_class": "validibot.validations.evil.Backdoor"}
        assert compute_semantic_digest(a) != compute_semantic_digest(b)

    def test_port_file_type_addition_changes_digest(self):
        """Widening one port's accepted carrier types changes the contract."""
        a = self._baseline()
        b = {
            **a,
            "catalog_entries": [
                {
                    "slug": "json_document",
                    "accepted_file_types": ["json", "yaml"],
                }
            ],
        }
        assert compute_semantic_digest(a) != compute_semantic_digest(b)

    def test_has_processor_flip_changes_digest(self):
        """Toggling whether the validator runs an intermediate step is semantic."""
        a = self._baseline()
        b = {**a, "has_processor": True}
        assert compute_semantic_digest(a) != compute_semantic_digest(b)


# ──────────────────────────────────────────────────────────────────────
# Order-sensitivity rules
# ──────────────────────────────────────────────────────────────────────


class TestComputeSemanticDigestOrdering:
    """List-of-strings is set-like; list-of-dicts keeps order."""

    def test_port_file_types_reorder_is_no_change(self):
        """Reordering set-like accepted carrier types changes no behavior."""
        a = {
            "catalog_entries": [
                {
                    "slug": "document",
                    "accepted_file_types": ["json", "xml", "yaml"],
                }
            ]
        }
        b = {
            "catalog_entries": [
                {
                    "slug": "document",
                    "accepted_file_types": ["yaml", "xml", "json"],
                }
            ]
        }
        assert compute_semantic_digest(a) == compute_semantic_digest(b)

    def test_catalog_entries_reorder_changes_digest(self):
        """Reordering ``catalog_entries`` IS a semantic change.

        Each entry has its own ``order`` field that drives display
        and sometimes execution sequence; the LIST order in the
        config is what produces those order numbers, so
        re-shuffling the config produces a different validator.
        """
        a = {
            "catalog_entries": [
                {"slug": "a", "order": 1},
                {"slug": "b", "order": 2},
            ],
        }
        b = {
            "catalog_entries": [
                {"slug": "b", "order": 2},
                {"slug": "a", "order": 1},
            ],
        }
        assert compute_semantic_digest(a) != compute_semantic_digest(b)


# ──────────────────────────────────────────────────────────────────────
# Empty / missing-key handling
# ──────────────────────────────────────────────────────────────────────


class TestComputeSemanticDigestEmptyInputs:
    def test_empty_dict_produces_stable_digest(self):
        """Empty input is valid — produces a known constant."""
        d1 = compute_semantic_digest({})
        d2 = compute_semantic_digest({})
        assert d1 == d2
        assert len(d1) == SHA256_HEX_LENGTH

    def test_missing_optional_field_does_not_crash(self):
        """Partial inputs work; only present semantic keys are hashed."""
        # Only validation_type present — should still compute fine.
        digest = compute_semantic_digest({"validation_type": "basic"})
        assert len(digest) == SHA256_HEX_LENGTH

    def test_extra_unknown_keys_are_ignored(self):
        """Keys outside SEMANTIC_FIELDS don't sneak into the digest."""
        a = {"validation_type": "basic"}
        b = {"validation_type": "basic", "future_field_we_dont_know_about": [1, 2, 3]}
        # The extra key isn't in SEMANTIC_FIELDS, so it's stripped.
        assert compute_semantic_digest(a) == compute_semantic_digest(b)
