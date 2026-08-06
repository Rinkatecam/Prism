"""Refuse to publish anything that names the real fleet.

Prism is going public. The repository must not carry real hostnames, the real
AD domain, real service accounts or real addresses — not in code, not in
comments, not in test fixtures, and least of all in the planning docs, which
are the worst offenders because they quote measurements taken from production.

    python tools/check_anonymised.py            # scan every tracked file
    python tools/check_anonymised.py --files a.py b.md
    python tools/check_anonymised.py --require-config   # what the hook runs

Exit status: 0 clean, 1 violations found, 2 the check could not run properly.

WHY THE DENY-LIST IS NOT IN THIS FILE
-------------------------------------
The obvious design — a committed list of forbidden strings — publishes the
exact thing it is meant to protect. The first person to read the deny-list
learns every hostname in the estate.

So the real values are read at check time from ``config.json``, which is
gitignored (it holds real hostnames and encrypted credentials). Nothing
secret is ever committed; this file contains only SHAPES.

That split has a second benefit: CI on Linux has no config.json, so it runs
the shape rules alone and stays green, while a developer's machine — where
pushes actually happen — has the config and runs the full check. The pre-push
hook passes ``--require-config`` so a missing config fails loudly there rather
than silently downgrading to the weaker ruleset.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = REPO_ROOT / "config.json"

# Optional, gitignored. For real identifiers the deployment config cannot
# supply — personal AD accounts, a colleague's name, an internal project
# codename. One term per line, '#' comments ignored. It is gitignored for the
# same reason the deny-list is not hardcoded: committing it would publish it.
LOCAL_TERMS_PATH = REPO_ROOT / ".anonymisation-denylist"

# ── Shapes that are identifying on their own ─────────────────────────────
# Safe to commit: they describe a FORM, not a value. Keep them anchored
# tightly enough that fictional replacements do not trip them.
SHAPE_RULES: list[tuple[str, str, str]] = [
    (
        "hostname-convention",
        r"\bDES[A-Z]{2,4}\d{2}\b",
        "looks like a real host from the estate's naming convention",
    ),
    (
        "ldap-dn-with-company-dc",
        # The exemption must be case-insensitive: DNs are conventionally
        # written uppercase, so a case-sensitive "example" lookahead flags the
        # very placeholder this rule tells people to use.
        r"(?i:DC=)(?!(?i:example|test|local|invalid|contoso)\b)[A-Za-z0-9-]{4,},\s*(?i:DC=)(?i:COM|NET|ORG|LOCAL)\b",
        "LDAP base DN naming a real domain — use DC=EXAMPLE,DC=COM",
    ),
]

# Generic domain labels that must never become deny-list entries: matching on
# them would flag half the repository.
GENERIC_LABELS = {
    "com", "net", "org", "local", "localhost", "corp", "intra", "internal",
    "ad", "dc", "www", "lan", "home", "test", "example", "invalid", "domain",
    "prism", "server", "servers", "admin", "user", "users", "srv", "host",
    # URL schemes and protocol words. Without these, splitting an LDAP URL on
    # its punctuation yields "ldap", which then matches ~700 legitimate lines
    # across auth.py and the settings UI. Measured, not hypothesised.
    "ldap", "ldaps", "http", "https", "smtp", "smtps", "winrm", "wsman",
}

# Documentation-reserved and conventional-placeholder addresses. RFC 5737
# (192.0.2.0/24, 198.51.100.0/24, 203.0.113.0/24) exists precisely for this,
# and the 10.0.0.x / 192.168.1.x fixtures predate this check.
ALLOWED_ADDRESS_PREFIXES = (
    "192.0.2.", "198.51.100.", "203.0.113.",
    "10.0.0.", "192.168.1.", "127.0.0.", "0.0.0.0", "255.255.255.",
)

# Third-party bundles are not ours to rewrite, and a minified blob will match
# almost any shape by accident.
#
# docs/csv/evidence/ was briefly in here too, on the reasoning that captured
# test output is not ours to edit. That was wrong: captured output is exactly
# where a hostname or a local user path ends up by accident, and "we can't fix
# it" is not a reason to stop looking. It is scanned. (It is currently clean.)
SKIP_PREFIXES = ("static/vendor/",)
SKIP_SUFFIXES = (
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".woff", ".woff2",
    ".ttf", ".otf", ".zip", ".db", ".lock",
)

# These two necessarily contain the shapes they forbid: the checker defines the
# patterns, and its test suite must exercise them with strings that match. The
# exemption is deliberately narrow — two named files, not a directory — and the
# values inside them are synthetic. Everything else, tests/ included, is scanned.
SELF = ("tools/check_anonymised.py", "tests/test_anonymisation_guard.py")


def _is_placeholder_address(value: str) -> bool:
    return (bool(re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){3}", value))
            and value.startswith(ALLOWED_ADDRESS_PREFIXES))


def _label_is_distinctive(label: str) -> bool:
    """Is this token worth forbidding?

    The address guard is here, not only on the JSON-wide address sweep: when a
    config addresses its hosts by IP rather than by name, the host value itself
    reaches this function. Without the check, a deployment using 10.0.0.x would
    add those to the deny-list and flag every 10.0.0.x fixture in the suite —
    turning the guard into noise on exactly the placeholder range the policy
    explicitly permits.
    """
    if _is_placeholder_address(label):
        return False
    return len(label) >= 4 and label.lower() not in GENERIC_LABELS


def load_secret_terms(config_path: Path | None = None) -> set[str]:
    """Derive the real values to forbid from the gitignored config.

    Returns lowercase terms. Never log the whole set anywhere durable.

    The path is resolved at call time, not bound as a default argument — a
    default binds at import and would silently ignore any later override,
    which is precisely the kind of thing a test of this module needs to do.
    """
    config_path = config_path or CONFIG_PATH
    if not config_path.exists():
        return set()
    try:
        cfg = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"warning: could not read {config_path.name}: {exc}", file=sys.stderr)
        return set()

    terms: set[str] = set()

    def add(value: str | None) -> None:
        if value and _label_is_distinctive(value):
            terms.add(value.lower())

    for server in cfg.get("servers", []) or []:
        add(server.get("name"))
        host = server.get("host") or ""
        add(host)
        for label in re.split(r"[.\\/@:]", host):
            add(label)
        username = server.get("username") or ""
        for part in re.split(r"[\\/@]", username):
            add(part)

    settings = cfg.get("settings", {}) or {}
    auth = settings.get("auth", {}) or {}

    # Parse the URL rather than splitting it on punctuation: the scheme is not
    # an identifier, and treating it as one poisons the whole deny-list.
    from urllib.parse import urlsplit
    ldap_url = str(auth.get("ldap_url") or "")
    hostname = urlsplit(ldap_url).hostname if "//" in ldap_url else ldap_url
    for label in re.split(r"[.\\/@:]", hostname or ""):
        add(label)

    # DN components and the bind account. ldap_user_filter is deliberately
    # excluded — it is a template like (sAMAccountName=%s), whose tokens are
    # LDAP vocabulary, not estate identifiers.
    for key in ("ldap_base_dn", "base_dn", "ldap_bind_user"):
        for label in re.split(r"[.\\/@:,=\s]", str(auth.get(key) or "")):
            add(label)

    # Any address literal anywhere in the config, minus the documentation
    # ranges that are meant to appear in a public repo.
    for addr in re.findall(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", json.dumps(cfg)):
        if not addr.startswith(ALLOWED_ADDRESS_PREFIXES):
            terms.add(addr.lower())

    terms |= load_local_terms()
    return terms


def load_local_terms(path: Path | None = None) -> set[str]:
    """Extra forbidden terms the deployment config cannot know about.

    Personal accounts are the motivating case: a real AD username sat in a test
    fixture for months because it appears nowhere in config.json, so nothing
    could derive it. Anything matched only by a shape rule would have to be a
    shape, and 'first.last' is far too common a shape to forbid.
    """
    path = path or LOCAL_TERMS_PATH
    if not path.exists():
        return set()
    terms = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        term = line.split("#", 1)[0].strip()
        if term:
            terms.add(term.lower())
    return terms


# Private ranges. A real address only reaches the deny-list if it is still in
# config.json, so a DECOMMISSIONED host's address would slip through — which is
# exactly what happened: 172.28.50.28 belonged to a box deleted on 2026-08-05
# and survived in two planning docs with nothing to catch it.
# Each branch carries its own octet count. Sharing a single "\.\d{1,3}\.\d{1,3}"
# tail across all three is wrong: the 10/8 branch consumes one octet where the
# others consume two, so "10.0.0.5" matched as the 3-octet string "10.0.0" and
# then failed the "10.0.0." allow-prefix test on a truncated value.
_PRIVATE_ADDRESS = re.compile(
    r"\b(?:"
    r"10(?:\.\d{1,3}){3}"
    r"|172\.(?:1[6-9]|2\d|3[01])(?:\.\d{1,3}){2}"
    r"|192\.168(?:\.\d{1,3}){2}"
    r")\b")


def address_findings(line: str) -> list[str]:
    """Private addresses that are not documentation placeholders."""
    return [m.group(0) for m in _PRIVATE_ADDRESS.finditer(line)
            if not m.group(0).startswith(ALLOWED_ADDRESS_PREFIXES)]


def tracked_files() -> list[str]:
    """Tracked files PLUS new files that are not gitignored.

    A brand-new doc is the likeliest place for a fresh leak, and it is not yet
    tracked. Scanning only `git ls-files` would wave it through right up until
    the commit that publishes it.
    """
    out = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
        cwd=REPO_ROOT, capture_output=True, text=True, check=True)
    return sorted({line for line in out.stdout.splitlines() if line.strip()})


def _should_skip(rel_path: str) -> bool:
    lowered = rel_path.lower()
    return (rel_path in SELF
            or lowered.startswith(SKIP_PREFIXES)
            or lowered.endswith(SKIP_SUFFIXES))


def scan_file(rel_path: str, secret_terms: set[str]) -> list[tuple[int, str, str]]:
    """Return [(line_no, rule, offending_text)] for one file."""
    path = REPO_ROOT / rel_path
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []   # binary or unreadable — nothing quotable in it

    findings: list[tuple[int, str, str]] = []
    for line_no, line in enumerate(text.splitlines(), start=1):
        lowered = line.lower()
        for rule, pattern, _why in SHAPE_RULES:
            for match in re.finditer(pattern, line):
                findings.append((line_no, rule, match.group(0)))
        for addr in address_findings(line):
            findings.append((line_no, "private-address", addr))
        for term in secret_terms:
            if term in lowered:
                findings.append((line_no, "config-derived", term))
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--files", nargs="*", default=None,
                        help="scan these paths instead of every tracked file")
    parser.add_argument("--require-config", action="store_true",
                        help="fail if config.json is absent (used by pre-push)")
    args = parser.parse_args(argv)

    secret_terms = load_secret_terms()
    # Check the FILE, not the derived terms: a populated local deny-list would
    # otherwise mask a missing config and let the weaker ruleset through.
    if args.require_config and not CONFIG_PATH.exists():
        print("REFUSING TO PUSH: config.json is missing or empty, so the real "
              "hostnames could not be loaded and the strongest checks did not "
              "run.\nRun the check on a machine with the deployment config.",
              file=sys.stderr)
        return 2

    paths = args.files if args.files is not None else tracked_files()
    paths = [p.replace("\\", "/") for p in paths]

    violations: list[tuple[str, int, str, str]] = []
    for rel_path in paths:
        if _should_skip(rel_path):
            continue
        for line_no, rule, text in scan_file(rel_path, secret_terms):
            violations.append((rel_path, line_no, rule, text))

    if not violations:
        scope = "shape rules only" if not secret_terms else "full"
        print(f"anonymisation check passed ({len(paths)} files, {scope})")
        return 0

    print(f"ANONYMISATION CHECK FAILED — {len(violations)} occurrence(s) name "
          f"the real estate:\n", file=sys.stderr)
    for rel_path, line_no, rule, text in violations:
        print(f"  {rel_path}:{line_no}  [{rule}]  {text}", file=sys.stderr)
    print("\nReplace real hostnames with the fictional fleet, the real domain "
          "with example.com, and real addresses with RFC 5737 documentation "
          "ranges (192.0.2.x / 198.51.100.x / 203.0.113.x).\n"
          "See docs/ANONYMISATION.md.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
