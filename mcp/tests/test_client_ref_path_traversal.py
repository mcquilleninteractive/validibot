"""Regression tests for opaque-ref path-traversal hardening in the MCP client.

WHY THIS SUITE EXISTS
---------------------
The MCP service has no database access, so it resolves workflows and runs by
forwarding caller-supplied opaque tokens (``workflow_ref``, ``run_ref``,
``run_id``) into ``httpx`` request paths built with f-strings. Those tokens
originate from untrusted callers — ultimately from AI agents. A crafted ref
such as ``../../license/features`` interpolated into
``/api/v1/mcp/workflows/<ref>/`` could traverse *out* of the workflow namespace
and hit a different Django API endpoint over the trusted service-to-service
channel (a path-traversal / request-forgery vector, ``mcp-ref-path-traversal``).

THE FIX (and why percent-encoding alone is not enough)
------------------------------------------------------
``_encode_ref`` *validates* every ref against the opaque-token character set
(``A-Za-z0-9_-`` — exactly what ``validibot_mcp.refs`` and UUID run-ids
produce) and rejects anything else, then percent-encodes it. Validation, not
encoding, is the real guarantee: ``httpx`` preserves ``%2F`` on the wire, but
WSGI percent-decodes ``PATH_INFO`` and a normalising proxy can then collapse
the ``..`` — so an encoding-only defence may not survive end to end. Rejecting
the charset up front neutralises the attack regardless of downstream decoding.

These tests pin both halves: a traversal ref is refused *before any request is
sent*, and a legitimate opaque ref passes through untouched.
"""

from __future__ import annotations

import pytest

from validibot_mcp.client import _encode_ref, get_authenticated_workflow_detail

# A traversal payload that, interpolated unencoded into
# ``/api/v1/mcp/workflows/{ref}/``, would normalise to
# ``/api/v1/license/features/`` — escaping the workflow namespace. The
# license-features endpoint is the real, sensitive sibling route the MCP
# license gate calls at startup.
TRAVERSAL_REF = "../../license/features"

# A normal opaque reference: the ``wf_`` prefix plus base64url payload
# characters that ``validibot_mcp.refs`` actually emits. Contains no path
# separator, dot, or whitespace, so it must be accepted.
LEGIT_REF = "wf_abc123_DEF-456"


class TestEncodeRefRejectsTraversal:
    """``_encode_ref`` must reject anything outside the opaque-token charset."""

    def test_traversal_ref_is_rejected(self):
        """A ref with path separators is refused outright, not just encoded.

        This is the core guarantee. Because legitimate refs are always opaque
        tokens, a value containing ``/`` (or ``..``) can only be an attack, so
        we reject it before it can be interpolated into a request path — no
        reliance on every downstream hop preserving ``%2F``.
        """
        with pytest.raises(ValueError, match="Invalid reference"):
            _encode_ref(TRAVERSAL_REF)

    def test_other_unsafe_refs_are_rejected(self):
        """Slashes, dots, backslashes, whitespace, and empty refs all reject.

        Each of these characters could contribute to traversal or otherwise
        break out of a single path segment, and none appears in a legitimate
        opaque token, so every one must be refused.
        """
        for bad in ("a/b", "a..b", "a.b", "a b", "a\\b", "wf_x%2Fy", ""):
            with pytest.raises(ValueError):
                _encode_ref(bad)

    def test_legit_opaque_ref_passes_through_unchanged(self):
        """A normal base64url / UUID ref is accepted and returned unchanged.

        The fix must not break real traffic: every character a genuine ref uses
        (``A-Za-z0-9_-``) is URL-safe, so encoding is a no-op and the ref is
        returned verbatim. This guards against an over-strict validator that
        would reject legitimate references.
        """
        assert _encode_ref(LEGIT_REF) == LEGIT_REF
        uuid_ref = "550e8400-e29b-41d4-a716-446655440000"
        assert _encode_ref(uuid_ref) == uuid_ref


class TestRefTraversalNeverHitsTheWire:
    """A traversal ref must raise pre-flight, so no request is ever sent."""

    async def test_authenticated_workflow_detail_rejects_traversal(self, mock_api):
        """A traversal ``workflow_ref`` raises before any HTTP request is made.

        The authenticated detail call builds its path via ``_encode_ref``, so a
        traversal ref is refused while constructing the request — we assert the
        ``ValueError`` propagates and the mocked API route is never called.
        """
        route = mock_api.route(host="app.validibot.com").respond(
            200,
            json={"ok": True},
        )

        with pytest.raises(ValueError, match="Invalid reference"):
            await get_authenticated_workflow_detail(TRAVERSAL_REF, user_sub="user-1")

        assert not route.called


if __name__ == "__main__":  # pragma: no cover - convenience runner
    raise SystemExit(pytest.main([__file__, "-v"]))
