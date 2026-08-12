from __future__ import annotations

import json
from typing import TYPE_CHECKING
from typing import Any

from django.utils.translation import gettext as _
from jsonschema import Draft202012Validator
from jsonschema import FormatChecker
from referencing import Registry
from referencing.exceptions import NoSuchResource
from referencing.exceptions import Unresolvable

from validibot.validations.constants import Severity
from validibot.validations.validators.base.base import AssertionStats
from validibot.validations.validators.base.base import BaseValidator
from validibot.validations.validators.base.base import ValidationIssue
from validibot.validations.validators.base.base import ValidationResult

if TYPE_CHECKING:
    from validibot.actions.protocols import RunContext
    from validibot.validations.models import Ruleset
    from validibot.validations.models import Submission
    from validibot.validations.models import Validator


def _reject_external_ref(uri: str) -> None:
    """
    Registry ``retrieve`` callback that refuses to fetch any external resource.

    WHAT: ``referencing`` calls this whenever a ``$ref`` points at a URI that is
    not already present in the in-memory registry (i.e. anything that is not an
    internal ``#/...`` pointer into the schema being validated). We always raise
    ``NoSuchResource`` so resolution fails locally instead of triggering a
    network or filesystem fetch.

    WHY: Ruleset schemas are author-controlled. Without this guard, building a
    ``Draft202012Validator`` from such a schema lets jsonschema resolve remote
    ``$ref`` values over the network (an SSRF vector against, e.g., the cloud
    metadata endpoint ``169.254.169.254``) and read local files via ``file://``
    refs. By rejecting every external URI here, an offending ``$ref`` surfaces as
    a handled ``referencing.exceptions.Unresolvable`` (which ``validate`` turns
    into a controlled ERROR issue) and never opens an outbound socket or touches
    the local filesystem.

    Args:
        uri: The external URI that a ``$ref`` attempted to resolve.

    Raises:
        NoSuchResource: Always, to signal the URI cannot be retrieved.
    """
    raise NoSuchResource(ref=uri)


# A single shared registry whose ``retrieve`` callback denies every external
# fetch. It is safe to reuse across validations because it holds no per-request
# state — internal ``$ref``/``$defs`` resolution still works (those resources are
# crawled from the schema itself, never retrieved), only http(s)/file refs fail.
_NO_EXTERNAL_FETCH_REGISTRY = Registry(retrieve=_reject_external_ref)


class JsonSchemaValidator(BaseValidator):
    """
    JSON Schema validator (Draft 2020-12 compatible).

    It validates JSON documents against a JSON Schema and reports structural
    violations. Step-level assertions run afterward against the parsed JSON
    payload, which lets workflow authors layer business rules on top of the
    schema contract.

    Expects a JSON Schema stored on the associated ruleset via ``rules_text`` or
    ``rules_file`` (retrieved through ``ruleset.rules``).

    **No ``extract_input_values`` override (per ADR-2026-05-22b
    Phase 6).** JSON Schema validators don't parse an "arcane format" —
    the submission IS the JSON data, and assertions reference its
    paths directly via ``payload.<field>``. There are no hidden facts
    to derive from a different representation, so the ``i.*``
    namespace stays empty (the base default's None return). Authors
    write assertions against ``payload.*`` instead.
    """

    # PUBLIC METHODS
    # ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

    def validate(
        self,
        validator: Validator,
        submission: Submission,
        ruleset: Ruleset,
        run_context: RunContext | None = None,
    ) -> ValidationResult:
        """
        Validate a JSON document against the configured JSON Schema.

        Parses the submission content as JSON and validates it against the
        Draft 2020-12 JSON Schema stored in the ruleset. Returns ERROR issues
        for any schema violations.
        """
        self.run_context = run_context

        resolved_file = (
            (run_context.resolved_file_inputs or {}).get("json_document")
            if run_context is not None
            else None
        )

        if resolved_file is None or resolved_file.content is None:
            return ValidationResult(
                passed=False,
                issues=[
                    ValidationIssue(
                        path="json_document",
                        message=_("The JSON document input was not resolved."),
                        severity=Severity.ERROR,
                        code="required_input_missing",
                    ),
                ],
            )
        # Load the schema we'll be using...
        try:
            schema = self._load_schema(validator=validator, ruleset=ruleset)
        except Exception as e:
            return ValidationResult(
                passed=False,
                issues=[ValidationIssue("", str(e), Severity.ERROR)],
                stats={"exception": type(e).__name__},
            )

        # Now load incoming content...
        payload = resolved_file.content

        try:
            data = json.loads(payload)
        except Exception as e:
            return ValidationResult(
                passed=False,
                issues=[
                    ValidationIssue(
                        path="",
                        message=_("Invalid JSON payload") + f": {e}",
                    ),
                ],
                stats={"exception": type(e).__name__},
            )

        # Validate against JSON Schema.
        #
        # We pass an explicit ``registry`` whose ``retrieve`` callback rejects
        # every external URI (see ``_reject_external_ref``). This closes an SSRF
        # / local-file-read hole: an author-controlled schema could otherwise use
        # a remote ``$ref`` (e.g. ``http://169.254.169.254/...``) or a
        # ``file://`` ref, and jsonschema would dutifully open a socket or read
        # the file while resolving it. With the locked-down registry, internal
        # ``#/$defs`` refs still resolve, but any external ref fails fast as an
        # ``Unresolvable`` error that we convert into a controlled ERROR issue
        # below — no network or filesystem access ever happens.
        v = Draft202012Validator(
            schema,
            registry=_NO_EXTERNAL_FETCH_REGISTRY,
            format_checker=FormatChecker(),
        )
        try:
            errors = sorted(v.iter_errors(data), key=lambda e: list(e.path))
        except Unresolvable as e:
            # A ``$ref`` pointed at an external (or otherwise unresolvable)
            # resource. Surface it as a handled validation error rather than
            # letting it bubble up as an unhandled exception.
            return ValidationResult(
                passed=False,
                issues=[
                    ValidationIssue(
                        path="",
                        message=_(
                            "Schema references an external resource, which is "
                            "not permitted: %(detail)s",
                        )
                        % {"detail": str(e)},
                        severity=Severity.ERROR,
                    ),
                ],
                stats={"exception": type(e).__name__},
            )
        issues: list[ValidationIssue] = [
            ValidationIssue("/".join(map(str, e.path)), e.message) for e in errors
        ]

        assertion_result = self.evaluate_assertions_for_stages(
            validator=validator,
            ruleset=ruleset,
            payload=data,
        )
        issues.extend(assertion_result.issues)

        passed = not any(issue.severity == Severity.ERROR for issue in issues)
        return ValidationResult(
            passed=passed,
            issues=issues,
            assertion_stats=AssertionStats(
                total=assertion_result.total,
                failures=assertion_result.failures,
            ),
            stats={
                "error_count": len(errors),
                "schema_error_count": len(errors),
            },
        )

    # PRIVATE METHODS
    # ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

    def _load_schema(self, *, validator, ruleset) -> dict[str, Any]:
        raw_schema = getattr(ruleset, "rules", None)
        if not raw_schema:
            raise ValueError(
                _("Ruleset must provide schema text via rules_text or rules_file."),
            )
        if isinstance(raw_schema, dict):
            return raw_schema
        if isinstance(raw_schema, str):
            return json.loads(raw_schema)
        raise TypeError(_("Unsupported schema type; expected dict or JSON string."))
