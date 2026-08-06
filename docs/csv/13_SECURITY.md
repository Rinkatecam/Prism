# 13 — Security & Access Control Review

| Field | Value |
|---|---|
| Document ID | CSV-13 |
| Version | 1.0 |
| Date | 2026-05-22 |
| Status | **Final — pending Quality sign-off** |
| Parents | `03_FS.md`, `04_DS.md`, `12_AUDIT_TRAIL.md`, existing `docs/SECURITY.md` (system-level overview) |

## Purpose

Validate that Prism's security controls are sufficient to protect the regulated data flows identified in `11_DATA_INTEGRITY.md` and the operator actions in `12_AUDIT_TRAIL.md`. Cross-reference the prior audit cycles (S1-x, S2-x, S3-x findings from `AUDIT-2026-05.md`) so the validation auditor doesn't re-walk solved ground.

## A. Authentication

### Mechanisms

- **LDAP/AD** via `ldap3` library. Bind credentials encrypted at rest. (FS-070, DS-108)
- **Local backup-admin** account with werkzeug-hashed password. (FS-070)

### Controls

| Control | Implementation | Status |
|---|---|---|
| Strong password policy on backup-admin | ≥ 12 chars, ≥ 1 digit, ≥ 1 symbol, not in common list (FS-071) | **OK** |
| Account lockout | `auth_failures` table; threshold 10 / 30 min window / 15 min lockout (FS-072) | **OK** |
| Session timeout | 8 h default; 30 d remember-me; 15 min idle floor for backup-admin (FS-073) | **OK** |
| Forced session termination | `revoked_sessions` checked on every request (FS-074) | **OK** |
| LDAP startup safety | `assert_ldap_startup_safe` raises `SystemExit` if LDAP unreachable (FS-115) | **OK** |
| Password storage | LDAP bind password Fernet-encrypted; backup-admin werkzeug-hashed | **OK** |
| 2FA / MFA | **Not implemented** | Observation — would be required for §11.50 Part 11 e-signatures |

## B. Authorisation (RBAC)

| Aspect | Implementation | Status |
|---|---|---|
| Per-server ACL with view < control < admin | `user_server_acl` table (FS-075, DS-109) | **OK** |
| Wildcard `*` for fleet-wide grants | Supported | **OK** |
| Permissive-when-empty | First row toggles into enforced mode | **OK** (documented; deliberate for migration) |
| Tier-0 dual-control | `pending_approvals` table; single-use tokens; 1 h expiry (FS-076) | **OK** |
| Global destructive approval | `_consume_global_destructive_approval` (FS-077) | **OK** |
| Uniform enforcement | `test_rbac_uniform.py` covers known destructive endpoints | **GAP — F-075**: no static-analysis enforcement that every NEW mutating route is auth-gated |

## C. Input handling

| Control | Implementation | Status |
|---|---|---|
| CSRF | Flask-WTF CSRFProtect; tokens on every mutating endpoint (FS-110) | **OK** (modulo no dedicated tests — rely on library) |
| XSS via CSP | `set_security_headers` after_request; per-request nonce; templates use `nonce="{{ csp_nonce }}"` on inline scripts (FS-111) | **OK on most pages**; 2 pre-existing failures on `/login` (F-111, Minor) |
| SQL injection | All queries parameterised (sqlite3 `?` placeholders); no f-string SQL | **OK** by inspection |
| PowerShell injection (sandbox) | `ps_sandbox.py` allowlist + HARD_DENY (FS-053) | **OK** with documented limitations |
| PowerShell injection (parameter binding) | Structured-field paths use `add_parameter` instead of script concatenation (FS-054) | **OK** — pinned by `test_workflow_param_binding.py` |
| SSRF via webhook URL | `webhooks.py` validates URL — `test_webhooks.py` covers extra-allowed host, credentials-in-URL rejection, control-char strip, length cap | **OK** |
| Path traversal in file endpoints (`/api/config/backups/download`) | Backup directory whitelisted in route | **OK** |
| Unbounded request bodies | Flask default config + reverse proxy expected to limit | **Observation — F-S-1 (Minor)**: explicit `MAX_CONTENT_LENGTH` would be cleaner |

## D. Transport security

| Control | Status |
|---|---|
| HTTPS to WinRM targets | Optional per-server (`use_https`, auto-port-flip to 5986) | OK |
| TLS cert verify on targets | Default true; **tier-0 cannot disable** (FS-114) | OK |
| HTTPS-downgrade protection | Refuse downgrading already-HTTPS server to plaintext unless RBAC-admin (FS-113) | OK |
| Browser → Prism HTTPS | Out of Prism scope — operator deploys behind reverse proxy with TLS termination | Documented in deployment SOP |
| HSTS header | Set by `set_security_headers` | OK |

## E. Secrets management

| Secret | Storage | Rotation |
|---|---|---|
| `PRISM_SECRET_KEY` (Flask session signing) | env var or auto-generated and persisted | Manual via env update; restart required |
| Server WinRM passwords | Fernet-encrypted in `config.json`; key in `data/.key` or `PRISM_PASSWORD_KEY` | `tools/rekey.py` (FS-095) |
| LDAP bind password | Same | Same |
| Email SMTP password | Same | Same |
| Backup-admin password hash | werkzeug bcrypt in `config.json` | UI: change-password flow |
| Database / mirror file | Not encrypted by Prism (filesystem-encryption is the operator's responsibility) | Out of scope; SOP-level |

## F. Defence-in-depth controls

| Control | Implementation |
|---|---|
| Sensitive `auth.*` fields stripped from POST `/api/config` for non-RBAC-admin | `routes/api/config.py` strip filter |
| Password masking in GET `/api/config` response | `crypto_utils.is_password_masked` sentinel |
| Masked-password round-trip preserves stored value | Same; FS-112 |
| Rate limiting on sensitive endpoints (login, Flask restart) | `flask_limiter` |
| `audit_mirror.jsonl` as out-of-band tampering detector for `prism.db` | (FS-081) |
| Hash-pinned dependency closure | `requirements.lock` + `--require-hashes` (FS-116) |
| Sigstore-signed release tarballs | `tools/verify_release.{sh,ps1}` |

## G. Supply chain

| Aspect | Implementation |
|---|---|
| Pinned versions | `requirements.lock` with cryptographic hashes |
| Hash-verified install | `pip install --require-hashes` |
| Release artefacts signed | Sigstore (cosign-keyless), verified by `tools/verify_release.ps1` |
| Dependency-update SOP | `docs/DEPENDENCIES.md` |
| Validation: post-update | Re-run full pytest; if green and IQ-002 passes, accept |

## H. Forensic readiness

| Capability | Implementation |
|---|---|
| Per-action source IP | `audit_log.source_ip` |
| Per-session correlation | `session_id` (SHA-256(user+login_time)) — links pre-session-kill actions |
| Per-request correlation | `request_id` UUID in `flask.g`, propagated to app logs + audit |
| Audit-log integrity | Hash chain + JSONL mirror (FS-080, FS-081) |
| Audit-log export | CSV + JSONL |
| Failed-login forensic detail | `failed_logins` table — IP, account, logon_type, workstation, process |
| Restart-action attribution | **GAP — F-A-1 (Minor)**: `restart_log` has no `actor` column |

## I. Known security risks (residual)

| ID | Description | Severity | Mitigation |
|---|---|---|---|
| F-075 | Uniform-RBAC enforcement relies on developer discipline | Critical | Static-analysis test in CI (Phase 13 remediation) |
| F-078 | Universal audit-log capture relies on developer discipline | Critical | Same |
| FS-053 known limitations | Sandbox can be bypassed by string-concat / char-code / backtick tricks | High (with mitigations) | Free-form PS field is auth-gated; structured fields use parameter binding (FS-054); documented in `docs/WORKFLOW_SANDBOX.md` |
| F-111 | CSP nonce pre-existing failure on `/login` | Minor | Acceptable for pre-auth page; remediation deferred |
| F-S-1 | No explicit `MAX_CONTENT_LENGTH` | Minor | Reverse proxy in deployment SOP |
| F-A-1 | `restart_log.actor` missing | Minor | Add column |
| F-D-1 | `log_audit` insert failure is silent | Minor | Add audit-fail telemetry |

All findings carried to `17_FINDINGS_AND_GAPS.md`.

## J. Prior-audit cross-reference

Prior internal security audits already closed many controls. Key items closed:

| Prior finding | Status |
|---|---|
| S1-1 RCE via f-string PS construction | **Closed** — replaced with parameter binding (FS-054) |
| S1-4 Uniform RBAC enforcement on destructive endpoints | **Closed** for known endpoints — `test_rbac_uniform.py`; F-075 is the next iteration |
| S1-7 Audit hash chain | **Closed** — implemented + tested |
| S1-8 Per-request UUID | **Closed** — auto-filled |
| S2-1 Session containment | **Closed** — `revoked_sessions` + check-on-request |
| S2-12 / W3 Account lockout | **Closed** — `auth_failures` + count window |
| S3-1 HTTPS downgrade protection | **Closed** |
| S3-9 / W2 CSP | **Closed** for authenticated pages; F-111 covers `/login` |
| S3-12 Tier-0 skip-verify block | **Closed** |

## K. Recommended SOPs (Phase 11)

- **User-lifecycle SOP**: onboarding (ACL grant), offboarding (disable + revoke session), periodic ACL review (quarterly).
- **Secret-rotation SOP**: backup-admin password, LDAP bind password, Fernet key (`tools/rekey.py`), Flask SECRET_KEY.
- **Sandbox-allowlist change SOP**: any addition to `DEFAULT_ALLOWED_CMDLETS` requires 2-person review + entry in `docs/WORKFLOW_SANDBOX.md` change log.

---
*End of document.*
