"""The gate that stops the real estate being published.

Prism is developed against production and released openly. `tools/check_anonymised.py`
is the last thing between a real hostname and a public repository, so it needs
tests of its own — a guard that silently stops matching is worse than no guard,
because it also removes the habit of checking by hand.

Two properties matter most and are each pinned below:

  * it FIRES on real identifiers (a guard that passes everything is useless), and
  * it stays QUIET on the fictional fleet and on legitimate placeholders (a
    guard that cries wolf gets bypassed, and a bypassed guard is no guard).

These tests must pass on CI, which has no config.json and therefore no
config-derived terms. Anything needing real values constructs them locally.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools import check_anonymised as guard  # noqa: E402


# ── shape rules: fire on the real convention, quiet on the fictional fleet ──

# SYNTHETIC names that match the estate's convention without being real
# machines. Using actual hostnames here would make this file the leak it is
# meant to prevent — and the checker scans tests/ too, so it would fail itself.
@pytest.mark.parametrize("text", [
    "DESAAA01", "DESZZ99", "the DESQQQ42 incident", "DESBB07 in a sentence",
])
def test_hostname_convention_is_caught(text):
    """Caught by shape alone, so a DECOMMISSIONED host is still caught.

    This matters: the config-derived terms only know hosts that are currently
    deployed. A machine deleted from the fleet keeps its name in the planning
    docs, and nothing else would catch it.
    """
    findings = _scan_text(text)
    assert any(rule == "hostname-convention" for _ln, rule, _t in findings), text


@pytest.mark.parametrize("text", [
    "FILE01", "DC01", "DC02", "APP01", "APP02", "SQL01", "MAIL01",
    "MGT01", "LAB01", "STANDALONE01", "ad.example.com", "DC=EXAMPLE,DC=COM",
    "a.admin", "svc@ad.example.com",
])
def test_fictional_fleet_is_not_flagged(text):
    """docs/ANONYMISATION.md tells people to write these. If the checker
    rejected its own recommended replacements it would be unusable."""
    assert _scan_text(text) == [], text


@pytest.mark.parametrize("text,flagged", [
    ("10.0.0.5", False),            # long-standing test fixture
    ("192.168.1.50", False),        # long-standing docs placeholder
    ("198.51.100.28", False),       # RFC 5737
    ("192.0.2.7", False),
    ("203.0.113.9", False),
    ("127.0.0.1", False),
    ("10.5.6.7", True),
    ("192.168.4.9", True),
    ("172.31.7.7", True),           # RFC 1918, outside the placeholder ranges
    ("172.15.0.1", False),          # outside RFC 1918
    ("lucide 10.702.5", False),     # version string, not an address
    ("version 10.0.0", False),      # three octets is not an address
])
def test_private_address_rule(text, flagged):
    """The 10/8 branch originally shared an octet count with the others, so
    '10.0.0.5' matched as the truncated string '10.0.0' and then failed its
    own allow-prefix test."""
    assert bool(guard.address_findings(text)) is flagged, text


@pytest.mark.parametrize("text,flagged", [
    ("DC=ACMECORP,DC=COM", True),
    ("DC=EXAMPLE,DC=COM", False),
    ("DC=example,DC=com", False),   # the exemption must be case-insensitive
    ("DC=TEST,DC=LOCAL", False),
    ("DC=AD,DC=COM", False),        # too short to be distinctive
])
def test_ldap_dn_rule(text, flagged):
    findings = _scan_text(text)
    assert any(r == "ldap-dn-with-company-dc" for _l, r, _t in findings) is flagged, text


# ── config-derived terms ─────────────────────────────────────────────────

def test_generic_and_protocol_labels_never_become_secrets(tmp_path):
    """Splitting an LDAP URL on punctuation yields 'ldap'.

    Measured: with 'ldap' in the deny-list the checker reported 715
    violations, ~700 of them legitimate lines in auth.py and the settings UI.
    A checker that noisy gets switched off.
    """
    config = tmp_path / "config.json"
    config.write_text("""{
      "servers": [{"name": "ACMEFS01", "host": "ACMEFS01.AD.ACMECORP.COM",
                   "username": "ACMECORP\\\\svcmonitor"}],
      "settings": {"auth": {"ldap_url": "ldap://acmedc01.ad.acmecorp.com:389",
                            "ldap_base_dn": "DC=AD,DC=ACMECORP,DC=COM",
                            "ldap_user_filter": "(sAMAccountName={username})"}}
    }""", encoding="utf-8")

    terms = guard.load_secret_terms(config)

    for poisonous in ("ldap", "ldaps", "http", "com", "net", "local", "ad",
                      "dc", "user", "users", "host", "server", "admin"):
        assert poisonous not in terms, f"{poisonous!r} would flag the whole repo"
    # sAMAccountName comes from ldap_user_filter, which is LDAP vocabulary.
    assert "samaccountname" not in terms


def test_real_identifiers_do_become_secrets(tmp_path):
    config = tmp_path / "config.json"
    config.write_text("""{
      "servers": [{"name": "ACMEFS01", "host": "ACMEFS01.AD.ACMECORP.COM",
                   "username": "ACMECORP\\\\svcmonitor"}],
      "settings": {"auth": {"ldap_url": "ldap://acmedc01.ad.acmecorp.com:389",
                            "ldap_base_dn": "DC=AD,DC=ACMECORP,DC=COM"}}
    }""", encoding="utf-8")

    terms = guard.load_secret_terms(config)

    for expected in ("acmefs01", "acmecorp", "svcmonitor", "acmedc01"):
        assert expected in terms, expected


def test_addresses_in_config_become_secrets_but_placeholders_do_not(tmp_path):
    config = tmp_path / "config.json"
    config.write_text(
        '{"servers": [{"name": "ACMEFS01", "host": "172.20.9.9"},'
        ' {"name": "ACMEFS02", "host": "10.0.0.4"}], "settings": {}}',
        encoding="utf-8")

    terms = guard.load_secret_terms(config)

    assert "172.20.9.9" in terms
    assert "10.0.0.4" not in terms, "documentation placeholders stay allowed"


def test_missing_config_yields_no_config_derived_terms(tmp_path):
    """CI has no config.json. It must still run, on shape rules alone."""
    assert guard.load_secret_terms(tmp_path / "absent.json") <= guard.load_local_terms()


def test_local_denylist_is_parsed(tmp_path):
    path = tmp_path / "denylist"
    path.write_text("# a comment\n\nj.doe\nSomeCodename  # trailing note\n",
                    encoding="utf-8")

    terms = guard.load_local_terms(path)

    assert terms == {"j.doe", "somecodename"}


def test_local_denylist_is_gitignored():
    """It holds the real values. Committing it publishes them."""
    import subprocess
    result = subprocess.run(
        ["git", "check-ignore", ".anonymisation-denylist"],
        cwd=PROJECT_ROOT, capture_output=True, text=True)
    assert result.returncode == 0, ".anonymisation-denylist must be gitignored"


# ── the gate itself ──────────────────────────────────────────────────────

def test_require_config_refuses_when_config_is_absent(tmp_path, monkeypatch, capsys):
    """The hook passes --require-config so a missing config FAILS the push
    instead of quietly downgrading to the weaker shape-only ruleset."""
    monkeypatch.setattr(guard, "CONFIG_PATH", tmp_path / "absent.json")

    assert guard.main(["--require-config", "--files"]) == 2
    assert "REFUSING TO PUSH" in capsys.readouterr().err


def test_checker_skips_itself():
    """This module necessarily contains the shapes it forbids."""
    assert guard._should_skip("tools/check_anonymised.py")
    assert guard._should_skip("static/vendor/lucide-0.344.0.js")
    assert not guard._should_skip("analytics.py")


def test_scan_covers_new_files_not_yet_tracked():
    """A brand-new planning doc is the likeliest place for a fresh leak, and
    it is untracked right up until the commit that publishes it."""
    import subprocess
    listed = set(guard.tracked_files())
    cmd = ["git", "ls-files", "--others", "--exclude-standard"]
    untracked = {p for p in subprocess.run(
        cmd, cwd=PROJECT_ROOT, capture_output=True, text=True,
        check=True).stdout.splitlines() if p.strip()}
    assert untracked <= listed


def test_the_repository_is_currently_clean():
    """The whole point. Runs the full check the hook runs.

    On CI this is shape rules only; on a developer machine it also uses the
    config-derived terms. Either way a real hostname committed to this repo
    fails the build.
    """
    assert guard.main([]) == 0


def test_pre_push_hook_template_refuses_when_python_is_missing():
    """A hook that silently no-ops when its interpreter is absent is worse
    than no hook: it reports success."""
    from tools.install_hooks import PRE_PUSH
    assert "--require-config" in PRE_PUSH
    assert "exit 1" in PRE_PUSH
    assert "Refusing to push rather than skipping it." in PRE_PUSH


def _scan_text(text: str) -> list[tuple[int, str, str]]:
    """Run the shape rules over a literal string, with no config-derived terms."""
    findings = []
    import re
    for rule, pattern, _why in guard.SHAPE_RULES:
        for match in re.finditer(pattern, text):
            findings.append((1, rule, match.group(0)))
    for addr in guard.address_findings(text):
        findings.append((1, "private-address", addr))
    return findings
