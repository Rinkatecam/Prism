"""Supply-chain integrity tests.

Filesystem-only checks that the hash-pinned lockfile exists, contains hashes,
and covers every direct dependency declared in requirements.txt.

These tests do NOT install anything. They are purely a guard against the
lockfile being deleted, replaced with an unhashed version, or going out
of sync with the human-edited requirements.txt.

See docs/DEPENDENCIES.md for the operator workflow.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
REQUIREMENTS_TXT = REPO_ROOT / "requirements.txt"
REQUIREMENTS_LOCK = REPO_ROOT / "requirements.lock"

# PEP 508 / pip requirement spec: leading package name is letters/digits/-/_/.
_PKG_NAME_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)")


def _normalize(name: str) -> str:
    """PEP 503 normalization: lowercase, runs of [-_.] collapse to '-'."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _parse_top_level(path: Path) -> list[str]:
    """Return normalized package names from a pip-style requirements file,
    ignoring blank lines, comments, and environment markers."""
    names: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        # Drop env markers ("; sys_platform == 'win32'") for name extraction.
        head = stripped.split(";", 1)[0].strip()
        m = _PKG_NAME_RE.match(head)
        if m:
            names.append(_normalize(m.group(1)))
    return names


def test_requirements_lock_exists() -> None:
    assert REQUIREMENTS_LOCK.is_file(), (
        f"{REQUIREMENTS_LOCK} is missing. Regenerate with:\n"
        "  pip-compile --generate-hashes --strip-extras --no-emit-trusted-host "
        "--output-file=requirements.lock requirements.txt"
    )


def test_requirements_lock_has_hashes() -> None:
    """The lockfile must use --hash=sha256:... pins; an unhashed lock is
    no better than the unpinned requirements.txt for supply-chain defence."""
    text = REQUIREMENTS_LOCK.read_text(encoding="utf-8")
    assert "--hash=sha256:" in text, (
        "requirements.lock contains no '--hash=sha256:' lines — it was "
        "generated without --generate-hashes. Regenerate with hashes; an "
        "unhashed lock provides no integrity guarantee."
    )


def test_requirements_lock_covers_main_deps() -> None:
    """Every direct dependency in requirements.txt must appear in
    requirements.lock. Catches the case where a direct dep was added
    to requirements.txt but pip-compile was not re-run."""
    top_level = _parse_top_level(REQUIREMENTS_TXT)
    assert top_level, "requirements.txt parsed as empty — parser bug"

    lock_text = REQUIREMENTS_LOCK.read_text(encoding="utf-8")
    # Pip-compile emits each pinned package as "name==version \" at column 0.
    locked_names: set[str] = set()
    for line in lock_text.splitlines():
        if line and not line.startswith((" ", "\t", "#")):
            m = _PKG_NAME_RE.match(line)
            if m and "==" in line:
                locked_names.add(_normalize(m.group(1)))

    missing = [name for name in top_level if name not in locked_names]
    assert not missing, (
        f"requirements.txt declares {missing!r} but they are not pinned in "
        "requirements.lock. Regenerate the lockfile: pip-compile --generate-hashes "
        "--strip-extras --no-emit-trusted-host --output-file=requirements.lock "
        "requirements.txt"
    )
