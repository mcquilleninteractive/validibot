"""Shared workflow launch admission for every public execution channel.

This service checks workflow readiness, primary-submission admission, payload
bounds, validator runtime availability, and structural step dependencies before
a run is created. It deliberately does not test the primary file against every
validator: each step resolves its selected source through its typed input-port
contract and reports a concrete incompatibility as a normal failed-step result.

Callers translate the same structured violation into their own web, REST, MCP,
CLI, or x402 response envelope. Authorization, authentication, workflow-version
selection, and concrete validator input resolution remain separate concerns.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

if TYPE_CHECKING:
    from validibot.workflows.models import Workflow

# Maximum decoded payload size accepted by any launch path. Operators can
# override the effective limit through the caller's launch policy.
DEFAULT_MAX_PAYLOAD_BYTES = 10 * 1024 * 1024  # 10 MiB


class ViolationCode(StrEnum):
    """Stable codes for launch-contract violations.

    These appear in error responses, in logs, and in support
    diagnostics. Adding a new code is a backward-compatible change;
    renaming or removing one is a breaking change for any operator
    or integration that filters on the code.

    The codes are intentionally lowercase-snake. Each path translates
    the code to whatever case its error envelope expects, but the
    canonical form here is stable.
    """

    WORKFLOW_INACTIVE = "workflow_inactive"
    NO_STEPS = "no_steps"
    VALIDATOR_UNAVAILABLE = "validator_unavailable"
    INVALID_STEP_DEPENDENCY = "invalid_step_dependency"
    UNSUPPORTED_FILE_TYPE = "unsupported_file_type"
    PAYLOAD_TOO_LARGE = "payload_too_large"
    PAYLOAD_EMPTY = "payload_empty"


@dataclass(frozen=True)
class LaunchContractViolation:
    """Structured violation returned by the launch contract.

    Each launch path maps this to its own response shape:

    - Web view -> ``LaunchValidationError`` with a form-friendly message.
    - REST API -> ``LaunchValidationError`` with status_code + code.
    - MCP helper API -> the REST API mapping (same mechanism).
    - x402 cloud agent -> ``AgentRunCreationError`` with error envelope.

    The ``code`` field is the load-bearing contract; the ``message``
    is human-readable and translatable. Callers should route on
    ``code``, not on substring-matching ``message``.

    Attributes:
        code: Stable :class:`ViolationCode` value.
        message: Human-readable description of why the launch was
            rejected. Translatable.
        detail: Optional extra context useful for logs and support
            bundles. Not user-facing by default.
    """

    code: ViolationCode
    message: str
    detail: str | None = None


class LaunchContract:
    """The launch decision point.

    Every code path that wants to launch a workflow must call
    :meth:`validate` first. The method returns a
    :class:`LaunchContractViolation` if the launch should be rejected,
    or ``None`` if the launch is allowed to proceed.

    Doesn't raise. Callers translate the returned violation to their
    path-specific exception type. We avoid raising here because:

    - Raising couples the contract to one path's exception hierarchy
      (web's ``LaunchValidationError`` vs. x402's
      ``AgentRunCreationError``).
    - Returning a value lets callers decide whether to short-circuit,
      log-and-continue (rare but useful for dry-run modes), or
      aggregate multiple violations.
    - Tests can assert on the structured violation directly without
      the round-trip through an exception.

    The class itself is stateless; methods are static. We use a class
    rather than a module-level function namespace so the contract has
    a single import-friendly anchor (``from ... import LaunchContract;
    LaunchContract.validate(...)``) and so future extensions (e.g. a
    ``LaunchContract.dry_run(...)`` returning all applicable
    violations rather than the first) have a natural home.
    """

    @staticmethod
    def validate(
        *,
        workflow: Workflow,
        file_type: str | None = None,
        payload_size_bytes: int | None = None,
        max_payload_bytes: int = DEFAULT_MAX_PAYLOAD_BYTES,
    ) -> LaunchContractViolation | None:
        """Return the first applicable violation, or ``None`` if launch is OK.

        The order of checks is:

        1. Workflow is active
        2. Workflow has at least one step
        3. Every validator step has an available runtime config/class
        4. (If ``file_type`` provided) workflow accepts the file type
        5. (If ``payload_size_bytes`` provided) payload is non-empty
        6. (If ``payload_size_bytes`` provided) payload is within max

        We return on the *first* violation rather than aggregating.
        That matches operator expectation ("tell me the first thing
        that's wrong") and avoids cascading false-positives — e.g. an
        inactive workflow's step list might be stale, so reporting
        "no_steps" alongside "workflow_inactive" would be noise.

        Args:
            workflow: The :class:`Workflow` instance the caller wants
                to launch. Must already be resolved (by
                ``WorkflowAccessResolver`` for member paths or
                ``AgentWorkflowResolver`` for public paths).
            file_type: Optional submission file type. Pass when the
                payload includes a known file type so the contract
                can verify primary-submission admission. Pass
                ``None`` for paths that haven't determined a file
                type yet (rare — most callers know it by the time
                they reach the contract).
            payload_size_bytes: Optional decoded payload size. Pass
                when the payload size is knowable up-front (e.g.
                from a Content-Length header or after base64
                decoding). Pass ``None`` to skip the size check.
            max_payload_bytes: Override for the size limit. Defaults
                to :data:`DEFAULT_MAX_PAYLOAD_BYTES`. Operators can
                pass a smaller value for stricter limits on a given
                path, but should not raise it without ADR review.

        Returns:
            A :class:`LaunchContractViolation` if the launch should
            be rejected, ``None`` otherwise.
        """
        if not workflow.is_active:
            return LaunchContractViolation(
                code=ViolationCode.WORKFLOW_INACTIVE,
                message=str(_("This workflow is not currently active.")),
            )

        if not workflow.steps.exists():
            return LaunchContractViolation(
                code=ViolationCode.NO_STEPS,
                message=str(
                    _("This workflow has no steps defined and cannot be executed."),
                ),
            )

        unavailable_step = workflow.first_unavailable_validator_step()
        if unavailable_step is not None:
            validator = unavailable_step.validator
            reason = validator.runtime_unavailable_reason()
            return LaunchContractViolation(
                code=ViolationCode.VALIDATOR_UNAVAILABLE,
                message=str(
                    _(
                        "Step %(step)s (%(validator)s) uses a validator that "
                        "is not available in this deployment."
                    )
                    % {
                        "step": unavailable_step.step_number_display,
                        "validator": validator.name,
                    },
                ),
                detail=reason,
            )

        from validibot.validations.services.artifact_bindings import (
            validate_workflow_dependencies,
        )

        try:
            validate_workflow_dependencies(workflow)
        except ValidationError as exc:
            return LaunchContractViolation(
                code=ViolationCode.INVALID_STEP_DEPENDENCY,
                message=str(
                    _(
                        "This workflow contains an invalid earlier-step file "
                        "dependency and cannot be launched."
                    ),
                ),
                detail="; ".join(exc.messages),
            )

        # Primary file type is a workflow admission concern only.
        if file_type is not None:
            file_type_violation = LaunchContract._check_file_type(
                workflow=workflow,
                file_type=file_type,
            )
            if file_type_violation is not None:
                return file_type_violation

        # Payload size checks apply uniformly to every channel.
        if payload_size_bytes is not None:
            payload_violation = LaunchContract._check_payload_size(
                payload_size_bytes=payload_size_bytes,
                max_payload_bytes=max_payload_bytes,
            )
            if payload_violation is not None:
                return payload_violation

        return None

    # ── internals ──────────────────────────────────────────────────────────

    @staticmethod
    def _check_file_type(
        *,
        workflow: Workflow,
        file_type: str,
    ) -> LaunchContractViolation | None:
        """Verify the primary file satisfies workflow admission policy.

        Individual step contracts are evaluated against their selected concrete
        sources during execution. A multi-type workflow may therefore admit a
        file that a particular primary-bound step will reject with a structured
        validation finding.
        """
        if not workflow.supports_file_type(file_type):
            allowed = workflow.allowed_file_type_labels()
            allowed_display = ", ".join(allowed) if allowed else str(_("no file types"))
            return LaunchContractViolation(
                code=ViolationCode.UNSUPPORTED_FILE_TYPE,
                message=str(
                    _("This workflow accepts %(allowed)s submissions.")
                    % {"allowed": allowed_display},
                ),
                detail=f"workflow accepts {allowed_display}; got {file_type}",
            )

        return None

    @staticmethod
    def _check_payload_size(
        *,
        payload_size_bytes: int,
        max_payload_bytes: int,
    ) -> LaunchContractViolation | None:
        """Verify the payload is non-empty and within the size limit.

        We check empty-vs-too-large as separate violations so that
        "you forgot to attach a file" produces a clearly different
        error message from "your file is too big."
        """
        if payload_size_bytes <= 0:
            return LaunchContractViolation(
                code=ViolationCode.PAYLOAD_EMPTY,
                message=str(_("Submission payload is empty.")),
            )

        if payload_size_bytes > max_payload_bytes:
            mib = payload_size_bytes / (1024 * 1024)
            limit_mib = max_payload_bytes / (1024 * 1024)
            return LaunchContractViolation(
                code=ViolationCode.PAYLOAD_TOO_LARGE,
                message=str(
                    _(
                        "Submission payload is %(size).1f MiB, which exceeds "
                        "the %(limit).1f MiB limit.",
                    )
                    % {"size": mib, "limit": limit_mib},
                ),
                detail=(
                    f"payload_size_bytes={payload_size_bytes} "
                    f"max_payload_bytes={max_payload_bytes}"
                ),
            )

        return None


__all__ = [
    "DEFAULT_MAX_PAYLOAD_BYTES",
    "LaunchContract",
    "LaunchContractViolation",
    "ViolationCode",
]
