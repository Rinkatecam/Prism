<p align="center">
  <img src="static/img/logo.png" alt="Prism logo" width="140" height="140">
</p>

<h1 align="center">Prism — Windows Server Monitoring</h1>

Real-time monitoring dashboard for Windows server fleets. Built with Python (Flask + Jinja2 + HTMX). Agentless — communicates with servers via WinRM (no software to install on targets).

**Author:** Rinkatecam & Atlas

## How it works

Prism is **one process on one host inside your network**. There is no cloud
component, no account to create, and nothing to register.

It polls each server in your `config.json` over WinRM for CPU, memory, disk,
services, Windows updates, event logs and failed logins; stores what it finds in
a local SQLite file; and renders it through a Flask + HTMX web UI. Optionally it
can authenticate operators against your Active Directory, send alerts through
your mail server, and post to a webhook you nominate. That is the whole system.

```
your browser ──▶ Prism (Flask + collector) ──▶ your Windows servers   (WinRM)
                        │                  ──▶ your directory         (LDAP, optional)
                        │                  ──▶ your mail server       (SMTP, optional)
                        ▼
                  data/prism.db  ◀── everything Prism knows lives here
```

## Does it send your data anywhere?

**No — and here is the version of that answer you can check.**

Prism has **no vendor endpoint**. There is no telemetry, no licence check, no
update ping, no analytics, and no crash reporting. Nothing in the source
contains an address belonging to us, because no such address exists.

What it does *not* claim is "no data leaves". That would be false and you would
disprove it in one `grep`: a monitoring tool that connected to nothing could not
monitor anything. Prism opens sockets constantly. The honest claim is about
**destination and consent** — every destination comes from your configuration
file:

- **24 outbound call sites across 9 files, and 0 of them have a hardcoded
  destination.** Every one resolves to a value you set.
- **Core monitoring is LAN-only.** Block all non-LAN egress at the host
  firewall and the dashboard, metrics, health checks and TLS checks keep working
  unchanged.
- **The browser loads no third-party code.** Tailwind, HTMX, Idiomorph,
  Chart.js, Lucide and the web fonts are vendored in `static/vendor/` and served
  by Prism; the Content-Security-Policy names no external origin at all.
- **Opt-in integrations are off until you turn them on** — LDAP, SMTP and
  webhooks. You choose whether they run and where they point. Webhooks are the
  one integration whose defaults point outward (Teams / Slack / Discord), and
  they ship disabled.
- **One hardcoded destination exists**, and it cannot leave your segment: the
  Wake-on-LAN magic packet, a broadcast to `255.255.255.255:9` that routers do
  not forward.
- **Everything is stored on your host** in a single SQLite file, with server
  credentials encrypted at rest.

### Verify it rather than believing it

```bash
python tools/audit_outbound.py                              # every outbound call site + its destination
python tools/verify_lan_only.py --port 5000 --seconds 180   # live census of what it actually connected to
```

The second one exits non-zero if Prism reached a routable public address.

### It stays true because tests enforce it

A document goes stale the day it is written. These fail the build:

| test | fails on |
|---|---|
| `tests/test_outbound_ratchet.py` | a new outbound path, a hardcoded destination, or an external host literal anywhere in the Python |
| `tests/test_csp.py` | any CSP directive naming an external origin |
| `tests/test_route_governance.py` | a mutating endpoint without auth or without an audit write |

Each is additionally mutation-checked — `python tools/verify_guardrails.py`
reintroduces every defect on purpose and fails if the test does not notice.

**Full detail:** [`docs/DATA_FLOWS.md`](docs/DATA_FLOWS.md) is the complete
inventory, including the findings we fixed and the ones we argued and left.
[`docs/LAN_ONLY_VERIFICATION.md`](docs/LAN_ONLY_VERIFICATION.md) is the
procedure for proving it on your own kit.
[`docs/SECURITY_REVIEW.md`](docs/SECURITY_REVIEW.md) is written for a security
reviewer doing vendor due diligence, and includes the plain statement about what
a host administrator can read. Policy, threat model and how to report a
vulnerability are in [`SECURITY.md`](SECURITY.md).

## Quick Start

```bash
# Install dependencies (hash-verified, deterministic closure)
pip install --require-hashes -r requirements.lock

# Edit config.json with your servers (see Configuration below)
# Then run:
python app.py

# Open http://localhost:5000
```

To update dependencies: see `docs/DEPENDENCIES.md`.

### Verify your install

If you downloaded a release tarball (instead of `git clone`), verify the
Sigstore signature **before** unpacking and installing — see
[`docs/RELEASE_VERIFICATION.md`](docs/RELEASE_VERIFICATION.md). One-liner:

```sh
./tools/verify_release.sh prism-vX.Y.Z.tar.gz <OWNER>/<REPO>     # POSIX
.\tools\verify_release.ps1 -Tarball .\prism-vX.Y.Z.tar.gz -OwnerRepo <OWNER>/<REPO>  # PowerShell
```

A signature failure means **do not install** — see the doc for escalation.

## Tech Stack

- **Backend:** Python 3.12+ / Flask / SQLite (WAL mode)
- **Frontend:** Jinja2 templates + HTMX + Idiomorph (smooth DOM-diffing) + Chart.js + Tailwind CSS + Lucide Icons
- **Monitoring:** WinRM via pypsrp (agentless, no software to install on servers)
- **Vendored assets:** HTMX, Idiomorph, Chart.js, Tailwind, Lucide served locally from `static/vendor/` — no external CDN, no build step

## Features

### Dashboard
- Status overview tiles (healthy / warning / critical / offline counts with progress bar)
- Critical issues cards with per-server threshold coloring
- Server grid grouped by type with tag filtering
- Activity feed with alert-fatigue noise scoring
- Incidents panel with correlated events
- TLS certificate alerts (expiring / expired / error)
- **Windows Update alerts** — shows servers with pending updates, active installs (with live stage progress: queued / searching / downloading / installing), errors, and pending reboots

### Server Detail
- Real-time CPU / RAM / Disk metrics with threshold bars
- 24h history chart with annotation overlays
- Windows event logs (balanced collection: errors/warnings prioritized over info noise)
- Failed login heatmap (4-week view with week selector)
- Security status (Defender / Firewall / BitLocker / open ports / local users)
- Anomaly detection with acknowledgment/snooze
- Disk capacity forecasting (growth vs stationary vs memory-leak detection)
- **Windows Update management** — view pending updates, install with one click, automatic retry on delta-download failures, restart-after-update checkbox, live install progress banner
- **Restart overlay** — when you restart a server, a loading screen covers the content area (sidebar stays usable), polls 4 times at 30s intervals, auto-reloads when the server comes back, shows warning if it doesn't respond

### Topology (Infrastructure Map)
- Interactive SVG canvas with pan (drag) + zoom (mousewheel, zoom-to-cursor)
- Top-down layered dependency tree (roots at top, dependents flowing down)
- Workflow-block-style nodes with type-based icons (DC, SQL, File, App, Web, Mail, Backup)
- Curved Bezier edges with arrowheads
- Rich hover tooltip: status badge, CPU/RAM/Disk bar graphs, dependency lists
- Search + status/type filter chips
- Blast radius highlighting
- Click-to-navigate to server detail
- Auto-refresh on new collector data (preserves pan/zoom)

### Operations
- Scheduled server restarts with maintenance windows
- Runbook execution (PowerShell-based, per-server)
- Health checks (TCP/HTTP probes)

### Monitoring
- Detection mode selection (thresholds / anomaly / baseline)
- Baseline detection with per-metric stddev floors
- CPU N-of-M consecutive-cycles gating (anti-noise)
- Alert fatigue management

### Reports
- CSV/JSON/PDF export for metrics, events, and capacity data
- SLA/uptime tracking
- Server comparison (multi-server overlay charts with fleet bands)
- Windows event log comparison across servers

### Workflows
- Visual drag-and-drop workflow editor (Drawflow)
- 16 block types in 4 categories (checks, actions, flow control, notifications)
- Scheduled / event-driven / manual triggers

## Configuration

Edit `config.json` to add your servers. Each server needs:
- `name`: Display name
- `type`: `file_server`, `app_server`, `domain_controller`, `database_server`, `web_server`, `mail_server`, `print_server`, `backup_server`, or `other`
- `host`: FQDN or IP address
- `username` / `password`: WinRM credentials (passwords are encrypted at rest via `crypto_utils.py`)
- `port`: WinRM port (default 5985 for HTTP, 5986 for HTTPS)
- `use_https`: `true` to use WinRM over HTTPS (port auto-flips to 5986)
- `https_skip_verify`: `true` to disable certificate validation (use only during initial cert rollout)
- `tier`: RBAC tier — `0` (critical: DC, primary DB, mail), `1` (standard, default), `2` (dev/test). Tier-0 servers require explicit `admin` ACL grants and dual-admin approval for destructive ops.
- `thresholds`: Per-server warning/critical levels for CPU, RAM, Disk

Settings configurable in the UI (Settings page):
- **Poll Interval** — how often to collect metrics (default 60s)
- **Log Collection Interval** — how often to pull Windows event logs (default 5 min)
- **Update Check Interval** — how often to check for pending Windows Updates (default 30 min)
- **Data Retention** — how long to keep historical data (default 30 days)
- Language (en/de/fr/es/ja), Timezone, Theme (light/dark)

## Architecture

### Collector Loop (`collector_v2/`)

The collector runs as a daemon thread, executing cycles every `poll_interval` seconds:

1. **Metrics collection** — WinRM PowerShell scripts for CPU/RAM/Disk on each server (parallel, 5-thread pool)
2. **Status computation** — 7-phase decision tree (`_effective_status`), documented in `docs/STATUS_FLOW.md`
3. **Sub-checks** (gated by configurable intervals):
   - Windows event logs (balanced: 200 events/log, severity-prioritized, Security log uses Keywords bitmask)
   - Windows Update check (Microsoft Update catalog, ServerSelection=2)
   - Hardware specs (CPU model, RAM, disk sizes)
4. **Post-collection** — anomaly detection, baseline checks, TLS cert checks, security status, config drift, event correlation
5. **Cache refresh** — `latest_by_server` dict updated so dashboard reads are instant (no DB hit)

### Per-Server Accelerated Polling

When an admin action is triggered (restart, install updates, cancel updates), that server enters **accelerated mode** for 5 minutes. All sub-checks (updates, logs, hardware) run every cycle (~60s) instead of their normal 30-min/5-min cadence. This ensures the UI reflects reality within seconds after an action.

### Windows Update Install Flow

The WU COM API refuses `Download()` and `Install()` calls from remote WinRM sessions (returns `0x80070005 E_ACCESSDENIED`). Prism works around this with a **scheduled task**:

1. User clicks "Install Updates" on the server detail page
2. Prism encodes the install script as UTF-16LE base64 — that is the wire format `-EncodedCommand` takes, and it is what lets the script reach the scheduled task **without writing a `.ps1` file to disk**. No script file is created, so none is left behind for anything to read or modify between registration and execution.
3. Registers a scheduled task running as `NT AUTHORITY\SYSTEM` with `-EncodedCommand`
4. Task writes progress to `C:\ProgramData\Prism\update-status.json` and logs to `update-log.txt`
5. UI polls `/api/servers/<name>/update-status` every 5s, showing live stage transitions
6. Auto-retry on `0x8024200D` (WU_E_UH_NEEDANOTHERDOWNLOAD) — re-downloads then retries install
7. Optional "Restart after update" checkbox triggers automatic reboot on success

### Smooth UI Refresh

- **Idiomorph** (HTMX extension) — DOM-diffing swaps instead of innerHTML replacement. Only changed nodes are touched; icons stay stable, numbers morph in place.
- **CSS transitions** on metric values, bar widths, and badges (250ms ease-out)
- **`prismRefresh`** event fired globally from `base.html` when the collector completes a cycle — every page reacts (dashboard partials, server detail fetches, topology canvas)
- **CSRF token auto-refresh** every 30 min via `/api/csrf-token` endpoint + `WTF_CSRF_TIME_LIMIT=None` so tokens never expire on long-lived pages

### Restart / Shutdown

Uses `shutdown.exe /r /t 5 /f` (5-second delay) instead of `Restart-Computer -Force` so WinRM has time to return a clean response before the session dies. Expected "session forcibly closed" errors are whitelisted as success.

## Project Structure

```
app.py                - Flask entry point + collector startup + CSRF config
collector_v2/         - WinRM metric collection, status computation, sub-checks (supervisor/workers/aggregator/periodics)
database.py           - SQLite schema (WAL), queries, cleanup
config_manager.py     - Config loading, defaults, validation
models.py             - ServerConfig dataclass, default thresholds per type
topology.py           - Dependency graph layout + interactive canvas data
workflow_engine.py    - Drawflow graph executor + block runners
baseline_engine.py    - Baseline detection (hour-of-week Z-score)
analytics.py          - Anomaly detection, forecasting, rate analysis
routes/api.py         - JSON API (~100 endpoints, split into per-domain blueprints under routes/api/)
routes/views.py       - HTML page + HTMX partial routes
ps_sandbox.py         - Workflow PowerShell allowlist + HARD_DENY validator
winrm_factory.py      - Centralised WSMan connection factory (HTTPS-aware)
crypto_utils.py       - Fernet encryption + DPAPI key wrapping
templates/            - Jinja2 templates (dashboard, server detail, topology, rbac, etc.)
static/js/topology.js - Interactive topology canvas (pan/zoom/tooltips)
static/css/app.css    - Custom styles
docs/csv/             - Computerised-system-validation (CSV) package (URS, risk, security controls, findings register)
docs/STATUS_FLOW.md   - Status computation reference documentation
tests/                - Pytest suite (650+ tests: models, sandbox, RBAC, audit, webhooks, CSP, collector)
config.json           - Server list and settings (excluded from git)
data/                 - SQLite database + keys (excluded from git)
```

## Security Notes

Outbound connections, storage and the data-flow story are covered above under
[How it works](#how-it-works) and [Does it send your data
anywhere?](#does-it-send-your-data-anywhere), with the full evidence in
[`docs/SECURITY_REVIEW.md`](docs/SECURITY_REVIEW.md). This section is the
application-level controls.

- `config.json` contains encrypted server passwords — excluded from git via `.gitignore`
- `data/` directory contains the SQLite DB, Flask secret key, and encryption key — excluded from git
- CSRF protection via Flask-WTF on all POST endpoints
- Rate limiting on sensitive endpoints (restart, install, login)
- Session cookies hardened: `HttpOnly`, `SameSite=Lax`, optional `Secure` (set `PRISM_HTTPS_ONLY=1` env var), 8-hour idle timeout
- WinRM credentials encrypted at rest via `crypto_utils.py` using Fernet (AES-128-CBC + HMAC-SHA256)
- **Encryption key wrapped with Windows DPAPI** when `pywin32` is installed — the Fernet key on disk is bound to the running user account; a stolen DB backup is useless without that user's profile. Falls back to plain-text key (icacls-restricted) on non-Windows / first-run before migration.
- **Append-only audit log** — SQLite triggers reject `UPDATE` and `DELETE` on the `audit_log` table. Even an attacker with SQL access can't tamper with the trail. Archive to JSONL via `POST /api/audit-log/archive`.

### Per-Server RBAC

Default mode is **permissive**: with auth enabled, any logged-in user has full access. The moment an admin grants the first ACL, enforcement begins for everyone.

- Permissions: `view` < `control` < `admin`
- ACLs are per-(user, server). `server_name='*'` is a wildcard.
- Tier-0 servers (DCs, primary DB, mail) **always** require an explicit `admin` ACL — even in permissive mode. Destructive ops on tier-0 (power, install updates) additionally require a **second admin's approval token** (`?approval_id=<id>`), which is single-use and tied to that specific server.
- Manage ACLs and approvals at **`/admin/rbac`** (UI) or via `POST /api/rbac/grant`, `/api/rbac/revoke`, `/api/approvals` (API).

### Workflow PowerShell Sandbox

Workflow `run_powershell` and `condition` blocks pass user-authored scripts through `ps_sandbox.py`:

- Allowlist of safe cmdlets (`Get-Service`, `Restart-Service`, `Get-Process`, etc.)
- HARD_DENY layer that always blocks `Invoke-Expression`, `iex`, `Invoke-WebRequest`, `Add-Type`, `-EncodedCommand`, `Remove-Item`, etc. — even if an operator allowlists them.
- 4000-char script limit
- Validate without executing: `POST /api/workflows/validate-script`
- Disable (NOT recommended) via `settings.workflows.sandbox.enabled = false`

### Webhook Hardening

`POST` to a webhook URL goes through `validate_webhook_url()`:
- HTTPS only
- DNS hostname only (rejects raw IPs)
- Embedded credentials rejected
- Host must match the allowed list (Teams / Slack / Discord by default; extensible via `settings.webhooks.allowed_hosts`)
- Message body sanitised (strips control chars, caps at 2 KB) so a crafted message can't smuggle headers
