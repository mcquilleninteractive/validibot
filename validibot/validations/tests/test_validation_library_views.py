import json
from http import HTTPStatus

import pytest
from django.urls import reverse

from validibot.submissions.constants import SubmissionDataFormat
from validibot.users.constants import RoleCode
from validibot.users.tests.factories import MembershipFactory
from validibot.users.tests.factories import OrganizationFactory
from validibot.users.tests.factories import UserFactory
from validibot.validations.constants import VALIDATION_LIBRARY_LAYOUT_SESSION_KEY
from validibot.validations.constants import ValidationType
from validibot.validations.models import Validator
from validibot.validations.utils import create_custom_validator
from validibot.workflows.tests.factories import WorkflowFactory
from validibot.workflows.tests.factories import WorkflowStepFactory


@pytest.mark.django_db
class TestValidationLibraryViews:
    def _setup_user(self, client, role: str = RoleCode.ADMIN):
        org = OrganizationFactory()
        user = UserFactory()
        membership = MembershipFactory(user=user, org=org)
        membership.add_role(role)
        user.set_current_org(org)
        client.force_login(user)
        session = client.session
        session["active_org_id"] = org.id
        session.save()
        return user, org

    def test_library_page_lists_validators(self, client):
        user, org = self._setup_user(client, RoleCode.OWNER)
        Validator.objects.create(
            name="EnergyPlus Validator",
            slug="energyplus-validation",
            validation_type="ENERGYPLUS",
            description="System validator",
            is_system=True,
        )
        create_custom_validator(
            org=org,
            user=user,
            name="Modelica Validator",
            description="Custom validator description",
            custom_type="MODELICA",
            input_data_format=SubmissionDataFormat.JSON,
        )

        response = client.get(reverse("validations:validation_library"))
        assert response.status_code == HTTPStatus.OK
        content = response.content.decode()
        assert "EnergyPlus Validator" in content
        assert "Modelica Validator" in content
        options = response.context["validator_create_options"]
        option_values = {opt["value"] for opt in options}
        assert {"custom-basic", "fmu"} <= option_values
        assert response.context["validator_create_selected"] == options[0]["value"]

    def test_library_page_lists_only_latest_validator_version(self, client):
        """The library index should show validator families, not every row.

        Multiple rows with the same slug are legitimate version history. The
        normal library page stays simple by showing only the latest visible
        version; older rows remain reachable through the hidden versions URL.
        """
        self._setup_user(client, RoleCode.OWNER)
        older = Validator.objects.create(
            name="EnergyPlus Old",
            slug="energyplus-idf-validator",
            validation_type=ValidationType.ENERGYPLUS,
            description="Old system validator",
            is_system=True,
            version=1,
        )
        latest = Validator.objects.create(
            name="EnergyPlus Current",
            slug="energyplus-idf-validator",
            validation_type=ValidationType.ENERGYPLUS,
            description="Current system validator",
            is_system=True,
            version=3,
        )

        response = client.get(reverse("validations:validation_library"))

        assert response.status_code == HTTPStatus.OK
        ids = {validator.pk for validator in response.context["system_validators"]}
        assert latest.pk in ids
        assert older.pk not in ids
        content = response.content.decode()
        assert "EnergyPlus Current" in content
        assert "EnergyPlus Old" not in content
        assert "v3" in content

    def test_default_validator_detail_route_resolves_latest_version(self, client):
        """The slug-only detail URL should resolve to the newest version row."""
        self._setup_user(client, RoleCode.ADMIN)
        Validator.objects.create(
            name="Basic Old",
            slug="basic-validator",
            validation_type=ValidationType.BASIC,
            description="Old",
            is_system=True,
            version=1,
        )
        latest = Validator.objects.create(
            name="Basic Current",
            slug="basic-validator",
            validation_type=ValidationType.BASIC,
            description="Current",
            is_system=True,
            version=2,
        )

        response = client.get(
            reverse("validations:validator_detail", kwargs={"slug": "basic-validator"}),
        )

        assert response.status_code == HTTPStatus.OK
        assert response.context["validator"].pk == latest.pk
        assert response.context["is_latest_validator_version"] is True

    def test_version_detail_route_resolves_exact_read_only_version(self, client):
        """The hidden version URL should pin an older row and lock editing."""
        _user, org = self._setup_user(client, RoleCode.AUTHOR)
        older = Validator.objects.create(
            name="Org Validator v1",
            slug="org-validator",
            validation_type=ValidationType.CUSTOM_VALIDATOR,
            description="Old custom validator",
            org=org,
            is_system=False,
            version=1,
        )
        Validator.objects.create(
            name="Org Validator v2",
            slug="org-validator",
            validation_type=ValidationType.CUSTOM_VALIDATOR,
            description="Latest custom validator",
            org=org,
            is_system=False,
            version=2,
        )

        response = client.get(
            reverse(
                "validations:validator_version_detail",
                kwargs={"slug": "org-validator", "version": 1},
            ),
        )

        assert response.status_code == HTTPStatus.OK
        assert response.context["validator"].pk == older.pk
        assert response.context["can_edit_validator"] is False
        assert response.context["is_locked_validator_version"] is True
        assert "older validator version" in response.content.decode()

    def test_hidden_versions_route_lists_all_versions(self, client):
        """The manual versions URL should expose history without navbar links."""
        self._setup_user(client, RoleCode.ADMIN)
        v1 = Validator.objects.create(
            name="SHACL v1",
            slug="shacl-validator",
            validation_type=ValidationType.SHACL,
            description="Old",
            is_system=True,
            version=1,
        )
        v3 = Validator.objects.create(
            name="SHACL v3",
            slug="shacl-validator",
            validation_type=ValidationType.SHACL,
            description="Current",
            is_system=True,
            version=3,
        )

        response = client.get(
            reverse(
                "validations:validator_versions",
                kwargs={"slug": "shacl-validator"},
            ),
        )

        assert response.status_code == HTTPStatus.OK
        versions = response.context["versions"]
        assert [validator.pk for validator in versions] == [v3.pk, v1.pk]
        content = response.content.decode()
        assert "v1" in content
        assert "v3" in content

    def test_library_page_honors_tab_query_param(self, client):
        self._setup_user(client, RoleCode.OWNER)
        response = client.get(
            f"{reverse('validations:validation_library')}?tab=system",
        )
        assert response.status_code == HTTPStatus.OK
        assert response.context["active_tab"] == "system"

    def test_library_layout_persists_in_session(self, client):
        self._setup_user(client, RoleCode.OWNER)
        url = reverse("validations:validation_library")

        response = client.get(f"{url}?layout=list")
        assert response.status_code == HTTPStatus.OK
        assert response.context["current_layout"] == "list"
        assert client.session[VALIDATION_LIBRARY_LAYOUT_SESSION_KEY] == "list"

        response = client.get(url)
        assert response.context["current_layout"] == "list"

    def test_library_page_requires_author_admin_owner(self, client):
        self._setup_user(client, RoleCode.WORKFLOW_VIEWER)
        response = client.get(reverse("validations:validation_library"))
        assert response.status_code == HTTPStatus.FOUND
        assert "workflows" in response.headers["Location"]

    def test_create_custom_validator(self, client):
        user, org = self._setup_user(client, RoleCode.AUTHOR)
        response = client.post(
            reverse("validations:custom_validator_create"),
            data={
                "name": "KerML Validator",
                "description": "Checks KerML outputs",
                "custom_type": "KERML",
                "notes": "First iteration",
            },
            follow=True,
        )
        assert response.status_code == HTTPStatus.OK
        assert Validator.objects.filter(
            name="KerML Validator",
            org=org,
            is_system=False,
        ).exists()

    def test_create_requires_permission(self, client):
        self._setup_user(client, RoleCode.WORKFLOW_VIEWER)
        response = client.post(
            reverse("validations:custom_validator_create"),
            data={
                "name": "Unauthorized",
                "description": "",
                "custom_type": "MODELICA",
            },
        )
        assert response.status_code == HTTPStatus.FOUND
        assert not Validator.objects.filter(name="Unauthorized").exists()

    def test_create_breadcrumb_includes_create_label(self, client):
        self._setup_user(client, RoleCode.AUTHOR)
        response = client.get(reverse("validations:custom_validator_create"))
        assert response.status_code == HTTPStatus.OK
        breadcrumbs = response.context["breadcrumbs"]
        assert breadcrumbs[-1]["name"] == "Create new validator"

    def test_edit_breadcrumb_uses_validator_name(self, client):
        user, org = self._setup_user(client, RoleCode.AUTHOR)
        custom_validator = create_custom_validator(
            org=org,
            user=user,
            name="Room Automation",
            description="Validates room schedules.",
            custom_type="MODELICA",
            input_data_format=SubmissionDataFormat.JSON,
        )
        response = client.get(
            reverse(
                "validations:custom_validator_update",
                kwargs={"slug": custom_validator.validator.slug},
            ),
        )
        assert response.status_code == HTTPStatus.OK
        breadcrumbs = response.context["breadcrumbs"]
        assert breadcrumbs[-1]["name"] == "Edit Settings"
        assert "Room Automation" in breadcrumbs[-2]["name"]

    def test_system_validator_detail_preserves_tab_query(self, client):
        self._setup_user(client, RoleCode.ADMIN)
        validator = Validator.objects.create(
            name="System Check",
            slug="system-check",
            validation_type="ENERGYPLUS",
            description="System validator",
            is_system=True,
            has_processor=True,
        )

        response = client.get(
            reverse(
                "validations:validator_detail",
                kwargs={"slug": validator.slug},
            )
            + "?tab=system",
        )

        assert response.status_code == HTTPStatus.OK
        assert response.context["return_tab"] == "system"

    def test_validator_detail_requires_author_admin_owner(self, client):
        self._setup_user(client, RoleCode.EXECUTOR)
        validator = Validator.objects.create(
            name="System Check",
            slug="system-check",
            validation_type="ENERGYPLUS",
            description="System validator",
            is_system=True,
        )
        response = client.get(
            reverse(
                "validations:validator_detail",
                kwargs={"slug": validator.slug},
            ),
        )
        assert response.status_code == HTTPStatus.FOUND
        assert "workflows" in response.headers["Location"]

    def test_htmx_delete_custom_validator_succeeds(self, client):
        user, org = self._setup_user(client, RoleCode.AUTHOR)
        custom_validator = create_custom_validator(
            org=org,
            user=user,
            name="Transient Validator",
            description="Delete me",
            custom_type="MODELICA",
            input_data_format=SubmissionDataFormat.JSON,
        )
        client.get(reverse("validations:validation_library"))
        csrf_token = client.cookies["csrftoken"].value

        response = client.delete(
            reverse(
                "validations:custom_validator_delete",
                kwargs={"slug": custom_validator.validator.slug},
            ),
            HTTP_HX_REQUEST="true",
            HTTP_HX_TARGET=f"validator-card-{custom_validator.validator.pk}",
            HTTP_X_CSRFTOKEN=csrf_token,
        )

        assert response.status_code == HTTPStatus.OK
        assert not Validator.objects.filter(pk=custom_validator.validator.pk).exists()
        trigger = json.loads(response.headers["HX-Trigger"])
        assert trigger["toast"]["level"] == "success"

    def test_htmx_delete_custom_validator_blocked_when_in_use(self, client):
        user, org = self._setup_user(client, RoleCode.AUTHOR)
        custom_validator = create_custom_validator(
            org=org,
            user=user,
            name="In Use Validator",
            description="Still referenced",
            custom_type="MODELICA",
            input_data_format=SubmissionDataFormat.JSON,
        )
        workflow = WorkflowFactory(org=org, user=user)
        WorkflowStepFactory(workflow=workflow, validator=custom_validator.validator)

        client.get(reverse("validations:validation_library"))
        csrf_token = client.cookies["csrftoken"].value

        response = client.delete(
            reverse(
                "validations:custom_validator_delete",
                kwargs={"slug": custom_validator.validator.slug},
            ),
            HTTP_HX_REQUEST="true",
            HTTP_HX_TARGET=f"validator-card-{custom_validator.validator.pk}",
            HTTP_X_CSRFTOKEN=csrf_token,
        )

        assert response.status_code == HTTPStatus.BAD_REQUEST
        assert Validator.objects.filter(pk=custom_validator.validator.pk).exists()
        trigger = json.loads(response.headers["HX-Trigger"])
        assert trigger["toast"]["level"] == "danger"

    def test_http_delete_custom_validator_blocked_when_in_use(self, client):
        user, org = self._setup_user(client, RoleCode.AUTHOR)
        custom_validator = create_custom_validator(
            org=org,
            user=user,
            name="In Use Validator",
            description="Still referenced",
            custom_type="MODELICA",
            input_data_format=SubmissionDataFormat.JSON,
        )
        workflow = WorkflowFactory(org=org, user=user)
        WorkflowStepFactory(workflow=workflow, validator=custom_validator.validator)

        response = client.post(
            reverse(
                "validations:custom_validator_delete",
                kwargs={"slug": custom_validator.validator.slug},
            ),
            follow=True,
        )

        assert response.status_code == HTTPStatus.OK
        # The validator still exists because of workflow reference.
        assert Validator.objects.filter(pk=custom_validator.validator.pk).exists()
        content = response.content.decode()
        assert "Cannot delete" in content
        # Error should appear in the delete template (non-HTMX path).
        assert "alert-danger" in content
        # Delete button should be disabled when deletion is blocked.
        assert 'type="submit"' in content
        assert "disabled" in content
        # Blockers are listed
        assert "Workflow step" in content
        assert "View" in content

    def test_http_delete_custom_validator_blockers_listed_on_get(self, client):
        user, org = self._setup_user(client, RoleCode.AUTHOR)
        custom_validator = create_custom_validator(
            org=org,
            user=user,
            name="In Use Validator",
            description="Still referenced",
            custom_type="MODELICA",
            input_data_format=SubmissionDataFormat.JSON,
        )
        workflow = WorkflowFactory(org=org, user=user)
        WorkflowStepFactory(workflow=workflow, validator=custom_validator.validator)

        response = client.get(
            reverse(
                "validations:custom_validator_delete",
                kwargs={"slug": custom_validator.validator.slug},
            ),
        )

        assert response.status_code == HTTPStatus.OK
        content = response.content.decode()
        assert "cannot be deleted" in content
        assert "Workflow step" in content
        # Delete button disabled in initial GET when blocked
        assert "disabled" in content
        # View link present
        assert "View" in content
