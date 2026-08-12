from __future__ import annotations

import uuid

from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from model_utils.models import TimeStampedModel

from validibot.core.constants import InviteStatus
from validibot.core.metadata import MetadataPolicyError
from validibot.core.metadata import canonical_metadata_bytes
from validibot.users.models import User


class SupportMessage(TimeStampedModel):
    """
    Simple model to hold user support messages.
    """

    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="support_messages",
    )
    subject = models.CharField(max_length=1000)
    message = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.subject


class SiteSettings(TimeStampedModel):
    """
    Singleton-style container for platform-wide configuration.

    A single row (slugged as ``default``) holds settings that control how the
    platform processes submissions and other cross-cutting concerns.  System
    administrators manage these values via the Django admin.
    """

    DEFAULT_SLUG = "default"

    slug = models.SlugField(
        max_length=100,
        unique=True,
        default=DEFAULT_SLUG,
        help_text="Identifier for this settings record. Only 'default' is used.",
    )

    # ── Submission metadata policy ─────────────────────────────────────
    metadata_key_value_only = models.BooleanField(
        default=False,
        help_text=_(
            "When checked, metadata values must be scalars (no nested lists or dicts).",
        ),
    )
    metadata_max_bytes = models.PositiveIntegerField(
        default=4096,
        help_text=_(
            "Maximum size (in bytes) of stored metadata per submission. "
            "Set to 0 to disable the limit.",
        ),
    )
    metadata_max_depth = models.PositiveIntegerField(
        default=8,
        help_text=_(
            "Maximum nesting depth for submission metadata, counting the "
            "top-level object as depth 1. Set to 0 to disable the limit.",
        ),
    )

    # ── Guest access kill-switches ─────────────────────────────────────
    # Two booleans operators can flip to lock down the guest-account
    # experience without a code change or migration. ``allow_guest_access``
    # gates login for GUEST-classified users; ``allow_guest_invites``
    # gates the creation AND acceptance of guest invites (two-sided so
    # in-flight invites cannot sneak through during a temporary
    # disable). Both default to True so an existing deployment that
    # upgrades across the schema change keeps its previous behaviour.
    allow_guest_access = models.BooleanField(
        default=True,
        help_text=_(
            "When False, users classified as GUEST cannot log in. "
            "Existing guest accounts are not deleted — just denied "
            "access while the flag is False.",
        ),
    )
    allow_guest_invites = models.BooleanField(
        default=True,
        help_text=_(
            "When False, no user (other than superusers) can create OR "
            "accept guest invites, regardless of role. Two-sided gate "
            "so pending invites cannot be redeemed during a disable "
            "window.",
        ),
    )

    # ── Catch-all for future complex settings ──────────────────────────
    data = models.JSONField(
        default=dict,
        blank=True,
        help_text="JSON catch-all for settings not yet promoted to model fields.",
    )

    class Meta:
        verbose_name = "Site settings"
        verbose_name_plural = "Site settings"

    def __str__(self):
        return f"SiteSettings<{self.slug}>"

    def enforce_metadata_policy(self, metadata: object) -> None:
        """Validate submission metadata against the configured policy.

        Raises ``MetadataPolicyError`` if metadata is not a finite JSON object
        or violates the scalar-only, maximum-depth, or canonical-byte limits.
        """
        if self.metadata_key_value_only:
            if not isinstance(metadata, dict):
                raise MetadataPolicyError("Submission metadata must be a JSON object.")
            for key, value in metadata.items():
                if isinstance(value, (dict, list)):
                    raise MetadataPolicyError(
                        f"Metadata value for '{key}' must be a scalar when "
                        "key/value enforcement is enabled.",
                    )
        canonical_bytes = canonical_metadata_bytes(
            metadata,
            max_depth=self.metadata_max_depth,
        )
        if self.metadata_max_bytes > 0:
            if len(canonical_bytes) > self.metadata_max_bytes:
                raise MetadataPolicyError(
                    "Metadata is too large for this workflow start request.",
                )


class CredentialVerificationKey(TimeStampedModel):
    """Public key retained so historical credentials remain verifiable.

    Signing backends and private key material remain Pro-owned. This community
    model stores only the public JWK needed by the instance-local verifier and
    JWKS endpoint. Rows are intentionally permanent during ordinary rotation.
    """

    kid = models.CharField(
        max_length=255,
        unique=True,
        help_text=_("Stable key identifier published in credential JOSE headers."),
    )
    jwk = models.JSONField(
        help_text=_("Public JSON Web Key used for verification."),
    )
    provider_reference = models.CharField(
        max_length=1024,
        blank=True,
        help_text=_(
            "Optional provider reference, such as a Google Cloud KMS key version."
        ),
    )

    class Meta:
        ordering = ["created", "kid"]
        verbose_name = "Credential verification key"
        verbose_name_plural = "Credential verification keys"

    def __str__(self) -> str:
        return self.kid


class BaseInvite(TimeStampedModel):
    """
    Abstract base model for all invite types.

    Provides common fields and lifecycle methods for:
    - MemberInvite (org membership invites)
    - GuestInvite (multi-workflow guest access)
    - WorkflowInvite (single workflow guest access)

    Each subclass must define:
    - inviter ForeignKey (with unique related_name)
    - invitee_user ForeignKey (with unique related_name)
    - accept() method (return types differ per invite type)
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    invitee_email = models.EmailField(blank=True)
    status = models.CharField(
        max_length=16,
        choices=InviteStatus.choices,
        default=InviteStatus.PENDING,
    )
    token = models.UUIDField(default=uuid.uuid4, editable=False, unique=True)
    expires_at = models.DateTimeField()

    class Meta:
        abstract = True
        ordering = ["-created"]

    @property
    def is_expired(self) -> bool:
        """Check if invite has expired without updating status."""
        return self.status == InviteStatus.EXPIRED or timezone.now() >= self.expires_at

    @property
    def is_pending(self) -> bool:
        """Check if invite is still pending and not expired."""
        return self.status == InviteStatus.PENDING and not self.is_expired

    def mark_expired_if_needed(self) -> bool:
        """
        Check if invite has expired and update status if so.

        Returns:
            True if the invite was marked as expired, False otherwise.
        """
        if self.status != InviteStatus.PENDING:
            return False

        if timezone.now() >= self.expires_at:
            self.status = InviteStatus.EXPIRED
            self.save(update_fields=["status", "modified"])
            return True

        return False

    def _validate_pending_status(self) -> None:
        """
        Validate invite is pending before accepting.

        Called by subclass accept() methods to ensure invite is in valid state.

        Raises:
            ValueError: If invite is not in PENDING status or has expired.
        """
        if self.mark_expired_if_needed():
            raise ValueError("Invite has expired")

        if self.status != InviteStatus.PENDING:
            msg = f"Cannot accept invite with status {self.status}"
            raise ValueError(msg)

    def decline(self) -> None:
        """Mark invite as declined."""
        if self.status != InviteStatus.PENDING:
            return
        self.status = InviteStatus.DECLINED
        self.save(update_fields=["status", "modified"])

    def cancel(self) -> None:
        """Mark invite as canceled (by inviter)."""
        if self.status != InviteStatus.PENDING:
            return
        self.status = InviteStatus.CANCELED
        self.save(update_fields=["status", "modified"])


# Default TTL for idempotency keys (24 hours)
IDEMPOTENCY_KEY_TTL_HOURS = 24


class IdempotencyKeyStatus(models.TextChoices):
    """
    Status of an idempotency key request.

    Used for API request deduplication. Requests are marked PROCESSING on
    receipt, then updated to COMPLETED when done.
    """

    PROCESSING = "PROCESSING", _("Processing")
    COMPLETED = "COMPLETED", _("Completed")


class CallbackReceiptStatus(models.TextChoices):
    """
    Status of a callback receipt for validator callbacks.

    Used for validator callback deduplication. Callbacks are marked PROCESSING
    on receipt, then move to a terminal state:

    - COMPLETED: processing finished successfully.
    - REJECTED: processing hit a PERMANENT, non-retryable error (e.g. the
      ``result_uri`` failed the per-run allowlist, no output envelope class is
      registered, or the envelope's validator/run IDs didn't match). Marking it
      terminal stops Cloud Tasks from retrying a callback that can never
      succeed and records the outcome honestly in the audit trail. Transient
      failures (e.g. a storage blip while downloading the envelope) deliberately
      leave the receipt PROCESSING so a retry can re-attempt.
    """

    PROCESSING = "PROCESSING", _("Processing")
    COMPLETED = "COMPLETED", _("Completed")
    REJECTED = "REJECTED", _("Rejected")


class IdempotencyKey(TimeStampedModel):
    """
    Stores idempotency keys to prevent duplicate API requests.

    Keys are scoped to an organization and endpoint. When a request arrives
    with a key we've seen before, we return the stored response instead of
    processing the request again.

    This follows the Stripe idempotency pattern:
    - Client sends Idempotency-Key header with a unique identifier
    - Server stores the key and response for 24 hours
    - Duplicate requests return the cached response
    - Different request body with same key returns 422 error
    """

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["org", "key", "endpoint"],
                name="uq_idempotency_org_key_endpoint",
            ),
        ]
        indexes = [
            models.Index(fields=["org", "key", "endpoint"]),
            models.Index(fields=["expires_at"]),
        ]
        verbose_name = "Idempotency key"
        verbose_name_plural = "Idempotency keys"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    org = models.ForeignKey(
        "users.Organization",
        on_delete=models.CASCADE,
        related_name="idempotency_keys",
    )
    key = models.CharField(max_length=255, db_index=True)
    endpoint = models.CharField(max_length=100)

    # Request fingerprint to detect key reuse with different payload
    request_hash = models.CharField(max_length=64)

    # Processing status - distinguishes in-flight from completed requests
    status = models.CharField(
        max_length=20,
        choices=IdempotencyKeyStatus,
        default=IdempotencyKeyStatus.PROCESSING,
    )

    # Cached response (populated when request completes)
    response_status = models.SmallIntegerField(null=True, blank=True)
    response_body = models.JSONField(null=True, blank=True)
    response_headers = models.JSONField(default=dict, blank=True)

    # Reference to created resource (if applicable)
    validation_run = models.ForeignKey(
        "validations.ValidationRun",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )

    # Expiration
    expires_at = models.DateTimeField()

    # For debugging
    request_ip = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=500, blank=True)

    def save(self, *args, **kwargs):
        """Set expiration time on first save."""
        if not self.expires_at:
            self.expires_at = timezone.now() + timezone.timedelta(
                hours=IDEMPOTENCY_KEY_TTL_HOURS,
            )
        super().save(*args, **kwargs)

    def __str__(self):
        return f"IdempotencyKey({self.key[:8]}... for {self.endpoint})"
