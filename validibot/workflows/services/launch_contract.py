"""The launch contract — the single decision point for "can this workflow run?".

ADR-2026-04-27 Phase 2: extract the four-way duplicated launch validation
logic (web view, REST API, MCP helper API, x402 cloud agent) into one
service. Every launch path must call :meth:`LaunchContract.validate`
before persisting a submission, so that a rejection on one path produces
the same violation kind and message as a rejection on any other path.

Why a single service?
=====================

Before this extraction, each path enforced launch preconditions in its
own way:

- The web view used ``views_helpers.describe_workflow_file_type_violation``
  which returned a free-form translated string.
- The REST API used the same helper but mapped the string to a
  ``LaunchValidationError`` with an ad-hoc error code.
- The MCP helper API delegated to the REST API helper, so it inherited
  the API's behaviour.
- The x402 cloud agent path had its own copy of the file-type and
  step-compatibility checks (``_enforce_launch_contract`` in
  ``validibot-cloud``) which raised a different exception class with
  yet another error code.

The duplication meant the four paths could disagree on what a
violation looked like, and adding a new precondition (e.g. payload
size limit, which Phase 0 added only on x402) required four separate
edits and four sets of tests.

This module replaces that with one decision function and one
structured violation type. Each path translates a returned violation
to its own response shape — web → form error, API → 400 with code,
MCP → JSON-RPC error envelope, x402 → AgentRunCreationError — but the
*decision* is the same.

What's in scope vs. out of scope
================================

In scope (this module):

- Workflow active state
- Workflow has steps
- File type supported by the workflow
- File type supported by every step that consumes the primary submission file
- Payload size within configured maximum

Out of scope (handled elsewhere):

- Per-user permission checks (``WorkflowAccessResolver``, sibling
  module). Two distinct concerns: "can this user launch this workflow?"
  vs. "can this workflow be launched with this payload at all?". A
  guest with a workflow grant might still hit a payload-size violation;
  a user without permission shouldn't even reach the contract check.
- Latest-version selection for public agent paths
  (``AgentWorkflowResolver``, sibling module). The contract is checked
  *after* a specific workflow version is resolved.
- Authentication / auth challenges (handled by DRF authentication
  classes upstream).

Pattern adopted from how similar projects centralise launch
validation: GitLab's CI pipeline pre-validation gate, GitHub Actions'
workflow contract checks, and Argo Workflows' template validation.
All implementation in this module is original work for Validibot.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

if TYPE_CHECKING:
    from validibot.workflows.models import Workflow

# Maximum decoded payload size accepted by any launch path. Phase 0
# added this limit on x402 only (10 MiB) to bound paid-orphan risk;
# Phase 2 generalises it to all paths so an oversized JSON envelope
# can't leak past the API rejecting it on x402 but accepting it via
# CLI. Operators can override per-deployment via Django settings if
# they need a higher cap (see ``MAX_LAUNCH_PAYLOAD_BYTES`` setting).
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
    INCOMPATIBLE_STEP = "incompatible_step"
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
        5. (If ``file_type`` provided) every step consuming the primary
           submission accepts the file type
        6. (If ``payload_size_bytes`` provided) payload is non-empty
        7. (If ``payload_size_bytes`` provided) payload is within max

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
                can verify file-type and step compatibility. Pass
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
        # 1. Workflow inactive — covered by the existing
        # ``ensure_workflow_ready_for_launch`` check on most paths
        # but the x402 path doesn't (yet) call that, so include it
        # here for completeness. After Phase 2 wires every path
        # through this contract, the path-specific check can be
        # retired.
        if not workflow.is_active:
            return LaunchContractViolation(
                code=ViolationCode.WORKFLOW_INACTIVE,
                message=str(_("This workflow is not currently active.")),
            )

        # 2. Workflow has no steps — same rationale as #1.
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

        # 4. and 5. — file-type and step-compatibility checks.
        if file_type is not None:
            file_type_violation = LaunchContract._check_file_type(
                workflow=workflow,
                file_type=file_type,
            )
            if file_type_violation is not None:
                return file_type_violation

        # 6. and 7. — payload size checks.
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
        """Verify the workflow and primary-file consumers accept ``file_type``.

        Later validators may consume typed artifacts exposed by earlier file
        ports. ``Workflow.first_incompatible_step`` therefore checks the launch
        type only for steps bound to the primary submitted file (and legacy
        validators without declared artifact inputs).
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

        incompatible_step = workflow.first_incompatible_step(file_type)
        if incompatible_step is not None:
            validator_name = getattr(incompatible_step.validator, "name", "")
            if validator_name:
                msg = _(
                    "Step %(step)s (%(validator)s) does not support "
                    "%(file_type)s files.",
                ) % {
                    "step": incompatible_step.step_number_display,
                    "validator": validator_name,
                    "file_type": file_type,
                }
            else:
                msg = _("Step %(step)s does not support %(file_type)s files.") % {
                    "step": incompatible_step.step_number_display,
                    "file_type": file_type,
                }
            return LaunchContractViolation(
                code=ViolationCode.INCOMPATIBLE_STEP,
                message=str(msg),
                detail=(
                    f"step {incompatible_step.step_number_display} "
                    f"({validator_name or 'unknown validator'}) "
                    f"rejected file_type={file_type}"
                ),
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
