"""
Idempotency key support for DRF views.

This module provides a mixin that implements the Stripe-style idempotency key
pattern for API endpoints. Clients can send an Idempotency-Key header with a
unique identifier, and the server will return cached responses for duplicate
requests instead of processing them again.

Usage:
    class MyViewSet(IdempotencyMixin, viewsets.ModelViewSet):
        idempotent_actions = ["create", "start_validation"]
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import timedelta
from functools import wraps
from http import HTTPStatus
from typing import TYPE_CHECKING
from typing import Any
from typing import Literal

from django.db import IntegrityError
from django.db import transaction
from django.utils import timezone
from rest_framework.response import Response

from validibot.core.models import IDEMPOTENCY_KEY_TTL_HOURS
from validibot.core.models import IdempotencyKey
from validibot.core.models import IdempotencyKeyStatus

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from collections.abc import Callable

    from validibot.users.models import Organization
    from validibot.validations.models import ValidationRun


# Header name (Django normalizes to HTTP_IDEMPOTENCY_KEY)
IDEMPOTENCY_HEADER = "HTTP_IDEMPOTENCY_KEY"
MAX_KEY_LENGTH = 255


class IdempotencyError:
    """Error codes for idempotency-related failures."""

    KEY_TOO_LONG = "idempotency_key_too_long"
    KEY_REUSED = "idempotency_key_reused"
    KEY_IN_PROGRESS = "idempotency_key_in_progress"


@dataclass(frozen=True)
class IdempotencyDecision:
    """Describe whether a transport-neutral operation should run or replay.

    REST views and MCP tools share this result so retry safety is enforced by
    the application rather than being reimplemented by each transport.
    """

    action: Literal[
        "process",
        "replay",
        "conflict",
        "hash_mismatch",
        "process_without_idempotency",
    ]
    key_record: IdempotencyKey | None


def compute_request_hash(request) -> str:
    """
    Compute a SHA256 hash of the request body for fingerprinting.

    This is used to detect when a client reuses an idempotency key
    with a different request payload (which is an error).
    """
    body = request.body or b""
    return hashlib.sha256(body).hexdigest()


def get_client_ip(request) -> str | None:
    """Extract client IP from request for debugging."""
    x_forwarded_for = request.headers.get("x-forwarded-for")
    if x_forwarded_for:
        return x_forwarded_for.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR")


def idempotent(func: Callable) -> Callable:
    """
    Decorator that adds idempotency key handling to a DRF view action.

    This decorator wraps a view action to:
    1. Check for an Idempotency-Key header
    2. If found, check if we've seen this key before
    3. If seen and completed, return the cached response
    4. If seen and in progress, return 409 Conflict
    5. If new, process the request and cache the response

    Usage:
        @action(detail=True, methods=["post"])
        @idempotent
        def start_validation(self, request, pk=None):
            ...
    """

    @wraps(func)
    def wrapper(self, request, *args, **kwargs):
        # Extract idempotency key from header
        idempotency_key = request.META.get(IDEMPOTENCY_HEADER)

        # If no key provided, process normally (no idempotency)
        if not idempotency_key:
            return func(self, request, *args, **kwargs)

        # Validate key length
        if len(idempotency_key) > MAX_KEY_LENGTH:
            return Response(
                {
                    "detail": (
                        f"Idempotency key exceeds maximum length "
                        f"of {MAX_KEY_LENGTH} characters."
                    ),
                    "code": IdempotencyError.KEY_TOO_LONG,
                },
                status=HTTPStatus.BAD_REQUEST,
            )

        # Determine organization and endpoint
        org = _get_org_from_request(self, request)
        if org is None:
            # Can't enforce idempotency without org scope
            return func(self, request, *args, **kwargs)

        endpoint = _get_endpoint_name(self)
        request_hash = compute_request_hash(request)

        # Try to find existing key or create a new one
        result = _process_idempotency_key(
            org=org,
            key=idempotency_key,
            endpoint=endpoint,
            request_hash=request_hash,
            request=request,
        )

        if result["action"] == "replay":
            # Return cached response with replay indicator
            response = Response(
                result["key_record"].response_body,
                status=result["key_record"].response_status,
            )
            response["Idempotent-Replayed"] = "true"
            response["Original-Request-Id"] = str(result["key_record"].id)
            return response

        if result["action"] == "conflict":
            return Response(
                {
                    "detail": (
                        "A request with this idempotency key "
                        "is currently being processed."
                    ),
                    "code": IdempotencyError.KEY_IN_PROGRESS,
                },
                status=HTTPStatus.CONFLICT,
            )

        if result["action"] == "hash_mismatch":
            return Response(
                {
                    "detail": (
                        "Idempotency key has already been used "
                        "with a different request body."
                    ),
                    "code": IdempotencyError.KEY_REUSED,
                },
                status=HTTPStatus.UNPROCESSABLE_ENTITY,
            )

        # Process without idempotency (edge case from race condition)
        if result["action"] == "process_without_idempotency":
            return func(self, request, *args, **kwargs)

        # Process new request
        key_record = result["key_record"]
        try:
            response = func(self, request, *args, **kwargs)
        except Exception:
            # On error, try to delete the key record so client can retry.
            # If we're in a broken transaction, the delete will fail too,
            # but that's okay - the key record will be unusable anyway.
            try:
                key_record.delete()
            except Exception:
                logger.debug("Failed to delete key record after error")
            raise
        else:
            # Cache the response
            _complete_idempotency_key(
                key_record=key_record,
                response=response,
                validation_run=_extract_validation_run(response),
            )

            return response

    return wrapper


def _get_org_from_request(view, request):
    """Extract organization from request or view context."""
    # For workflow actions, we can get org from the workflow object
    if hasattr(view, "get_object"):
        try:
            obj = view.get_object()
            if hasattr(obj, "org"):
                return obj.org
        except Exception:
            logger.debug("Failed to get org from view object")

    # Fall back to user's current org
    user = request.user
    if hasattr(user, "current_org"):
        return user.current_org

    return None


def _get_endpoint_name(view) -> str:
    """Generate endpoint identifier from view class and action."""
    class_name = view.__class__.__name__
    action = getattr(view, "action", "unknown")
    return f"{class_name}.{action}"


def _process_idempotency_key(
    org,
    key: str,
    endpoint: str,
    request_hash: str,
    request,
) -> dict[str, Any]:
    """
    Process an idempotency key, returning action to take.

    Returns dict with:
    - action: "replay" | "conflict" | "hash_mismatch" | "process"
    - key_record: The IdempotencyKey instance (if applicable)
    """
    decision = claim_idempotency_key(
        org=org,
        key=key,
        endpoint=endpoint,
        request_hash=request_hash,
        request_ip=get_client_ip(request),
        user_agent=request.headers.get("user-agent", ""),
    )
    return {"action": decision.action, "key_record": decision.key_record}


def claim_idempotency_key(
    *,
    org: Organization,
    key: str,
    endpoint: str,
    request_hash: str,
    request_ip: str | None = None,
    user_agent: str = "",
) -> IdempotencyDecision:
    """Atomically claim or inspect an idempotency key for any transport.

    Args:
        org: Organization that owns the operation.
        key: Caller-supplied retry key.
        endpoint: Stable operation scope. Callers may include a user identity
            here when an organization operation is principal-specific.
        request_hash: SHA-256 fingerprint of the canonical operation inputs.
        request_ip: Optional originating IP retained for diagnostics.
        user_agent: Optional bounded client identifier retained for diagnostics.

    Returns:
        A decision whose action is ``process``, ``replay``, ``conflict``,
        ``hash_mismatch``, or the defensive ``process_without_idempotency``.
    """

    if len(key) > MAX_KEY_LENGTH:
        msg = f"Idempotency key exceeds {MAX_KEY_LENGTH} characters."
        raise ValueError(msg)

    now = timezone.now()

    # First, try to find an existing non-expired key
    existing = IdempotencyKey.objects.filter(
        org=org,
        key=key,
        endpoint=endpoint,
        expires_at__gt=now,
    ).first()

    if existing:
        # Check if request hash matches
        if existing.request_hash != request_hash:
            return IdempotencyDecision("hash_mismatch", existing)

        # Check if still processing
        if existing.status == IdempotencyKeyStatus.PROCESSING:
            return IdempotencyDecision("conflict", existing)

        # Completed - return cached response
        return IdempotencyDecision("replay", existing)

    # Delete any expired keys with same (org, key, endpoint) before creating new one
    IdempotencyKey.objects.filter(
        org=org,
        key=key,
        endpoint=endpoint,
        expires_at__lte=now,
    ).delete()

    # Create a new key
    try:
        with transaction.atomic():
            key_record = IdempotencyKey.objects.create(
                org=org,
                key=key,
                endpoint=endpoint,
                request_hash=request_hash,
                status=IdempotencyKeyStatus.PROCESSING,
                expires_at=now + timedelta(hours=IDEMPOTENCY_KEY_TTL_HOURS),
                request_ip=request_ip,
                user_agent=user_agent[:500],
            )
            return IdempotencyDecision("process", key_record)
    except IntegrityError:
        # Race condition - another request created the key first
        # Re-fetch and handle appropriately (only non-expired keys)
        existing = IdempotencyKey.objects.filter(
            org=org,
            key=key,
            endpoint=endpoint,
            expires_at__gt=now,
        ).first()

        if existing:
            if existing.request_hash != request_hash:
                return IdempotencyDecision("hash_mismatch", existing)
            if existing.status == IdempotencyKeyStatus.PROCESSING:
                return IdempotencyDecision("conflict", existing)
            return IdempotencyDecision("replay", existing)

        # Key doesn't exist or is expired - something unusual happened
        # Process without idempotency
        return IdempotencyDecision("process_without_idempotency", None)


def complete_idempotency_key(
    *,
    key_record: IdempotencyKey,
    response_body: Any,
    response_status: int,
    validation_run: ValidationRun | None = None,
) -> None:
    """Persist a successful application result for later replay."""

    key_record.status = IdempotencyKeyStatus.COMPLETED
    key_record.response_status = response_status
    key_record.response_body = _serialize_response_data(response_body)
    if validation_run is not None:
        key_record.validation_run = validation_run
    key_record.save()


def _serialize_response_data(data: Any) -> Any:
    """
    Convert response data to JSON-serializable format.

    Handles UUIDs, Django lazy strings, dates, and other types
    that aren't directly JSON-serializable.
    """
    import uuid as uuid_module

    from django.utils.functional import Promise

    if isinstance(data, dict):
        return {k: _serialize_response_data(v) for k, v in data.items()}
    if isinstance(data, list):
        return [_serialize_response_data(item) for item in data]
    if isinstance(data, uuid_module.UUID):
        return str(data)
    if isinstance(data, Promise):
        # Django lazy translation strings
        return str(data)
    if hasattr(data, "isoformat"):
        return data.isoformat()
    return data


def _complete_idempotency_key(
    key_record: IdempotencyKey,
    response: Response,
    validation_run=None,
):
    """Update idempotency key with completed response."""
    try:
        response_body = _serialize_response_data(response.data)
    except Exception:
        # Fallback: try rendering to JSON and parsing back
        try:
            response_body = json.loads(response.rendered_content)
        except Exception:
            response_body = {"_serialization_error": True}

    complete_idempotency_key(
        key_record=key_record,
        response_body=response_body,
        response_status=response.status_code,
        validation_run=validation_run,
    )


def _extract_validation_run(response: Response):
    """Extract ValidationRun from response if present."""
    # Check if response data contains a run ID
    if hasattr(response, "data") and isinstance(response.data, dict):
        run_id = response.data.get("id") or response.data.get("run_id")
        if run_id:
            from validibot.validations.models import ValidationRun

            try:
                return ValidationRun.objects.get(pk=run_id)
            except (ValidationRun.DoesNotExist, ValueError):
                pass
    return None


class IdempotencyMixin:
    """
    Mixin for DRF views that provides idempotency key support.

    This mixin is provided for views that want to customize idempotency
    behavior. For most cases, use the @idempotent decorator directly.

    Usage:
        class MyViewSet(IdempotencyMixin, viewsets.ModelViewSet):
            idempotent_actions = ["create", "start_validation"]
    """

    idempotent_actions: list[str] = []
    idempotency_key_header = IDEMPOTENCY_HEADER
    idempotency_ttl_hours = IDEMPOTENCY_KEY_TTL_HOURS

    def get_idempotency_key(self, request) -> str | None:
        """Extract idempotency key from request headers."""
        return request.META.get(self.idempotency_key_header)

    def get_idempotency_endpoint(self) -> str:
        """Generate endpoint identifier for this view action."""
        return _get_endpoint_name(self)
