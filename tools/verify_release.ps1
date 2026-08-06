<#
.SYNOPSIS
    Verifies a downloaded Prism release tarball against its Sigstore signature.

.DESCRIPTION
    Operator-facing release verification. Given a downloaded tarball, this
    script:
      1. Confirms the tarball SHA256 matches SHA256SUMS.
      2. Runs `cosign verify-blob` on the tarball signature against the
         expected GitHub Actions release identity.
      3. Runs `cosign verify-blob` on the SHA256SUMS signature.

    B7 audit framing: an attacker who compromises a maintainer's GitHub
    account can ship a malicious revision. Without this step, the install
    path has no out-of-band integrity check.

.PARAMETER Tarball
    Path to the downloaded prism-vX.Y.Z.tar.gz file. The script looks for the
    sibling .sig, .pem, SHA256SUMS, SHA256SUMS.sig, SHA256SUMS.pem files in
    the same directory.

.PARAMETER OwnerRepo
    GitHub <owner>/<repo> slug used to build the expected signing identity.
    Defaults to the <OWNER>/<REPO> placeholder — pass your fork's slug.

.EXAMPLE
    .\tools\verify_release.ps1 -Tarball .\prism-v1.2.3.tar.gz -OwnerRepo acmecorp/Prism

.NOTES
    Requires cosign (https://docs.sigstore.dev/cosign/installation/) on PATH.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [string]$Tarball,

    [Parameter(Position = 1)]
    [string]$OwnerRepo = '<OWNER>/<REPO>'
)

$ErrorActionPreference = 'Stop'

function Fail($msg) {
    Write-Host "FAIL: $msg" -ForegroundColor Red
    exit 1
}

if (-not (Test-Path -LiteralPath $Tarball -PathType Leaf)) {
    Fail "tarball not found: $Tarball"
}

$tarballItem = Get-Item -LiteralPath $Tarball
$tarballName = $tarballItem.Name
$dir = $tarballItem.DirectoryName

# Derive tag from filename: prism-vX.Y.Z.tar.gz -> vX.Y.Z
if ($tarballName -notmatch '^prism-(v\d+\.\d+\.\d+)\.tar\.gz$') {
    Fail "cannot derive vX.Y.Z tag from filename '$tarballName' (expected prism-vX.Y.Z.tar.gz)"
}
$tag = $Matches[1]

$required = @(
    $tarballName,
    "$tarballName.sig",
    "$tarballName.pem",
    'SHA256SUMS',
    'SHA256SUMS.sig',
    'SHA256SUMS.pem'
)
foreach ($f in $required) {
    if (-not (Test-Path -LiteralPath (Join-Path $dir $f))) {
        Fail "missing required file: $f"
    }
}

if (-not (Get-Command cosign -ErrorAction SilentlyContinue)) {
    Fail 'cosign not installed. See https://docs.sigstore.dev/cosign/installation/'
}

$identity = "https://github.com/$OwnerRepo/.github/workflows/release.yml@refs/tags/$tag"
$issuer = 'https://token.actions.githubusercontent.com'

Write-Host "Tarball:  $tarballName"
Write-Host "Tag:      $tag"
Write-Host "Identity: $identity"
Write-Host ''

# --- Step 1: SHA256 check -------------------------------------------------
Write-Host '[1/3] Checking SHA256SUMS against tarball bytes...'
$sumsPath = Join-Path $dir 'SHA256SUMS'
$expected = $null
foreach ($line in Get-Content -LiteralPath $sumsPath) {
    # Format: "<hex>  <name>" or "<hex>  *<name>" (binary mode marker).
    if ($line -match '^([0-9a-fA-F]{64})\s+\*?(.+)$') {
        if ($Matches[2].Trim() -eq $tarballName) {
            $expected = $Matches[1].ToLower()
            break
        }
    }
}
if (-not $expected) {
    Fail "no SHA256SUMS entry for $tarballName"
}
$actual = (Get-FileHash -Algorithm SHA256 -LiteralPath $tarballItem.FullName).Hash.ToLower()
if ($actual -ne $expected) {
    Fail "SHA256 mismatch. Expected $expected, got $actual"
}
Write-Host '  OK'
Write-Host ''

# --- Step 2: tarball signature -------------------------------------------
Write-Host '[2/3] Verifying tarball Sigstore signature...'
Push-Location $dir
try {
    & cosign verify-blob `
        --certificate "$tarballName.pem" `
        --signature "$tarballName.sig" `
        --certificate-identity $identity `
        --certificate-oidc-issuer $issuer `
        $tarballName
    if ($LASTEXITCODE -ne 0) { Fail "tarball signature does not verify against $identity" }
    Write-Host '  OK'
}
finally {
    Pop-Location
}
Write-Host ''

# --- Step 3: SHA256SUMS signature ----------------------------------------
Write-Host '[3/3] Verifying SHA256SUMS Sigstore signature...'
Push-Location $dir
try {
    & cosign verify-blob `
        --certificate 'SHA256SUMS.pem' `
        --signature 'SHA256SUMS.sig' `
        --certificate-identity $identity `
        --certificate-oidc-issuer $issuer `
        'SHA256SUMS'
    if ($LASTEXITCODE -ne 0) { Fail "SHA256SUMS signature does not verify against $identity" }
    Write-Host '  OK'
}
finally {
    Pop-Location
}
Write-Host ''

Write-Host "OK: $tarballName is authentic and untampered (tag $tag, identity $OwnerRepo)." -ForegroundColor Green
