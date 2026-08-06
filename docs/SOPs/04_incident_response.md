# SOP-04 — Incident Response

| Field | Value |
|---|---|
| Document ID | SOP-04 |
| Version | 1.0 |
| Effective from | 2026-05-22 |
| Owner | IT operations |
| Implements | URS-010, URS-013, URS-020 / FS-010, FS-013, FS-020 |
| Closes finding | F-SOP-4 |
| Review cadence | Annual or after a major incident |

## 1. Purpose

Define the on-call operator's response when Prism flags an incident (status transition, anomaly, failed-login spike, TLS expiry, dependency outage). Ensures consistent triage, action, and post-incident record-keeping.

## 2. Trigger

Any of:
- Email / Teams alert from Prism with severity `critical` or `warning`.
- Dashboard tile transitions to red / amber.
- Pulse widget shows degraded / critical / dead state.
- Operator notices anomaly in the events feed.

## 3. Procedure

### 3.1 Triage (≤ 5 min)

1. Open the dashboard at `/`.
2. Open the failing server's detail page.
3. Read the most recent events for that server.
4. Identify class of failure:

| Class | Action |
|---|---|
| Threshold breach (cpu/ram/disk over) | Apply runbook 4.2.1 |
| Anomaly (sustained deviation from baseline) | Apply runbook 4.2.2 |
| Failed-login spike | Apply runbook 4.2.3 |
| TLS cert expiring | Apply runbook 4.2.4 |
| Server offline / unreachable | Apply runbook 4.2.5 |
| Pulse widget dead state | Apply runbook 4.2.6 |

### 3.2 Per-class runbooks

#### 4.2.1 Threshold breach
- Open the server detail; look at the metric chart.
- If transient (spike + recover): acknowledge the alert (writes `anomaly_acknowledgments` row).
- If sustained: investigate the underlying cause (process, scheduled job, runaway leak). Use the `Run PowerShell` workflow block (with sandbox) if needed.
- If a Windows-update install is pending: consider running it after-hours (SOP-05 informs scheduling).

#### 4.2.2 Anomaly
- Confirm against the baseline-coverage UI that there's enough baseline data (≥ 24 h) for the alert to be reliable.
- If false positive: ack with note.
- If true: same as 4.2.1.

#### 4.2.3 Failed-login spike
- Open `/api/servers/<n>/failed-logins/heatmap` for the affected server.
- Look at source IPs — distinguish broken automation from a real attack.
- If real attack: escalate to security per the org's IR playbook + close the affected port at the firewall.

#### 4.2.4 TLS expiring
- Schedule cert renewal per the renewal SOP (out-of-app).

#### 4.2.5 Server offline
- Verify external (not just from Prism's vantage point) — ping, RDP attempt.
- If genuinely down: follow the org's server-down playbook.
- If only Prism can't reach it: check WinRM (`Test-WSMan <host>` from the Prism host).

#### 4.2.6 Pulse widget dead
- Open `/api/system/health` and inspect each subsystem heartbeat.
- If a daemon thread is dead: collect logs, then restart Prism (per `14_CHANGE_CONTROL.md`).
- If everything looks alive: hard-refresh the browser (Ctrl-F5).

### 3.3 Post-incident

When the incident is resolved (status returns to green):

1. Open `/api/incidents/<id>` if Prism auto-correlated.
2. Mark `status='resolved'`, write `resolution_notes` describing the root cause + actions taken.

   ```
   PUT /api/incidents/<id>
   { "status": "resolved",
     "resolution_notes": "Restart-Service spooler on SRV05 — driver leak; long-term fix tracked as ITS-1234" }
   ```

3. The `update_incident` audit row is automatically written.

## 4. Escalation criteria

Escalate to L2 if:
- A critical event persists > 30 min despite operator action.
- A failed-login spike exceeds 50/hour from a single IP (likely automated).
- Pulse widget shows dead state and Prism restart doesn't restore it.
- Audit chain verification (per SOP-05) reports tampering.

## 5. Sign-off

| Role | Name | Date | Signature |
|---|---|---|---|
| Owner | | | |
| Reviewer | | | |

---
*End of SOP.*
