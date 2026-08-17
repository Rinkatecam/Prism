# Where Prism's data goes

An inventory of every connection Prism can open, written for someone doing
vendor due diligence who intends to verify rather than believe.

Everything here is reproducible on your own copy. The commands are given.

---

## The claim, stated so it can be falsified

> Prism runs on your LAN. It has **no vendor endpoint** — no telemetry, no
> licence check, no update ping, no analytics, no crash reporting — and no
> path by which your data reaches anyone you did not choose. The only outbound
> connections it makes are the ones you configure: to your own servers, your
> own directory, your own mail server, your own webhook. Everything is stored
> on your host.

Note what this does **not** say. It does not say "no data leaves". That would
be false and you would disprove it in one `grep`: Prism is a monitoring tool,
and a monitoring tool that connected to nothing could not monitor anything. It
opens sockets constantly. The claim is about **destination and consent** — every
destination comes from your configuration file, and there is no address in the
source code for anything of ours.

---

## The inventory

Generated, not hand-maintained:

```bash
python tools/audit_outbound.py
```

It walks the AST of every shipped `.py` file, resolves each call through that
file's own imports, and prints the **destination expression** for each site.
As of this writing: **24 call sites across 9 files, and 0 of them have a
literal destination.** Every one resolves to a variable that traces back to
`config.json`.

| module | sites | what it opens | destination comes from |
|---|---|---|---|
| `winrm_factory.py` | 1 | WinRM (WS-Man) to a monitored server | `server_config.host` — your `servers` list |
| `restart_scheduler.py` | 2 | WinRM; SMTP | your `servers` list; your `email.smtp_server` |
| `routes/api/config.py` | 3 | WinRM; LDAP | your `servers` list; your `auth.ldap_url` |
| `auth.py` | 3 | LDAP bind + a TCP reachability probe | your `auth.ldap_url` |
| `routes/api/misc.py` | 2 | LDAP | your `auth.ldap_url` |
| `email_alerts.py` | 6 | SMTP | your `email.smtp_server` |
| `health_checker.py` | 3 | TCP connect, HTTP(S) GET, UDP probe | the host/port/path in each health check you define |
| `tls_checker.py` | 3 | TCP + TLS handshake | the host/port of each certificate you ask it to watch |
| `routes/api/power.py` | 1 | UDP broadcast (Wake-on-LAN) | **hardcoded — see below** |

### Every WinRM connection goes through one function

`winrm_factory.make_wsman()` is the single place a WS-Man transport is built
for the collector and for every feature that runs PowerShell on a monitored
host — drift, failed-login collection, updates, security checks, runbooks,
workflows. They import the factory rather than constructing their own.

Two sites do construct their own transport, `restart_scheduler.py:462` and
`routes/api/config.py:820`. They connect to the same operator-configured hosts,
so this is not a data-flow finding, but it does mean transport policy lives in
three places rather than one. Recorded as a maintenance observation.

---

## The two things a reviewer will grep for, addressed directly

**1. There is exactly one hardcoded destination in the application, and it
cannot leave your network.**

`routes/api/power.py` sends a Wake-on-LAN magic packet:

```python
sock.sendto(magic, ('<broadcast>', 9))
```

`<broadcast>` is `255.255.255.255`. A limited broadcast is not forwarded by
routers — it reaches the local segment and stops. The payload is the standard
WoL frame: six `0xFF` bytes followed by the target MAC repeated sixteen times.
The MAC is the one you typed into the server's configuration. No other data is
in the packet, and it goes nowhere you do not already control.

**2. The UDP health probe sends an empty datagram.**

`health_checker.udp_probe()` calls `sock.sendto(b'', (host, port))` — zero
payload bytes. It is testing whether a port answers, and it tells the port
nothing.

---

## What was searched for and is not there

Absence is harder to evidence than presence, so this lists the search rather
than asserting the conclusion. Run it yourself:

```bash
grep -rn --include=*.py -iE "telemetry|phone.?home|check.?for.?update|version.?check|licen[cs]e.?check|beacon|sentry|bugsnag|rollbar|mixpanel|segment\.io|google-analytics|posthog" .
```

Every hit is Prism's own vocabulary for local counters it displays to you —
`audit_telemetry` is a number on the health page; `analytics.py` computes fleet
statistics from your own database. There is no third-party SDK, no error
reporter, no update check and no licence call.

**No external host appears anywhere in the Python source.** The only absolute
URLs in shipped code are two copies of `http://adaptivecards.io/schemas/adaptive-card.json`
in `webhooks.py`, which is the `$schema` identifier inside a Microsoft Teams
Adaptive Card payload. It is a name, not an address — it is never fetched, and
it only exists inside a message you asked Prism to send to a webhook you chose.

### Front-end: everything is served from your own host

The browser loads no third-party asset. Tailwind, htmx, idiomorph, Chart.js,
Lucide and both web fonts are vendored under `static/vendor/` and served by
Prism itself. Measured on a dashboard load: **every request goes to the Prism
origin and none goes anywhere else.** Reproduce it with your browser's network
tab and sort by domain.

---

## Two classes of connection, and only you can create the second

**Core — required to monitor anything, all on your LAN:**

- **WinRM / WS-Man** to each server in your `servers` list. This is how metrics
  are collected. Credentials are encrypted at rest (`crypto_utils.py`).
- **Health-check probes** — TCP connect, HTTP(S) GET, or an empty UDP datagram,
  to the endpoints you define.
- **TLS certificate checks** — a handshake against the host:port you name, to
  read the certificate's expiry.

Nothing in this class has a default destination. A fresh install with an empty
`servers` list connects to nothing at all.

**Opt-in integrations — off by default, destination chosen by you:**

- **LDAP / Active Directory** (`auth.ldap_url`) — directory authentication and
  the optional "discover servers" query, which is an LDAP search against your
  own domain controller and requires an RBAC admin.
- **SMTP** (`email.smtp_server`) — alert and report email.
- **Webhooks** (`webhooks.py`) — `enabled: false` by default. **This is the one
  integration whose defaults point off your network, and it deserves saying
  plainly.** When you turn it on, the URL must pass `validate_webhook_url()`:
  HTTPS only, no embedded credentials, a DNS hostname rather than a raw IP, and
  a host matching an allowlist — whose default entries are
  `outlook.office.com`, `webhook.office.com`, `hooks.slack.com`, `discord.com`
  and `discordapp.com`. Those are Microsoft Teams, Slack and Discord: external
  services. So enabling webhooks is a deliberate decision to send alert
  summaries to a third party you already use, and the allowlist is what stops a
  typo or a crafted URL going somewhere else. What crosses is the alert text,
  control characters stripped and capped at 2 KB by `sanitize_alert_text()` —
  not metrics, not credentials, not the database.

These are consensual by construction: they exist because an operator asked for
them and pointed them somewhere. They are not the thing this document is
defending against, and pretending they do not exist would be the fastest way to
lose a reviewer's trust.

### A monitored server cannot be told to fetch from the internet

Worth stating because it is the obvious way round everything above: workflows
let an operator author PowerShell that runs on a monitored host, which would be
a general-purpose egress channel. `ps_sandbox.py` HARD_DENYs
`Invoke-WebRequest` / `iwr` and `Invoke-RestMethod` / `irm` — and HARD_DENY
means denied even if an operator adds them to their own allowlist.

---

## The Content-Security-Policy names no external origin

The policy Prism serves on every response:

```
default-src 'self'; script-src 'self' 'nonce-<per-request>';
style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:;
frame-ancestors 'none'; base-uri 'self'; form-action 'self'
```

Read it against the claim: the browser may load scripts, styles, images and
XHR/fetch targets from Prism's origin and nowhere else. There is no host to
allow-list because there is no third party in the front end.

`style-src 'unsafe-inline'` is the one concession and it is not a hole in the
above: it permits inline STYLE, not an external origin. The vendored Tailwind
browser build generates CSS into a `<style>` element at runtime, and inline
`style="..."` attributes are used throughout the UI.

**This was a finding before it was evidence.** Until recently the policy
allowed `script-src` from `cdn.tailwindcss.com`, `unpkg.com` and
`cdn.jsdelivr.net`, with `style-src` and `connect-src` carrying some of the
same — entries left behind when the front end stopped using them, justified by
a comment that still described "Tailwind's CDN runtime" long after Tailwind was
vendored. Nothing was broken and nothing was being fetched, which is exactly
why it survived: **an allowlist entry that nothing uses still grants the
capability.** It is recorded here rather than quietly corrected, because how a
project treats a finding is itself evidence.

Two tests hold the line, in `tests/test_csp.py`:
`test_no_csp_directive_permits_an_external_origin` reads the WHOLE header —
the earlier per-directive check passed while two other directives named CDNs,
because it only ever looked at `script-src`.

---

## HTTPS health checks verify certificates — and that was a finding first

`health_checker.py` set `check_hostname = False` and `verify_mode = CERT_NONE`
unconditionally, with no way to change it. An HTTPS health check therefore
proved that *something* answered on that port, never that it was the service
you meant: a mis-issued certificate, an expired one, or a machine-in-the-middle
all read as a clean "up".

Verification is now on by default. It can be turned off **per check**, because
internal endpoints with self-signed certificates are ordinary and a monitor
that turns a wave of them red is a monitor people switch off — but it is off in
a row that an operator set, and the probe returns `tls_verified` so the weaker
setting leaves a trace in the result instead of living in a constant nobody
reads. Certificate *validity* remains a separate question that `tls_checker.py`
answers on its own.

`tests/test_health_check_tls.py` covers the four layers the setting travels
through, and two of those tests exist because a mutation aimed at them found
nothing to fail: the store was tested, the API parsing and the runner's
hand-off were not.

---

## Open findings

None at present. Items deliberately left unchanged are argued in place — the
Wake-on-LAN broadcast above, and the two `WSMan` sites that build their own
transport rather than using the shared factory.

---

## What this inventory cannot tell you

Stated because a check's blind spots are the first thing a security document
overclaims.

- **It walks Prism's source, not its dependencies.** A library could open a
  connection on its own initiative. Prism's direct dependency list is nine
  packages — `flask`, `flask-wtf`, `flask-limiter`, `pypsrp`, `waitress`,
  `cryptography`, `ldap3`, `reportlab`, and `pywin32` on Windows — chosen to be
  small and well-known for this reason. Verifying their behaviour is a separate
  exercise from reading this file.
- **It reports destination expressions, not resolved values.** `urlopen(url)`
  is reported as `url`; a human still traces where `url` came from. What it does
  guarantee is that no site passes a string literal, which is the case that
  would need no tracing and would be the finding.
- **It does not cover `subprocess`.** PowerShell executed on monitored hosts is
  constrained separately by the cmdlet allowlist in `ps_sandbox.py`.
- **It is a static reading.** For the dynamic proof — block all outbound at the
  host firewall, start Prism, watch core monitoring keep working — see the
  LAN-only verification procedure.

---

## A note on the tool itself

The first version of `audit_outbound.py` reported **43** call sites. Nineteen
of them were false: it matched module names by prefix, and `"requests"` starts
with `"r"`, so every `r.get("cpu_percent")` dict access in the codebase was
recorded as an outbound HTTP GET. The inventory built on that number would have
been fiction that happened to look thorough.

It is mentioned here because it is the honest answer to "how do you know the
tool is right", and because the correction is the reason the table above says
24 and not 43. The matcher now requires an exact module name or a dotted
boundary, and the reasoning is written into the function that does it.
