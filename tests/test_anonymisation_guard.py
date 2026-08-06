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


def test_pre_push_hook_refuses_when_python_is_missing():
    """A hook that silently no-ops when its interpreter is absent is worse
    than no hook: it reports success."""
    body = (PROJECT_ROOT / ".githooks" / "pre-push").read_text(encoding="utf-8")
    assert "--require-config" in body
    assert "exit 1" in body
    assert "Refusing to push rather than skipping it." in body


# ── the five holes that made "never again" untrue ────────────────────────

def test_commit_messages_are_scanned():
    """A hostname in a commit message is as public as one in a file.

    On 2026-08-03 exactly that happened while every file was clean, and nothing
    was looking at messages.
    """
    findings = guard.scan_text("chore: bump DESQQQ42 timeout", set())
    assert any(rule == "hostname-convention" for _l, rule, _t in findings)


def test_scan_range_covers_commits_not_just_the_worktree():
    """The tip being clean says nothing about the commits underneath it.

    Three commits went out on 2026-08-06 carrying a file whose cleanup only
    landed in the fourth. A worktree-only check waves that through, and once
    pushed the only remedy is a history rewrite.
    """
    import inspect
    src = inspect.getsource(guard)
    assert "def scan_commit_trees" in src
    assert "def scan_commit_messages" in src
    # and the range must reach both
    main_src = inspect.getsource(guard.main)
    assert "scan_commit_messages" in main_src and "scan_commit_trees" in main_src


def test_git_grep_flags_precede_the_pattern():
    """`git grep -oE pat -i` silently does NOT apply -i.

    Git stops reading flags at the first pattern. That mistake produced a
    false-negative "clean" result which hid five real hostnames, so the built
    argument list must put every flag first.
    """
    import inspect
    src = inspect.getsource(guard.scan_commit_trees)
    build = src[src.index('args = ['):src.index('args += [commit]')]
    assert '"-i"' in build.split('"-e"')[0], "-i must precede the first -e pattern"


def test_filenames_are_scanned_not_just_contents():
    """A path is published even when the blob is spotless.

    "DESFIL10_Deduplication.html" gives away a hostname from the directory
    listing alone, and naming a report after the server it describes is the
    most natural filename anyone could write.
    """
    findings = guard.scan_path("reports/DESQQQ42_Deduplication.html", set())
    assert findings, "a leaking filename must be caught"
    assert all(rule.startswith("path:") for _ln, rule, _t in findings)
    assert all(line == 0 for line, _r, _t in findings), "line 0 marks a name finding"


def test_a_binary_file_still_gets_its_name_checked():
    """Unreadable content is not a reason to skip the part that is published."""
    findings = guard.scan_file("static/img/DESQQQ42-diagram.png", set())
    assert any(rule.startswith("path:") for _ln, rule, _t in findings)


def test_clean_paths_are_not_flagged():
    for path in ("templates/reports.html", "tests/test_incident_dedup.py",
                 "docs/plans/FLEET_REPORT_SPEC.md", "tools/check_anonymised.py"):
        assert guard.scan_path(path, set()) == [], path


def test_redact_masks_the_value_but_keeps_it_locatable():
    """CI runs on a PUBLIC repo — printing the match would publish it.

    GitHub masks a secret's exact full value, not the individual lines inside a
    multi-line secret, so the tool must do its own redaction.
    """
    out = guard.redact("secrethost01")
    assert "secrethost01" not in out
    assert out.startswith("s") and "12 chars" in out
    assert guard.redact("ab") == "**"


def test_hooks_are_tracked_in_the_repo_not_only_in_dot_git():
    """.git/hooks is not cloned. A fresh clone must not be defenceless."""
    for name in ("pre-commit", "pre-push"):
        path = PROJECT_ROOT / ".githooks" / name
        assert path.is_file(), f".githooks/{name} must be tracked"

    import subprocess
    tracked = subprocess.run(["git", "ls-files", ".githooks"], cwd=PROJECT_ROOT,
                             capture_output=True, text=True).stdout
    assert "pre-push" in tracked and "pre-commit" in tracked


def test_hooks_are_executable_in_the_git_index():
    """Git silently refuses to run a hook without the execute bit.

    Windows does not track file mode, so `chmod +x` in the working tree leaves
    the index at 100644 and every Linux/macOS clone gets hooks that never fire —
    no error, no warning, just no protection. This is the fail-open case the
    whole gate exists to avoid, and CI caught it on the first real run.

    Fix with: git update-index --chmod=+x .githooks/<name>
    """
    import subprocess
    out = subprocess.run(["git", "ls-files", "-s", ".githooks"], cwd=PROJECT_ROOT,
                         capture_output=True, text=True, check=True).stdout
    assert out.strip(), ".githooks must be tracked"
    for line in out.splitlines():
        mode, _rest = line.split(" ", 1)
        assert mode == "100755", f"not executable in the index: {line}"


def test_ci_scans_all_history_not_just_the_root_commit():
    """`git rev-list <root>` walks the ANCESTORS of the root — i.e. only the
    root. CI reported success having scanned 1 commit of 4."""
    ci = (PROJECT_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "--range HEAD" in ci
    assert "--max-parents=0" not in ci, "that expression scans one commit, not history"


def test_pre_push_hook_scans_the_pushed_range_and_demands_config():
    body = (PROJECT_ROOT / ".githooks" / "pre-push").read_text(encoding="utf-8")
    assert "--range" in body, "must scan the commits being pushed"
    assert "--require-config" in body, "a missing config must fail the push"
    assert "remote_sha" in body, "must read git's stdin protocol, not guess"
    assert "Refusing to push rather than skipping it." in body


def test_pre_commit_hook_does_not_hard_require_config():
    """A contributor without the deployment config should still get shape rules
    on every commit, not a hard stop. pre-push is where it becomes fatal."""
    body = (PROJECT_ROOT / ".githooks" / "pre-commit").read_text(encoding="utf-8")
    # Strip comments — the hook explains in prose WHY it omits the flag, and a
    # naive substring search matches that explanation rather than the command.
    code = "\n".join(l for l in body.splitlines() if not l.lstrip().startswith("#"))
    assert "--require-config" not in code
    assert "--diff-filter=ACM" in code, "only inspect content that exists"


def test_ci_runs_the_check_so_a_missing_hook_is_not_fatal():
    """Local hooks are convenience; CI is the layer that survives a clone that
    never ran install_hooks.py, or a push with --no-verify."""
    ci = (PROJECT_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "check_anonymised.py" in ci
    assert "--redact" in ci, "CI output is public; matches must be masked"
    assert "fetch-depth: 0" in ci, "needs history to scan commits and messages"


def test_install_hooks_uses_hookspath_rather_than_copying():
    """Copies in .git/hooks drift from the versioned originals."""
    from tools import install_hooks
    import inspect
    src = inspect.getsource(install_hooks)
    assert "core.hooksPath" in src
    assert install_hooks.HOOK_NAMES == ("pre-commit", "pre-push")


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
