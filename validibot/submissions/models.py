from __future__ import annotations

import contextlib
import hashlib
import logging
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.core.files.base import File
from django.db import IntegrityError
from django.db import models
from django.db import transaction
from django.db.models import Q
from django.utils.text import slugify
from django.utils.timezone import now
from django.utils.translation import gettext_lazy as _
from model_utils.models import TimeStampedModel

from validibot.projects.models import Project
from validibot.submissions.constants import DataRetention
from validibot.submissions.constants import SubmissionFileType
from validibot.submissions.constants import SubmissionRetention
from validibot.submissions.constants import get_submission_retention_timedelta
from validibot.users.models import Organization
from validibot.users.models import User
from validibot.workflows.models import Workflow

if TYPE_CHECKING:
    from django.core.files.uploadedfile import UploadedFile

logger = logging.getLogger(__name__)


class SubmissionPurgeNotReadyError(RuntimeError):
    """The submission is still required by an active validation run."""


def submission_input_file_upload_to(instance: Submission, filename: str) -> str:
    """
    Generate a unique upload path for submission files based on
    organization and project.
    """
    if not instance:
        err_msg = "Instance must be provided for upload path generation."
        raise ValueError(err_msg)
    if not instance.org_id:
        err_msg = "Submission must be associated with an organization."
        raise ValueError(err_msg)
    if not filename:
        err_msg = "Filename must be provided for upload path generation."
        raise ValueError(err_msg)

    org_part = f"o{instance.org_id}"
    proj_slug = instance.project.slug if instance.project_id else "none"
    proj_part = f"p{proj_slug[:16]}"
    user_part = f"u{instance.user_id}" if instance.user_id else "uanon"
    date_part = now().strftime("%Y%m%d")

    # Slugify filename for URL-safe storage paths while preserving extension.
    # Avoid trusting user-supplied characters; cap the stem length to keep paths sane.
    name_path = Path(filename)
    ext = name_path.suffix.lower()
    safe_stem = slugify(name_path.stem)[:50] or "file"
    safe_name = f"{safe_stem}{ext}"

    unique = uuid.uuid4().hex[:12]
    p = (
        f"submissions/{org_part}/{proj_part}/"
        f"{user_part}/{date_part}/{unique}_{safe_name}"
    )
    return p


def submission_port_file_upload_to(instance, filename: str) -> str:
    """Generate an upload path for additional submitted artifact-port files."""

    if not instance:
        err_msg = "Instance must be provided for upload path generation."
        raise ValueError(err_msg)
    submission = getattr(instance, "submission", None)
    if not submission:
        err_msg = "SubmissionInputFile must be associated with a submission."
        raise ValueError(err_msg)
    if not getattr(submission, "org_id", None):
        err_msg = "Submission must be associated with an organization."
        raise ValueError(err_msg)
    if not filename:
        err_msg = "Filename must be provided for upload path generation."
        raise ValueError(err_msg)

    org_part = f"o{submission.org_id}"
    proj_slug = submission.project.slug if submission.project_id else "none"
    proj_part = f"p{proj_slug[:16]}"
    user_part = f"u{submission.user_id}" if submission.user_id else "uanon"
    date_part = now().strftime("%Y%m%d")
    port_part = slugify(getattr(instance, "port_key", "") or "port")[:40] or "port"

    name_path = Path(filename)
    ext = name_path.suffix.lower()
    safe_stem = slugify(name_path.stem)[:50] or "file"
    safe_name = f"{safe_stem}{ext}"

    unique = uuid.uuid4().hex[:12]
    return (
        f"submissions/{org_part}/{proj_part}/{user_part}/{date_part}/"
        f"ports/{port_part}/{unique}_{safe_name}"
    )


class Submission(TimeStampedModel):
    """
    The actual content sent by a user for validation.
    If the content is large, we store it in a FileField (backed by S3 or similar).
    Otherwise, we store in a TextField in this model.
    """

    class Meta:
        indexes = [
            models.Index(
                fields=[
                    "org",
                    "project",
                    "workflow",
                    "created",
                ],
            ),
            models.Index(
                fields=[
                    "org",
                    "created",
                ],
            ),
        ]
        constraints = [
            # At least one of content or input_file (unless purged)
            models.CheckConstraint(
                name="submission_content_present",
                condition=(
                    Q(content_purged_at__isnull=False)  # Purged - no content needed
                    | Q(content__gt="")
                    | (Q(input_file__isnull=False) & ~Q(input_file=""))
                ),
            ),
            # Not both content and input_file
            models.CheckConstraint(
                name="submission_content_not_both",
                condition=~(
                    Q(content__gt="")
                    & (Q(input_file__isnull=False) & ~Q(input_file=""))
                ),
            ),
            # Purged submissions must have content cleared
            models.CheckConstraint(
                name="submission_purged_content_cleared",
                condition=(
                    Q(content_purged_at__isnull=True)
                    | (Q(content="") & Q(input_file=""))
                ),
            ),
        ]
        ordering = ["-created"]

    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )

    name = models.CharField(
        max_length=256,
        blank=True,
        default="",
        help_text=_("Optional descriptive name."),
    )

    org = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="submissions",
    )

    project = models.ForeignKey(
        Project,
        on_delete=models.CASCADE,
        related_name="submissions",
        null=True,
        blank=True,
    )

    user = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="submissions",
    )

    # ACTUAL CONTENT PROVIDED BY USER
    # ~---------------------------------------------------------------

    # inline text for small JSON/XML/IDF
    content = models.TextField(
        blank=True,
        default="",
    )

    # file upload for larger content
    input_file = models.FileField(
        upload_to=submission_input_file_upload_to,
        help_text=_("The file to validate, e.g. IDF, JSON, XML, etc."),
        null=True,
        blank=True,
    )

    # ~---------------------------------------------------------------

    # Information about that user content ...

    file_type = models.CharField(
        max_length=64,
        choices=SubmissionFileType.choices,
    )

    original_filename = models.CharField(
        max_length=512,
        blank=True,
        default="",
    )

    size_bytes = models.BigIntegerField(default=0)

    checksum_sha256 = models.CharField(
        max_length=64,
        blank=True,
        default="",
    )

    metadata = models.JSONField(
        default=dict,
        blank=True,
    )

    # Info about how/why this submission was created

    workflow = models.ForeignKey(
        Workflow,
        on_delete=models.PROTECT,
        related_name="submissions",
        help_text=_("Workflow *version* to run."),
    )

    latest_run = models.OneToOneField(
        "validations.ValidationRun",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )

    # Retention fields
    # ~---------------------------------------------------------------

    retention_policy = models.CharField(
        max_length=32,
        choices=DataRetention.choices,
        default=DataRetention.DO_NOT_STORE,
        help_text=_("Snapshot of workflow's retention policy at submission time."),
    )

    expires_at = models.DateTimeField(
        null=True,
        blank=True,
        db_index=True,
        help_text=_(
            "When content should be purged (null = already purged or DO_NOT_STORE)."
        ),
    )

    content_purged_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text=_("When content was purged (for audit trail)."),
    )

    # ~---------------------------------------------------------------

    # Methods
    # ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

    def set_content(
        self,
        inline_text: str | None = None,
        uploaded_file: UploadedFile | File | None = None,
        filename: str | None = None,
        inline_max_bytes: int | None = None,
        file_type: str | None = None,
    ):
        """
        Take the content provided by the user in the POST request to start
        a validation run. Store that content either inline (if small enough)
        or in the FileField.

        IMPORTANT: This method does NOT call self.save(). The caller should
        call save() after setting any other fields.

        Args:
            inline_text (str | None, optional):
            uploaded_file (UploadedFile | File | None, optional):
            filename (str | None, optional):
            inline_max (int | None, optional):

        Raises:
            ValueError: _description_
        """

        inline_max_bytes = inline_max_bytes or int(
            getattr(settings, "SUBMISSION_INLINE_MAX_BYTES", 256 * 1024),
        )

        # Exactly one input
        if (inline_text is not None) and (uploaded_file is not None):
            raise ValueError(_("Cannot provide both inline_text and uploaded_file."))
        if (inline_text is None) and (uploaded_file is None):
            raise ValueError(_("Must provide either inline_text or uploaded_file."))

        # INLINE PATH
        if inline_text is not None:
            if not inline_text.strip():
                raise ValueError(_("inline_text cannot be empty."))
            data = inline_text.encode("utf-8")
            self.size_bytes = len(data)
            self.original_filename = filename or self.original_filename or "inline.txt"
            self.checksum_sha256 = self._compute_checksum(data)  # cheap, keep it
            if self.size_bytes <= inline_max_bytes:
                # store inline, delete any prior file
                self.content = inline_text
                if self.input_file:
                    with contextlib.suppress(Exception):
                        self.input_file.delete(save=False)
                self.input_file = None
            else:
                # spill to file storage
                self.content = ""
                if self.input_file:
                    with contextlib.suppress(Exception):
                        self.input_file.delete(save=False)
                final_name = Path(self.original_filename).name
                self.input_file.save(
                    final_name,
                    ContentFile(data),
                    save=False,
                )
                self.original_filename = final_name

        # UPLOAD PATH
        if uploaded_file is not None:
            final_name = filename or getattr(uploaded_file, "name", "") or "upload"
            final_name = Path(final_name).name
            # enforce XOR and delete any prior file to avoid orphans
            self.content = ""
            if self.input_file:
                with contextlib.suppress(Exception):
                    self.input_file.delete(save=False)

            # ensure at start then save in one pass
            with contextlib.suppress(Exception):
                uploaded_file.seek(0)
            self.input_file.save(final_name, uploaded_file, save=False)

            self.original_filename = final_name
            self.size_bytes = getattr(uploaded_file, "size", 0)
            if not self.size_bytes:
                # after save(), storage knows the size
                with contextlib.suppress(Exception):
                    self.size_bytes = self.input_file.size or 0

            # leave checksum blank; save() will backfill it efficiently
            self.checksum_sha256 = ""

        # File type detection (respect explicit valid value)
        if not file_type or file_type not in SubmissionFileType.values:
            file_type = detect_file_type(
                filename=self.original_filename or filename,
                text=inline_text if inline_text is not None else None,
            )
        self.file_type = file_type

        return True  # caller still does self.save()

    def get_content(self) -> str:
        """
        Retrieve the actual content of this submission, whether stored
        inline or in the FileField.

        Returns empty string if content has been purged.

        Returns:
            str: The content as a string, or empty string if purged/unavailable.
        """
        if self.content_purged_at:
            return ""  # Content has been purged
        if self.content:
            return self.content
        if self.input_file:
            try:
                with self.input_file.open("rb") as fh:
                    with contextlib.suppress(Exception):
                        fh.seek(0)
                    data = fh.read()
            except Exception:
                return ""
            return (
                data.decode("utf-8", errors="replace")
                if isinstance(data, bytes)
                else str(data)
            )
        return ""

    def read_bytes(self, *, max_bytes: int | None = None) -> bytes:
        """Return exact submission bytes, optionally enforcing a hard ceiling.

        Binary validators must not pass XLS/XLSX/ZIP data through
        :meth:`get_content`, which decodes file bytes as replacement-text for
        presentation-oriented callers. Reading one sentinel byte beyond the
        ceiling proves oversize input without loading an unbounded file.
        """
        if self.content_purged_at:
            return b""
        if self.content:
            data = self.content.encode("utf-8")
            if max_bytes is not None and len(data) > max_bytes:
                raise ValueError("Submission exceeds the configured byte limit.")
            return data
        if not self.input_file:
            return b""
        with self.input_file.open("rb") as file_handle:
            with contextlib.suppress(Exception):
                file_handle.seek(0)
            data = file_handle.read(None if max_bytes is None else max_bytes + 1)
        if not isinstance(data, bytes):
            data = str(data).encode("utf-8")
        if max_bytes is not None and len(data) > max_bytes:
            raise ValueError("Submission exceeds the configured byte limit.")
        return data

    @property
    def is_content_available(self) -> bool:
        """Check if content is still available (not purged)."""
        return self.content_purged_at is None

    @property
    def is_do_not_store_retention(self) -> bool:
        """Return whether this submission is covered by no-storage retention."""
        return self.retention_policy == SubmissionRetention.DO_NOT_STORE

    @property
    def is_content_retention_expired(self) -> bool:
        """Return whether retained content is past its access window."""
        return bool(self.expires_at and self.expires_at <= now())

    @property
    def is_content_viewable(self) -> bool:
        """Return whether submitted content may be shown in the UI."""
        try:
            policy = SubmissionRetention(self.retention_policy)
        except ValueError:
            return False
        if policy == SubmissionRetention.DO_NOT_STORE:
            return False
        if self.is_content_retention_expired:
            return False
        if not self.is_content_available:
            return False
        return bool(self.content or self.input_file)

    @property
    def has_data_filename(self) -> bool:
        """Return whether the submitted data should be presented with a filename."""
        if self.input_file:
            return True
        if self.content:
            return False
        return bool(self.original_filename)

    @property
    def data_filename(self) -> str:
        """Return the best display filename retained for submitted data."""
        if self.original_filename:
            return self.original_filename
        if self.input_file:
            return Path(self.input_file.name).name
        return ""

    def get_viewable_content(self) -> str:
        """Return submitted content only when retention allows interactive viewing."""
        if not self.is_content_viewable:
            return ""
        return self.get_content()

    def _user_has_submission_access(self) -> bool:
        """Allow org members plus public/guest launchers to own submissions."""
        if not self.user:
            return True
        if self.user.orgs.filter(id=self.org_id).exists():
            return True
        if self.workflow_id:
            return self.workflow.can_execute(user=self.user)
        return False

    def _sync_retention_expiry(self) -> set[str]:
        """Set ``expires_at`` from the saved submission retention policy."""
        touched: set[str] = set()
        if self.content_purged_at:
            if self.expires_at is not None:
                self.expires_at = None
                touched.add("expires_at")
            return touched

        try:
            retention_policy = SubmissionRetention(self.retention_policy)
        except ValueError:
            retention_policy = SubmissionRetention.DO_NOT_STORE
            if self.retention_policy != retention_policy:
                self.retention_policy = retention_policy
                touched.add("retention_policy")

        retention_delta = get_submission_retention_timedelta(retention_policy)
        if retention_delta is None or retention_delta.total_seconds() <= 0:
            if self.expires_at is not None:
                self.expires_at = None
                touched.add("expires_at")
            return touched

        should_set_expiry = self._state.adding or self.expires_at is None
        if not should_set_expiry and self.pk:
            previous_policy = (
                type(self)
                .objects.filter(pk=self.pk)
                .values_list("retention_policy", flat=True)
                .first()
            )
            should_set_expiry = previous_policy != self.retention_policy

        if should_set_expiry:
            self.expires_at = now() + retention_delta
            touched.add("expires_at")
        return touched

    def clean(self, *args, **kwargs):
        errors = {}

        # Require same-org relationships (DB can't enforce this natively)
        if self.project_id and self.project.org_id != self.org_id:
            errors["project"] = _("Project must belong to the same organization.")
        if self.workflow_id and self.workflow.org_id != self.org_id:
            errors["workflow"] = _("Workflow must belong to the same organization.")
        if errors:
            raise ValidationError(errors)

        if self.user and not self._user_has_submission_access():
            errors["user"] = _("User must belong to the same organization.")

        # Content presence: require exactly one of (content, input_file)
        # unless content has been purged
        if not self.content_purged_at:
            has_doc = bool(self.content)
            has_file = bool(self.input_file)
            if not (has_doc ^ has_file):
                errors["content"] = _("Provide exactly one of content or input_file.")

        if errors:
            raise ValidationError(errors)

        super().clean()

    def save(self, *args, **kwargs):
        update_fields = kwargs.get("update_fields")
        touched_fields: set[str] = set()

        # Backfill checksum for stored files with no checksum
        if self.input_file and not self.checksum_sha256:
            try:
                with self.input_file.open("rb"):
                    self.checksum_sha256 = self._compute_checksum_filelike(
                        self.input_file,
                    )
                    touched_fields.add("checksum_sha256")
            except Exception:
                logger.exception(
                    "Failed to compute checksum for submission",
                    extra={"id": self.id},
                )

        # Ensure file_type is set
        if not self.file_type:
            self.file_type = detect_file_type(
                filename=self.original_filename
                or getattr(self.input_file, "name", None),
                text=self.content or None,
            )
            touched_fields.add("file_type")

        touched_fields.update(self._sync_retention_expiry())

        if update_fields is not None and touched_fields:
            kwargs["update_fields"] = set(update_fields) | touched_fields

        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return self.name or f"Submission {self.id}"

    def _compute_checksum_filelike(self, f, chunk_size=1024 * 1024) -> str:
        h = hashlib.sha256()
        can_seek = True
        try:
            pos = f.tell()
        except Exception:
            can_seek = False
            pos = None
        if can_seek:
            with contextlib.suppress(Exception):
                f.seek(0)
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            if isinstance(chunk, str):
                chunk = chunk.encode("utf-8")
            h.update(chunk)
        if can_seek and pos is not None:
            with contextlib.suppress(Exception):
                f.seek(pos)
        return h.hexdigest()

    def _compute_checksum(self, data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    @transaction.atomic
    def purge_content(self) -> None:
        """
        Remove submission content and payload-derived context.

        This method is idempotent - calling it on an already-purged submission
        is a no-op.

        Keeps: id, checksum_sha256, size_bytes, file_type, created,
               retention_policy
        Clears: content, input_file, submitter labels/filenames/metadata
        Sets: content_purged_at
        Also cleans up: copied inputs from every related execution bundle

        Raises:
            SubmissionPurgeNotReadyError: If any related run is still active.
            Exception: If file deletion fails (caller should handle retry)
        """
        # Serialize purge with FK checks from any concurrent run creation. The
        # lock also ensures this method evaluates current policy/purge state
        # rather than a stale caller instance.
        type(self).objects.select_for_update().only("pk").get(pk=self.pk)
        self.refresh_from_db()

        if self.content_purged_at:
            return  # Already purged (idempotent)

        from validibot.validations.constants import VALIDATION_RUN_TERMINAL_STATUSES

        if self.runs.exclude(status__in=VALIDATION_RUN_TERMINAL_STATUSES).exists():
            msg = f"Submission {self.pk} is still required by an active run"
            raise SubmissionPurgeNotReadyError(msg)

        # Delete copied input data before the original input. Output files are
        # deliberately preserved: workflow authors configure input and output
        # retention independently, and no-input-retention must not silently
        # shorten an explicit output-retention window.
        for run in self.runs.all():
            try:
                _delete_run_input_files(run)
            except Exception:
                logger.exception(
                    "Failed to delete copied run inputs",
                    extra={"submission_id": str(self.id), "run_id": str(run.id)},
                )
                raise

        # Delete the original input only after every related run bundle is
        # confirmed absent. A failed input deletion likewise prevents the
        # database from claiming the submission was purged.
        if self.input_file:
            try:
                self.input_file.delete(save=False)
            except Exception:
                logger.exception(
                    "Failed to delete submission file",
                    extra={"id": str(self.id)},
                )
                raise

        for port_file in self.input_files.all():
            port_file.purge_file()

        # Clear submitter-provided context as well as raw bytes. Filenames,
        # display names, and arbitrary metadata can contain personal or
        # proprietary data and are not required for the minimal audit record.
        for run in self.runs.all():
            from validibot.validations.services.retention import (
                redact_run_input_records,
            )

            redact_run_input_records(run)

        self.name = ""
        self.content = ""
        self.input_file = None
        self.original_filename = ""
        self.metadata = {}
        self.content_purged_at = now()
        self.expires_at = None  # No longer pending
        self.save(
            update_fields=[
                "name",
                "content",
                "input_file",
                "original_filename",
                "metadata",
                "content_purged_at",
                "expires_at",
            ],
        )

        logger.info(
            "Purged submission content",
            extra={
                "submission_id": str(self.id),
                "retention_policy": self.retention_policy,
            },
        )


class SubmissionInputFile(TimeStampedModel):
    """Additional file supplied for a declared artifact input port.

    ``Submission`` keeps the historical single primary payload/file contract.
    This model stores extra submitted files, such as an EnergyPlus EPW weather
    file, that populate explicit workflow artifact ports at launch time.
    """

    submission = models.ForeignKey(
        Submission,
        on_delete=models.CASCADE,
        related_name="input_files",
    )
    workflow_step = models.ForeignKey(
        "workflows.WorkflowStep",
        on_delete=models.SET_NULL,
        related_name="+",
        null=True,
        blank=True,
        help_text=_("Workflow step whose artifact port consumes this file."),
    )
    port_key = models.SlugField(
        max_length=255,
        help_text=_("Artifact input port this submitted file satisfies."),
    )
    input_file = models.FileField(
        upload_to=submission_port_file_upload_to,
        null=True,
        blank=True,
        help_text=_("Submitted file for a workflow artifact input port."),
    )
    original_filename = models.CharField(max_length=512, blank=True, default="")
    content_type = models.CharField(max_length=255, blank=True, default="")
    size_bytes = models.BigIntegerField(default=0)
    checksum_sha256 = models.CharField(max_length=64, blank=True, default="")
    file_purged_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text=_("When this port file was purged for retention."),
    )
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["submission", "workflow_step", "port_key"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["submission", "workflow_step", "port_key"],
                name="uniq_submission_step_port_file",
            ),
        ]
        ordering = ["submission_id", "workflow_step_id", "port_key"]

    def set_file(self, *, uploaded_file: UploadedFile | File, filename: str | None):
        """Store one uploaded artifact-port file without saving the model."""

        final_name = filename or getattr(uploaded_file, "name", "") or "upload"
        final_name = Path(final_name).name
        with contextlib.suppress(Exception):
            uploaded_file.seek(0)

        if self.input_file:
            with contextlib.suppress(Exception):
                self.input_file.delete(save=False)

        self.input_file.save(final_name, uploaded_file, save=False)
        self.original_filename = final_name
        self.content_type = getattr(uploaded_file, "content_type", "") or ""
        self.size_bytes = getattr(uploaded_file, "size", 0)
        if not self.size_bytes:
            with contextlib.suppress(Exception):
                self.size_bytes = self.input_file.size or 0
        self.checksum_sha256 = ""
        self.file_purged_at = None

    @property
    def materialized_filename(self) -> str:
        """Return a flat, safe filename for per-run materialization."""

        name = Path(self.original_filename or self.input_file.name or "input").name
        path = Path(name)
        ext = path.suffix.lower()
        stem = slugify(path.stem)[:50] or slugify(self.port_key) or "input"
        port = slugify(self.port_key)[:40] or "port"
        return f"{port}-{stem}{ext}"

    def read_bytes(self) -> bytes:
        """Read the stored port file as bytes."""

        if not self.input_file:
            return b""
        with self.input_file.open("rb") as fh:
            with contextlib.suppress(Exception):
                fh.seek(0)
            data = fh.read()
        return data if isinstance(data, bytes) else str(data).encode("utf-8")

    def purge_file(self) -> None:
        """Delete stored bytes and submitter-provided file context."""

        if self.file_purged_at:
            return
        if self.input_file:
            try:
                self.input_file.delete(save=False)
            except Exception:
                logger.exception(
                    "Failed to delete submission port file",
                    extra={
                        "id": str(self.id),
                        "submission_id": str(self.submission_id),
                    },
                )
                raise
        self.input_file = None
        self.original_filename = ""
        self.content_type = ""
        self.metadata = {}
        self.file_purged_at = now()
        self.save(
            update_fields=[
                "input_file",
                "original_filename",
                "content_type",
                "metadata",
                "file_purged_at",
                "modified",
            ],
        )

    def clean(self, *args, **kwargs):
        errors = {}
        if (
            self.submission_id
            and self.workflow_step_id
            and self.workflow_step.workflow_id != self.submission.workflow_id
        ):
            errors["workflow_step"] = _(
                "Workflow step must belong to the submission workflow."
            )
        if errors:
            raise ValidationError(errors)
        super().clean()

    def save(self, *args, **kwargs):
        update_fields = kwargs.get("update_fields")
        if self.input_file and not self.checksum_sha256:
            try:
                self.checksum_sha256 = self.submission._compute_checksum_filelike(
                    self.input_file,
                )
            except Exception:
                logger.exception(
                    "Failed to compute submitted port-file checksum",
                    extra={
                        "id": str(self.id),
                        "submission_id": str(self.submission_id),
                    },
                )
            else:
                if update_fields is not None:
                    kwargs["update_fields"] = set(update_fields) | {
                        "checksum_sha256",
                    }
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f"{self.submission_id}:{self.port_key}:{self.original_filename}"


def _delete_run_files(run) -> None:
    """
    Delete all files associated with a validation run from storage.

    The Cloud Run launcher writes a run's bundle (``input.json``, ``model.*``,
    ``submission.rdf``, ``output.json``, …) DIRECTLY to its execution-bundle
    location:

        * GCS   — ``gs://<GCS_VALIDATION_BUCKET>/runs/<org_id>/<run_id>/``
        * local — ``<MEDIA_ROOT>/files/runs/<org_id>/<run_id>/``

    Crucially it does NOT go through the ``DataStorage`` abstraction, which
    prepends a ``private/`` prefix (``<bucket>/private/runs/…``). An earlier
    implementation deleted via ``get_data_storage().delete_prefix("runs/…")``,
    which therefore scanned ``private/runs/…`` — a prefix the run bundle is
    never written to — so the real objects survived in GCS even though the run
    was marked purged (a silent retention/privacy failure). This function
    deletes from the SAME raw location the launcher writes to, mirroring its
    GCS-vs-local branch.

    Called during output-retention purge. Submission input cleanup uses the
    selective :func:`_delete_run_input_files` helper so retained outputs remain
    available for their independently configured window.

    Args:
        run: ValidationRun instance

    Note:
        - Safe to call if the run directory doesn't exist (no-op, count 0).
        - On a real deletion failure it logs and re-raises, so callers can
          avoid marking the run/submission purged while objects may remain.
    """
    org_id = str(run.org_id)
    run_id = str(run.id)
    run_path = f"runs/{org_id}/{run_id}/"

    bucket = getattr(settings, "GCS_VALIDATION_BUCKET", "")

    try:
        if bucket:
            # GCS: delete the run bundle from the validation bucket directly,
            # using the same raw client the launcher writes with (NOT the
            # private/-prefixed DataStorage abstraction).
            from validibot.validations.services.cloud_run.gcs_client import (
                delete_prefix,
            )

            count = delete_prefix(f"gs://{bucket}/{run_path}")
        else:
            # Local dev / self-hosted filesystem: mirror the launcher's local
            # layout under MEDIA_ROOT/files/runs/<org>/<run>/.
            import shutil
            from pathlib import Path

            base_dir = Path(settings.MEDIA_ROOT) / "files" / "runs" / org_id / run_id
            if base_dir.exists():
                shutil.rmtree(base_dir)
                count = 1
            else:
                count = 0
    except Exception:
        logger.exception(
            "Failed to delete run files",
            extra={"run_id": run_id, "run_path": run_path},
        )
        raise

    if count > 0:
        logger.info(
            "Deleted run files",
            extra={
                "run_id": run_id,
                "run_path": run_path,
                "files_deleted": count,
            },
        )


def _delete_run_input_files(run) -> None:
    """Delete copied run inputs while preserving independently retained output.

    Cloud execution bundles historically place input files at the attempt root,
    while current local workspaces separate ``input/`` and ``output/``. This
    helper supports both layouts. It preserves only the contracted output
    envelope/directories plus explicit artifact objects; everything else below
    the run prefix is treated as copied input or transient launch data.

    External failures propagate so callers cannot stamp ``content_purged_at``
    while copied input bytes may remain.
    """
    org_id = str(run.org_id)
    run_id = str(run.id)
    run_path = f"runs/{org_id}/{run_id}/"
    bucket = getattr(settings, "GCS_VALIDATION_BUCKET", "")

    attempts = list(run.step_runs.prefetch_related("execution_attempts").all())
    attempt_rows = [
        attempt
        for step_run in attempts
        for attempt in step_run.execution_attempts.all()
    ]

    def _attempt_relative_prefix(attempt) -> str:
        return f"attempts/{attempt.id}/"

    keep_relative_uris = {"output.json"}
    keep_relative_prefixes = {"output/", "outputs/"}
    for attempt in attempt_rows:
        relative_prefix = _attempt_relative_prefix(attempt)
        keep_relative_uris.add(f"{relative_prefix}output.json")
        keep_relative_prefixes.add(f"{relative_prefix}output/")
        keep_relative_prefixes.add(f"{relative_prefix}outputs/")

        output_uri = (attempt.output_envelope_uri or "").strip()
        expected_prefix = f"gs://{bucket}/{run_path}" if bucket else ""
        if expected_prefix and output_uri.startswith(expected_prefix):
            keep_relative_uris.add(output_uri[len(expected_prefix) :])

    for artifact in run.artifacts.all():
        for uri in (artifact.storage_uri, artifact.manifest_uri):
            expected_prefix = f"gs://{bucket}/{run_path}" if bucket else ""
            if expected_prefix and uri.startswith(expected_prefix):
                keep_relative_uris.add(uri[len(expected_prefix) :])

    try:
        if bucket:
            from validibot.validations.services.cloud_run.gcs_client import (
                delete_prefix_except,
            )

            base_uri = f"gs://{bucket}/{run_path}"
            count = delete_prefix_except(
                base_uri,
                keep_uris=tuple(
                    f"{base_uri}{relative}" for relative in sorted(keep_relative_uris)
                ),
                keep_prefixes=tuple(
                    f"{base_uri}{relative}"
                    for relative in sorted(keep_relative_prefixes)
                ),
            )
        else:
            base_dir = Path(settings.MEDIA_ROOT) / "files" / "runs" / org_id / run_id
            count = 0
            if base_dir.exists():
                for path in sorted(
                    base_dir.rglob("*"),
                    key=lambda item: len(item.parts),
                    reverse=True,
                ):
                    relative = path.relative_to(base_dir).as_posix()
                    if path.is_file() or path.is_symlink():
                        if relative in keep_relative_uris or any(
                            relative.startswith(prefix)
                            for prefix in keep_relative_prefixes
                        ):
                            continue
                        path.unlink()
                        count += 1
                    elif path.is_dir():
                        with contextlib.suppress(OSError):
                            path.rmdir()
                with contextlib.suppress(OSError):
                    base_dir.rmdir()
    except Exception:
        logger.exception(
            "Failed to delete copied run inputs",
            extra={"run_id": run_id, "run_path": run_path},
        )
        raise

    if count > 0:
        logger.info(
            "Deleted copied run inputs",
            extra={
                "run_id": run_id,
                "run_path": run_path,
                "files_deleted": count,
            },
        )


def detect_file_type(
    *,
    filename: str | None = None,
    text: str | None = None,
) -> str:
    name = (filename or "").lower()
    if name.endswith((".json", ".epjson")):
        return SubmissionFileType.JSON
    if name.endswith(".jsonld"):
        return SubmissionFileType.JSON
    if name.endswith(".xml"):
        return SubmissionFileType.XML
    if name.endswith(".rdf"):
        return SubmissionFileType.XML
    if name.endswith((".yaml", ".yml")):
        return SubmissionFileType.YAML
    if name.endswith((".ttl", ".nt", ".nq")):
        return SubmissionFileType.TEXT
    if name.endswith(".idf") or "energyplus" in name:
        return SubmissionFileType.TEXT
    if name.endswith(".thmx"):
        return SubmissionFileType.XML
    if name.endswith(".thmz"):
        return SubmissionFileType.BINARY
    if name.endswith(".pdf"):
        return SubmissionFileType.PDF
    if name.endswith((".fmu", ".xls", ".xlsx", ".zip")):
        return SubmissionFileType.BINARY
    if name.endswith((".step", ".stp", ".p21")):
        return SubmissionFileType.TEXT
    if text:
        s = text.lstrip()
        if s.startswith(("{", "[")):
            return SubmissionFileType.JSON
        if s.startswith("<"):
            return SubmissionFileType.XML
        if s.startswith(("---", "- ")):
            return SubmissionFileType.YAML
    return SubmissionFileType.UNKNOWN  # default fallback


class PurgeRetry(models.Model):
    """
    Track submissions that need purge processing.

    A scheduled job processes these records and attempts to purge the
    associated submissions (for example DO_NOT_STORE submissions when
    a validation run completes, or retries after transient failures).

    This ensures data retention policies are eventually enforced
    even when transient failures occur.
    """

    # Deletion is never abandoned automatically. This threshold is an
    # operational alert boundary; retries continue with capped backoff.
    MAX_ATTEMPTS = 5
    RETRY_DELAYS = [60, 300, 3600, 21600, 86400]  # 1m, 5m, 1h, 6h, 24h

    submission = models.ForeignKey(
        Submission,
        on_delete=models.CASCADE,
        related_name="purge_retries",
        help_text=_("Submission that failed to purge."),
    )

    created_at = models.DateTimeField(
        auto_now_add=True,
        help_text=_("When the initial purge failure occurred."),
    )

    last_attempt_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text=_("When the last retry attempt was made."),
    )

    next_retry_at = models.DateTimeField(
        db_index=True,
        help_text=_("When to attempt the next retry."),
    )

    attempt_count = models.PositiveIntegerField(
        default=0,
        help_text=_("Number of retry attempts made."),
    )

    last_error = models.TextField(
        blank=True,
        default="",
        help_text=_("Error message from the last failed attempt."),
    )

    class Meta:
        verbose_name_plural = "Purge retries"
        indexes = [
            models.Index(fields=["next_retry_at", "attempt_count"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["submission"],
                name="uq_purge_retry_submission",
            ),
        ]

    def __str__(self) -> str:
        return f"PurgeRetry({self.submission_id}, attempts={self.attempt_count})"

    def record_failure(self, error: str) -> None:
        """
        Record a failed purge attempt and schedule the next retry.

        Uses exponential backoff with the delays defined in RETRY_DELAYS,
        capped at the final delay. ``MAX_ATTEMPTS`` is an alert threshold,
        not a point where privacy-critical deletion is abandoned.
        """
        from django.utils import timezone

        self.attempt_count += 1
        self.last_attempt_at = timezone.now()
        self.last_error = str(error)[:2000]  # Truncate long errors

        delay_seconds = self.RETRY_DELAYS[
            min(self.attempt_count - 1, len(self.RETRY_DELAYS) - 1)
        ]
        self.next_retry_at = timezone.now() + timezone.timedelta(
            seconds=delay_seconds,
        )

        self.save()


def queue_submission_purge(submission: Submission | None) -> None:
    """
    Queue a purge attempt for a submission.

    This is primarily used to enforce DO_NOT_STORE retention after a validation
    run completes without blocking request paths that also do critical state
    updates (for example Cloud Run Job callbacks).

    The scheduled `process_purge_retries` job performs the actual purge work.

    Args:
        submission: Submission to purge (no-op when None or already purged).
    """
    if not submission:
        return

    if submission.content_purged_at:
        return

    from django.utils import timezone

    from validibot.submissions.constants import SubmissionRetention

    if submission.retention_policy != SubmissionRetention.DO_NOT_STORE:
        return

    from validibot.validations.constants import VALIDATION_RUN_TERMINAL_STATUSES

    if submission.runs.exclude(status__in=VALIDATION_RUN_TERMINAL_STATUSES).exists():
        # One submission may be referenced by more than one run. Deleting after
        # the first completion would break a still-active sibling run.
        return

    now = timezone.now()
    try:
        with transaction.atomic():
            retry, created = PurgeRetry.objects.get_or_create(
                submission=submission,
                defaults={"next_retry_at": now},
            )
    except IntegrityError:
        # A concurrent terminal hook won the unique target row race.
        retry = PurgeRetry.objects.get(submission=submission)
        created = False
    if created:
        return

    if retry.next_retry_at > now:
        retry.next_retry_at = now
        retry.save(update_fields=["next_retry_at"])
