"""
Basic validator

Evaluates BASIC assertions against JSON and XML submissions. Assertions are
defined with paths (e.g., "payload.items[0].price") and operators (eq, ne,
gt, etc.). All assertion evaluation is delegated to the unified assertion
system via the BasicAssertionEvaluator.

For XML submissions, the XML is converted to a nested dict via
``xml_to_dict()`` so that CEL expressions and path-based assertions work
identically to JSON — the XML never hits the evaluator directly.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from django.utils.translation import gettext as _

from validibot.submissions.constants import SubmissionFileType
from validibot.validations.constants import Severity
from validibot.validations.validators.base.base import AssertionStats
from validibot.validations.validators.base.base import BaseValidator
from validibot.validations.validators.base.base import ValidationIssue
from validibot.validations.validators.base.base import ValidationResult
from validibot.validations.xml_utils import XmlParseError
from validibot.validations.xml_utils import xml_to_dict

if TYPE_CHECKING:
    from validibot.actions.protocols import RunContext
    from validibot.validations.models import Ruleset
    from validibot.validations.models import Submission
    from validibot.validations.models import Validator

logger = logging.getLogger(__name__)


class BasicValidator(BaseValidator):
    """
    Validates a submission by evaluating the BASIC assertions stored on a ruleset.

    Accepts JSON and XML submissions. Targets are resolved via dot / [index]
    paths (for example, ``payload.items[0].price``). For XML, the document is
    first converted to a nested dict so paths and CEL expressions work
    identically to JSON.

    **No ``extract_input_values`` override (per ADR-2026-05-22b
    Phase 6).** Basic validators don't parse a packed/arcane format —
    the submission JSON/XML IS the data, addressed directly through
    BASIC paths. Authors point ``target_data_path`` at
    ``payload.<field>`` instead of using ``i.*``. Phase 5 added
    namespace enrichment so BASIC assertions targeting workflow
    workflow signals or step input bindings still resolve, but the parser-fact
    pattern itself doesn't apply here.
    """

    _SUPPORTED_FILE_TYPES = frozenset({SubmissionFileType.JSON, SubmissionFileType.XML})

    # PUBLIC METHODS
    # ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

    def validate(
        self,
        validator: Validator,
        submission: Submission,
        ruleset: Ruleset,
        run_context: RunContext | None = None,
    ) -> ValidationResult:
        """
        Validate a submission by evaluating all assertions in order.

        Uses the unified assertion evaluation system which dispatches to
        type-specific evaluators (BASIC, CEL, etc.) registered in the registry.
        """
        # Store run_context on instance for assertion evaluation
        self.run_context = run_context

        resolved_file = (
            (run_context.resolved_file_inputs or {}).get("document")
            if run_context is not None
            else None
        )
        if resolved_file is None or resolved_file.content is None:
            return ValidationResult(
                passed=False,
                issues=[
                    ValidationIssue(
                        path="document",
                        message=_(
                            "The Basic validator document input was not resolved."
                        ),
                        severity=Severity.ERROR,
                        code="required_input_missing",
                    ),
                ],
            )

        raw_content = resolved_file.content
        resolved_file_type = resolved_file.file_type
        if not resolved_file_type:
            resolved_file_type = {
                "json": SubmissionFileType.JSON,
                "xml": SubmissionFileType.XML,
            }.get(resolved_file.data_format, "")
        if resolved_file_type not in self._SUPPORTED_FILE_TYPES:
            return ValidationResult(
                passed=False,
                issues=[
                    ValidationIssue(
                        path="document",
                        message=_("Basic validators require JSON or XML content."),
                        severity=Severity.ERROR,
                        code="input_file_type_incompatible",
                    )
                ],
                stats={"file_type": resolved_file_type},
            )

        # Parse submission content into a dict. The XML-to-dict conversion
        # happens once here; the resulting payload is reused for all
        # assertions (both BASIC and CEL) without re-parsing.
        payload: dict | list | None = None
        if resolved_file_type == SubmissionFileType.JSON:
            try:
                payload = json.loads(raw_content)
            except Exception as exc:
                return ValidationResult(
                    passed=False,
                    issues=[
                        ValidationIssue(
                            path="",
                            message=_(
                                "Invalid JSON submission: %(error)s",
                            )
                            % {"error": exc},
                        ),
                    ],
                    stats={"exception": type(exc).__name__},
                )
        elif resolved_file_type == SubmissionFileType.XML:
            try:
                payload = xml_to_dict(raw_content)
            except XmlParseError as exc:
                return ValidationResult(
                    passed=False,
                    issues=[
                        ValidationIssue(
                            path="",
                            message=_(
                                "Invalid XML submission: %(error)s",
                            )
                            % {"error": exc},
                        ),
                    ],
                    stats={"exception": type(exc).__name__},
                )

        # Evaluate all assertions using the unified system.
        # Basic validators have no external processor, so we evaluate
        # both input-stage and output-stage assertions together.
        #
        # The payload passed to evaluators is enriched with namespaced
        # values (resolved StepInputBindings, workflow_signals) so
        # BASIC assertions targeting i.<name> / s.<name> resolve via
        # the bare contract_key at the top level — see
        # ``BaseValidator._enrich_basic_payload``. CEL ignores
        # ``payload`` entirely (it reads from a separately-built
        # context), so the enrichment is a no-op for CEL targets.
        issues: list[ValidationIssue] = []
        total_assertions = 0
        total_failures = 0

        for stage in ("input", "output"):
            enriched_payload = self._enrich_basic_payload(payload, stage=stage)
            result = self.evaluate_assertions_for_stage(
                validator=validator,
                ruleset=ruleset,
                payload=enriched_payload,
                stage=stage,
            )
            issues.extend(result.issues)
            total_assertions += result.total
            total_failures += result.failures

        passed = not any(issue.severity == Severity.ERROR for issue in issues)
        return ValidationResult(
            passed=passed,
            issues=issues,
            assertion_stats=AssertionStats(
                total=total_assertions,
                failures=total_failures,
            ),
        )
