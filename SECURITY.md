# Security Policy

## Reporting a vulnerability

Use GitHub's private vulnerability reporting for this repository, or email
**security@mcquilleninteractive.com**. Do not open a public GitHub issue.

Please include a description of the vulnerability and steps to reproduce it.
Validibot is maintained by a small team, so response times cannot be
guaranteed, but critical issues are prioritized. If you would like credit in
the release notes when a fix ships, say so in your report.

## Scope

Security reports are welcome for:

- the Validibot Django web application;
- the REST API;
- authentication and authorization; and
- cryptographic operations, credential signing, and JWKS.

Report vulnerabilities in third-party dependencies to their upstream projects.

## Verifying Validibot releases

Validibot is distributed as source rather than as a PyPI wheel. Each release
has three complementary integrity controls:

1. an SSH-signed git tag;
2. checksums for the canonical source archive and its CycloneDX SBOMs; and
3. a GitHub artifact attestation binding the archive to the JSON SBOM and the
   release workflow.

### Verify the release assets

Download the explicitly uploaded `validibot-X.Y.Z.tar.gz`,
`validibot.cdx.json`, `validibot.cdx.xml`, and `SHA256SUMS` assets into one
directory. Then run:

```bash
sha256sum --check SHA256SUMS
gh attestation verify \
  validibot-X.Y.Z.tar.gz \
  --repo mcquilleninteractive/validibot \
  --predicate-type https://cyclonedx.org/bom \
  --signer-workflow mcquilleninteractive/validibot/.github/workflows/release.yml \
  --source-ref refs/tags/vX.Y.Z
```

The predicate is explicit because this release contains a CycloneDX SBOM
attestation rather than the GitHub CLI's default SLSA provenance predicate.
GitHub also generates automatic “Source code” archives. Those are convenience
downloads, not the canonical attested archive. Full release and verification
instructions are in [`RELEASING.md`](RELEASING.md).

### Verify the signed tag

```bash
git clone https://github.com/mcquilleninteractive/validibot.git
cd validibot
git fetch --tags

git \
  -c gpg.format=ssh \
  -c gpg.ssh.allowedSignersFile="$PWD/.allowed_signers" \
  verify-tag vX.Y.Z

git checkout vX.Y.Z
```

Stop if verification fails. Confirm that the clone is the canonical
`mcquilleninteractive/validibot` repository and that the chosen tag is listed
on the [release page](https://github.com/mcquilleninteractive/validibot/releases).

The release workflow does not trust a signer file carried only by the tag. It
reads `.allowed_signers` from protected `main` and also requires the tag commit
to be on that branch. Protected main is the release-signing trust anchor.

### Related packages

- [`validibot-shared`](https://pypi.org/project/validibot-shared/) is published
  to PyPI through trusted publishing with provenance, SBOMs, and checksums.
- [`validibot-validator-backends`](https://github.com/mcquilleninteractive/validibot-validator-backends)
  publishes validator images to
  `ghcr.io/mcquilleninteractive/validibot-validator-backend-<validator>`.
  Its `RELEASING.md` documents digest and attestation verification.

## Production deployment

When deploying Validibot:

1. Always use HTTPS.
2. Generate a strong `DJANGO_SECRET_KEY`.
3. Generate a separate `DJANGO_API_KEY_DIGEST_KEY`; never reuse the Django
   secret key.
4. Restrict `DJANGO_ALLOWED_HOSTS` to the deployment's domains.
5. Store credentials in environment variables or a secrets manager.
6. Never commit `.envs/` files.
7. Keep locked dependencies current and review security alerts.

Generate the Django secret key with:

```bash
python -c 'from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())'
```

Generate the API-key digest key with:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

## Disclaimer

This software is provided “as is”, without warranty of any kind. See
[`LICENSE`](LICENSE). Security fixes are provided on a best-effort basis.
