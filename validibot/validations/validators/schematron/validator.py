"""Schematron validator — dispatches to the isolated container backend.

Schematron rules compile to XSLT — a full programming language — so the
author's uploaded rules are executable code and must never run inside the
Django worker. Exactly like SHACL, ``SchematronValidator`` is an
:class:`AdvancedValidator`: Django resolves the rules from the step's
Ruleset (where the step-config upload stored them), ships them inline in a
``SchematronInputEnvelope``, and the
``validibot-validator-backend-schematron`` container compiles + runs them
under Saxon and returns a ``SchematronOutputEnvelope`` with the parsed SVRL
summary. See ADR-2026-07-01 (decisions D3/D4/D4b) for the execution model.

There is **one execution path**: this class never runs an XSLT engine
in-process. The XSLT-1.0 ``lxml.isoschematron`` capability seen in tests is a
fixture-generating helper only (ADR test layers A–C).

The base class handles the lifecycle (input-stage gate, dispatch, sync/async
completion). This subclass supplies:

1. :meth:`preprocess_submission` — the D8 hardened-XML guard. An XXE /
   entity-bomb / oversize / malformed submission is rejected *before*
   any container is launched.
2. :meth:`extract_output_values` — the ``o.*`` output-value dict for CEL
   assertions, filtered to the catalog-declared keys.
3. :meth:`post_execute_validate` — rebuilds findings from the container's
   structured output with the D10 contract (``code`` = native rule id,
   ``meta`` = location XPath + publisher deep link) and enforces the D9
   failure taxonomy: an engine failure surfaces as ONE reserved
   ``schematron.*`` finding flagged ``meta.infra_error`` — never as
   fabricated rule findings.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from typing import Any

from django.core.exceptions import ValidationError
from django.utils.translation import gettext as _

from validibot.validations.constants import Severity
from validibot.validations.validators.base.advanced import AdvancedValidator
from validibot.validations.validators.base.base import AssertionStats
from validibot.validations.validators.base.base import ValidationIssue
from validibot.validations.validators.base.base import ValidationResult
from validibot.validations.validators.schematron.security import SchematronSecurityError
from validibot.validations.validators.schematron.security import (
    assert_submission_is_safe_xml,
)

if TYPE_CHECKING:
    from validibot.actions.protocols import RunContext
    from validibot.submissions.models import Submission
    from validibot.workflows.models import WorkflowStep

logger = logging.getLogger(__name__)

# ── D9 reserved finding codes ───────────────────────────────────────────────
# Non-rule codes for "we couldn't run the check" outcomes. UI/API render
# findings carrying these (plus meta.infra_error=True) as infrastructure
# problems, never as business-rule failures — an engine crash must not read
# as "your invoice is non-compliant".
CODE_ENGINE_ERROR = "schematron.engine_error"
CODE_ENGINE_TIMEOUT = "schematron.engine_timeout"
# The author's uploaded rules failed to compile — a workflow-authoring
# problem, not a fact about the submitted document (still infra_error from
# the submitter's perspective: the check never ran).
CODE_RULES_INVALID = "schematron.rules_invalid"
CODE_BACKEND_UNAVAILABLE = "schematron.backend_unavailable"
# D10 truncation signal — emitted alongside the kept findings when the
# document blew the findings cap, so truncation is never silent.
CODE_FINDINGS_TRUNCATED = "schematron.findings_truncated"

# Container engine_status values (mirrors the shared envelope contract).
ENGINE_STATUS_OK = "ok"
ENGINE_STATUS_ERROR = "error"
ENGINE_STATUS_TIMEOUT = "timeout"

# Map the container's finding-severity strings to the Django Severity enum.
_SEVERITY_FROM_STRING = {
    "ERROR": Severity.ERROR,
    "WARNING": Severity.WARNING,
    "INFO": Severity.INFO,
}

# The o.* output-value keys this validator exposes — must match the catalog entries
# in config.py (the "catalog is the contract" rule, as SHACL/EnergyPlus).
_OUTPUT_VALUE_KEYS = (
    "passed",
    "error_count",
    "warning_count",
    "fired_rule_count",
    "finding_rule_ids_by_severity",
    "query_binding",
    "engine",
)

# Provenance keys surfaced in step-run stats (D5: a result is only meaningful
# if you can point at the exact rules + engine that produced it).
_PROVENANCE_KEYS = (
    "schematron_sha256",
    "query_binding",
    "engine",
)


class SchematronValidator(AdvancedValidator):
    """Schematron rule-pack validator dispatched to an isolated container."""

    @property
    def validator_display_name(self) -> str:
        return "Schematron"

    def preprocess_submission(
        self,
        *,
        step: WorkflowStep,
        submission: Submission,
    ) -> dict[str, object]:
        """Reject unsafe/malformed XML before any container is launched (D8a).

        Applies the hardened parser posture (defusedxml: no DTD, no entities,
        no external references) plus the size/depth caps. Raising
        ``ValidationError`` here means the base class returns a clean
        pre-dispatch failure — no compute is spent on a payload we would
        refuse anyway, and nothing dangerous ever reaches Saxon.
        """
        del step, submission
        try:
            resolved = self.resolve_file_input(
                "xml_document",
                load_content=True,
            )
            assert_submission_is_safe_xml(resolved.content or b"")
        except SchematronSecurityError as exc:
            raise ValidationError(str(exc)) from exc
        return {}

    def extract_output_values(self, output_envelope: Any) -> dict[str, Any] | None:
        """Pull the ``o.*`` output-value dict from the container's outputs.

        Filtered to the catalog-declared keys so extra output fields cannot
        leak into the ``o.*`` namespace. On an engine failure (D9) the rule
        counts are ``None`` — *unknown*, not zero — so a CEL gate like
        ``o.error_count == 0`` cannot accidentally pass when the rules never
        ran; the map stays empty per the ADR.
        """
        outputs = getattr(output_envelope, "outputs", None)
        if outputs is None:
            return None

        output_values = {key: getattr(outputs, key, None) for key in _OUTPUT_VALUE_KEYS}

        engine_status = getattr(outputs, "engine_status", ENGINE_STATUS_OK)
        if engine_status != ENGINE_STATUS_OK:
            # D9: findings/counts are only meaningful when the engine ran.
            output_values["passed"] = None
            output_values["error_count"] = None
            output_values["warning_count"] = None
            output_values["fired_rule_count"] = None
            output_values["finding_rule_ids_by_severity"] = {}
        return output_values

    def post_execute_validate(
        self,
        output_envelope: Any,
        run_context: RunContext | None = None,
    ) -> ValidationResult:
        """Process the container output: findings, output_values, assertions (D9/D10).

        Overrides the base because Schematron needs the richer mapping the
        generic envelope path doesn't provide:

        1. **Native rule ids as first-class findings (D10).** Findings are
           rebuilt from ``outputs.findings`` with ``code`` = the publisher's
           rule id and ``meta`` carrying the SVRL location XPath, the raw
           ``flag``/``role``, the pack id, and a deep link to the publisher's
           own rule documentation.
        2. **The failure taxonomy (D9).** ``engine_status != ok`` produces a
           single reserved ``schematron.*`` finding with
           ``meta.infra_error=True`` and *no* rule findings — "we couldn't
           run the check" is never rendered as a rule failure.
        3. **Explicit truncation (D10).** A capped findings list is
           accompanied by one ``schematron.findings_truncated`` finding.
        """
        self.run_context = run_context

        outputs = getattr(output_envelope, "outputs", None)

        if outputs is None:
            # No structured outputs at all — engine-level failure surfaced
            # via the generic envelope messages.
            issues = self._extract_issues_from_envelope(output_envelope)
            output_values: dict[str, Any] = {}
        elif getattr(outputs, "engine_status", ENGINE_STATUS_OK) != ENGINE_STATUS_OK:
            issues = [self._engine_failure_issue(outputs)]
            output_values = self.extract_output_values(output_envelope) or {}
        else:
            issues = self._issues_from_outputs(outputs)
            output_values = self.extract_output_values(output_envelope) or {}

        # Django-side CEL/Basic output-stage assertions against the o.*
        # output_values (e.g. a warnings-tolerant gate: ``o.error_count == 0``).
        assertion_total = 0
        assertion_failures = 0
        if run_context and run_context.step:
            validator = run_context.step.validator
            ruleset = run_context.step.ruleset
            if validator and ruleset:
                resolved_inputs = self._get_resolved_inputs(run_context)
                payload = self._build_assertion_payload(
                    output_values,
                    run_context,
                    resolved_inputs=resolved_inputs,
                )
                payload = self._enrich_basic_payload(
                    payload,
                    stage="output",
                    output_values=None,
                )
                assertion_result = self.evaluate_assertions_for_stage(
                    validator=validator,
                    ruleset=ruleset,
                    payload=payload,
                    stage="output",
                )
                issues.extend(assertion_result.issues)
                assertion_total = assertion_result.total
                assertion_failures = assertion_result.failures

        passed = self._determine_passed(
            output_envelope,
            assertion_failures=assertion_failures,
        )

        return ValidationResult(
            passed=passed,
            issues=issues,
            assertion_stats=AssertionStats(
                total=assertion_total,
                failures=assertion_failures,
            ),
            output_values=output_values,
            stats=self._build_stats(outputs),
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _issues_from_outputs(self, outputs: Any) -> list[ValidationIssue]:
        """Rebuild findings from structured output per the D10 contract."""
        url_template = self._rule_doc_url_template()

        issues: list[ValidationIssue] = []
        for finding in getattr(outputs, "findings", None) or []:
            rule_id = str(getattr(finding, "rule_id", "") or "")
            location = str(getattr(finding, "location_xpath", "") or "")
            meta: dict[str, Any] = {
                "location_xpath": location,
                "flag": getattr(finding, "flag", "") or "",
                "role": getattr(finding, "role", "") or "",
            }
            rule_url = self._rule_url(url_template, rule_id)
            if rule_url:
                meta["rule_url"] = rule_url
            issues.append(
                ValidationIssue(
                    path=location,
                    message=str(getattr(finding, "message", "") or ""),
                    severity=_SEVERITY_FROM_STRING.get(
                        str(getattr(finding, "severity", "") or ""),
                        # Fail-closed, matching svrl.py: nothing
                        # publisher-authored is silently downgraded.
                        Severity.ERROR,
                    ),
                    code=rule_id,
                    meta=meta,
                ),
            )

        if getattr(outputs, "findings_truncated", False):
            suppressed = int(getattr(outputs, "findings_suppressed_count", 0) or 0)
            issues.append(
                ValidationIssue(
                    path="",
                    message=_(
                        "Findings were truncated: %(count)d additional "
                        "findings were suppressed by the findings cap. The "
                        "counts above reflect the full totals.",
                    )
                    % {"count": suppressed},
                    severity=Severity.WARNING,
                    code=CODE_FINDINGS_TRUNCATED,
                    meta={"suppressed_count": suppressed},
                ),
            )
        return issues

    @staticmethod
    def _engine_failure_issue(outputs: Any) -> ValidationIssue:
        """Build the single reserved D9 finding for an engine failure.

        Reserved codes (``schematron.engine_timeout`` /
        ``schematron.rules_invalid`` / ``schematron.backend_unavailable``
        / ``schematron.engine_error``) plus ``meta.infra_error=True`` let the
        UI/API render this as "we couldn't run the check" — categorically
        distinct from a rule failure. No rule findings are synthesised.
        """
        engine_status = str(getattr(outputs, "engine_status", "") or "")
        engine_error_code = str(getattr(outputs, "engine_error_code", "") or "")
        engine_message = str(getattr(outputs, "engine_message", "") or "")

        if engine_status == ENGINE_STATUS_TIMEOUT:
            code = CODE_ENGINE_TIMEOUT
            default_message = _(
                "The Schematron engine timed out before completing. "
                "This says nothing about whether your document satisfies "
                "the rules.",
            )
        elif engine_error_code == "rules_invalid":
            code = CODE_RULES_INVALID
            default_message = _(
                "This step's Schematron rules failed to compile, so the "
                "submission was not checked. The workflow author needs to "
                "fix the uploaded rules.",
            )
        elif engine_error_code == "backend_unavailable":
            code = CODE_BACKEND_UNAVAILABLE
            default_message = _(
                "The Schematron validation backend is not available in "
                "this deployment, so the rules were not run.",
            )
        else:
            code = CODE_ENGINE_ERROR
            default_message = _(
                "The Schematron engine could not run the rules. "
                "This says nothing about whether your document satisfies "
                "the rules.",
            )

        return ValidationIssue(
            path="",
            message=engine_message or default_message,
            severity=Severity.ERROR,
            code=code,
            meta={
                "infra_error": True,
                "engine_status": engine_status,
                "engine_error_code": engine_error_code,
            },
        )

    def _rule_doc_url_template(self) -> str:
        """The step's optional rule-documentation URL template (D10).

        Authors validating against a published standard can set
        ``rule_doc_url_template`` (e.g.
        ``"https://docs.peppol.eu/poacc/billing/3.0/rules/#{rule_id}"``) in
        the step config so every finding deep-links to the publisher's own
        rule text. Stored on the step ruleset's metadata by
        ``build_schematron_config``.
        """
        step = getattr(self.run_context, "step", None) if self.run_context else None
        ruleset = getattr(step, "ruleset", None)
        metadata = getattr(ruleset, "metadata", None) or {}
        return str(metadata.get("rule_doc_url_template") or "")

    @staticmethod
    def _rule_url(template: str, rule_id: str) -> str:
        """Build the D10 deep link for one finding (empty when unavailable).

        The rule id comes from the author's SVRL ``@id`` and is interpolated
        into a URL that is rendered as a link, so it is percent-encoded before
        substitution — a rule id containing a quote, angle bracket, or space
        cannot break out of the URL/href. The template itself was already
        restricted to an http(s) scheme at authoring time
        (``SchematronStepConfigForm.clean_rule_doc_url_template``).
        """
        if not template or not rule_id:
            return ""
        from urllib.parse import quote

        try:
            return template.format(rule_id=quote(rule_id, safe=""))
        except (KeyError, IndexError, ValueError):
            logger.warning("Bad rule_doc_url_template on step ruleset")
            return ""

    @staticmethod
    def _build_stats(outputs: Any) -> dict[str, Any]:
        """Surface run provenance + engine metadata in step-run stats (D5).

        The sha256 of the executed rules plus the engine identity are what
        make a result reproducible. These keys land in ``step_run.output``
        alongside the serialized envelope.
        """
        if outputs is None:
            return {}
        stats: dict[str, Any] = {
            key: getattr(outputs, key, "") or "" for key in _PROVENANCE_KEYS
        }
        stats["engine_status"] = getattr(outputs, "engine_status", "") or ""
        stats["fired_rule_count"] = getattr(outputs, "fired_rule_count", 0)
        execution_seconds = getattr(outputs, "execution_seconds", None)
        if execution_seconds is not None:
            stats["execution_seconds"] = execution_seconds
        return stats
