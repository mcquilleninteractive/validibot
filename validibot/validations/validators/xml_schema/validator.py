from __future__ import annotations

import io
from typing import TYPE_CHECKING
from typing import Any

from django.utils.translation import gettext as _

from validibot.validations.constants import Severity
from validibot.validations.constants import XMLSchemaType
from validibot.validations.validators.base.base import AssertionStats
from validibot.validations.validators.base.base import BaseValidator
from validibot.validations.validators.base.base import ValidationIssue
from validibot.validations.validators.base.base import ValidationResult
from validibot.validations.xml_utils import XmlParseError
from validibot.validations.xml_utils import xml_to_dict

if TYPE_CHECKING:
    from validibot.actions.protocols import RunContext
    from validibot.submissions.models import Submission
    from validibot.validations.models import Ruleset
    from validibot.validations.models import Validator


class XmlSchemaValidator(BaseValidator):
    """
    XML validator that supports XSD (default), Relax NG, and DTD.

    It validates XML documents against an XML schema (XSD, Relax NG, or DTD)
    and reports structural violations. Step-level assertions run afterward
    against the parsed XML-as-dict payload, which lets workflow authors layer
    business rules on top of the schema contract.

    Ruleset requirements:
      * ``ruleset.metadata['schema_type']`` must be one of ``XMLSchemaType``.
      * ``ruleset.rules_text`` or ``ruleset.rules_file`` should provide the schema text.

    For legacy rulesets that did not embed the schema, we fall back to
    ``validator.config['schema']``. New rulesets should keep the schema in
    metadata so it travels with the reusable asset.

    **No ``extract_input_values`` override (per ADR-2026-05-22b
    Phase 6).** XML Schema validators don't parse an "arcane format" —
    the XML submission IS the data, converted to a nested dict via
    ``xml_to_dict`` so assertions can reference its paths directly via
    ``payload.<element>``. Nothing to derive in ``i.*`` that isn't
    already addressable via ``payload.*``.
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
        Validate the provided XML against the configured schema (XSD or Relax NG).
        Returns a ValidationResult with ERROR issues for any schema violations.
        """
        self.run_context = run_context

        resolved_file = (
            (run_context.resolved_file_inputs or {}).get("xml_document")
            if run_context is not None
            else None
        )
        if resolved_file is None or resolved_file.content is None:
            return ValidationResult(
                passed=False,
                issues=[
                    ValidationIssue(
                        path="xml_document",
                        message=_("The XML document input was not resolved."),
                        severity=Severity.ERROR,
                        code="required_input_missing",
                    ),
                ],
            )
        # lxml optional (import lazily)
        try:
            from lxml import etree
        except Exception as e:  # pragma: no cover
            return ValidationResult(
                passed=False,
                issues=[
                    ValidationIssue(
                        "",
                        _("lxml not installed or unusable: ") + str(e),
                        Severity.ERROR,
                    ),
                ],
                stats={"exception": type(e).__name__},
            )

        schema_type = self._resolve_schema_type(ruleset)
        raw = self._get_schema_raw(validator=validator, ruleset=ruleset)
        if not raw:
            return ValidationResult(
                passed=False,
                issues=[
                    ValidationIssue(
                        "",
                        _("Missing 'schema' in ruleset/validator config."),
                        Severity.ERROR,
                    ),
                ],
                stats={"schema_type": schema_type},
            )

        try:
            content = resolved_file.content
        except Exception as e:
            return ValidationResult(
                passed=False,
                issues=[
                    ValidationIssue(
                        "",
                        _("Could not read submission content: ") + str(e),
                        Severity.ERROR,
                    ),
                ],
                stats={"schema_type": schema_type, "exception": type(e).__name__},
            )
        if not content:
            return ValidationResult(
                passed=False,
                issues=[
                    ValidationIssue(
                        "",
                        _("Empty submission content."),
                        Severity.ERROR,
                    ),
                ],
                stats={"schema_type": schema_type},
            )

        # Parse XML payload
        try:
            parser = etree.XMLParser(
                recover=False,
                resolve_entities=False,
                no_network=True,
            )
            doc = etree.fromstring(content, parser=parser)
        except Exception as e:
            return ValidationResult(
                passed=False,
                issues=[
                    ValidationIssue(
                        "",
                        _("Invalid XML payload: ") + str(e),
                        Severity.ERROR,
                    ),
                ],
                stats={"schema_type": schema_type, "exception": type(e).__name__},
            )

        # Compile schema and validate
        try:
            schema = self._load_schema(schema_type=schema_type, raw=raw)
        except Exception as e:
            return ValidationResult(
                passed=False,
                issues=[ValidationIssue("", str(e), Severity.ERROR)],
                stats={"schema_type": schema_type, "exception": type(e).__name__},
            )

        ok = schema.validate(doc)
        issues: list[ValidationIssue] = []

        if not ok:
            for err in getattr(schema, "error_log", []) or []:
                if self._is_cascade_error(err):
                    continue
                path = self._extract_error_path(err)
                issues.append(ValidationIssue(path, str(err.message), Severity.ERROR))

        schema_issue_count = len(issues)
        assertion_total = 0
        assertion_failures = 0
        default_ruleset = getattr(validator, "default_ruleset", None)
        has_assertions = any(
            self._count_stage_assertions(
                ruleset,
                stage,
                default_ruleset=default_ruleset,
            )
            for stage in ("input", "output")
        )
        if has_assertions:
            try:
                assertion_payload = xml_to_dict(content)
            except XmlParseError as exc:
                issues.append(
                    ValidationIssue(
                        "",
                        _("Could not prepare XML payload for assertions: ") + str(exc),
                        Severity.ERROR,
                    )
                )
            else:
                assertion_result = self.evaluate_assertions_for_stages(
                    validator=validator,
                    ruleset=ruleset,
                    payload=assertion_payload,
                )
                issues.extend(assertion_result.issues)
                assertion_total = assertion_result.total
                assertion_failures = assertion_result.failures

        passed = not any(issue.severity == Severity.ERROR for issue in issues)
        return ValidationResult(
            passed=passed,
            issues=issues,
            assertion_stats=AssertionStats(
                total=assertion_total,
                failures=assertion_failures,
            ),
            stats={
                "error_count": schema_issue_count,
                "schema_error_count": schema_issue_count,
                "schema_type": schema_type,
            },
        )

    # PRIVATE METHODS
    # ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

    def _resolve_schema_type(self, ruleset) -> str:
        schema_type = None
        if ruleset is not None:
            metadata = getattr(ruleset, "metadata", None) or {}
            if isinstance(metadata, dict):
                schema_type = (metadata.get("schema_type") or "").strip().upper()
        # Expect the upper-case string of the enum's value (e.g., "XSD" or "RELAXNG")
        if schema_type not in {
            XMLSchemaType.XSD,
            XMLSchemaType.RELAXNG,
            XMLSchemaType.DTD,
        }:
            err_msg = _(
                "Invalid or missing XML schema_type '%(schema_type)s';"
                "must be 'XSD', 'RELAXNG', or 'DTD'.",
            ) % {"schema_type": schema_type or "<missing>"}
            raise ValueError(err_msg)
        return schema_type

    def _load_schema(self, schema_type: str, raw: str) -> Any:
        """
        Parse an author-supplied XML schema string into an lxml schema object.

        WHY hardened parsing matters here: the schema document is just as
        untrusted as the instance document — it is author-supplied ruleset text.
        A malicious XSD/RelaxNG can carry ``xs:import``/``xs:include`` (or DTD
        external entities) whose ``schemaLocation`` points at ``file://`` (local
        file disclosure) or ``http://`` (SSRF / outbound socket) URLs, and lxml
        will dereference those *during schema compilation* unless told not to.
        The original code compiled the schema with lxml's BARE default parser,
        so such an import resolved and read the targeted file / opened a socket.

        Two things harden it, and BOTH are required:

        1. The ``etree.XMLParser`` mirrors the instance parser in ``validate``
           (``recover=False``, ``resolve_entities=False``, ``no_network=True``).
           ``resolve_entities=False`` blocks external DTD entity expansion in
           the schema text itself.
        2. A custom :class:`etree.Resolver` is attached to that parser which
           REFUSES every external system URL. This is the part that actually
           stops ``xs:import``/``xs:include``/``xi:include`` dereferencing,
           because libxml2's schema compiler resolves ``schemaLocation`` through
           the parser's resolver chain rather than honouring ``no_network``
           alone for ``file://`` URLs. Refusing in the resolver means a schema
           pointing at ``file:///etc/passwd`` or ``http://attacker/`` neither
           reads the file nor opens a socket — compilation simply fails.

        Legitimate self-contained schemas (no external imports) are unaffected
        and still compile and validate normally.

        Args:
            schema_type: One of the ``XMLSchemaType`` names ("XSD", "RELAXNG",
                "DTD"), already validated by ``_resolve_schema_type``.
            raw: The raw schema document text supplied by the ruleset/validator.

        Returns:
            An lxml schema validator object (``XMLSchema``, ``RelaxNG``, or
            ``DTD``) suitable for ``.validate(doc)``.

        Raises:
            ImportError: If lxml is unavailable.
            ValueError: If ``schema_type`` is not a supported value.
        """
        try:
            from lxml import etree
        except Exception as e:  # pragma: no cover
            raise ImportError(_("XML validation requires lxml: ") + str(e)) from e

        class _BlockExternalResolver(etree.Resolver):
            """Refuse every external resource lxml tries to dereference.

            Defined locally because subclassing ``etree.Resolver`` requires
            lxml, which is an optional dependency imported lazily above. lxml
            invokes ``resolve`` for each ``schemaLocation``/``xi:include`` target
            during schema compilation; raising here turns an attempted ``file://``
            disclosure or ``http://`` SSRF into a clean compilation failure.
            """

            def resolve(self, system_url, public_id, context):
                msg = _("External schema resource resolution is not allowed: ")
                raise OSError(msg + str(system_url))

        # Same hardening as the instance parser in ``validate`` (no network, no
        # external-entity resolution), PLUS a resolver that blocks the schema
        # compiler from fetching any ``schemaLocation`` / include target.
        schema_parser = etree.XMLParser(
            recover=False,
            resolve_entities=False,
            no_network=True,
        )
        schema_parser.resolvers.add(_BlockExternalResolver())

        if schema_type == XMLSchemaType.XSD.name:
            return etree.XMLSchema(
                etree.XML(raw.encode("utf-8"), parser=schema_parser),
            )
        if schema_type == XMLSchemaType.RELAXNG.name:
            return etree.RelaxNG(
                etree.XML(raw.encode("utf-8"), parser=schema_parser),
            )
        if schema_type == XMLSchemaType.DTD.name:
            # ``etree.DTD()`` accepts no ``parser`` argument, so the
            # ``_BlockExternalResolver`` above cannot be attached to it the way
            # it is for XSD/RelaxNG. That gap is exploitable: a DTD whose text
            # declares an external *parameter entity* and then references it —
            # ``<!ENTITY % x SYSTEM "file:///etc/passwd"> %x;`` — makes lxml
            # dereference that URL while compiling the DTD (local-file
            # disclosure / SSRF). Reading the bytes through ``BytesIO`` does
            # NOT prevent this; the external subset is still fetched.
            #
            # So gate the DTD first: parse its text as the *internal subset* of
            # a throwaway document using a parser that DOES carry the blocking
            # resolver. ``load_dtd=True`` makes libxml2 actually process the
            # subset — including any external parameter-entity references — so
            # an attempt to dereference an external resource fires
            # ``_BlockExternalResolver`` and raises here (the caller turns that
            # into a clean validation error rather than a 500). Only once the
            # gate proves the DTD is self-contained do we build the real
            # validator the original way: by then there is provably nothing
            # external left for it to fetch. A malformed DTD also raises at the
            # gate, which is the desired fail-closed behaviour.
            dtd_guard_parser = etree.XMLParser(
                resolve_entities=False,
                no_network=True,
                load_dtd=True,
            )
            dtd_guard_parser.resolvers.add(_BlockExternalResolver())
            dtd_probe = (
                b"<!DOCTYPE _vb_dtd_probe [\n"
                + raw.encode("utf-8")
                + b"\n]>\n<_vb_dtd_probe/>"
            )
            etree.fromstring(dtd_probe, parser=dtd_guard_parser)
            return etree.DTD(io.BytesIO(raw.encode("utf-8")))
        raise ValueError(_("Unsupported XML schema type: ") + schema_type)

    # Error types that just restate a parent failure and add no useful info.
    _CASCADE_ERROR_TYPES = frozenset(
        {
            "RELAXNG_ERR_INTERSEQ",
            "RELAXNG_ERR_CONTENTVALID",
            "RELAXNG_ERR_EXTRACONTENT",
            "RELAXNG_ERR_INTEREXTRA",
            "SCHEMASV_CVC_COMPLEX_TYPE_2_4",
            "SCHEMAV_CVC_ELT_1",
        }
    )

    @classmethod
    def _is_cascade_error(cls, err) -> bool:
        """Return True for errors that cascade from a root cause."""
        type_name = getattr(err, "type_name", "") or ""
        return type_name in cls._CASCADE_ERROR_TYPES

    @staticmethod
    def _extract_error_path(err) -> str:
        """Build a human-readable path from an lxml error entry."""
        raw_path = getattr(err, "path", None) or ""
        line = getattr(err, "line", 0) or 0
        # lxml sometimes returns the string "None" instead of a real path.
        if raw_path in ("None", ""):
            raw_path = ""
        elif raw_path == "/*":
            # Root element — show a friendlier label.
            return f"(document root), line {line}" if line else "(document root)"
        if raw_path:
            return f"{raw_path} (line {line})" if line else raw_path
        if line:
            return f"line {line}"
        return ""

    def _get_schema_raw(
        self,
        *,
        validator: Validator,
        ruleset: Ruleset,
    ) -> str | None:
        raw: str | None = None
        if ruleset is not None:
            raw_candidate = getattr(ruleset, "rules", None)
            if isinstance(raw_candidate, str) and raw_candidate.strip():
                raw = raw_candidate
        if not raw and isinstance(getattr(validator, "config", None), dict):
            raw_config = validator.config.get("schema")
            if isinstance(raw_config, str):
                raw = raw_config
        return raw
