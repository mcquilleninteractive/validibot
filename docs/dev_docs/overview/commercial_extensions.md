# Commercial Extensions

Validibot follows an open-core model. The core application is open source under AGPL-3.0 and includes the full validation system, all built-in validators, workflows, and single-user management. Two optional commercial packages add team and enterprise capabilities:

- **validibot-pro** -- team management, billing, advanced analytics, signed credentials, MCP server
- **validibot-enterprise** -- multi-org support, SSO/SAML, LDAP integration (includes all Pro features)

!!! note "MCP availability"
    The MCP (Model Context Protocol) server is built from community
    source for portability and transparency. Its availability in a
    deployed product depends on the installed Validibot edition and
    the applicable commercial license.

If you want the lower-level extension mechanics, read [Plugin Architecture](plugin_architecture.md) alongside this page. That document explains the shared registry and sync pattern used by both validators and actions.

## How commercial packages plug in

Commercial packages are standard Python packages distributed through a private package index. Your license materials provide the package version, index URL, and installation credentials.

To activate a commercial package, the customer does two things:

1. Install the package into the Python environment or Docker image that runs Validibot.
2. Add the package's Django app to `INSTALLED_APPS`.

This is an explicit opt-in. It keeps activation visible in settings and supports commercial packages that ship models, migrations, templates, static files, or other normal Django app behavior.

## Installing a commercial package

### Host-managed Python environment

Install the package into the same Python environment that runs Validibot (see your license email for the index URL and credentials):

```bash
uv pip install --python .venv/bin/python --index <private-index-url> validibot-pro==<version>
```

Then add the Django app in `config/settings/base.py`:

```python
INSTALLED_APPS += ["validibot_pro"]
```

Restart the application after updating settings.

### Docker-based self-hosting

For Docker-based installs, bake the package into the image using the optional `.build` file:

```bash
cp .envs.example/.production/.self-hosted/.build .envs/.production/.self-hosted/.build
```

Then set:

```bash
VALIDIBOT_COMMERCIAL_PACKAGE=validibot-pro==<version>
VALIDIBOT_COMMERCIAL_NETRC=/absolute/path/to/commercial.netrc
```

`VALIDIBOT_COMMERCIAL_PACKAGE` must be an exact package reference. Use either
an exact version like `validibot-pro==0.1.0`, or a quoted exact wheel URL on
`pypi.validibot.com` such as
`"https://pypi.validibot.com/packages/validibot_pro-0.1.0-py3-none-any.whl#sha256=<hash>"`.
Put the package login and key in the referenced mode-0600 netrc. BuildKit
mounts that file only for the install step, keeping credentials out of build
arguments and image metadata.
Floating names like `validibot-pro` are intentionally rejected during Docker
builds.

Installing the wheel into the image is only the first step. Add the Django app
in `config/settings/base.py` before you rebuild:

```python
INSTALLED_APPS += ["validibot_pro"]
```

For Enterprise, use `validibot-enterprise` instead and add both Django apps:

```python
INSTALLED_APPS += ["validibot_pro", "validibot_enterprise"]
```

After that, rebuild with `just self-hosted bootstrap` on first install or `just self-hosted deploy` for later rebuilds.

## What you'll see in the codebase

As you browse the core codebase, you'll encounter two patterns that reference commercial features:

**Feature flags in templates.** Some navigation links and UI elements are wrapped in `{% if feature_team_management %}` or similar checks. These elements are hidden when the corresponding commercial package is not installed.

**Feature guard mixins on views.** Some views include `FeatureRequiredMixin` with a `required_commercial_feature` attribute. These views return a 404 when the feature is not in the active license's feature set. This is defense-in-depth alongside the template-level hiding.

Both patterns read the capabilities advertised by the installed edition. Community code owns the stable extension contracts, while commercial packages provide their implementations through normal Django application hooks. For lower-level community extension APIs, see [Plugin Architecture](plugin_architecture.md).
