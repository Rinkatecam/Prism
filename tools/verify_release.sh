#!/bin/sh
# verify_release.sh — operator-facing verification for a downloaded Prism release.
#
# Usage:
#   tools/verify_release.sh <tarball> [<owner>/<repo>]
#
# Expects these files alongside <tarball>:
#   <tarball>             e.g. prism-v1.2.3.tar.gz
#   <tarball>.sig         Sigstore signature
#   <tarball>.pem         Sigstore certificate
#   SHA256SUMS            sha256 line for the tarball
#   SHA256SUMS.sig        Sigstore signature for SHA256SUMS
#   SHA256SUMS.pem        Sigstore certificate for SHA256SUMS
#
# Verifies, in order:
#   1. SHA256SUMS line matches the tarball bytes (sha256sum -c).
#   2. Tarball signature is valid for the GitHub Actions release identity.
#   3. SHA256SUMS signature is valid for the same identity.
#
# Requires: cosign (>= 2.x), sha256sum, grep, sed.
#
# B7 audit framing: the tarball itself is supply-chain-outbound. Without this
# step, an operator pulling from a compromised mirror or a hijacked maintainer
# account installs whatever the attacker shipped.

set -eu

OWNER_REPO="${2:-<OWNER>/<REPO>}"   # e.g. acmecorp/Prism — fill in for your fork

if [ "$#" -lt 1 ]; then
    echo "Usage: $0 <tarball> [<owner>/<repo>]" >&2
    echo "  Default OWNER/REPO is the placeholder <OWNER>/<REPO> — pass your fork's slug." >&2
    exit 2
fi

TARBALL="$1"

if [ ! -f "$TARBALL" ]; then
    echo "FAIL: tarball not found: $TARBALL" >&2
    exit 1
fi

# Derive tag from filename: prism-vX.Y.Z.tar.gz -> vX.Y.Z
BASENAME="$(basename "$TARBALL")"
TAG="$(printf '%s\n' "$BASENAME" | sed -n 's/^prism-\(v[0-9][0-9.]*\)\.tar\.gz$/\1/p')"
if [ -z "$TAG" ]; then
    echo "FAIL: cannot derive vX.Y.Z tag from filename '$BASENAME'" >&2
    echo "       Expected pattern: prism-vX.Y.Z.tar.gz" >&2
    exit 1
fi

DIR="$(dirname "$TARBALL")"
cd "$DIR"
TARBALL_NAME="$(basename "$TARBALL")"

for f in "$TARBALL_NAME" "$TARBALL_NAME.sig" "$TARBALL_NAME.pem" SHA256SUMS SHA256SUMS.sig SHA256SUMS.pem; do
    if [ ! -f "$f" ]; then
        echo "FAIL: missing required file: $f" >&2
        exit 1
    fi
done

if ! command -v cosign >/dev/null 2>&1; then
    echo "FAIL: cosign not installed. See https://docs.sigstore.dev/cosign/installation/" >&2
    exit 1
fi

IDENTITY="https://github.com/${OWNER_REPO}/.github/workflows/release.yml@refs/tags/${TAG}"
ISSUER="https://token.actions.githubusercontent.com"

echo "Tarball:  $TARBALL_NAME"
echo "Tag:      $TAG"
echo "Identity: $IDENTITY"
echo

echo "[1/3] Checking SHA256SUMS against tarball bytes..."
# Filter to just the line for our tarball — SHA256SUMS may list more files.
if grep -E "[[:space:]]\\*?${TARBALL_NAME}\$" SHA256SUMS | sha256sum -c -; then
    echo "  OK"
else
    echo "FAIL: tarball checksum does not match SHA256SUMS" >&2
    exit 1
fi
echo

echo "[2/3] Verifying tarball Sigstore signature..."
if cosign verify-blob \
    --certificate "${TARBALL_NAME}.pem" \
    --signature "${TARBALL_NAME}.sig" \
    --certificate-identity "${IDENTITY}" \
    --certificate-oidc-issuer "${ISSUER}" \
    "${TARBALL_NAME}"; then
    echo "  OK"
else
    echo "FAIL: tarball signature does not verify against $IDENTITY" >&2
    exit 1
fi
echo

echo "[3/3] Verifying SHA256SUMS Sigstore signature..."
if cosign verify-blob \
    --certificate "SHA256SUMS.pem" \
    --signature "SHA256SUMS.sig" \
    --certificate-identity "${IDENTITY}" \
    --certificate-oidc-issuer "${ISSUER}" \
    SHA256SUMS; then
    echo "  OK"
else
    echo "FAIL: SHA256SUMS signature does not verify against $IDENTITY" >&2
    exit 1
fi
echo

echo "OK: $TARBALL_NAME is authentic and untampered (tag $TAG, identity $OWNER_REPO)."
