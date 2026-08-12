"""OIDC provider customizations for Validibot.

This app sits on top of django-allauth's generic OIDC authorization-server
implementation and adds the small Validibot-specific policy needed for JWT
access tokens accepted by the embedded MCP resource server:

- A custom adapter (``adapter.ValidibotOIDCAdapter``) that labels the
  ``validibot:mcp`` scope, restricts RFC 8707 resources, and adjusts allauth's
  discovery metadata to the canonical ``SITE_URL`` origin.
- A second URL for allauth's discovery view at the RFC 8414 metadata path
  required by MCP clients.
- An ``ensure_oidc_clients`` management command that idempotently creates
  the predefined public clients used by Claude Desktop and ChatGPT.

Placement note: this app lives in the community repo (not in
``validibot-pro`` or ``validibot-cloud``) because self-hosted Pro users
need MCP OAuth to work. There is no proprietary IP in the code — it's a
thin customization of a public spec (OIDC) wrapped around django-allauth.
The MCP audience is the deployment's exact same-origin ``/mcp`` resource and
the business logic remains here for every deployment target.
"""
