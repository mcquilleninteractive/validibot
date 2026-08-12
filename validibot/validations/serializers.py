from __future__ import annotations

import base64
import binascii
import json
from typing import Any

from django.apps import apps
from django.conf import settings
from django.urls import NoReverseMatch
from django.utils.translation import gettext_lazy as _
from rest_framework import serializers
from rest_framework.relations import PrimaryKeyRelatedField
from rest_framework.relations import SlugRelatedField
from rest_framework.reverse import reverse

from validibot.submissions.constants import SubmissionFileType
from validibot.validations.constants import VALIDATION_RUN_SHORT_DESCRIPTION_MAX_LENGTH
from validibot.validations.constants import VALIDATION_RUN_TERMINAL_STATUSES
from validibot.validations.constants import ValidationRunErrorCategory
from validibot.validations.constants import ValidationRunResult
from validibot.validations.constants import ValidationRunStatus
from validibot.validations.constants import project_run_state
from validibot.validations.models import ValidationRun
from validibot.validations.services.findings_display import summarize_failed_rows
from validibot.workflows.constants import SUPPORTED_CONTENT_TYPES

CONTENT_TYPE_BY_FILE_TYPE = {
    file_type: content_type
    for content_type, file_type in SUPPORTED_CONTENT_TYPES.items()
}


class ValidationRunSerializer(serializers.ModelSerializer):
    """
    Provides a read-only view into the status of a ValidationRun.
    This is the serializer used by the API to return information
    to the user about an existing, in-progress or completed run.
    """

    workflow = PrimaryKeyRelatedField(read_only=True)

    workflow_slug = serializers.CharField(
        source="workflow.slug",
        read_only=True,
    )

    org = SlugRelatedField(
        read_only=True,
        slug_field="slug",
    )

    user = PrimaryKeyRelatedField(read_only=True)

    submission = PrimaryKeyRelatedField(
        read_only=True,
    )

    state = serializers.SerializerMethodField()

    result = serializers.SerializerMethodField()

    user_friendly_error = serializers.CharField(
        read_only=True,
    )

    steps = serializers.SerializerMethodField()

    credential = serializers.SerializerMethodField()

    def get_state(self, obj: ValidationRun) -> str:
        # Delegated to ``project_run_state`` so this projection has one
        # implementation across the community API and the cloud agent
        # endpoints.
        return project_run_state(obj.status)

    def get_result(self, obj: ValidationRun) -> str:
        """
        Derive a stable run outcome for automation.

        - PASS: Run succeeded.
        - FAIL: Run completed but validation failed (user input issues).
        - ERROR: Run failed due to runtime/system issues.
        - CANCELED / TIMED_OUT: Terminal non-success outcomes.
        - UNKNOWN: Run not yet completed.
        """

        if obj.status == ValidationRunStatus.SUCCEEDED:
            return ValidationRunResult.PASS
        if obj.status == ValidationRunStatus.CANCELED:
            return ValidationRunResult.CANCELED
        if obj.status == ValidationRunStatus.TIMED_OUT:
            return ValidationRunResult.TIMED_OUT

        if obj.status == ValidationRunStatus.FAILED:
            if obj.error_category == ValidationRunErrorCategory.VALIDATION_FAILED:
                return ValidationRunResult.FAIL
            if obj.error_category == ValidationRunErrorCategory.TIMEOUT:
                return ValidationRunResult.TIMED_OUT
            return ValidationRunResult.ERROR

        if obj.status in VALIDATION_RUN_TERMINAL_STATUSES:
            return ValidationRunResult.ERROR

        return ValidationRunResult.UNKNOWN

    def get_steps(self, obj: ValidationRun) -> list[dict]:
        if not obj.are_outputs_viewable:
            return []

        from validibot.validations.services.step_output_display import (
            build_display_step_outputs,
        )
        from validibot.validations.services.step_output_display import (
            build_template_params_display,
        )

        step_runs = list(obj.step_runs.all())
        if not step_runs:
            return []
        step_runs.sort(key=lambda sr: (sr.step_order or 0, sr.pk))
        payload: list[dict] = []
        for step_run in step_runs:
            workflow_step = getattr(step_run, "workflow_step", None)
            findings = list(step_run.findings.all())
            output = step_run.output or {}

            # Enrich with display-ready step outputs and template parameters.
            display_outputs = build_display_step_outputs(step_run)
            params = build_template_params_display(step_run)

            payload.append(
                {
                    "step_id": step_run.workflow_step_id or step_run.pk,
                    "name": getattr(workflow_step, "name", _("Step")),
                    "status": step_run.status,
                    "issues": [
                        {
                            "id": finding.id,
                            "message": finding.message,
                            "path": finding.path,
                            "severity": finding.severity,
                            "code": finding.code,
                            "assertion_id": finding.ruleset_assertion_id,
                            # Structured failing-row examples ({sample_rows,
                            # count, truncated}) for validators that aggregate a
                            # bulk failure into one finding; None otherwise.
                            "failed_rows": summarize_failed_rows(finding.meta),
                        }
                        for finding in findings
                    ],
                    "error": step_run.error,
                    "output_values": [
                        {
                            "slug": output.slug,
                            "label": output.label,
                            "value": output.value,
                            "formatted_value": output.formatted_value,
                            "units": output.units,
                        }
                        for output in display_outputs
                    ],
                    "template_parameters_used": (
                        [
                            {
                                "name": p["name"],
                                "label": p["label"],
                                "value": p["value"],
                                "units": p["units"],
                            }
                            for p in params
                        ]
                        or None
                    ),
                    "template_warnings": output.get("template_warnings"),
                },
            )
        return payload

    def _has_credential_action(self, instance: ValidationRun) -> bool:
        """Return whether the run's workflow has a signed-credential step.

        ``Workflow.has_signed_credential_action`` is a property that
        issues an ``EXISTS`` subquery every time it's accessed. The
        serializer reads the same answer in two places —
        :meth:`get_credential` (to decide whether to query Pro) and
        :meth:`to_representation` (to decide whether to pop the field)
        — and the API viewsets list many rows at once. Three layers
        of fallback here, from best to worst:

        1. **Queryset annotation** (``_has_credential_action``) on the
           run itself. The viewsets in ``api/viewsets.py`` and
           ``api_views.py`` apply this via ``.annotate(Exists(...))``,
           so the whole list is computed in one SQL round-trip instead
           of one ``EXISTS`` per row.
        2. **Per-instance cache** (``_cached_has_credential_action``)
           populated on first property-fallback access. Collapses the
           two intra-serialization calls to one.
        3. **Property access** on the workflow. Still correct, just
           slow — covers callers who build a ValidationRun directly
           in memory without going through the viewset.
        """
        annotated = getattr(instance, "_has_credential_action", None)
        if annotated is not None:
            return bool(annotated)

        cached = getattr(instance, "_cached_has_credential_action", None)
        if cached is not None:
            return cached
        value = instance.workflow.has_signed_credential_action
        instance._cached_has_credential_action = value
        return value

    def get_credential(self, obj: ValidationRun) -> dict | None:
        """Return credential metadata if one was issued for this run.

        Returns None when no credential exists (community-only install,
        Pro app not registered, or the run didn't have a credential
        action). The compact JWS is not inlined — use the download_url
        instead.

        The ``apps.is_installed`` gate is the right question to ask
        here, not "is the feature flag enabled". The feature registry
        gets populated as a side-effect of importing ``validibot_pro``,
        which can happen indirectly during test collection — leaving
        the flag True even when the app isn't actually wired into
        ``INSTALLED_APPS``. Querying ``IssuedCredential`` in that state
        raises ``ValueError: Related model 'validibot_pro.
        SignedCredentialAction' cannot be resolved`` because the FK
        target is unreachable. ``apps.is_installed`` reflects the
        actual Django configuration, so it's both the correct gate
        and test-isolation safe.

        We also short-circuit when the run's workflow has no signed-
        credential action configured — see refactor-step item
        ``[review-#5]``. ``to_representation`` pops the ``credential``
        field in that case, but only after every ``SerializerMethodField``
        getter has already run. Skipping the ``IssuedCredential`` query
        up front saves one DB round-trip per run for the common case
        of workflows that never issue credentials (the majority of
        runs in any org).
        """
        if not self._has_credential_action(obj):
            return None

        if not apps.is_installed("validibot_pro"):
            return None

        from validibot_pro.credentials.models import IssuedCredential

        credential = IssuedCredential.objects.filter(workflow_run=obj).first()

        if credential is None:
            return None

        request = self.context.get("request")
        try:
            download_url = reverse(
                "api:org-runs-credential-download",
                kwargs={
                    "org_slug": obj.org.slug,
                    "pk": obj.id,
                },
                request=request,
            )
        except NoReverseMatch:
            download_url = (
                f"/api/v1/orgs/{obj.org.slug}/runs/{obj.id}/credential/download/"
            )

        return {
            "id": str(credential.id),
            "media_type": credential.media_type,
            "issued_at": (
                credential.created.isoformat() if credential.created else None
            ),
            "download_url": download_url,
        }

    def to_representation(self, instance):
        """Apply credential and retention-aware response projection rules."""
        data = super().to_representation(instance)
        if not self._has_credential_action(instance):
            data.pop("credential", None)
        if not instance.are_outputs_viewable:
            data["error"] = ""
            data["user_friendly_error"] = ""
            data["output_hash"] = ""
            data["steps"] = []
        return data

    class Meta:
        model = ValidationRun
        fields = [
            "id",
            "status",
            "state",
            "result",
            "source",
            "error_category",
            "org",
            "user",
            "workflow",
            "workflow_slug",
            # "project", # Not implemented yet...
            "submission",
            "started_at",
            "ended_at",
            "duration_ms",
            # "summary", # We use "steps" field to dig into summary and get steps.
            "steps",
            "credential",
            "error",
            "user_friendly_error",
            "output_hash",
            "output_retention_policy",
            "output_expires_at",
            "output_purged_at",
        ]
        read_only_fields = fields


class FlexibleContentField(serializers.CharField):
    """
    Accepts either a string payload or JSON-like objects.
    Dict/list values are passed through for later coercion in ``validate``.
    """

    def to_internal_value(self, data):
        if isinstance(data, (dict, list)):
            return data
        if isinstance(data, (bytes, bytearray)):
            try:
                data = data.decode("utf-8")
            except UnicodeDecodeError:
                data = data.decode("latin-1")
        return super().to_internal_value(data)


class ValidationRunStartSerializer(serializers.Serializer):
    """
    Normalizes Workflow start requests for JSON-envelope and multipart inputs.

    The view instantiates this serializer for:
      * Mode 2 (application/json envelope) - we accept strings, dicts, or lists
        in ``content`` and coerce them to text via ``FlexibleContentField``.
      * Mode 3 (multipart/form-data uploads) - we expect a ``file`` part plus
        optional metadata overrides.

    Validated data always contains exactly one of ``normalized_content`` (text)
    or ``file``; downstream submission creation relies on that contract.
    """

    # Optional org for sanity checking (not required; view can enforce match)
    org = serializers.IntegerField(required=False)

    # Envelope textual content
    content = FlexibleContentField(required=False)  # plain or base64 text
    content_type = serializers.CharField(required=False)
    content_encoding = serializers.ChoiceField(
        choices=["base64"],
        required=False,
        allow_null=True,
        help_text="Only 'base64' if provided.",
    )

    filename = serializers.CharField(required=False, allow_blank=True)
    metadata = serializers.JSONField(required=False, default=dict)
    # Submitter-set run description, surfaced to assertions as
    # ``submission.short_description`` (ADR-2026-06-03b). Accepted on the API
    # as a trusted-setter field — it is NOT gated by the workflow's
    # ``allow_submission_short_description`` flag, which governs only the
    # anonymous web form. (filename maps to ``submission.name``; metadata to
    # the ``submission.metadata.*`` bag.)
    #
    # ``max_length`` mirrors the model column (single source:
    # ``VALIDATION_RUN_SHORT_DESCRIPTION_MAX_LENGTH``) so an over-long value
    # returns a clean 400 here instead of reaching ``ValidationRun.objects
    # .create(**extra)`` (which skips ``full_clean``) and erroring at the DB
    # as a 500.
    short_description = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=VALIDATION_RUN_SHORT_DESCRIPTION_MAX_LENGTH,
    )

    # Multipart binary file
    file = serializers.FileField(required=False)

    def to_internal_value(self, data: Any):
        """
        Allow metadata to arrive as a JSON string (multipart) and coerce.
        """
        iv = super().to_internal_value(data)
        meta = iv.get("metadata")
        if isinstance(meta, str):
            meta = meta.strip()
            if meta:
                try:
                    iv["metadata"] = json.loads(meta)
                except json.JSONDecodeError as e:
                    raise serializers.ValidationError(
                        {
                            "metadata": _("Must be valid JSON."),
                        },
                    ) from e
            else:
                iv["metadata"] = {}
        return iv

    def _map_content_type(self, ct: str):
        if not ct:
            raise serializers.ValidationError(
                {
                    "content_type": _("content_type is required."),
                },
            )
        lowered = ct.split(";", maxsplit=1)[0].strip().lower()
        if lowered not in SUPPORTED_CONTENT_TYPES:
            raise serializers.ValidationError(
                {
                    "content_type": _(
                        "Unsupported content_type '%(ct)s'. Supported: %(supported)s"
                    )
                    % {
                        "ct": ct,
                        "supported": ", ".join(SUPPORTED_CONTENT_TYPES),
                    },
                },
            )
        return lowered, SUPPORTED_CONTENT_TYPES[lowered]

    def validate(self, attrs):
        file_obj = attrs.get("file")
        raw_content = attrs.get("content")
        content_type = attrs.get("content_type")
        content_encoding = attrs.get("content_encoding")
        filename = attrs.get("filename")

        normalized_content = raw_content
        if isinstance(normalized_content, (dict, list)):
            # Preserve a consistent storage format by converting Python lists/dicts
            # into JSON strings. This matches the raw JSON payload workflows expect
            # when they ingest submissions later in the pipeline.
            normalized_content = json.dumps(normalized_content)
        elif normalized_content is not None and not isinstance(normalized_content, str):
            normalized_content = str(normalized_content)

        self._check_content(normalized_content, content_encoding)

        # Exactly one of file OR content
        if (file_obj is None and normalized_content is None) or (
            file_obj is not None and normalized_content is not None
        ):
            raise serializers.ValidationError(
                _(
                    "Provide exactly one of 'file' (multipart) "
                    "or 'content' (JSON envelope).",
                ),
            )

        # File path
        if file_obj is not None:
            # tries to read file_obj.content_type (Django's UploadedFile usually
            # sets this from the multipart part's header)
            guessed_ct = content_type or getattr(file_obj, "content_type", None)
            if not guessed_ct:
                raise serializers.ValidationError(
                    {
                        "content_type": _(
                            "content_type required (or detectable from file).",
                        ),
                    },
                )
            lowered, file_type = self._map_content_type(guessed_ct)
            attrs["content_type"] = lowered
            attrs["file_type"] = file_type
            return attrs

        # Textual path
        lowered = file_type = None
        if content_type:
            lowered, file_type = self._map_content_type(content_type)
        else:
            lowered, file_type = self._infer_content_type(
                raw_content=raw_content,
                normalized_content=normalized_content,
                filename=filename,
                content_encoding=content_encoding,
            )
            if lowered is None or file_type is None:
                raise serializers.ValidationError(
                    {
                        "content_type": _("content_type is required with content."),
                    },
                )

        attrs["content_type"] = lowered
        attrs["file_type"] = file_type

        # Base64 decode if requested
        if content_encoding == "base64":
            if normalized_content is None:
                raise serializers.ValidationError(
                    {
                        "content": _("Invalid base64 content."),
                    },
                )
            try:
                decoded = base64.b64decode(normalized_content, validate=True)
            except (binascii.Error, ValueError) as e:
                raise serializers.ValidationError(
                    {
                        "content": _("Invalid base64 content."),
                    },
                ) from e
            try:
                normalized_content = decoded.decode("utf-8")
            except UnicodeDecodeError:
                # Fallback...best effort
                normalized_content = decoded.decode("latin-1")

        attrs["normalized_content"] = normalized_content
        attrs["content"] = normalized_content
        return attrs

    def _infer_content_type(
        self,
        *,
        raw_content: Any,
        normalized_content: str | None,
        filename: str | None,
        content_encoding: str | None,
    ) -> tuple[str | None, SubmissionFileType | None]:
        """
        Attempt to derive a supported content type when the client did not
        supply one explicitly.
        """
        guess = self._guess_file_type(
            raw_content=raw_content,
            normalized_content=normalized_content,
            filename=filename,
            content_encoding=content_encoding,
        )
        if guess and guess in CONTENT_TYPE_BY_FILE_TYPE:
            ct = CONTENT_TYPE_BY_FILE_TYPE[guess]
            return ct, guess

        request = self.context.get("request") if hasattr(self, "context") else None
        if request:
            for header_name in (
                "X-Content-Type",
                "X-Submission-Content-Type",
                "Content-Type",
            ):
                header_ct = request.headers.get(header_name)
                if not header_ct:
                    continue
                try:
                    return self._map_content_type(header_ct)
                except serializers.ValidationError:
                    continue

        return None, None

    def _guess_file_type(
        self,
        *,
        raw_content: Any,
        normalized_content: str | None,
        filename: str | None,
        content_encoding: str | None,
    ) -> SubmissionFileType | None:
        """
        Lightweight heuristics so envelopes can omit content_type when obvious.
        """
        name = (filename or "").lower()
        if name.endswith((".json", ".epjson")):
            return SubmissionFileType.JSON
        if name.endswith(".xml"):
            return SubmissionFileType.XML
        if name.endswith(".pdf"):
            return SubmissionFileType.PDF
        if name.endswith(".idf") or "energyplus" in name:
            return SubmissionFileType.TEXT

        if isinstance(raw_content, (dict, list)):
            return SubmissionFileType.JSON

        if content_encoding == "base64":
            return None  # cannot inspect encoded payload safely

        sample = None
        if isinstance(raw_content, str):
            sample = raw_content.lstrip()
        elif isinstance(normalized_content, str):
            sample = normalized_content.lstrip()

        if not sample:
            return None
        if sample.startswith(("{", "[")):
            return SubmissionFileType.JSON
        if sample.startswith("<"):
            return SubmissionFileType.XML
        return None

    def _check_content(self, content: str | None, content_encoding: str | None) -> bool:
        """
        Basic sanity checks on textual content field.
        """
        # cap on input JSON field length to avoid massive strings
        if content is None:
            return False
        if content_encoding == "base64":
            # Base64 inflates size by ~33%, so limit pre-decode size
            max_b64_b = getattr(settings, "SUBMISSION_BASE64_MAX_BYTES", 13_000_000)
            if len(content.encode("utf-8", errors="ignore")) > max_b64_b:
                raise serializers.ValidationError(
                    {
                        "content": _("Base64 content exceeds size limit."),
                    },
                )
        else:
            max_inline_b = getattr(settings, "SUBMISSION_INLINE_MAX_BYTES", 10_000_000)
            # Base64 payload size will be enforced before decode in validate()
            if len(content.encode("utf-8", errors="ignore")) > max_inline_b:
                raise serializers.ValidationError(
                    {
                        "content": _("Inline content exceeds size limit."),
                    },
                )
        return True
