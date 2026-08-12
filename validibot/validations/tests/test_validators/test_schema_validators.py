"""Runtime tests for schema validators with step-level assertions.

JSON Schema and XML Schema validators perform their schema check first and then
run workflow-authored assertions against the parsed payload. These tests prove
that the authoring UI capability is backed by validator execution, not just a
catalog flag.
"""

from validibot.actions.protocols import RunContext
from validibot.submissions.constants import SubmissionFileType
from validibot.submissions.tests.factories import SubmissionFactory
from validibot.validations.constants import AssertionOperator
from validibot.validations.constants import RulesetType
from validibot.validations.constants import ValidationType
from validibot.validations.constants import XMLSchemaType
from validibot.validations.tests.factories import RulesetAssertionFactory
from validibot.validations.tests.factories import RulesetFactory
from validibot.validations.tests.factories import ValidationRunFactory
from validibot.validations.tests.factories import ValidatorFactory
from validibot.validations.tests.resolved_file_inputs import resolved_file_input
from validibot.validations.tests.resolved_file_inputs import run_context_with_file
from validibot.validations.validators.json_schema.validator import JsonSchemaValidator
from validibot.validations.validators.xml_schema.validator import XmlSchemaValidator
from validibot.workflows.tests.factories import WorkflowStepFactory


def _run_context_for(validator, submission):
    """Build a real run_context so the submission envelope resolves.

    The ``submission.*`` namespace is read from ``run_context.validation_run``
    by both ``_build_cel_context`` and ``_enrich_basic_payload``. A validator
    invoked without a run context sees an empty envelope, so these tests must
    thread a real run whose submission carries the metadata under test.
    """
    step = WorkflowStepFactory(validator=validator)
    submission.org = step.workflow.org
    submission.project = step.workflow.project
    submission.workflow = step.workflow
    submission.save(update_fields=["org", "project", "workflow", "modified"])
    run = ValidationRunFactory(workflow=step.workflow, submission=submission)
    contract_key = {
        ValidationType.JSON_SCHEMA: "json_document",
        ValidationType.XML_SCHEMA: "xml_document",
    }[validator.validation_type]
    return run_context_with_file(
        contract_key=contract_key,
        content=submission.content,
        file_type=submission.file_type,
        validation_run=run,
        step=step,
        upstream_steps={},
    )


def test_json_schema_validator_runs_step_assertions_after_schema_validation(db):
    """A valid JSON document can still fail an added business-rule assertion."""
    validator = ValidatorFactory(
        validation_type=ValidationType.JSON_SCHEMA,
        supports_assertions=True,
    )
    ruleset = RulesetFactory(
        ruleset_type=RulesetType.JSON_SCHEMA,
        rules_text=(
            '{"$schema": "https://json-schema.org/draft/2020-12/schema", '
            '"type": "object", "properties": {"height": {"type": "number"}}, '
            '"required": ["height"]}'
        ),
    )
    assertion = RulesetAssertionFactory(
        ruleset=ruleset,
        target_data_path="height",
        operator=AssertionOperator.GE,
        rhs={"value": 20},
        message_template="Height is below the project minimum.",
    )
    submission = SubmissionFactory(
        content='{"height": 12}',
        file_type=SubmissionFileType.JSON,
    )

    result = JsonSchemaValidator().validate(
        validator,
        submission,
        ruleset,
        run_context=run_context_with_file(
            contract_key="json_document",
            content=submission.content,
            file_type=SubmissionFileType.JSON,
        ),
    )

    assert result.passed is False
    assert result.stats["schema_error_count"] == 0
    assert result.assertion_stats.total == 1
    assert result.assertion_stats.failures == 1
    assert result.issues[0].assertion_id == assertion.pk
    assert result.issues[0].message == "Height is below the project minimum."


def test_xml_schema_validator_runs_step_assertions_after_schema_validation(db):
    """A valid XML document can still fail an added business-rule assertion."""
    validator = ValidatorFactory(
        validation_type=ValidationType.XML_SCHEMA,
        supports_assertions=True,
    )
    ruleset = RulesetFactory(
        ruleset_type=RulesetType.XML_SCHEMA,
        rules_text="""
        <xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
          <xs:element name="building">
            <xs:complexType>
              <xs:sequence>
                <xs:element name="area" type="xs:int"/>
              </xs:sequence>
            </xs:complexType>
          </xs:element>
        </xs:schema>
        """,
        metadata={"schema_type": XMLSchemaType.XSD.value},
    )
    assertion = RulesetAssertionFactory(
        ruleset=ruleset,
        target_data_path="building.area",
        operator=AssertionOperator.GE,
        rhs={"value": 20},
        options={"coerce_types": True},
        message_template="Area is below the project minimum.",
    )
    submission = SubmissionFactory(
        content="<building><area>12</area></building>",
        file_type=SubmissionFileType.XML,
    )

    result = XmlSchemaValidator().validate(
        validator,
        submission,
        ruleset,
        run_context=run_context_with_file(
            contract_key="xml_document",
            content=submission.content,
            file_type=SubmissionFileType.XML,
        ),
    )

    assert result.passed is False
    assert result.stats["schema_error_count"] == 0
    assert result.assertion_stats.total == 1
    assert result.assertion_stats.failures == 1
    assert result.issues[0].assertion_id == assertion.pk
    assert result.issues[0].message == "Area is below the project minimum."


def test_json_schema_validator_prefers_resolved_artifact_bytes(db):
    """A composed JSON step validates its selected upstream artifact, not the PDF."""
    validator = ValidatorFactory(
        validation_type=ValidationType.JSON_SCHEMA,
        supports_assertions=False,
    )
    ruleset = RulesetFactory(
        ruleset_type=RulesetType.JSON_SCHEMA,
        rules_text=(
            '{"$schema": "https://json-schema.org/draft/2020-12/schema", '
            '"type": "object", "required": ["asset_id"]}'
        ),
    )
    submission = SubmissionFactory(
        content="%PDF-2.0 not JSON",
        file_type=SubmissionFileType.BINARY,
    )
    run_context = RunContext(
        resolved_file_inputs={
            "json_document": resolved_file_input(
                contract_key="json_document",
                content=b'{"asset_id": "A-42"}',
                file_type=SubmissionFileType.JSON,
            )
        }
    )

    result = JsonSchemaValidator().validate(
        validator,
        submission,
        ruleset,
        run_context=run_context,
    )

    assert result.passed is True
    assert result.stats["schema_error_count"] == 0


def test_xml_schema_validator_prefers_resolved_artifact_bytes(db):
    """A composed XML step validates its selected upstream artifact, not the PDF."""
    validator = ValidatorFactory(
        validation_type=ValidationType.XML_SCHEMA,
        supports_assertions=False,
    )
    ruleset = RulesetFactory(
        ruleset_type=RulesetType.XML_SCHEMA,
        rules_text=(
            '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">'
            '<xs:element name="asset" type="xs:string"/></xs:schema>'
        ),
        metadata={"schema_type": XMLSchemaType.XSD.value},
    )
    submission = SubmissionFactory(
        content="%PDF-2.0 not XML",
        file_type=SubmissionFileType.BINARY,
    )
    run_context = RunContext(
        resolved_file_inputs={
            "xml_document": resolved_file_input(
                contract_key="xml_document",
                content=b"<asset>A-42</asset>",
                file_type=SubmissionFileType.XML,
            )
        }
    )

    result = XmlSchemaValidator().validate(
        validator,
        submission,
        ruleset,
        run_context=run_context,
    )

    assert result.passed is True
    assert result.stats["schema_error_count"] == 0


# ── submission.* in BASIC assertions, per validator (ADR-2026-06-03b) ─────
# These prove the envelope namespace resolves in a REAL validator's BASIC
# assertion path — the gap fixed by centralizing enrichment in the basic
# evaluator. Previously JSON Schema and XML Schema handed the evaluator a raw
# payload, so a ``submission.*`` BASIC target reported "not found".


def test_json_schema_basic_assertion_reads_submission_metadata(db):
    """A BASIC ``submission.metadata.*`` target resolves for JSON Schema.

    The submitter-attached metadata must be readable by a basic assertion, not
    just CEL. We make the schema pass and gate purely on the envelope, so a
    failure would mean the namespace didn't resolve.
    """
    validator = ValidatorFactory(
        validation_type=ValidationType.JSON_SCHEMA,
        supports_assertions=True,
    )
    ruleset = RulesetFactory(
        ruleset_type=RulesetType.JSON_SCHEMA,
        rules_text='{"$schema": "https://json-schema.org/draft/2020-12/schema"}',
    )
    RulesetAssertionFactory(
        ruleset=ruleset,
        target_data_path="submission.metadata.deliverable",
        operator=AssertionOperator.EQ,
        rhs={"value": "handover"},
        message_template="Deliverable must be a handover.",
    )
    submission = SubmissionFactory(
        content="{}",
        file_type=SubmissionFileType.JSON,
        metadata={"deliverable": "handover"},
    )
    run_context = _run_context_for(validator, submission)

    result = JsonSchemaValidator().validate(
        validator,
        submission,
        ruleset,
        run_context=run_context,
    )

    # The envelope resolved and the assertion passed (handover == handover).
    assert result.assertion_stats.total == 1
    assert result.assertion_stats.failures == 0
    assert result.passed is True


def test_xml_schema_basic_assertion_reads_submission_metadata(db):
    """A BASIC ``submission.metadata.*`` target resolves for XML Schema.

    XML is a NON-JSON format, so this is the basic-path counterpart to the
    ADR's headline requirement: the envelope works where the file content is
    not JSON. A non-matching metadata value must fail with a *comparison*
    message, not "not found" — proving the value was actually read, not missing.
    """
    validator = ValidatorFactory(
        validation_type=ValidationType.XML_SCHEMA,
        supports_assertions=True,
    )
    ruleset = RulesetFactory(
        ruleset_type=RulesetType.XML_SCHEMA,
        rules_text=(
            '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">'
            '<xs:element name="building" type="xs:string"/></xs:schema>'
        ),
        metadata={"schema_type": XMLSchemaType.XSD.value},
    )
    RulesetAssertionFactory(
        ruleset=ruleset,
        target_data_path="submission.metadata.deliverable",
        operator=AssertionOperator.EQ,
        rhs={"value": "handover"},
    )
    # Metadata says "draft", but the gate requires "handover" → must FAIL,
    # and crucially fail because the value differs, not because it's missing.
    submission = SubmissionFactory(
        content="<building>ok</building>",
        file_type=SubmissionFileType.XML,
        metadata={"deliverable": "draft"},
    )
    run_context = _run_context_for(validator, submission)

    result = XmlSchemaValidator().validate(
        validator,
        submission,
        ruleset,
        run_context=run_context,
    )

    assert result.assertion_stats.total == 1
    assert result.assertion_stats.failures == 1
    # The value was resolved (to "draft") and compared — NOT reported missing.
    assert "was not found" not in result.issues[0].message
