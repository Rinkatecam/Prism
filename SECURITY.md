# Security Policy

## Supported versions

Prism is single-tenant, on-premises software. Each operator runs their own
fork. Only the current `main` branch is supported with security fixes.

| Branch / Version | Supported          |
|------------------|--------------------|
| `main` (HEAD)    | Yes                |
| Older revisions  | No — pull `main`   |

If you are running a fork pinned to an old commit, please rebase onto the
current `main` before reporting an issue. Many findings have already been
addressed by the Sprint-2 / Sprint-3 hardening work tracked in the
[`docs/csv/`](docs/csv/) validation package.

## Reporting a vulnerability

**Do not file public GitHub Issues for security problems.**

Instead, email the maintainer privately:

```
security@<OPERATOR-DOMAIN>          # placeholder — operators replace with
                                    # their own monitored mailbox
```

> Operators forking this repo: replace the placeholder with a mailbox you
> actually monitor, and (optionally) enable GitHub's private vulnerability
> reporting on the fork's Security tab.

Please include:

- A description of the issue and its impact (what an attacker can do).
- Steps to reproduce, ideally a minimal repro against a fresh checkout.
- The commit hash or release tag you tested against.
- Whether you believe the issue is being actively exploited.

We aim to acknowledge reports within 5 business days. Coordinated
disclosure is appreciated; we will credit reporters in release notes
unless you prefer to remain anonymous.

## What is in scope

- Authentication, authorization, RBAC bypass.
- PowerShell sandbox escapes (`ps_sandbox.py`).
- Credential exposure (DPAPI / Fernet / config.json).
- Audit-log integrity.
- WinRM transport / connection-handler abuse.
- Workflow engine privilege escalation.
- Supply-chain issues (dependency or release-channel tampering).

## What is out of scope

- Findings against features you have explicitly disabled in `config.json`.
- Self-XSS, social-engineering of an admin, physical access.
- Issues that require pre-existing local administrator on the Prism host
  (the Prism host itself is assumed trusted; local admin on it is out of scope).
- Denial-of-service via legitimate authenticated requests at the rate
  limit (the rate limits are tunable in `routes/api/__init__.py`).

## Verifying releases

All release tags (`vX.Y.Z`) are signed via Sigstore keyless using the
GitHub Actions OIDC identity of the release workflow. Operators **must**
verify before installing — see
[`docs/RELEASE_VERIFICATION.md`](docs/RELEASE_VERIFICATION.md).

## Evidence of ongoing security work

This project takes security seriously and treats it as ongoing engineering
rather than a checkbox:

- [`docs/SECURITY_REVIEW.md`](docs/SECURITY_REVIEW.md) — **start here if you
  are reviewing Prism as a vendor.** Assembles the evidence behind every claim
  in one place, including where data is stored, what a host administrator can
  read, and the findings this audit fixed.
- [`docs/DATA_FLOWS.md`](docs/DATA_FLOWS.md) — every outbound connection Prism
  can make, with the destination of each. No vendor endpoint; no hardcoded
  destinations.
- [`docs/LAN_ONLY_VERIFICATION.md`](docs/LAN_ONLY_VERIFICATION.md) — the
  procedure for proving LAN-only operation on your own hardware, rather than
  taking our word for it.
- [`docs/csv/`](docs/csv/) — the computerised-system-validation (CSV)
  package: user requirements, risk assessment, the security-controls
  catalogue, and a findings/gaps register tracking remediation status.
- [`docs/DEPENDENCIES.md`](docs/DEPENDENCIES.md) — inbound supply-chain
  story (Sprint-2 hash-pinned `requirements.lock`).
- [`docs/RELEASE_VERIFICATION.md`](docs/RELEASE_VERIFICATION.md) —
  outbound supply-chain story (Sprint-3 signed release channel).

## Threat model

Prism is a fleet-wide WinRM console for an on-prem Windows server fleet,
single-tenant, with LDAP authentication plus a break-glass backup admin. It
manages tier-0 servers, so the most credible adversary is a malicious or
compromised insider; the design assumes that and layers defence-in-depth
(RBAC tiers, dual-control on tier-0 destructive ops, hash-pinned dependencies,
signed releases, an append-only audit log, and an auth-gated admin-only
PowerShell path). See the `docs/csv/` package for the control catalogue.
