from http import HTTPStatus

import pytest
from django.urls import reverse

from validibot.submissions.constants import SubmissionDataFormat
from validibot.users.constants import RoleCode
from validibot.users.tests.factories import OrganizationFactory
from validibot.users.tests.factories import UserFactory
from validibot.users.tests.factories import grant_role
from validibot.users.tests.utils import ensure_all_roles_exist
from validibot.validations.constants import AssertionType
from validibot.validations.constants import Severity
from validibot.validations.constants import StepIODirection
from validibot.validations.constants import ValidationType
from validibot.validations.constants import ValidatorRuleType
from validibot.validations.models import RulesetAssertion
from validibot.validations.tests.factories import StepIODefinitionFactory
from validibot.validations.tests.factories import ValidatorFactory
from validibot.validations.utils import create_custom_validator


@pytest.mark.django_db
def test_default_assertion_nulls_io_definition_on_delete():
    """Deleting a StepIODefinition nulls the FK on referencing assertions (SET_NULL).

    The target_io_definition FK uses SET_NULL so deleting a step I/O definition
    does not cascade-delete or block deletion of assertions. After deletion
    the assertion still exists but its target_io_definition is None.
    """
    validator = ValidatorFactory()
    io_definition = StepIODefinitionFactory(validator=validator, contract_key="foo")
    default_ruleset = validator.ensure_default_ruleset()
    RulesetAssertion.objects.create(
        ruleset=default_ruleset,
        assertion_type=AssertionType.CEL_EXPRESSION,
        target_io_definition=io_definition,
        rhs={"expr": "foo > 0"},
        severity=Severity.ERROR,
        order=0,
        message_template="Sample",
        cel_cache="foo > 0",
    )

    # Deleting the I/O definition should succeed (SET_NULL, not PROTECT).
    io_definition.delete()

    # The assertion still exists but the FK is now None.
    assertion = default_ruleset.assertions.get(message_template="Sample")
    assert assertion.target_io_definition is None


@pytest.mark.django_db
def test_default_assertions_modal_lists_rules(client):
    """The default assertions modal shows assertions from default_ruleset."""
    ensure_all_roles_exist()
    org = OrganizationFactory()
    user = UserFactory()
    grant_role(user, org, RoleCode.AUTHOR)
    user.set_current_org(org)
    user.refresh_from_db()
    client.force_login(user)
    session = client.session
    session["active_org_id"] = org.pk
    session.save()
    validator = ValidatorFactory(
        org=org,
        is_system=False,
        validation_type=ValidationType.BASIC,
        slug="default-assertions-validator",
    )
    default_ruleset = validator.ensure_default_ruleset()
    RulesetAssertion.objects.create(
        ruleset=default_ruleset,
        assertion_type=AssertionType.CEL_EXPRESSION,
        target_data_path="payload.value >= 0",
        rhs={"expr": "payload.value >= 0"},
        severity=Severity.ERROR,
        order=1,
        message_template="Always positive",
        cel_cache="payload.value >= 0",
    )

    url = reverse(
        "validations:validator_default_assertions",
        kwargs={"slug": validator.slug},
    )
    response = client.get(url, HTTP_HX_REQUEST="true")

    assert response.status_code == HTTPStatus.OK
    html = response.content.decode()
    assert "Default assertions for" in html
    assert "Always positive" in html
    assert "payload.value" in html
    assert "View Validator Assertions" in html


@pytest.mark.django_db
def test_default_assertion_allows_boolean_literal(client):
    """Creating a default assertion with boolean literals works via the API."""
    ensure_all_roles_exist()
    org = OrganizationFactory()
    user = UserFactory()
    grant_role(user, org, RoleCode.AUTHOR)
    user.set_current_org(org)
    client.force_login(user)
    session = client.session
    session["active_org_id"] = org.pk
    session.save()

    validator = ValidatorFactory(org=org, is_system=False)
    StepIODefinitionFactory(
        validator=validator,
        contract_key="bool_out",
        direction=StepIODirection.OUTPUT,
    )

    response = client.post(
        reverse(
            "validations:validator_rule_create",
            kwargs={"pk": validator.pk},
        ),
        data={
            "name": "Bool check",
            "description": "",
            "rule_type": ValidatorRuleType.CEL_EXPRESSION,
            "cel_expression": "o.bool_out == true",
            "order": 0,
        },
        HTTP_HX_REQUEST="true",
    )

    assert response.status_code == HTTPStatus.NO_CONTENT
    default_ruleset = validator.default_ruleset
    assertion = default_ruleset.assertions.get(message_template="Bool check")
    assert assertion.rhs["expr"] == "o.bool_out == true"
    assert assertion.target_io_definition is not None
    assert assertion.target_io_definition.contract_key == "bool_out"


@pytest.mark.django_db
def test_default_assertion_move_reorders(client):
    """Moving default assertions up/down reorders them on default_ruleset."""
    ensure_all_roles_exist()
    org = OrganizationFactory()
    user = UserFactory()
    grant_role(user, org, RoleCode.AUTHOR)
    user.set_current_org(org)
    client.force_login(user)
    session = client.session
    session["active_org_id"] = org.pk
    session.save()

    validator = create_custom_validator(
        org=org,
        user=user,
        name="Movable",
        description="",
        custom_type="BASIC",
        input_data_format=SubmissionDataFormat.JSON,
    ).validator
    default_ruleset = validator.ensure_default_ruleset()
    RulesetAssertion.objects.create(
        ruleset=default_ruleset,
        assertion_type=AssertionType.CEL_EXPRESSION,
        target_data_path="payload.a == 1",
        rhs={"expr": "payload.a == 1"},
        severity=Severity.ERROR,
        order=10,
        message_template="First",
        cel_cache="payload.a == 1",
    )
    second = RulesetAssertion.objects.create(
        ruleset=default_ruleset,
        assertion_type=AssertionType.CEL_EXPRESSION,
        target_data_path="payload.b == 2",
        rhs={"expr": "payload.b == 2"},
        severity=Severity.ERROR,
        order=20,
        message_template="Second",
        cel_cache="payload.b == 2",
    )

    response = client.post(
        reverse(
            "validations:validator_rule_move",
            kwargs={"pk": validator.pk, "rule_pk": second.pk},
        ),
        data={"direction": "up"},
        HTTP_HX_REQUEST="true",
    )

    assert response.status_code == HTTPStatus.OK
    names = list(
        default_ruleset.assertions.order_by("order").values_list(
            "message_template",
            flat=True,
        ),
    )
    assert names[0] == "Second"
    assert names[1] == "First"


@pytest.mark.django_db
def test_author_not_creator_cannot_move(client):
    """Only the creator of a custom validator can move its default assertions."""
    ensure_all_roles_exist()
    org = OrganizationFactory()
    creator = UserFactory()
    grant_role(creator, org, RoleCode.AUTHOR)
    creator.set_current_org(org)
    non_creator = UserFactory()
    grant_role(non_creator, org, RoleCode.AUTHOR)
    non_creator.set_current_org(org)

    created = create_custom_validator(
        org=org,
        user=creator,
        name="Movable",
        description="",
        custom_type="BASIC",
        input_data_format=SubmissionDataFormat.JSON,
    ).validator
    default_ruleset = created.ensure_default_ruleset()
    assertion = RulesetAssertion.objects.create(
        ruleset=default_ruleset,
        assertion_type=AssertionType.CEL_EXPRESSION,
        target_data_path="payload.a == 1",
        rhs={"expr": "payload.a == 1"},
        severity=Severity.ERROR,
        order=10,
        message_template="Only creator",
        cel_cache="payload.a == 1",
    )

    client.force_login(non_creator)
    session = client.session
    session["active_org_id"] = org.pk
    session.save()

    response = client.post(
        reverse(
            "validations:validator_rule_move",
            kwargs={"pk": created.pk, "rule_pk": assertion.pk},
        ),
        data={"direction": "up"},
        HTTP_HX_REQUEST="true",
    )

    assert response.status_code == HTTPStatus.FORBIDDEN
