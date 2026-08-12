"""OpenAPI schema customization hooks for drf-spectacular.

The preprocessing hook imports this module during schema generation, which
registers the custom bearer authentication extension below. It deliberately
leaves the endpoint collection unchanged.
"""

from __future__ import annotations

from drf_spectacular.extensions import OpenApiAuthenticationExtension
from drf_spectacular.plumbing import build_bearer_security_scheme_object


class BearerAuthenticationScheme(OpenApiAuthenticationExtension):
    """Describe Validibot API keys as standard HTTP Bearer credentials.

    ``BearerAuthentication`` is intentionally custom because it accepts both
    hashed ``vbk_...`` API keys and legacy DRF tokens.  drf-spectacular cannot
    infer an OpenAPI security scheme from a custom authenticator, so without
    this extension the generated schema silently omits bearer authentication
    even though the API accepts it at runtime.
    """

    target_class = "validibot.core.api.authentication.BearerAuthentication"
    name = "bearerAuth"

    def get_security_definition(self, auto_schema):
        """Return the OpenAPI security-scheme object for Authorization."""

        return build_bearer_security_scheme_object(
            header_name="Authorization",
            token_prefix=self.target.keyword.decode(),
        )


def register_schema_extensions(endpoints, **kwargs):
    """Return endpoints unchanged after importing this extension module.

    Args:
        endpoints: list of (path, path_regex, method, callback) tuples that
            spectacular collected from the URL conf.

    Returns:
        The original endpoint list.
    """
    return endpoints
