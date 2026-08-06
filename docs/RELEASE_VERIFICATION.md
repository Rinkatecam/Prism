# Release Verification

## Why we sign releases

Prism runs as a privileged service against tier-0 infrastructure on every
fleet that deploys it. An internal supply-chain review made the threat
explicit: an attacker who compromises a maintainer GitHub account can
push a malicious revision, and every customer who pulls and restarts ships
the malicious code with no offline integrity check. That is *outbound*
supply-chain — distinct from the inbound-dependency story covered by
`docs/DEPENDENCIES.md` (B3 / Sprint-2 hash-pinned `requirements.lock`).

To close that gap, every release tag (`vX.Y.Z`) is signed by the release
workflow itself. Operators verify the signature **before** unpacking and
installing — a tampered tarball fails verification and never runs.

## How we sign — Sigstore keyless via GitHub OIDC

The `.github/workflows/release.yml` workflow runs on every `vX.Y.Z` tag push
and produces, per release:

| Artifact                       | What it is                              |
|--------------------------------|-----------------------------------------|
| `prism-vX.Y.Z.tar.gz`          | Source tarball at the tagged commit     |
| `prism-vX.Y.Z.tar.gz.sig`      | Sigstore signature over the tarball     |
| `prism-vX.Y.Z.tar.gz.pem`      | Short-lived signing certificate         |
| `SHA256SUMS`                   | sha256 line for the tarball             |
| `SHA256SUMS.sig` / `.pem`      | Sigstore signature + cert for the sums  |

There is **no maintainer-managed signing key**. Sigstore keyless mode binds
each signature to the workflow's GitHub OIDC identity:

```
issuer:   https://token.actions.githubusercontent.com
identity: https://github.com/<OWNER>/<REPO>/.github/workflows/release.yml@refs/tags/vX.Y.Z
```

The signing event is recorded in the public Rekor transparency log. An
attacker cannot forge this without compromising both GitHub Actions OIDC
*and* the Sigstore Fulcio/Rekor infrastructure simultaneously. A maintainer
account compromise alone is not enough — the attacker would also need to
push a tag from a workflow at that exact path on that exact ref, which is
itself a public, auditable event.

> Replace `<OWNER>/<REPO>` with your fork's slug everywhere in this document.
> The placeholder exists because Prism is a single-tenant deploy and each
> operator runs their own fork.

## How operators verify — quickstart

You need [`cosign`](https://docs.sigstore.dev/cosign/installation/) v2.x
installed. Helper scripts are in `tools/`.

### POSIX (Linux / macOS / WSL)

```sh
# Download all six artifacts into one directory, then:
./tools/verify_release.sh prism-v1.2.3.tar.gz <OWNER>/<REPO>
```

Expected output ends with:

```
OK: prism-v1.2.3.tar.gz is authentic and untampered (tag v1.2.3, identity <OWNER>/<REPO>).
```

### PowerShell (Windows)

```powershell
.\tools\verify_release.ps1 -Tarball .\prism-v1.2.3.tar.gz -OwnerRepo <OWNER>/<REPO>
```

### Manual `cosign verify-blob` (no helper script)

If you prefer to invoke cosign directly:

```sh
TAG=v1.2.3
OWNER_REPO=<OWNER>/<REPO>
IDENTITY="https://github.com/${OWNER_REPO}/.github/workflows/release.yml@refs/tags/${TAG}"
ISSUER="https://token.actions.githubusercontent.com"

# 1. Tarball bytes match SHA256SUMS
sha256sum -c SHA256SUMS

# 2. Tarball signature
cosign verify-blob \
    --certificate "prism-${TAG}.tar.gz.pem" \
    --signature   "prism-${TAG}.tar.gz.sig" \
    --certificate-identity     "${IDENTITY}" \
    --certificate-oidc-issuer  "${ISSUER}" \
    "prism-${TAG}.tar.gz"

# 3. SHA256SUMS signature
cosign verify-blob \
    --certificate "SHA256SUMS.pem" \
    --signature   "SHA256SUMS.sig" \
    --certificate-identity     "${IDENTITY}" \
    --certificate-oidc-issuer  "${ISSUER}" \
    SHA256SUMS
```

All three checks must pass. If any of them fails, treat the tarball as
hostile.

## What to do if verification fails

1. **DO NOT install or unpack the tarball.** A failure is a real
   integrity-failure signal: either the artifact was tampered with, the
   signing identity is not what you expected (check for a wrong fork URL or
   a wrong tag), or someone is attempting to ship you a malicious build.
2. **Open a GitHub issue** on the upstream repo describing the failure.
   Include the exact `cosign` output, the artifact filenames, and the
   sha256 of the file you actually have on disk.
3. **Notify the maintainers via a separately-trusted channel.** If your
   organisation has an out-of-band channel to the upstream maintainers
   (private email, signal, in-person), use it — the attack model includes
   "GitHub Issues themselves are compromised", so do not rely on the
   GitHub UI as the only escalation path.
4. **Do not retry the install.** Pulling the same artifact again from the
   same source will not help. Delete the local copy and wait for upstream
   to confirm whether a re-publish is required.

## What this signing scheme does NOT cover

Be honest about the boundary:

- It does not protect against a malicious change merged into the upstream
  repo *before* the tag is pushed. Code review and protected branches
  cover that — release signing only certifies "the bytes you downloaded
  came from the workflow we publish."
- It does not cover dependencies of Prism — those are verified via
  hash-pinned `requirements.lock` (`pip install --require-hashes`). See
  [docs/DEPENDENCIES.md](DEPENDENCIES.md).
- It does not cover runtime artifacts — anything Prism downloads or
  generates after install (Windows updates, scheduled task scripts, etc.)
  is out of scope here.

## CI hygiene

`.github/workflows/verify-release.yml` runs on every push and pull request.
If the current ref points at a `vX.Y.Z` tag, it downloads the published
release artifacts and re-runs `cosign verify-blob` against them. This
catches future regressions in the release workflow itself — if a change
breaks the verification path, CI fails before any operator runs into it.
When no `vX.Y.Z` tag is present, the workflow is a no-op.

## Cross-references

- `docs/DEPENDENCIES.md` — inbound supply-chain story (Sprint-2):
  how `requirements.lock` is generated and why hash-pinning matters.
- `SECURITY.md` — security reporting + supported versions.
