"""The Tabular Validator — ties the reader and native validation together.

``validate()`` reads the submitted CSV into the shared in-memory model, runs
native structured validation against the ruleset's Table Schema, evaluates
per-row CEL assertions through ``row.*`` and aggregate assertions through
``col.*``, maps the resulting :class:`NativeFinding`s onto the platform's
``ValidationIssue``, and runs the standard dataset ``i.*`` CEL lane. It also
exposes the ``i.*`` dataset input values so a ``i.num_rows >= 100``-style assertion
can resolve.

Configuration lives on the ruleset, mirroring the JSON Schema validator:

- ``ruleset.rules`` (``rules_text``/``rules_file``) holds the **Table Schema
  descriptor** (JSON) — the structured column config.
- ``ruleset.metadata`` holds the **dialect** (``delimiter``, ``has_header``,
  ``quotechar``) and ``report_max_examples``.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from typing import Any

from django.utils.translation import gettext as _

from validibot.validations.constants import Severity
from validibot.validations.validators.base.base import AssertionStats
from validibot.validations.validators.base.base import BaseValidator
from validibot.validations.validators.base.base import ValidationIssue
from validibot.validations.validators.base.base import ValidationResult
from validibot.validations.validators.tabular.column_eval import ColumnAssertion
from validibot.validations.validators.tabular.column_eval import (
    evaluate_column_assertions,
)
from validibot.validations.validators.tabular.metadata import TABULAR_DATASET_INPUTS
from validibot.validations.validators.tabular.native import DEFAULT_REPORT_MAX_EXAMPLES
from validibot.validations.validators.tabular.native import validate_native
from validibot.validations.validators.tabular.preflight import TabularDialect
from validibot.validations.validators.tabular.preflight import TabularLimits
from validibot.validations.validators.tabular.preflight import TabularReadError
from validibot.validations.validators.tabular.readers.csv import read_csv
from validibot.validations.validators.tabular.row_eval import RowAssertion
from validibot.validations.validators.tabular.row_eval import evaluate_row_assertions
from validibot.validations.validators.tabular.schema import parse_table_schema

if TYPE_CHECKING:
    from validibot.actions.protocols import RunContext
    from validibot.validations.models import Ruleset
    from validibot.validations.models import Submission
    from validibot.validations.models import Validator
    from validibot.validations.validators.tabular.native import NativeFinding
    from validibot.validations.validators.tabular.readers.csv import ReadResult
    from validibot.validations.validators.tabular.schema import TabularSchema

# A schema that won't parse is a configuration error, surfaced as a finding.
CODE_INVALID_SCHEMA = "tabular.invalid_schema"
# The fallback when a ruleset doesn't pin its own ``report_max_examples``. Kept
# as an alias of the canonical native default so there is one number to change,
# Ruleset metadata remains the native-check default; row assertions can override
# it individually through their options.
_DEFAULT_REPORT_MAX_EXAMPLES = DEFAULT_REPORT_MAX_EXAMPLES


class TabularValidator(BaseValidator):
    """In-process validator for tabular data (CSV in V1).

    See the module docstring and ADR-2026-05-26 for the full design. The
    validate flow is: load schema → read CSV → native validation → row CEL →
    column CEL → dataset CEL, returning aggregated ``ValidationIssue``s.
    """

    def __init__(self, *, config: dict[str, Any] | None = None) -> None:
        super().__init__(config=config)
        # Populated in validate() and returned by extract_input_values() so
        # dataset (input-stage) CEL assertions can resolve the i.* namespace.
        self._input_values: dict[str, Any] = {}

    def validate(
        self,
        validator: Validator,
        submission: Submission,
        ruleset: Ruleset,
        run_context: RunContext | None = None,
    ) -> ValidationResult:
        """Validate a tabular submission and return aggregated issues."""
        self.run_context = run_context
        self._input_values = {}

        # 1. Load the structured config (Table Schema). A bad schema is a
        #    configuration error reported as a single finding, not a crash.
        try:
            schema = self._load_schema(ruleset)
        except (ValueError, TypeError) as exc:
            return self._single_error(
                CODE_INVALID_SCHEMA,
                str(exc),
                stats={"exception": type(exc).__name__},
            )

        dialect, limits, report_max_examples = self._load_settings(ruleset)

        resolved_file = (
            (run_context.resolved_file_inputs or {}).get("table_document")
            if run_context is not None
            else None
        )
        if resolved_file is None or resolved_file.content is None:
            return self._single_error(
                "required_input_missing",
                "The table document input was not resolved.",
            )
        content_bytes = resolved_file.content
        declared_columns = None if dialect.has_header else schema.field_names()
        try:
            read_result = read_csv(
                content_bytes,
                dialect=dialect,
                declared_columns=declared_columns,
                limits=limits,
            )
        except TabularReadError as exc:
            return self._single_error(
                exc.code,
                str(exc),
                stats={"read_error": exc.code},
            )

        # 3. Dataset input values (i.*) — built before the dataset gate so
        #    `i.num_rows`/`i.column_names`/… resolve, and returned for downstream
        #    steps. Derived only from the parsed dataframe + submission.
        self._input_values = self._build_input_values(read_result, resolved_file.name)

        # 4. Dataset (input-stage) CEL assertions run BEFORE the native / row /
        #    column passes (ADR-2026-05-26): a *failing* dataset assertion
        #    short-circuits that work, so e.g. `i.num_rows <= 1_000_000` rejects
        #    an oversized table without validating every row. Only ERROR-severity
        #    failures gate; a WARNING dataset assertion is carried forward. The
        #    output stage is deferred to step 8 — it needs the validator's
        #    outputs, which don't exist yet.
        dataset_result = self.evaluate_assertions_for_stages(
            validator=validator,
            ruleset=ruleset,
            payload={},
            stages=("input",),
        )
        if any(issue.severity == Severity.ERROR for issue in dataset_result.issues):
            return ValidationResult(
                passed=False,
                issues=list(dataset_result.issues),
                assertion_stats=AssertionStats(
                    total=dataset_result.total,
                    failures=dataset_result.failures,
                ),
                output_values=self._input_values,
                stats={
                    "num_rows": read_result.num_rows,
                    "num_columns": read_result.num_columns,
                    "short_circuited": "dataset_assertion_failed",
                },
            )
        # Carry forward any non-error (WARNING) dataset findings.
        issues = list(dataset_result.issues)

        # 5. Native structured validation against the schema. The wall-clock
        #    budget bounds the author-supplied regex pattern checks (which run
        #    against every submitter cell) the same way the row lane is bounded.
        native_findings = validate_native(
            read_result,
            schema,
            report_max_examples=report_max_examples,
            wall_clock_budget_s=limits.max_wallclock_s,
        )
        issues.extend(self._to_issue(finding) for finding in native_findings)

        # 6. Row-stage CEL (the row.* loop). Validator-owned: these assertions
        #    are skipped by the generic lane (they reference row.*, which it
        #    doesn't bind) and evaluated here against every row, with now()
        #    pinned to the run clock.
        row_assertions = self._collect_row_assertions(validator, ruleset)
        row_findings = evaluate_row_assertions(
            read_result,
            schema,
            row_assertions,
            signals=self._workflow_signals(run_context),
            input_values=self._input_values,
            now=self._run_clock(run_context),
            report_max_examples=report_max_examples,
        )
        issues.extend(self._to_issue(finding) for finding in row_findings)

        # 7. Column-stage CEL runs once against typed per-column aggregates.
        column_assertions = self._collect_column_assertions(validator, ruleset)
        column_findings = evaluate_column_assertions(
            read_result,
            schema,
            column_assertions,
            signals=self._workflow_signals(run_context),
            input_values=self._input_values,
            now=self._run_clock(run_context),
            wall_clock_budget_s=limits.max_wallclock_s,
        )
        issues.extend(self._to_issue(finding) for finding in column_findings)

        # 8. Output-stage CEL assertions (those that read the validator's
        #    outputs). Dataset/input-stage assertions already ran in step 4;
        #    row/column assertions are excluded by the lane itself.
        output_result = self.evaluate_assertions_for_stages(
            validator=validator,
            ruleset=ruleset,
            payload={},
            stages=("output",),
        )
        issues.extend(output_result.issues)

        # Assertion stats count *assertions* (not rows): the generic lane's
        # totals plus the row/column assertions, with a row/column assertion
        # counted as a failure when it produced any finding.
        failed_row_assertion_ids = {
            finding.assertion_id
            for finding in row_findings
            if finding.assertion_id is not None
        }
        failed_column_assertion_ids = {
            finding.assertion_id
            for finding in column_findings
            if finding.assertion_id is not None
        }
        passed = not any(issue.severity == Severity.ERROR for issue in issues)
        return ValidationResult(
            passed=passed,
            issues=issues,
            assertion_stats=AssertionStats(
                total=(
                    dataset_result.total
                    + output_result.total
                    + len(row_assertions)
                    + len(column_assertions)
                ),
                failures=(
                    dataset_result.failures
                    + output_result.failures
                    + len(failed_row_assertion_ids)
                    + len(failed_column_assertion_ids)
                ),
            ),
            output_values=self._input_values,
            stats={
                "num_rows": read_result.num_rows,
                "num_columns": read_result.num_columns,
                "native_finding_count": len(native_findings),
                "row_assertion_count": len(row_assertions),
                "column_assertion_count": len(column_assertions),
            },
        )

    def extract_input_values(self, payload: Any) -> dict[str, Any] | None:
        """Expose the ``i.*`` dataset metadata computed during ``validate()``.

        The input values are derived from the parsed dataframe (row/column counts,
        column names, dialect), not from re-parsing *payload*, so the argument
        is ignored. Returns ``None`` when no dataset has been read yet, matching
        the base default (which leaves ``i.*`` empty).
        """
        return self._input_values or None

    # ------------------------------------------------------------------ private

    def _single_error(
        self,
        code: str,
        message: str,
        *,
        stats: dict[str, Any] | None = None,
    ) -> ValidationResult:
        """Build a failed result carrying one ERROR issue (config/read failure)."""
        return ValidationResult(
            passed=False,
            issues=[
                ValidationIssue(
                    path="",
                    message=message,
                    severity=Severity.ERROR,
                    code=code,
                ),
            ],
            stats=stats,
        )

    def _to_issue(self, finding: NativeFinding) -> ValidationIssue:
        """Map a :class:`NativeFinding` onto a platform ``ValidationIssue``.

        The richer native shape (count + sample rows + column) is preserved in
        ``meta`` so the finding row and UI can show "12 of 50 rows failed; e.g.
        rows 3, 7, 9" without the message text having to carry it.
        """
        return ValidationIssue(
            path=finding.column or "",
            message=finding.message,
            severity=finding.severity,
            code=finding.code,
            meta={
                "count": finding.count,
                "sample_rows": list(finding.sample_rows),
                "column": finding.column,
            },
            assertion_id=finding.assertion_id,
        )

    def _load_schema(self, ruleset: Ruleset) -> TabularSchema:
        raw_schema = getattr(ruleset, "rules", None)
        if not raw_schema:
            msg = _(
                "Tabular ruleset must provide a Table Schema via rules_text "
                "or rules_file.",
            )
            raise ValueError(msg)
        descriptor = (
            raw_schema if isinstance(raw_schema, dict) else json.loads(raw_schema)
        )
        return parse_table_schema(descriptor)

    def _load_settings(
        self,
        ruleset: Ruleset,
    ) -> tuple[TabularDialect, TabularLimits, int]:
        metadata = getattr(ruleset, "metadata", None) or {}
        dialect = TabularDialect(
            # None means "sniff"; an empty string in metadata also means sniff.
            delimiter=metadata.get("delimiter") or None,
            quotechar=metadata.get("quotechar", '"'),
            # Pinned to UTF-8 in V1; metadata["encoding"] is always "utf-8".
            encoding="utf-8",
            has_header=bool(metadata.get("has_header", True)),
        )
        report_max_examples = metadata.get(
            "report_max_examples",
            _DEFAULT_REPORT_MAX_EXAMPLES,
        )
        try:
            report_max_examples = int(report_max_examples)
        except (TypeError, ValueError):
            report_max_examples = _DEFAULT_REPORT_MAX_EXAMPLES
        return dialect, TabularLimits(), report_max_examples

    def _collect_row_assertions(
        self,
        validator: Validator,
        ruleset: Ruleset,
    ) -> list[RowAssertion]:
        """Gather the ruleset's row CEL assertions as engine specs.

        Row assertions are ``RulesetAssertion`` rows tagged
        ``options["tabular_stage"] == "row"`` (the persistence decision in
        ADR-2026-05-26). Both the validator's default ruleset and the step
        ruleset are scanned, matching the generic lane's source order.
        """
        specs: list[RowAssertion] = []
        for source in (getattr(validator, "default_ruleset", None), ruleset):
            if source is None:
                continue
            for assertion in source.assertions.all():
                if (assertion.options or {}).get("tabular_stage") != "row":
                    continue
                expression = (
                    (assertion.rhs or {}).get("expr") or assertion.cel_cache or ""
                )
                if not expression:
                    continue
                specs.append(
                    RowAssertion(
                        expression=expression,
                        message=assertion.message_template or "",
                        severity=Severity(assertion.severity or Severity.ERROR),
                        assertion_id=assertion.pk,
                        report_max_examples=self._assertion_example_limit(assertion),
                        # The optional `when` guard — the generic lane evaluates
                        # it for dataset assertions, but it skips row/col
                        # assertions, so the validator must honour it itself.
                        when_expression=(assertion.when_expression or "").strip(),
                    ),
                )
        return specs

    def _collect_column_assertions(
        self,
        validator: Validator,
        ruleset: Ruleset,
    ) -> list[ColumnAssertion]:
        """Gather V2 column-stage assertions as engine specs."""
        specs: list[ColumnAssertion] = []
        for source in (getattr(validator, "default_ruleset", None), ruleset):
            if source is None:
                continue
            for assertion in source.assertions.all():
                if (assertion.options or {}).get("tabular_stage") != "column":
                    continue
                expression = (
                    (assertion.rhs or {}).get("expr") or assertion.cel_cache or ""
                )
                if not expression:
                    continue
                specs.append(
                    ColumnAssertion(
                        expression=expression,
                        message=assertion.message_template or "",
                        severity=Severity(assertion.severity or Severity.ERROR),
                        assertion_id=assertion.pk,
                        when_expression=(assertion.when_expression or "").strip(),
                    ),
                )
        return specs

    @staticmethod
    def _assertion_example_limit(assertion: Any) -> int:
        """Return a bounded per-assertion sample-row limit."""
        raw = (assertion.options or {}).get(
            "report_max_examples",
            _DEFAULT_REPORT_MAX_EXAMPLES,
        )
        try:
            return max(1, min(100, int(raw)))
        except (TypeError, ValueError):
            return _DEFAULT_REPORT_MAX_EXAMPLES

    def _run_clock(self, run_context: RunContext | None) -> Any:
        """Return the run's ``started_at`` to pin ``now()`` (or None).

        When there is no run context (e.g. a direct unit-test call), ``now()``
        is left unbound and any assertion using it fails cleanly — never the
        wall clock.
        """
        run = getattr(run_context, "validation_run", None)
        return getattr(run, "started_at", None)

    def _workflow_signals(self, run_context: RunContext | None) -> dict[str, Any]:
        """Return the workflow signals (s.*) available to row assertions."""
        return getattr(run_context, "workflow_signals", None) or {}

    def _build_input_values(
        self,
        read_result: ReadResult,
        filename: str,
    ) -> dict[str, Any]:
        preflight = read_result.preflight
        values = {
            "num_rows": read_result.num_rows,
            "num_columns": read_result.num_columns,
            "column_names": list(read_result.column_names),
            "delimiter": preflight.delimiter,
            "encoding": preflight.encoding,
            "has_header": preflight.has_header,
            "size_bytes": preflight.size_bytes,
            "filename": filename,
        }
        return {
            contract_key: values[contract_key]
            for contract_key, _label in TABULAR_DATASET_INPUTS
        }
