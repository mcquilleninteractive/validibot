"""Finalize deferred signed-credential workflow steps.

Credential actions finish provisionally during step iteration because the
permanent evidence manifest must exist before a commercial signing provider is
called. This service owns that post-run finalization policy while depending
only on the community credential-issuer interface.
"""

from __future__ import annotations

import logging
from typing import Any
from typing import Protocol

from django.utils.translation import gettext as _

from validibot.actions.constants import ActionFailureMode
from validibot.actions.constants import CredentialActionType
from validibot.validations.constants import Severity
from validibot.validations.constants import StepStatus
from validibot.validations.constants import ValidationRunErrorCategory
from validibot.validations.constants import ValidationRunStatus
from validibot.validations.models import ValidationRun
from validibot.validations.models import ValidationStepRun
from validibot.validations.services.credential_issuance import CredentialIssuanceError
from validibot.validations.services.credential_issuance import (
    CredentialIssuerUnavailableError,
)
from validibot.validations.services.credential_issuance import get_credential_issuer
from validibot.validations.services.credential_issuance import (
    issue_registered_credential,
)
from validibot.validations.services.findings_persistence import persist_findings
from validibot.validations.validators.base import ValidationIssue

logger = logging.getLogger(__name__)


class StepRunFinalizer(Protocol):
    """Persist the standard terminal bookkeeping for one workflow step."""

    def __call__(
        self,
        *,
        step_run: ValidationStepRun,
        status: StepStatus,
        stats: dict[str, Any] | None,
        error: str | None = None,
    ) -> ValidationStepRun:
        """Finalize ``step_run`` using the orchestrator's lifecycle policy."""


def finalize_deferred_signed_credentials(
    *,
    validation_run: ValidationRun,
    finalize_step_run: StepRunFinalizer,
) -> bool:
    """Issue deferred credentials and persist their final workflow outcome.

    Returns ``True`` when findings or run state changed and the caller must
    rebuild the summary and restamp permanent evidence.
    """
    signed_step_runs = list(
        ValidationStepRun.objects.select_related(
            "workflow_step",
            "workflow_step__action",
            "workflow_step__action__definition",
        )
        .filter(
            validation_run=validation_run,
            workflow_step__action__definition__type=(
                CredentialActionType.SIGNED_CREDENTIAL
            ),
        )
        .order_by("step_order", "pk")
    )
    if not signed_step_runs:
        return False

    if get_credential_issuer() is None:
        logger.error(
            "Signed credential steps exist for run %s but no credential "
            "issuer registered during application startup.",
            validation_run.id,
        )
        message = _("Signed credential support is not installed on this instance.")
        return _record_failures(
            validation_run=validation_run,
            step_runs=signed_step_runs,
            message=message,
            finalize_step_run=finalize_step_run,
        )

    summary_needs_rebuild = False
    for step_run in signed_step_runs:
        try:
            credential_id = issue_registered_credential(step_run)
        except CredentialIssuerUnavailableError as exc:
            logger.exception(
                "Credential issuer disappeared while finalizing run %s step %s.",
                validation_run.id,
                step_run.id,
            )
            summary_needs_rebuild = (
                _record_failure(
                    validation_run=validation_run,
                    step_run=step_run,
                    message=str(exc),
                    finalize_step_run=finalize_step_run,
                )
                or summary_needs_rebuild
            )
        except CredentialIssuanceError as exc:
            logger.warning(
                "Deferred credential issuance failed for run %s step %s: %s",
                validation_run.id,
                step_run.id,
                exc,
            )
            summary_needs_rebuild = (
                _record_failure(
                    validation_run=validation_run,
                    step_run=step_run,
                    message=str(exc),
                    finalize_step_run=finalize_step_run,
                )
                or summary_needs_rebuild
            )
        except Exception as exc:
            logger.exception(
                "Unexpected deferred credential issuance failure for run %s step %s.",
                validation_run.id,
                step_run.id,
            )
            summary_needs_rebuild = (
                _record_failure(
                    validation_run=validation_run,
                    step_run=step_run,
                    message=str(exc),
                    finalize_step_run=finalize_step_run,
                )
                or summary_needs_rebuild
            )
        else:
            stats = dict(step_run.output or {})
            stats["credential_issuance"] = "issued"
            stats["credential_id"] = credential_id
            step_run.output = stats
            step_run.save(update_fields=["output"])

    return summary_needs_rebuild


def _record_failures(
    *,
    validation_run: ValidationRun,
    step_runs: list[ValidationStepRun],
    message: str,
    finalize_step_run: StepRunFinalizer,
) -> bool:
    """Persist the same unavailable-provider failure for every signed step."""
    summary_needs_rebuild = False
    for step_run in step_runs:
        summary_needs_rebuild = (
            _record_failure(
                validation_run=validation_run,
                step_run=step_run,
                message=message,
                finalize_step_run=finalize_step_run,
            )
            or summary_needs_rebuild
        )
    return summary_needs_rebuild


def _record_failure(
    *,
    validation_run: ValidationRun,
    step_run: ValidationStepRun,
    message: str,
    finalize_step_run: StepRunFinalizer,
) -> bool:
    """Persist a deferred issuance failure on the credential step run."""
    action = getattr(step_run.workflow_step, "action", None)
    failure_mode = getattr(action, "failure_mode", ActionFailureMode.ADVISORY)
    severity = (
        Severity.WARNING
        if failure_mode == ActionFailureMode.ADVISORY
        else Severity.ERROR
    )
    persist_findings(
        validation_run=validation_run,
        step_run=step_run,
        issues=[
            ValidationIssue(
                path="",
                message=message,
                severity=severity,
                code="credential_issuance_failed",
            ),
        ],
    )
    stats = dict(step_run.output or {})
    stats["credential_issuance"] = "failed"
    finalize_step_run(
        step_run=step_run,
        status=StepStatus.FAILED,
        stats=stats,
        error=message,
    )

    if failure_mode == ActionFailureMode.BLOCKING:
        validation_run.status = ValidationRunStatus.FAILED
        validation_run.error = _("Signed credential issuance failed.")
        validation_run.error_category = ValidationRunErrorCategory.RUNTIME_ERROR
        validation_run.save(update_fields=["status", "error", "error_category"])

    return True
