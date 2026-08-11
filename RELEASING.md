# Releasing Validibot

Validibot is distributed as source rather than as a PyPI package. Each release
publishes one canonical source archive together with CycloneDX SBOMs, flat
checksums, and a GitHub artifact attestation.

## Maintainer procedure

1. Merge all release changes to protected `main`.
2. Check out `main`, pull it, and confirm the working tree is clean.
3. Run:

   ```bash
   just release X.Y.Z
   ```

The release recipe confirms that local `main` equals `origin/main`, checks that
the pinned `validibot-shared` version is current on PyPI, and runs `just
release-check`. That gate covers both Python locks, Python and MCP checks,
frontend type checking/tests/generated bundles, explicit locked dependency
audits, and the successful `ci.yml` push run for the exact release commit. The
recipe confirms those checks left the worktree clean, creates an SSH-signed
`vX.Y.Z` tag, verifies the tag locally against the signer allowlist on
`origin/main`, and pushes only that tag.

Run `just release-check` alone to inspect every prerequisite without creating a
tag. `just check` is the local integration subset; `just audit` remains a
separate networked operation because advisory data changes independently of the
checkout.

The release workflow then independently:

1. confirms the tag exactly matches `vX.Y.Z`;
2. confirms the tag, event, and checkout identify the same commit;
3. requires that commit to be on protected `origin/main`;
4. verifies the tag against `.allowed_signers` read from protected main;
5. builds the canonical source archive once;
6. generates JSON and XML CycloneDX SBOMs plus `SHA256SUMS`;
7. attests that source archive with the JSON SBOM; and
8. creates the GitHub release from those exact files.

The workflow has no manual-dispatch path and does not update an existing
release. A version is therefore published once from one immutable artifact
bundle.

## Verify a downloaded release

Download these assets from the release page into the same directory:

- `validibot-X.Y.Z.tar.gz`
- `validibot.cdx.json`
- `validibot.cdx.xml`
- `SHA256SUMS`

Then verify their checksums:

```bash
sha256sum --check SHA256SUMS
```

Verify the GitHub artifact attestation:

```bash
gh attestation verify \
  validibot-X.Y.Z.tar.gz \
  --repo mcquilleninteractive/validibot
```

The automatically generated GitHub “Source code” archives are convenience
downloads and are not the canonical attested artifact. Use the explicitly
uploaded `validibot-X.Y.Z.tar.gz` asset for verification.

To verify the signed git tag as well, follow the procedure in
[`SECURITY.md`](SECURITY.md).

## Trust boundary

The release tag does not authorize its own signer. CI reads `.allowed_signers`
from protected `origin/main`, and the tag commit must be an ancestor of that
branch. Protected main is therefore the trust anchor for release signing.

Keep prior public signer entries available when they are needed to verify older
releases. Signing keys and the signer allowlist are managed manually by the
repository owner; the release tooling never creates or changes them.
