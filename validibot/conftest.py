from unittest.mock import patch

import pytest

from validibot.users.models import User
from validibot.users.tests.factories import UserFactory


@pytest.fixture(autouse=True)
def _media_storage(settings, tmpdir) -> None:
    settings.MEDIA_ROOT = tmpdir.strpath


@pytest.fixture
def user(db) -> User:
    return UserFactory()


@pytest.fixture
def pro_installed():
    """Pretend ``validibot_pro`` is in INSTALLED_APPS for the test.

    Production code gates Pro-only paths (signed credentials etc.) on
    ``apps.is_installed("validibot_pro")`` so it doesn't query
    unregistered models. Community tests that exercise those paths
    (typically via ``patch.dict("sys.modules", _fake_pro_modules(...))``
    to inject a fake credential model) need this fixture so the
    is_installed guard returns True.

    Patches every call site that imports ``django.apps.apps``. We
    patch each site individually rather than the global registry so
    the fixture is explicit about what it touches and can't leak
    across tests through monkey-patched globals. Adding a new gate
    elsewhere means listing it here too — failing tests will surface
    that quickly.
    """
    import contextlib

    targets = (
        "validibot.validations.serializers.apps.is_installed",
        "validibot.validations.credential_utils.apps.is_installed",
        "validibot.validations.api_views.apps.is_installed",
        "validibot.validations.views.runs.apps.is_installed",
        "validibot.workflows.views.management.apps.is_installed",
    )
    with contextlib.ExitStack() as stack:
        for target in targets:
            stack.enter_context(patch(target, return_value=True))
        yield
