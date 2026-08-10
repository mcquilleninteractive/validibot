"""
GCP execution backend using Cloud Run Jobs.

This backend runs validator containers as Cloud Run Jobs on Google Cloud Platform.
Execution is asynchronous - the job is triggered and returns immediately, with
results delivered later via HTTP callback.

## Execution Flow

```
1. Upload input envelope to GCS (gs://)
2. Trigger Cloud Run Job via Jobs API
3. Return immediately with pending status
4. (Later) Job POSTs results to callback endpoint
5. Callback handler processes results and resumes workflow
```

## When to Use

Use this backend for:
- Production GCP deployments
- High-availability setups with multiple workers
- Deployments requiring IAM-based authentication

## Configuration

Settings:
- `VALIDATOR_RUNNER = "google_cloud_run"`
- `GCP_PROJECT_ID`, `GCP_REGION`
- `GCS_VALIDATION_BUCKET` for file storage
- `WORKER_URL` for callback routing
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.conf import settings

from validibot.validations.constants import ProviderStatusLookupCapability
from validibot.validations.services.execution.base import ExecutionBackend
from validibot.validations.services.execution.base import ExecutionRequest
from validibot.validations.services.execution.base import ExecutionResponse
from validibot.validations.services.execution.base import (
    ProviderStatusTemporarilyUnavailableError,
)

if TYPE_CHECKING:
    from validibot.validations.models import ValidatorExecutionDeployment
    from validibot.validations.validators.base.base import ValidationResult

logger = logging.getLogger(__name__)


class CloudRunJobsExecutionBackend(ExecutionBackend):
    """
    GCP execution backend using Cloud Run Jobs.

    This backend wraps the existing Cloud Run launcher code and provides
    asynchronous execution of validator containers. Results are delivered
    via HTTP callback to the worker service.

    ## Callback Flow

    After triggering a Cloud Run Job, this backend returns a pending response.
    The job container:
    1. Downloads input envelope from GCS
    2. Runs validation
    3. Uploads output envelope to GCS
    4. POSTs callback with result_uri to Django

    The callback is handled by `ValidationCallbackService` which resumes
    the workflow execution.
    """

    def __init__(
        self,
        *,
        deployment: ValidatorExecutionDeployment | None = None,
    ) -> None:
        """Initialize the GCP backend."""
        self.deployment = deployment
        self._project_id = None
        self._region = None

    @property
    def is_async(self) -> bool:
        """GCP execution is asynchronous with callbacks."""
        return True

    @property
    def provider_resource_label(self) -> str:
        """Return the operator-facing provider primitive used by this adapter."""
        return "Cloud Run Job"

    @property
    def status_lookup_capability(self) -> ProviderStatusLookupCapability:
        """Cloud Run Jobs expose durable execution status through the Jobs API."""
        return ProviderStatusLookupCapability.SUPPORTED

    @property
    def project_id(self) -> str:
        """GCP project ID."""
        if self._project_id is None:
            self._project_id = (
                self.deployment.provider_configuration["project_id"]
                if self.deployment is not None
                else getattr(settings, "GCP_PROJECT_ID", "")
            )
        return self._project_id

    @property
    def region(self) -> str:
        """GCP region for Cloud Run Jobs."""
        if self._region is None:
            self._region = (
                self.deployment.provider_configuration["region"]
                if self.deployment is not None
                else getattr(settings, "GCP_REGION", "us-central1")
            )
        return self._region

    def is_available(self) -> bool:
        """Check if GCP Cloud Run is configured."""
        return bool(self.project_id)

    def check_status(self, execution_id: str) -> ExecutionResponse | None:
        """
        Check the status of a Cloud Run Job execution.

        Used by reconciliation to determine if a Cloud Run Job has completed
        but its callback was lost. Delegates to the Cloud Run runner's
        get_execution_status() method.

        Args:
            execution_id: Full Cloud Run execution name
                (projects/.../jobs/.../executions/...).

        Returns:
            ExecutionResponse when the provider answered the query.

        Raises:
            ProviderStatusTemporarilyUnavailableError: If the SDK or provider API
                cannot answer now. This remains retryable and is never confused
                with an execution failure.
        """
        try:
            from validibot.validations.services.runners.base import ExecutionStatus
            from validibot.validations.services.runners.google_cloud_run import (
                GoogleCloudRunValidatorRunner,
            )
        except ImportError:
            logger.debug(
                "google-cloud-run not available, cannot check execution status"
            )
            raise ProviderStatusTemporarilyUnavailableError(
                "Cloud Run status client is unavailable"
            ) from None

        try:
            runner = GoogleCloudRunValidatorRunner(
                project_id=self.project_id,
                region=self.region,
            )
            info = runner.get_execution_status(execution_id)
        except Exception:
            logger.warning(
                "Failed to check Cloud Run execution status for %s",
                execution_id,
                exc_info=True,
            )
            raise ProviderStatusTemporarilyUnavailableError(
                "Cloud Run execution status is temporarily unavailable"
            ) from None

        return ExecutionResponse(
            execution_id=info.execution_id,
            is_complete=info.status
            in (
                ExecutionStatus.SUCCEEDED,
                ExecutionStatus.FAILED,
                ExecutionStatus.CANCELLED,
            ),
            execution_status=info.status,
            error_message=info.error_message,
        )

    def cancel(self, execution_id: str) -> bool:
        """Cancel the exact Cloud Run Job execution through its pinned region."""
        from validibot.validations.services.runners.google_cloud_run import (
            GoogleCloudRunValidatorRunner,
        )

        runner = GoogleCloudRunValidatorRunner(
            project_id=self.project_id,
            region=self.region,
        )
        return runner.cancel(execution_id)

    def get_container_image(self, validator_type: str) -> str:
        """
        Get the container image / Cloud Run job name for a validator type.

        Resolution order:

        1. ``ValidatorConfig.image_name`` — the validator's own declaration
           (the canonical source for system validators, set in each
           validator's ``config.py``).
        2. Convention fallback — ``validibot-validator-backend-{slug}``,
           used when no config is registered or ``image_name`` is empty.
        """
        from validibot.validations.validators.base.config import get_config

        if self.deployment is not None:
            return str(self.deployment.provider_configuration["job_name"])

        vtype = validator_type.lower()

        config = get_config(vtype.upper())
        if config and config.image_name:
            return config.image_name

        return f"validibot-validator-backend-{vtype}"

    def _launcher_kwargs(self, validator_type: str) -> dict:
        """Return the exact pinned Job target supplied to shared staging code."""
        return {
            "job_name": self.get_container_image(validator_type),
            "expected_image_digest": (
                self.deployment.backend_image_digest if self.deployment else None
            ),
        }

    def _launch_result_to_response(
        self,
        result: ValidationResult,
    ) -> ExecutionResponse:
        """Convert a Cloud Run launcher result into backend response semantics."""
        stats = result.stats or {}
        execution_id = stats.get("execution_name", "")
        common = {
            "execution_id": execution_id,
            "input_uri": stats.get("input_uri"),
            "output_uri": stats.get("result_uri"),
            "execution_bundle_uri": stats.get("execution_bundle_uri"),
        }
        if result.passed is False:
            messages = [
                issue.message
                for issue in result.issues
                if getattr(issue, "message", None)
            ]
            error_message = "\n".join(messages) or (
                f"Failed to launch {self.provider_resource_label}"
            )
            # Preserve a reserved failure code + meta (e.g.
            # ``schematron.rules_invalid`` with ``meta.infra_error``) from the
            # launcher, so a LAUNCH-time infrastructure failure renders the same
            # as a callback-time one instead of collapsing to a bare message.
            coded_issue = next(
                (issue for issue in result.issues if getattr(issue, "code", None)),
                None,
            )
            return ExecutionResponse(
                is_complete=True,
                error_message=error_message,
                error_code=getattr(coded_issue, "code", None) if coded_issue else None,
                error_meta=(
                    dict(getattr(coded_issue, "meta", None) or {})
                    if coded_issue
                    else None
                ),
                **common,
            )
        return ExecutionResponse(
            is_complete=False,  # Async - waiting for callback
            **common,
        )

    def execute(self, request: ExecutionRequest) -> ExecutionResponse:
        """
        Execute a validation via Cloud Run Jobs (async).

        This method delegates to the existing Cloud Run launcher code,
        which handles GCS uploads and job triggering.

        Args:
            request: Execution request with run, validator, submission, step.

        Returns:
            ExecutionResponse with is_complete=False (pending).
        """
        if not self.is_available():
            return ExecutionResponse(
                execution_id="",
                is_complete=True,
                error_message=(
                    "GCP Cloud Run is not configured (GCP_PROJECT_ID not set)"
                ),
            )

        # Delegate to existing launcher based on validator type
        validator_type = request.validator_type.upper()

        try:
            if validator_type == "ENERGYPLUS":
                return self._execute_energyplus(request)
            if validator_type == "FMU":
                return self._execute_fmu(request)
            if validator_type == "SHACL":
                return self._execute_shacl(request)
            if validator_type == "SCHEMATRON":
                return self._execute_schematron(request)
            if validator_type == "PORTFOLIO_MANAGER":
                return self._execute_portfolio_manager(request)
            if validator_type == "PDF":
                return self._execute_pdf(request)
            return ExecutionResponse(
                execution_id="",
                is_complete=True,
                error_message=f"Unsupported validator type for GCP: {validator_type}",
            )

        except Exception as e:
            logger.exception(
                "Failed to launch %s for run %s",
                self.provider_resource_label,
                request.run_id,
            )
            return ExecutionResponse(
                execution_id="",
                is_complete=True,
                error_message=f"Failed to launch {self.provider_resource_label}: {e}",
            )

    def _execute_energyplus(self, request: ExecutionRequest) -> ExecutionResponse:
        """
        Execute EnergyPlus validation via Cloud Run.

        Delegates to the existing launcher function.
        """
        from validibot.validations.services.cloud_run.launcher import (
            launch_energyplus_validation,
        )

        # Get ruleset if available
        ruleset = None
        step_config = request.step.config or {}
        ruleset_id = step_config.get("ruleset_id")
        if ruleset_id:
            from validibot.validations.models import Ruleset

            ruleset = Ruleset.objects.filter(id=ruleset_id).first()

        # Launch via existing code
        result = launch_energyplus_validation(
            run=request.run,
            validator=request.validator,
            submission=request.submission,
            ruleset=ruleset,
            step=request.step,
            **self._launcher_kwargs(request.validator_type),
        )

        return self._launch_result_to_response(result)

    def _execute_shacl(self, request: ExecutionRequest) -> ExecutionResponse:
        """
        Execute SHACL validation via Cloud Run.

        Delegates to the launcher, which uploads the RDF submission + input
        envelope and triggers the isolated SHACL Cloud Run Job.
        """
        from validibot.validations.services.cloud_run.launcher import (
            launch_shacl_validation,
        )

        ruleset = request.step.ruleset

        result = launch_shacl_validation(
            run=request.run,
            validator=request.validator,
            submission=request.submission,
            ruleset=ruleset,
            step=request.step,
            **self._launcher_kwargs(request.validator_type),
        )

        return self._launch_result_to_response(result)

    def _execute_schematron(self, request: ExecutionRequest) -> ExecutionResponse:
        """
        Execute Schematron validation via Cloud Run.

        Delegates to the launcher, which resolves the author's rules from the
        step's ruleset (falling back to the validator's ``default_ruleset``),
        ships them **inline** in the typed input envelope
        (``SchematronInputs.schematron_text``) alongside the staged XML
        submission, and triggers the isolated Schematron Cloud Run Job. The
        container compiles the rules (SchXslt2) and runs them under Saxon
        (ADR-2026-07-01 D4/D4b) — there is no separate checksum-verified rule
        pack artefact.
        """
        from validibot.validations.services.cloud_run.launcher import (
            launch_schematron_validation,
        )

        result = launch_schematron_validation(
            run=request.run,
            validator=request.validator,
            submission=request.submission,
            ruleset=request.step.ruleset,
            step=request.step,
            **self._launcher_kwargs(request.validator_type),
        )

        return self._launch_result_to_response(result)

    def _execute_portfolio_manager(
        self,
        request: ExecutionRequest,
    ) -> ExecutionResponse:
        """Launch the isolated Portfolio Manager report backend."""
        from validibot.validations.services.cloud_run.launcher import (
            launch_portfolio_manager_validation,
        )

        result = launch_portfolio_manager_validation(
            run=request.run,
            validator=request.validator,
            submission=request.submission,
            ruleset=request.step.ruleset,
            step=request.step,
            **self._launcher_kwargs(request.validator_type),
        )
        return self._launch_result_to_response(result)

    def _execute_pdf(self, request: ExecutionRequest) -> ExecutionResponse:
        """Launch the isolated PDF package backend."""
        from validibot.validations.services.cloud_run.launcher import (
            launch_pdf_validation,
        )

        result = launch_pdf_validation(
            run=request.run,
            validator=request.validator,
            submission=request.submission,
            ruleset=request.step.ruleset,
            step=request.step,
            **self._launcher_kwargs(request.validator_type),
        )
        return self._launch_result_to_response(result)

    def _execute_fmu(self, request: ExecutionRequest) -> ExecutionResponse:
        """
        Execute FMU validation via Cloud Run.

        Delegates to the existing launcher function.
        """
        from validibot.validations.services.cloud_run.launcher import (
            launch_fmu_validation,
        )

        # Get ruleset if available
        ruleset = None
        step_config = request.step.config or {}
        ruleset_id = step_config.get("ruleset_id")
        if ruleset_id:
            from validibot.validations.models import Ruleset

            ruleset = Ruleset.objects.filter(id=ruleset_id).first()

        # Launch via existing code
        logger.info(
            "Launching FMU %s for run %s (validator=%s)",
            self.provider_resource_label,
            request.run_id,
            request.validator.slug if request.validator else "unknown",
        )
        result = launch_fmu_validation(
            run=request.run,
            validator=request.validator,
            submission=request.submission,
            ruleset=ruleset,
            step=request.step,
            **self._launcher_kwargs(request.validator_type),
        )

        stats = result.stats or {}
        execution_id = stats.get("execution_name", "")
        if not execution_id:
            logger.error(
                "FMU launch returned empty execution_id for run %s. "
                "Stats: %s. The %s may not have been created.",
                request.run_id,
                stats,
                self.provider_resource_label,
            )
        else:
            logger.info(
                "FMU %s dispatched for run %s: execution_id=%s",
                self.provider_resource_label,
                request.run_id,
                execution_id,
            )
        return self._launch_result_to_response(result)
