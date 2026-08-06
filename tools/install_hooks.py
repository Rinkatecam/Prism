"""Install Prism's git hooks into this clone.

    python tools/install_hooks.py

Git hooks live in .git/hooks, which is not version-controlled, so a hook cannot
simply be committed — every clone has to install it. This script does that and
is safe to re-run.

Currently installs:

  pre-push — refuses to push anything that names the real estate.
             See docs/ANONYMISATION.md.
"""

from __future__ import annotations

import stat
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# POSIX sh: works with Git Bash on Windows, which is what git invokes for hooks.
PRE_PUSH = """#!/bin/sh
# Prism pre-push hook — installed by tools/install_hooks.py. Do not edit here;
# edit the template in tools/install_hooks.py and re-run it.
#
# Blocks any push that would publish real hostnames, the real AD domain, real
# accounts or real addresses. See docs/ANONYMISATION.md.

if command -v python >/dev/null 2>&1; then
  PY=python
elif command -v python3 >/dev/null 2>&1; then
  PY=python3
else
  echo "pre-push: no python on PATH — cannot run the anonymisation check." >&2
  echo "Refusing to push rather than skipping it." >&2
  exit 1
fi

"$PY" "$(git rev-parse --show-toplevel)/tools/check_anonymised.py" --require-config
status=$?
if [ $status -ne 0 ]; then
  echo "" >&2
  echo "PUSH BLOCKED by the anonymisation check." >&2
  echo "Fix the occurrences above, or read docs/ANONYMISATION.md." >&2
  exit $status
fi
exit 0
"""

HOOKS = {"pre-push": PRE_PUSH}


def hooks_dir() -> Path:
    """Resolve .git/hooks, honouring worktrees and core.hooksPath."""
    try:
        configured = subprocess.run(
            ["git", "config", "--get", "core.hooksPath"],
            cwd=REPO_ROOT, capture_output=True, text=True, check=False)
        if configured.returncode == 0 and configured.stdout.strip():
            path = Path(configured.stdout.strip())
            return path if path.is_absolute() else REPO_ROOT / path
        common = subprocess.run(["git", "rev-parse", "--git-common-dir"],
                                cwd=REPO_ROOT, capture_output=True,
                                text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        raise SystemExit(f"not a git repository, or git is unavailable: {exc}")
    git_dir = Path(common)
    if not git_dir.is_absolute():
        git_dir = REPO_ROOT / git_dir
    return git_dir / "hooks"


def main() -> int:
    target_dir = hooks_dir()
    target_dir.mkdir(parents=True, exist_ok=True)

    for name, body in HOOKS.items():
        target = target_dir / name
        if target.exists() and target.read_text(encoding="utf-8") == body:
            print(f"  {name}: already current")
            continue
        if target.exists():
            backup = target.with_suffix(".prism-replaced")
            backup.write_text(target.read_text(encoding="utf-8"), encoding="utf-8")
            print(f"  {name}: existing hook saved to {backup.name}")
        target.write_text(body, encoding="utf-8", newline="\n")
        target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP)
        print(f"  {name}: installed")

    print(f"\nhooks installed into {target_dir}")
    print("Verify with:  python tools/check_anonymised.py --require-config")
    return 0


if __name__ == "__main__":
    sys.exit(main())
