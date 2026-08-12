"""Test settings for community + validibot-pro.

Extends ``config.settings.test`` with the single change needed to activate
Pro: adding ``validibot_pro`` to ``INSTALLED_APPS``.

Django only runs a commercial package's ``AppConfig.ready()`` — and imports
its ``__init__.py`` (which calls
``validibot.core.license.set_license(PRO_LICENSE)``) — when the app is in
``INSTALLED_APPS``. Installing the wheel alone is not enough; this module is
that final activation step for the test harness, mirroring what
``local_pro.py`` does for the ``just local-pro`` dev stack.

Used by the ``just test pro`` tier. It runs the community test suite with Pro
active — exercising the *enabled* branches of the feature-gated business logic
that lives in this repo — alongside validibot-pro's own tests. Requires
``validibot_pro`` to be importable (a licensed install); the ``just test pro``
recipe checks for that and degrades gracefully when it is absent.

Pair with ``DJANGO_SETTINGS_MODULE=config.settings.test_pro``. The equivalent
cloud test module is ``validibot_cloud.settings.test``, which layers the cloud
apps on top and already includes ``validibot_pro`` in its app list.
"""

from .test import *  # noqa: F403
from .test import DATABASES as _BASE_DATABASES
from .test import INSTALLED_APPS as _BASE_INSTALLED_APPS

DATABASES = {
    alias: {
        **configuration,
        "TEST": {
            **configuration.get("TEST", {}),
            "NAME": f"test_{configuration['NAME']}_pro",
        },
    }
    for alias, configuration in _BASE_DATABASES.items()
}

INSTALLED_APPS = [*_BASE_INSTALLED_APPS]
if "validibot_pro" not in INSTALLED_APPS:
    INSTALLED_APPS.append("validibot_pro")
