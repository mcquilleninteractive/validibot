# Third-Party License Notices

Validibot is licensed under AGPL-3.0-only. This file documents third-party
dependencies that require explicit license choice or acknowledgement due to
copyleft, dual-license, or non-standard licensing terms.

All dependencies listed here have been reviewed and confirmed compatible with
AGPL-3.0.

Dependencies with standard permissive licenses (MIT, BSD, Apache-2.0) that do
not offer a license choice are not listed individually. Their license texts are
included in the installed packages' `dist-info` directories in built artifacts
such as Docker images.

## Runtime Dependencies

### cel-python

- **Version:** 0.5.0
- **License:** Apache-2.0
- **Type:** Direct runtime dependency
- **Notes:** Upstream package metadata omits standard license fields. License
  confirmed as Apache-2.0 from source repository and LICENSE file in the
  installed distribution.

### certifi

- **Version:** 2026.1.4
- **License:** MPL-2.0
- **Type:** Transitive (via requests, httpx)
- **Notes:** MPL-2.0 is file-level copyleft. We use certifi unmodified as a
  CA certificate bundle. No modifications to certifi source files have been
  made. Only modifications to certifi's own files would trigger the MPL-2.0
  share-alike requirement.

### cryptography

- **Version:** 46.0.7
- **License:** Apache-2.0 OR BSD-3-Clause (dual-licensed)
- **Type:** Direct runtime dependency
- **Chosen license:** BSD-3-Clause
- **Notes:** cryptography is dual-licensed. We elect to use it under
  BSD-3-Clause.

### fmpy

- **Version:** 0.3.27
- **License:** BSD-2-Clause
- **Type:** Direct runtime dependency
- **Notes:** Upstream package metadata omits standard license fields. License
  confirmed as BSD-2-Clause from source repository and LICENSE.txt file in the
  installed distribution. fmpy also bundles FMI headers (BSD-3-Clause variant)
  and SUNDIALS solver code (BSD-3-Clause).

### fido2

- **Version:** 2.1.1
- **License:** BSD-2-Clause OR Apache-2.0 OR MPL-2.0 (tri-licensed)
- **Type:** Transitive (via django-allauth[mfa])
- **Chosen license:** BSD-2-Clause
- **Notes:** fido2 is offered under three licenses. We elect to use it under
  the BSD-2-Clause license, which is fully permissive.

### packaging

- **Version:** 25.0
- **License:** Apache-2.0 OR BSD-2-Clause (dual-licensed)
- **Type:** Transitive (via gunicorn, setuptools, others)
- **Chosen license:** BSD-2-Clause
- **Notes:** packaging is dual-licensed. We elect to use it under BSD-2-Clause.

### psycopg / psycopg-c

- **Version:** 3.3.2
- **License:** LGPL-3.0-only
- **Type:** Direct runtime dependency
- **Notes:** LGPL allows use as a library without copyleft obligations on the
  consuming application. We import psycopg as a standard Python package and do
  not modify or statically link it. Users can substitute their own version of
  psycopg per LGPL requirements.

### python-crontab

- **Version:** 3.3.0
- **License:** LGPLv3
- **Type:** Transitive (via django-celery-beat)
- **Notes:** LGPL allows use as a library without copyleft obligations on the
  consuming application. We import python-crontab as a standard Python package
  and do not modify or statically link it. Users can substitute their own
  version of python-crontab per LGPL requirements.

### python-dateutil

- **Version:** 2.9.0.post0
- **License:** BSD-3-Clause OR Apache-2.0 (dual-licensed)
- **Type:** Transitive (via celery, django-celery-beat)
- **Chosen license:** BSD-3-Clause
- **Notes:** python-dateutil offers a choice of BSD or Apache-2.0. We elect
  BSD-3-Clause.

### qrcode

- **Version:** 8.2
- **License:** BSD
- **Type:** Transitive (via django-allauth[mfa])
- **Notes:** The PyPI classifier includes "License :: Other/Proprietary
  License" alongside BSD, but the actual source repository license is BSD. The
  "Other/Proprietary" classifier refers to the QR code specification data
  tables that are vendored in the package, not to the package code itself.

### text-unidecode

- **Version:** 1.3
- **License:** Artistic License OR GPLv2+ (dual-licensed)
- **Type:** Transitive (via python-slugify)
- **Chosen license:** Artistic License
- **Notes:** text-unidecode is dual-licensed under the Artistic License and
  GPL. We elect to use it under the Artistic License to avoid introducing
  additional GPL election obligations for downstream redistributors.

### uritemplate

- **Version:** 4.2.0
- **License:** BSD-3-Clause OR Apache-2.0 (dual-licensed)
- **Type:** Transitive (via drf-spectacular, google-api-python-client)
- **Chosen license:** BSD-3-Clause
- **Notes:** uritemplate is dual-licensed. We elect to use it under
  BSD-3-Clause.

## Dev-Only Dependencies (not shipped)

These packages are used only during development, testing, or linting and are
not included in production deployments. They are listed here for completeness.

### djlint

- **Version:** 1.36.4
- **License:** GPL-3.0-or-later
- **Type:** Dev dependency (pre-commit hook / linter)
- **Notes:** Used as a standalone linting tool via pre-commit. Not imported
  into or distributed with the application. GPL obligations do not apply to
  the Validibot codebase because djlint is a separate tool, not a linked
  library.

### pathspec

- **Version:** 1.0.4
- **License:** MPL-2.0
- **Type:** Transitive (via djlint)
- **Notes:** MPL-2.0 file-level copyleft. Used unmodified. Not shipped in the
  deployed application (dev/build dependency only).

### tqdm

- **Version:** 4.67.1
- **License:** MPL-2.0 AND MIT
- **Type:** Transitive (via djlint)
- **Notes:** The MIT license applies to the tqdm code. The MPL-2.0 component
  applies to specific files. Used unmodified. Not shipped in the deployed
  application (dev/build dependency only).

## Frontend Runtime Dependencies (shipped static assets)

These packages are included in frontend bundles that are shipped with the app.
They use standard permissive licenses and do not add extra copyleft/choice
requirements beyond normal attribution.

### bootstrap

- **Version:** 5.3.7
- **License:** MIT
- **Type:** Frontend runtime dependency

### bootstrap-icons

- **Version:** 1.13.1
- **License:** MIT
- **Type:** Frontend runtime dependency

### chart.js

- **Version:** 4.4.6
- **License:** MIT
- **Type:** Frontend runtime dependency

### htmx.org

- **Version:** 2.0.6
- **License:** 0BSD
- **Type:** Frontend runtime dependency

### documentation fonts

- **Versions:** Inter 5.3.0, JetBrains Mono 5.3.0, Space Grotesk 5.3.0
- **License:** SIL Open Font License 1.1
- **Type:** Developer-documentation runtime, exact npm dev-dependencies
- **Notes:** Browser files are copied from the lockfile-pinned Fontsource
  packages. Their full licenses are kept beside the files in
  `docs/dev_docs/fonts/`; the docs do not fetch Google Fonts at runtime.
