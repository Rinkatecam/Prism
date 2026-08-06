"""Point this clone's git hooks at the tracked .githooks/ directory.

    python tools/install_hooks.py          # enable
    python tools/install_hooks.py --check  # verify, non-zero if not enabled

Git hooks live in .git/hooks, which is NOT cloned — so a hook cannot simply be
committed, and every clone starts with no protection. Rather than copying files
into .git/hooks (where they immediately drift from the versioned copy), this
sets `core.hooksPath` to the tracked .githooks/ directory. One setting, and the
hooks are whatever the repository says they are.

Installs:

  pre-commit — catch a leak before it enters a commit
  pre-push   — refuse to publish anything that names the real estate

See docs/ANONYMISATION.md.
"""

from __future__ import annotations

import argparse
import stat
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOKS_DIR = REPO_ROOT / ".githooks"
HOOK_NAMES = ("pre-commit", "pre-push")


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=REPO_ROOT,
                          capture_output=True, text=True, check=False)


def configured_path() -> str:
    result = _git("config", "--get", "core.hooksPath")
    return result.stdout.strip() if result.returncode == 0 else ""


def is_enabled() -> bool:
    return configured_path() in (".githooks", str(HOOKS_DIR))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true",
                        help="report status instead of changing anything")
    args = parser.parse_args(argv)

    missing = [n for n in HOOK_NAMES if not (HOOKS_DIR / n).is_file()]
    if missing:
        print(f"missing hook script(s) in .githooks/: {', '.join(missing)}",
              file=sys.stderr)
        return 2

    if args.check:
        if is_enabled():
            print("git hooks are enabled (core.hooksPath = .githooks)")
            return 0
        current = configured_path() or "<unset, using .git/hooks>"
        print(f"git hooks are NOT enabled — core.hooksPath is {current}\n"
              f"Run: python tools/install_hooks.py", file=sys.stderr)
        return 1

    if _git("config", "core.hooksPath", ".githooks").returncode != 0:
        print("could not set core.hooksPath", file=sys.stderr)
        return 2

    # Git needs the execute bit on POSIX. On Windows it is ignored, and setting
    # it is harmless.
    for name in HOOK_NAMES:
        path = HOOKS_DIR / name
        path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP)
        print(f"  {name}: enabled")

    # A stale copy in .git/hooks is now dead code — core.hooksPath overrides the
    # whole directory — but leaving it there invites someone to edit the wrong
    # file. Say so rather than deleting anything.
    legacy = REPO_ROOT / ".git" / "hooks"
    stale = [n for n in HOOK_NAMES if (legacy / n).is_file()]
    if stale:
        print(f"\nnote: .git/hooks still contains {', '.join(stale)}. "
              f"core.hooksPath now takes precedence, so those copies are "
              f"inert — edit .githooks/ instead, and delete the old ones when "
              f"convenient.")

    print(f"\ncore.hooksPath = .githooks")
    print("Verify with:  python tools/install_hooks.py --check")
    return 0


if __name__ == "__main__":
    sys.exit(main())
