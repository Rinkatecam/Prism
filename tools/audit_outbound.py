"""Enumerate every call site in Prism that can open an outbound connection.

    python tools/audit_outbound.py              # grouped report
    python tools/audit_outbound.py --json       # machine-readable, for the test
    python tools/audit_outbound.py --unlisted   # only sites not in the baseline

WHY THIS IS AN AST WALK AND NOT A GREP
--------------------------------------
A grep for `urlopen` finds the call and cannot tell you WHERE IT CONNECTS TO,
which is the only question that matters here. It also fires on the word inside
a comment or a docstring — this repository has shipped four text-scanning
checks that failed on their own documentation, and a check that can be silenced
by deleting the paragraph explaining it is worse than no check.

So this resolves the call target through the module's own imports and records
the DESTINATION EXPRESSION as source text. `WSMan(server_config.host)` and
`WSMan("updates.example.com")` are the same grep hit and completely different
facts.

WHAT "OUTBOUND" MEANS HERE
-------------------------
A call that can cause this process to send bytes to another host. Deliberately
INCLUDED even though they are harmless, because a reviewer greps for them and
must find them already accounted for:

  * `socket.socket(...)` used for a Wake-on-LAN broadcast — a hardcoded
    destination, and provably a local one (see the report's notes).
  * `ssl.wrap_socket` / `SSLContext.wrap_socket` — wraps an existing socket
    rather than opening one, but it is what makes a TLS check a TLS check.

Deliberately EXCLUDED, with reasons, because including them would bury the
signal:

  * `socket.gethostname()` and friends — local lookups, no packet.
  * Flask/waitress LISTENING sockets. Inbound is a different question and is
    covered by the route-authentication audit, not by this file.
  * Anything under `tests/`. A test that opens a socket is not a shipped
    outbound path. `--include-tests` overrides this when auditing the tests
    themselves.

WHAT THIS CANNOT SEE, stated because a check's blind spots are the first thing
to overclaim:

  * A connection made by a DEPENDENCY on its own initiative. This walks Prism's
    own source. `pypsrp`, `ldap3`, `reportlab` and `cryptography` are trusted
    to do only what they are asked; that trust is evidence from the dependency
    audit (T3), not from this tool.
  * A destination assembled at runtime. It reports the EXPRESSION, so
    `urlopen(url)` is reported as `url` and a human still has to trace it. The
    report flags any destination that is a bare string literal, which is the
    case that needs no tracing and the one that would be a finding.
  * `subprocess` invoking something network-capable. Covered separately —
    `ps_sandbox.py` allowlists the PowerShell cmdlets that may run.
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from dataclasses import dataclass, asdict
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Directories never walked. `.claude` holds worktrees — a stale copy of the
# whole app, which would double every finding.
SKIP_DIRS = {".git", ".claude", "__pycache__", "node_modules", "venv", ".venv",
             "instance", "data", "static", "templates", "docs"}

# (module, attribute) -> what the call does. The module part is matched against
# the name the file actually imported, so `import urllib.request as ur` and
# `from urllib.request import urlopen` both resolve.
OUTBOUND_CALLS: dict[tuple[str, str], str] = {
    ("socket", "create_connection"): "TCP connect",
    ("socket", "socket"): "raw socket",
    ("smtplib", "SMTP"): "SMTP connect",
    ("smtplib", "SMTP_SSL"): "SMTP connect (TLS)",
    ("urllib.request", "urlopen"): "HTTP(S) request",
    ("urllib.request", "Request"): "HTTP(S) request object",
    ("http.client", "HTTPConnection"): "HTTP connect",
    ("http.client", "HTTPSConnection"): "HTTPS connect",
    ("ldap3", "Connection"): "LDAP bind",
    ("ldap3", "Server"): "LDAP server handle",
    ("pypsrp.wsman", "WSMan"): "WinRM transport",
    ("requests", "get"): "HTTP GET",
    ("requests", "post"): "HTTP POST",
    ("requests", "request"): "HTTP request",
}

# Bare callables that carry their own meaning regardless of module, for the
# `from x import y` case where the module name is gone by the call site.
BARE_CALLS = {name: what for (_mod, name), what in OUTBOUND_CALLS.items()}


@dataclass(frozen=True)
class Site:
    path: str
    line: int
    call: str
    kind: str
    destination: str        # source text of the first argument
    literal_destination: bool   # True when it is a bare string constant


class _Walker(ast.NodeVisitor):
    def __init__(self, path: str) -> None:
        self.path = path
        self.sites: list[Site] = []
        # local alias -> dotted module, built from this file's own imports
        self.aliases: dict[str, str] = {}
        # local name -> dotted module it was imported FROM
        self.from_imports: dict[str, str] = {}

    def visit_Import(self, node: ast.Import) -> None:
        for a in node.names:
            self.aliases[a.asname or a.name.split(".")[0]] = a.name
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.module:
            for a in node.names:
                self.from_imports[a.asname or a.name] = node.module
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        target = self._resolve(node.func)
        if target:
            call_text, kind = target
            dest, is_literal = self._destination(node)
            self.sites.append(Site(self.path, node.lineno, call_text, kind,
                                   dest, is_literal))
        self.generic_visit(node)

    def _resolve(self, func: ast.AST) -> tuple[str, str] | None:
        # attribute form: socket.create_connection(...), urllib.request.urlopen(...)
        if isinstance(func, ast.Attribute):
            base = _dotted(func.value)
            if base is None:
                return None
            full = self.aliases.get(base.split(".")[0], base.split(".")[0])
            rest = base.split(".", 1)[1] if "." in base else ""
            module = f"{full}.{rest}" if rest else full
            for (mod, attr), kind in OUTBOUND_CALLS.items():
                if attr == func.attr and _module_matches(module, mod):
                    return f"{module}.{func.attr}", kind
            return None
        # bare name form: urlopen(...), WSMan(...), after `from x import y`
        if isinstance(func, ast.Name):
            name = func.id
            if name in self.from_imports and name in BARE_CALLS:
                return f"{self.from_imports[name]}.{name}", BARE_CALLS[name]
        return None

    @staticmethod
    def _destination(node: ast.Call) -> tuple[str, bool]:
        # `socket.socket(AF_INET, SOCK_DGRAM)` takes a family, not an address.
        # Reporting arg 0 would print `socket.AF_INET` under a column headed
        # "destination", which is worse than printing nothing — it reads like
        # an answer. The address appears later, at .connect()/.sendto().
        if isinstance(node.func, ast.Attribute) and node.func.attr == "socket":
            return "(family only — address is at the send call)", False
        if not node.args:
            kwargs = {k.arg: k.value for k in node.keywords if k.arg}
            for key in ("host", "url", "server", "address"):
                if key in kwargs:
                    return _src(kwargs[key]), isinstance(kwargs[key], ast.Constant)
            return "(no positional destination)", False
        first = node.args[0]
        return _src(first), isinstance(first, ast.Constant) and isinstance(first.value, str)


def _module_matches(observed: str, target: str) -> bool:
    """Does `observed` name the module `target`, exactly or as a submodule?

    NOT a prefix test. The first version of this used
    `target.startswith(observed)` and matched **every `r.get(...)` dict access
    in the repository** against `requests.get`, because "requests" starts with
    "r". It reported 43 outbound call sites where there are far fewer, and the
    inventory built on it would have been fiction. A one-letter local variable
    is a prefix of half the standard library; only a dotted boundary counts.
    """
    if observed == target:
        return True
    return observed.startswith(target + ".") or target.startswith(observed + ".")


def _dotted(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value)
        return f"{base}.{node.attr}" if base else None
    return None


def _src(node: ast.AST) -> str:
    try:
        return ast.unparse(node)
    except Exception:
        return f"<{type(node).__name__}>"


def scan(include_tests: bool = False) -> list[Site]:
    sites: list[Site] = []
    for path in sorted(PROJECT_ROOT.rglob("*.py")):
        rel_parts = path.relative_to(PROJECT_ROOT).parts
        if any(p in SKIP_DIRS for p in rel_parts):
            continue
        if not include_tests and rel_parts[0] == "tests":
            continue
        if path.name == "audit_outbound.py":
            continue        # this file names every call it looks for
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError:
            continue
        w = _Walker(path.relative_to(PROJECT_ROOT).as_posix())
        w.visit(tree)
        sites.extend(w.sites)
    return sites


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--include-tests", action="store_true")
    args = ap.parse_args()

    sites = scan(include_tests=args.include_tests)

    if args.json:
        print(json.dumps([asdict(s) for s in sites], indent=2))
        return 0

    by_file: dict[str, list[Site]] = {}
    for s in sites:
        by_file.setdefault(s.path, []).append(s)

    print(f"{len(sites)} outbound call sites across {len(by_file)} files\n")
    for path in sorted(by_file):
        print(path)
        for s in sorted(by_file[path], key=lambda x: x.line):
            flag = "  <-- LITERAL DESTINATION" if s.literal_destination else ""
            print(f"  {s.line:>5}  {s.kind:<22} -> {s.destination}{flag}")
        print()

    literals = [s for s in sites if s.literal_destination]
    print(f"literal destinations: {len(literals)}")
    for s in literals:
        print(f"  {s.path}:{s.line}  {s.destination}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
