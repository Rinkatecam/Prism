"""Apply the colour token mapping to template files. Idempotent.

    python tools/migrate_tokens.py --check templates/reports.html
    python tools/migrate_tokens.py templates/reports.html

Always run `tools/verify_token_equivalence.py` afterwards. This script reports
how many literals it rewrote; only the verifier can tell you whether the result
renders the same colours. The two questions are different, and the converter
had four bugs that "N literals rewritten" would have reported as success.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools import design_tokens as dt  # noqa: E402

_LITERAL = re.compile(r"-\[#[0-9A-Fa-f]{6}\]")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+", help="template files to convert")
    parser.add_argument("--check", action="store_true",
                        help="report what would change, write nothing")
    parser.add_argument("--paired-only", action="store_true",
                        help="convert only utilities that already have a dark: "
                             "counterpart, so both themes render identically. "
                             "The unpaired ones change dark mode and get their "
                             "own commit — see DESIGN_TOKENS_SPEC.md §5.")
    args = parser.parse_args()

    changed = total_before = total_after = 0

    for raw in args.paths:
        path = Path(raw)
        if not path.is_file():
            print(f"  skipped (not a file): {path}", file=sys.stderr)
            continue
        before = path.read_text(encoding="utf-8")
        after = dt.convert(before, paired_only=args.paired_only)
        n_before = len(_LITERAL.findall(before))
        n_after = len(_LITERAL.findall(after))
        total_before += n_before
        total_after += n_after
        if before == after:
            continue
        changed += 1
        print(f"  {path.as_posix()}: {n_before - n_after} tokenised, "
              f"{n_after} literal(s) remain")
        if not args.check:
            path.write_text(after, encoding="utf-8")

    verb = "would change" if args.check else "changed"
    print(f"\n{changed} file(s) {verb}; "
          f"literals {total_before} -> {total_after}")
    if not args.check and changed:
        print("Now run: python tools/verify_token_equivalence.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
