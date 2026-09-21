"""The outbound-connection ratchet: Prism may not grow a new way to phone out.

`docs/DATA_FLOWS.md` makes a claim to a customer's security reviewer — that
every destination Prism connects to comes from their own configuration and
there is no vendor endpoint anywhere in the source. **A document goes stale the
day it is written; a test does not.** This file is what keeps that document
true.

Three properties, in descending order of how much they matter:

  1. **No outbound call site has a literal destination.** This is the whole
     claim in one assertion. A hardcoded host is the difference between "it
     connects where you tell it to" and "it connects somewhere we chose".
  2. **The set of (file, connection kind) pairs matches the audited set.** A
     new kind of outbound connection, or an old kind appearing in a new file,
     fails the build and has to be argued into the baseline deliberately.
  3. **No external host appears as a string literal in shipped Python**, even
     one never passed to a connect call. A URL sitting in a constant is one
     refactor away from being used.

The unauthenticated-route half of the same guarantee lives in
`tests/test_route_governance.py`, which already ratchets `_AUTH_ALLOWLIST` and
`_AUDIT_ALLOWLIST`. It is not duplicated here.

WHAT THIS IS BLIND TO, stated because the repository's most-repeated failure is
a check that reports success without doing the work:

  * **Dependencies.** This reads Prism's own source. A library that beacons on
    its own initiative is invisible here and is covered by the dependency
    audit instead.
  * **Runtime-assembled destinations.** Property 1 proves no site passes a
    string CONSTANT. A destination built by concatenation would pass this and
    still be hardcoded. Property 3 is the partial backstop — the pieces would
    have to be literals somewhere — and it is a backstop, not a proof.
  * **`subprocess`.** PowerShell run on monitored hosts is constrained by the
    cmdlet allowlist in `ps_sandbox.py`, not by this file.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.audit_outbound import scan, SKIP_DIRS      # noqa: E402


# ── 1. the audited set ────────────────────────────────────────────────────
#
# (file, connection kind). Deliberately NOT line numbers: those move on every
# edit above them, and a baseline that fails for an unrelated reason gets
# updated without being read, which is how a ratchet becomes a rubber stamp.
#
# Each entry is an outbound path a reviewer can trace in docs/DATA_FLOWS.md.
# Adding one means adding a way for Prism to reach the network — argue it in
# the pull request, not in a baseline bump.
OUTBOUND_BASELINE = frozenset({
    # WinRM to monitored servers — the core of what Prism does. Three sites:
    # the shared factory every consumer routes through, plus two that build
    # their own transport to the same operator-configured hosts.
    ("winrm_factory.py", "WinRM transport"),
    ("restart_scheduler.py", "WinRM transport"),
    ("routes/api/config.py", "WinRM transport"),

    # LDAP / Active Directory — opt-in, destination is auth.ldap_url.
    ("auth.py", "LDAP server handle"),
    ("auth.py", "LDAP bind"),
    ("auth.py", "TCP connect"),              # reachability probe, no bind
    ("routes/api/config.py", "LDAP server handle"),
    ("routes/api/config.py", "LDAP bind"),   # "discover servers", RBAC admin
    ("routes/api/misc.py", "LDAP server handle"),
    ("routes/api/misc.py", "LDAP bind"),

    # SMTP — opt-in, destination is email.smtp_server.
    ("email_alerts.py", "SMTP connect"),
    ("restart_scheduler.py", "SMTP connect"),

    # Health checks — destination is whatever endpoint the operator defined.
    ("health_checker.py", "TCP connect"),
    ("health_checker.py", "HTTP(S) request"),
    ("health_checker.py", "raw socket"),     # UDP probe, sends b'' — no payload

    # TLS certificate expiry checks.
    ("tls_checker.py", "TCP connect"),

    # Wake-on-LAN. The ONE hardcoded destination in the app, and it is
    # ('<broadcast>', 9) — a limited broadcast, which routers do not forward.
    ("routes/api/power.py", "raw socket"),
})


def _sites():
    return scan(include_tests=False)


def test_no_outbound_call_site_has_a_literal_destination():
    """The claim in docs/DATA_FLOWS.md, as one assertion.

    Every destination must resolve to a variable that traces back to
    `config.json`. A string constant here would mean Prism connects somewhere
    the operator did not choose — which is the single thing the whole data-flow
    document promises does not happen.
    """
    offenders = [f"{s.path}:{s.line} -> {s.destination}"
                 for s in _sites() if s.literal_destination]
    assert not offenders, (
        "outbound call site(s) with a hardcoded destination:\n  "
        + "\n  ".join(offenders)
        + "\n\nEvery destination must come from operator configuration. If "
          "this is genuinely local (see the Wake-on-LAN broadcast), say so in "
          "docs/DATA_FLOWS.md and here — do not just silence the check.")


def test_the_outbound_call_sites_match_the_audited_set():
    """No new way to reach the network without a deliberate decision."""
    observed = {(s.path, s.kind) for s in _sites()}

    added = sorted(observed - OUTBOUND_BASELINE)
    assert not added, (
        "NEW outbound connection path(s) not in the audited set:\n  "
        + "\n  ".join(f"{p}  [{k}]" for p, k in added)
        + "\n\nThis is a change to what Prism can reach. Add it to "
          "OUTBOUND_BASELINE *and* to the table in docs/DATA_FLOWS.md, so the "
          "document a reviewer reads still describes the software.")


def test_the_outbound_baseline_is_not_left_stale():
    """A ratchet that is never tightened is a ratchet in name only.

    If an outbound path is removed, the baseline must shrink with it —
    otherwise the audited set slowly becomes a list of things that used to be
    true, and the next genuine addition hides inside the slack.
    """
    observed = {(s.path, s.kind) for s in _sites()}
    gone = sorted(OUTBOUND_BASELINE - observed)
    assert not gone, (
        "OUTBOUND_BASELINE lists path(s) that no longer exist:\n  "
        + "\n  ".join(f"{p}  [{k}]" for p, k in gone)
        + "\n\nRemove them from the baseline and from docs/DATA_FLOWS.md.")


# ── 2. no external host literal anywhere in shipped Python ────────────────

# Host-shaped literals that are NOT network destinations. Each needs a reason,
# not just an entry.
_HOST_LITERAL_ALLOWLIST = {
    # The $schema identifier inside a Microsoft Teams Adaptive Card. It names
    # the card format; it is never fetched. It travels only inside a message
    # sent to a webhook URL the operator configured.
    "http://adaptivecards.io/schemas/adaptive-card.json",
}

_URLISH = re.compile(
    r"""(?xi)
    ^(?:https?|ftp|ldaps?)://          # a real scheme
    (?!localhost\b)                    # local is not external
    (?!127\.0\.0\.1\b)
    (?!0\.0\.0\.0\b)
    (?!\{)                             # f-string / format placeholder host
    (?!%s\b)
    \S+
    """)


def _shipped_python() -> list[Path]:
    out = []
    for path in sorted(PROJECT_ROOT.rglob("*.py")):
        parts = path.relative_to(PROJECT_ROOT).parts
        if any(p in SKIP_DIRS for p in parts) or parts[0] == "tests":
            continue
        out.append(path)
    return out


def _string_constants(tree: ast.AST) -> list[tuple[int, str]]:
    """Every string constant that is a VALUE, with docstrings excluded.

    A docstring is prose, not a destination. Including them would make this
    check fire on `tools/audit_outbound.py`'s own module docstring, which
    quotes `WSMan("updates.example.com")` to explain the difference between a
    literal and a variable destination — and the cheapest way to make the
    check pass would then be to delete the explanation. That failure mode has
    happened four times in this repository; see the conventions in
    docs/plans/NEXT_SESSION.md.
    """
    docstring_nodes = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef,
                             ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", None)
            if body and isinstance(body[0], ast.Expr) and \
                    isinstance(body[0].value, ast.Constant) and \
                    isinstance(body[0].value.value, str):
                docstring_nodes.add(id(body[0].value))

    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                and id(node) not in docstring_nodes:
            found.append((node.lineno, node.value))
    return found


def test_no_external_host_literal_in_shipped_python():
    """A URL in a constant is one refactor away from being a destination.

    Catches what the call-site walk cannot: an external address that is not
    passed to a connect call *today*. That is the shape a beacon takes when
    someone is not trying to be obvious about it, and it is also the shape a
    well-meaning "check for updates" feature takes on its first commit.
    """
    offenders = []
    for path in _shipped_python():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError:
            continue
        rel = path.relative_to(PROJECT_ROOT).as_posix()
        for lineno, value in _string_constants(tree):
            if value in _HOST_LITERAL_ALLOWLIST:
                continue
            if _URLISH.match(value.strip()):
                offenders.append(f"{rel}:{lineno}  {value[:90]}")

    assert not offenders, (
        "external host literal(s) in shipped Python:\n  "
        + "\n  ".join(offenders)
        + "\n\nPrism has no vendor endpoint (docs/DATA_FLOWS.md). If this is a "
          "non-destination identifier, add it to _HOST_LITERAL_ALLOWLIST with "
          "the reason it is not an address.")


def test_the_host_literal_allowlist_stays_small():
    """The allowlist may shrink, never grow quietly.

    Same shape as the literal ratchets in `tests/test_design_tokens.py`. One
    entry today; each future entry is an argument someone has to make in
    review rather than a line someone adds while making a test pass.
    """
    assert len(_HOST_LITERAL_ALLOWLIST) <= 1, (
        f"_HOST_LITERAL_ALLOWLIST has grown to {len(_HOST_LITERAL_ALLOWLIST)} "
        "entries. Each one is a host-shaped string a reviewer will grep for "
        "and find excused — make the case in the PR, not here.")
