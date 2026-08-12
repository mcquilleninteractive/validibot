"""Minimal URLConf for Validibot OIDC provider tests.

These tests only need the OIDC issuer endpoints, not the entire URL stack
with all the API overrides. Keeping a focused test URLConf avoids
unrelated optional dependencies.
"""

from allauth.idp.oidc.views import configuration as oidc_server_metadata
from django.urls import include
from django.urls import path

urlpatterns = [
    path(
        ".well-known/oauth-authorization-server",
        oidc_server_metadata,
        name="oauth-authorization-server-metadata",
    ),
    path(
        "",
        include("allauth.idp.urls", namespace="idp"),
    ),
]
