"""Tests for sortable table columns (static/js/table-sort.js).

WHAT THIS FILE DOES AND DOES NOT EXECUTE
-----------------------------------------
There is no Node.js (or any other JS runtime — checked: no selenium/
playwright/js2py/PyMiniRacer in this environment, and this repo's test
suite runs under plain `python -m pytest`) available to this test suite, so
the actual `table-sort.js` cannot be *run*. Per docs/OPS-LEARNINGS.md §2.2
("a test suite written from one mental model tests one shape... run the
transformation over the real input") and §3 ("a test whose verdict is
computed by your own helper inherits that helper's bugs"), re-implementing
the parsers in Python here and testing THAT would be exactly the trap the
codebase's own history warns about — the test would pass forever even if
the shipped regex were wrong, because it would never look at the shipped
regex.

So instead, every "content type" test below EXTRACTS the literal regex (or
unit-multiplier table) from the real `static/js/table-sort.js` source text
and runs it with Python's `re` — the patterns are plain character-class /
group regex with no JS-specific syntax, so they are byte-for-byte
executable as Python regex. A change to the shipped pattern is what these
tests are exercising; a hand-duplicated pattern in this file would not be.

The interactive behaviour that genuinely needs a DOM (click handling, row
reordering, aria-sort, sessionStorage, the MutationObserver) is verified
separately against the real running app with the browser tooling — see the
task report for that evidence; it is not repeated here as a fake pytest
assertion.

The other half of this file is purely structural: it parses the actual
template sources (also plain text — several of the tables are built via JS
string concatenation, not server-rendered HTML, so an HTML parser would not
see them) and asserts the exact, column-by-column `data-sort` wiring this
task decided on, including a NEGATIVE assertion for every column and every
table that was deliberately left unsorted. A wiring test with no negative
case is the "guardrail that finds nothing" trap from OPS-LEARNINGS #15.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

JS_PATH = PROJECT_ROOT / "static" / "js" / "table-sort.js"
BASE_HTML = PROJECT_ROOT / "templates" / "base.html"


@pytest.fixture(scope="module")
def js_source() -> str:
    return JS_PATH.read_text(encoding="utf-8")


# ── extraction helpers: pull the REAL pattern out of the shipped file ──────

def _extract_regex(source: str, marker: str, occurrence: int = 1) -> "re.Pattern[str]":
    """Find the Nth `/pattern/flags` regex literal appearing after `marker`.

    None of table-sort.js's regex literals contain a literal `/` inside the
    pattern (checked by eye — they're character classes and groups only),
    so the first `/` after the marker opens the pattern and the next `/`
    closes it, exactly as the JS engine itself would tokenise it.
    """
    idx = source.index(marker)
    pos = idx
    for _ in range(occurrence):
        start = source.index("/", pos)
        end = source.index("/", start + 1)
        pos = end + 1
    pattern = source[start + 1:end]
    flag_str = re.match(r"[a-z]*", source[end + 1:]).group(0)
    flags = re.IGNORECASE if "i" in flag_str else 0
    return re.compile(pattern, flags)


def _extract_object_literal(source: str, marker: str) -> dict:
    """Parse a `var NAME = { k: expr, ... };` object literal into a dict of
    evaluated numbers, straight out of the real source (not retyped here).
    """
    idx = source.index(marker)
    start = source.index("{", idx)
    depth = 0
    end = None
    for i in range(start, len(source)):
        if source[i] == "{":
            depth += 1
        elif source[i] == "}":
            depth -= 1
            if depth == 0:
                end = i
                break
    assert end is not None, f"unterminated object literal after {marker!r}"
    body = source[start + 1:end]

    # Split on top-level commas only — BYTE_MULT's values include
    # `Math.pow(1024, 4)`, whose internal comma must not be mistaken for a
    # key/value pair separator.
    parts, buf, paren_depth = [], [], 0
    for ch in body:
        if ch == "(":
            paren_depth += 1
        elif ch == ")":
            paren_depth -= 1
        if ch == "," and paren_depth == 0:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    if buf:
        parts.append("".join(buf))

    out = {}
    for part in parts:
        part = part.strip()
        if not part:
            continue
        key, expr = part.split(":", 1)
        expr = re.sub(r"Math\.pow\((\d+),\s*(\d+)\)", r"(\1**\2)", expr.strip())
        out[key.strip()] = eval(expr, {"__builtins__": {}}, {})  # noqa: S307 — source-derived arithmetic only
    return out


# ── content type: number / percent ─────────────────────────────────────────

def test_number_regex_extracted_from_source_orders_percentages_correctly(js_source):
    """The bug this type exists to fix: a naive string sort puts "10%"
    before "9%". Extract the real parseNumber() regex and prove it doesn't."""
    rx = _extract_regex(js_source, "function parseNumber(text)")
    nine = float(rx.search("9%").group(0))
    ten = float(rx.search("10%").group(0))
    assert nine == 9.0 and ten == 10.0
    assert nine < ten  # numeric order — a string compare would disagree


def test_number_regex_handles_trend_slope_shape(js_source):
    """compare-stats' Trend slope cell is "↑ -0.012%/d" — a leading arrow
    glyph, then a signed decimal, then a unit suffix. The regex must find
    the signed number and ignore both the arrow and the suffix."""
    rx = _extract_regex(js_source, "function parseNumber(text)")
    m = rx.search("↑ -0.012%/d")
    assert m and float(m.group(0)) == -0.012


def test_number_regex_treats_placeholder_as_no_match(js_source):
    rx = _extract_regex(js_source, "function parseNumber(text)")
    assert rx.search("—") is None  # em dash placeholder — no digits


# ── content type: bytes ─────────────────────────────────────────────────────

def test_bytes_parsing_orders_kb_before_mb(js_source):
    """"900 KB" must sort before "1 MB" — a plain numeric-prefix compare
    would put 900 before 1 the wrong way around."""
    rx = _extract_regex(js_source, "function parseBytes(text)")
    mult = _extract_object_literal(js_source, "var BYTE_MULT")

    def to_bytes(text):
        m = rx.search(text)
        n = float(m.group(1).replace(",", ""))
        unit = (m.group(2) or "B").upper()
        return n * mult[unit]

    assert to_bytes("900 KB") < to_bytes("1 MB")
    assert to_bytes("1 MB") < to_bytes("1 GB") < to_bytes("1 TB")


def test_byte_multiplier_table_is_binary_1024_based(js_source):
    """operations.html's own backup-size code divides by 1024
    (`size_bytes / 1024`) to render KB — the sort parser must use the same
    base or "12.0 KB" and the size that produced it stop agreeing."""
    mult = _extract_object_literal(js_source, "var BYTE_MULT")
    assert mult["B"] == 1
    assert mult["KB"] == 1024
    assert mult["MB"] == 1024 * 1024
    assert mult["GB"] == 1024 * 1024 * 1024
    assert mult["TB"] == 1024 ** 4


def test_bytes_regex_treats_bare_hyphen_as_no_match(js_source):
    """server_detail.html's Windows-updates Size column renders '-' for a
    zero/unknown size. That must not parse as some fabricated size."""
    rx = _extract_regex(js_source, "function parseBytes(text)")
    assert rx.search("-") is None


# ── content type: relative time ("49s ago", "2h ago") ───────────────────────

def test_relative_time_orders_seconds_before_hours(js_source):
    """The literal example pair from the brief: "49s ago" must sort before
    "2h ago" — a lexical sort of the raw strings ("2h ago" < "49s ago")
    gets this backwards."""
    rx = _extract_regex(js_source, "function parseRelativeTime(text)")
    mult = _extract_object_literal(js_source, "var RELTIME_MULT")

    def to_seconds(text):
        m = rx.search(text)
        return float(m.group(1)) * mult[m.group(2).lower()]

    assert to_seconds("49s ago") < to_seconds("2h ago")
    # The brief's other named pitfall: "45s ago" (45s) is more recent than
    # "2m ago" (120s), i.e. numerically smaller — but "2m ago" < "45s ago"
    # as raw strings ('2' < '4'), which is backwards.
    assert to_seconds("45s ago") < to_seconds("2m ago")
    assert "2m ago" < "45s ago"  # the naive string compare — backwards


def test_relative_time_multiplier_table_values(js_source):
    mult = _extract_object_literal(js_source, "var RELTIME_MULT")
    assert mult == {"s": 1, "m": 60, "h": 3600, "d": 86400}


# ── content type: timestamp (formatTs() round-trip) ─────────────────────────

def _parse_timestamp_branches(js_source: str) -> dict:
    """Extract the 4 (date-format regex, field order) pairs from
    parseTimestamp(), keyed by the date_format name each `if` condition
    names (the trailing bare `else` is the DD.MM.YYYY default).

    NOTE: the MM/DD/YYYY and DD/MM/YYYY branches use the textually IDENTICAL
    regex `^(\\d{2})/(\\d{2})/(\\d{4})` — only `order` differs between them
    ('mdy' vs 'dmy'). A sample string like "07/08/2026" matches BOTH, so a
    test that just tries regexes in source order until one matches (the
    first version of this helper did exactly that) silently picks the wrong
    branch. Keying by the condition's format name, not by which regex
    happens to match, is what makes this test actually distinguish them.
    """
    fn_start = js_source.index("function parseTimestamp(text)")
    fn_end = js_source.index("\n  function fallbackTimestamp", fn_start)
    body = js_source[fn_start:fn_end]
    pattern = re.compile(
        r"(?:dateFmt === '([^']+)'\)\s*\{\s*|else \{\s*)dp = (/.*?/); order = '(\w+)';"
    )
    branches = {}
    for m in pattern.finditer(body):
        fmt_name = m.group(1) or "DD.MM.YYYY"  # bare `else` is the default
        branches[fmt_name] = (re.compile(m.group(2)[1:-1]), m.group(3))
    return branches


def test_four_date_format_branches_are_present(js_source):
    branches = _parse_timestamp_branches(js_source)
    assert set(branches) == {"YYYY-MM-DD", "MM/DD/YYYY", "DD/MM/YYYY", "DD.MM.YYYY"}, (
        "expected exactly these 4 date_format branches — base.html's "
        "PRISM_TZ.date_format (see formatTs() in templates/base.html) "
        "supports exactly these four"
    )


@pytest.mark.parametrize(
    "date_format,sample,expected_ymd",
    [
        ("YYYY-MM-DD", "2026-08-07 14:32", (2026, 8, 7)),
        ("MM/DD/YYYY", "08/07/2026 14:32", (2026, 8, 7)),
        ("DD/MM/YYYY", "07/08/2026 14:32", (2026, 8, 7)),
        ("DD.MM.YYYY", "07.08.2026 14:32", (2026, 8, 7)),
    ],
)
def test_each_date_format_branch_extracts_the_right_calendar_date(js_source, date_format, sample, expected_ymd):
    """Every formatTs() output shape in this app resolves to the same real
    date (2026-08-07) once ITS OWN format's branch is applied — proving the
    y/m/d group order recorded per branch (order='ymd'|'mdy'|'dmy') matches
    which format actually produced the string."""
    branches = _parse_timestamp_branches(js_source)
    rx, order = branches[date_format]
    m = rx.match(sample)
    assert m, f"{date_format}'s own regex did not match its own sample {sample!r}"
    mapping = {"ymd": (1, 2, 3), "mdy": (3, 1, 2), "dmy": (3, 2, 1)}
    yi, mi, di = mapping[order]
    y, mo, d = int(m.group(yi)), int(m.group(mi)), int(m.group(di))
    assert (y, mo, d) == expected_ymd


def test_timestamp_default_format_sorts_end_of_july_before_start_of_august(js_source):
    """The whole reason `timestamp` is its own type and not `text`: under
    the default DD.MM.YYYY format, "31.07.2026" < "07.08.2026" as CALENDAR
    dates, but "07.08.2026" < "31.07.2026" as STRINGS. A plain string sort
    gets this backwards; the extracted regex + order must not."""
    branches = _parse_timestamp_branches(js_source)
    dmy, _order = branches["DD.MM.YYYY"]

    def ymd(text):
        m = dmy.match(text)
        d, mo, y = m.group(1), m.group(2), m.group(3)
        return (int(y), int(mo), int(d))

    assert ymd("31.07.2026 09:00") < ymd("07.08.2026 09:00")
    assert "07.08.2026" < "31.07.2026"  # the naive string compare — backwards


def test_12h_time_regex_extracts_hour_minute_ampm(js_source):
    fn_start = js_source.index("function parseTimestamp(text)")
    fn_end = js_source.index("\n  function fallbackTimestamp", fn_start)
    body = js_source[fn_start:fn_end]
    rx = _extract_regex(body, "timeFmt === '12h'")
    m = rx.search("02:05 PM")
    assert m.group(1) == "02" and m.group(2) == "05" and m.group(3).upper() == "PM"


# ── cellRawText / status extraction rule ────────────────────────────────────

def test_cell_raw_text_prefers_first_element_child_source_present(js_source):
    """servers.html's Status cell is two sibling <span>s in one <td> — the
    badge, then a trailing '49s ago'. Sorting the whole concatenation would
    tie-break on staleness instead of grouping by status. This asserts the
    extraction rule that avoids that is actually the one shipped (checks
    the real function body, not a re-implementation)."""
    fn_start = js_source.index("function cellRawText(cell)")
    fn_end = js_source.index("}", js_source.index("return", fn_start))
    body = js_source[fn_start:fn_end]
    assert "firstElementChild" in body
    assert "cell.textContent" in body  # fallback path for a plain-text cell


# ── row grouping: continuation-row + multi-tbody assumptions still hold ────

def test_servers_list_still_uses_one_tbody_per_server(js_source):
    """table-sort.js reorders whole <tbody> elements for this table
    specifically because servers.html wraps each server's visible row and
    its hidden hover/test-result rows in their OWN <tbody class="server-
    group">. If that structural choice in servers.html ever changed to a
    single shared <tbody>, the sort would silently start reordering
    individual <tr>s and separate a server's hover-detail row from it."""
    servers_html = (PROJECT_ROOT / "templates" / "servers.html").read_text(encoding="utf-8")
    assert 'class="server-group' in servers_html
    assert "sortUnits" in js_source and "tBodies.length > 1" in js_source


@pytest.mark.parametrize(
    "template,detail_row_marker",
    [
        ("reports.html", "fleet-detail-"),
        ("server_detail.html", 'colspan="6" class="px-4 py-3"'),
    ],
)
def test_detail_rows_are_single_colspan_cells(template, detail_row_marker):
    """The continuation-row heuristic (isContinuationRow: exactly one <td>
    carrying a colspan attribute) only folds a detail row into its owning
    row's group if the real markup is shaped that way. Confirms it still
    is, in the actual row-generation code for two of the tables that rely
    on it."""
    src = (PROJECT_ROOT / "templates" / template).read_text(encoding="utf-8")
    assert detail_row_marker in src


def test_is_continuation_row_checks_single_cell_and_colspan(js_source):
    fn_start = js_source.index("function isContinuationRow(row)")
    fn_end = js_source.index("}", fn_start)
    body = js_source[fn_start:fn_end]
    assert "cells.length === 1" in body
    assert "hasAttribute('colspan')" in body


# ── empties-always-last comparison rule ─────────────────────────────────────

def test_apply_sort_suppresses_the_mutation_observer_during_its_own_reorder(js_source):
    """Found live, not by reasoning about it first: clicking a header on
    /servers hung the browser tab solid. Reordering rows via appendChild()
    is itself a childList mutation, and the MutationObserver exists
    specifically to notice a table's rows changing and re-apply the stored
    sort — so an unguarded applySort() fed its own output straight back
    into restoreSortState(), which called applySort() again, forever.
    Fixed with a `_sorting` flag: set around the reorder, checked (and
    bailed on) at the top of the observer callback, reset via a
    Promise.resolve().then() microtask queued strictly after the mutations
    it guards, so same-queue microtask ordering keeps it true for exactly
    the mutations this function caused."""
    fn_start = js_source.index("function applySort(table, colIndex, type, dir)")
    fn_end = js_source.index("\n  }", fn_start)
    apply_body = js_source[fn_start:fn_end]
    assert "_sorting = true" in apply_body
    assert "_sorting = false" in apply_body
    # the flag must be set BEFORE the reorder (d.u.place() calls), not after
    assert apply_body.index("_sorting = true") < apply_body.index("d.u.place()")

    mo_start = js_source.index("var _mo = new MutationObserver")
    mo_guard_region = js_source[mo_start:mo_start + 200]
    assert "if (_sorting) return" in mo_guard_region


def test_set_arrow_is_idempotent(js_source):
    """Found live, the same session as the applySort/_sorting bug above but
    a SEPARATE feedback path _sorting doesn't cover: restoreSortState()
    calls setArrow() unconditionally for every sortable header on every
    invocation, and (before this fix) setArrow() always replaced the arrow
    span's innerHTML even when the icon wasn't changing. That's a real
    childList mutation the MutationObserver cannot tell apart from new data
    arriving, so it called restoreSortState() again, which called
    setArrow() again — reloading any page with a stored sort locked the tab
    solid. Confirmed by instrumented reproduction: ~40 rapid mutation
    batches of exactly 4 added nodes (one <svg> per sortable header in
    reports-fleet), all with the `_sorting` guard reading false, before
    this fix existed."""
    fn_start = js_source.index("function setArrow(th, state)")
    fn_end = js_source.index("\n  }", fn_start)
    body = js_source[fn_start:fn_end]
    # must bail out EARLY (before touching innerHTML) when the arrow
    # already reflects the requested state — not just track it after the
    # fact, which would still mutate every time.
    assert "if (arrow.dataset.state === state) return;" in body
    assert body.index("if (arrow.dataset.state === state) return;") < body.index("innerHTML")


def test_mutation_observer_ignores_mutations_inside_thead(js_source):
    """The other half of the same fix: even with setArrow() idempotent,
    the observer should never have treated header-internal DOM changes
    (bindHeader building the button/arrow, or any future change in that
    area) as "this table's data changed" in the first place. Scoping to
    mutations whose target is outside any <thead> is the general fix;
    test_set_arrow_is_idempotent is the specific one for the exact mutation
    that was observed causing it."""
    mo_start = js_source.index("var _mo = new MutationObserver")
    mo_end = js_source.index("\n  _mo.observe(", mo_start)
    body = js_source[mo_start:mo_end]
    assert "if (target.closest && target.closest('thead')) continue;" in body
    # the thead check must happen before a table is ever added to
    # tablesToRestore, not after
    assert body.index("target.closest && target.closest('thead')") < body.index("tablesToRestore.push")


def test_compare_for_sort_pins_empties_last_regardless_of_direction(js_source):
    fn_start = js_source.index("function compareForSort(a, b, dir)")
    fn_end = js_source.index("\n  }", js_source.index("return dir", fn_start))
    body = js_source[fn_start:fn_end]
    # The empty-handling branch must return before the `dir === 'asc' ? c : -c`
    # flip, otherwise descending order would surface empties first.
    empty_branch = body[: body.index("var c;")]
    assert "aEmpty" in empty_branch and "return aEmpty ? 1 : -1" in empty_branch


# ── design-token discipline: no raw hex, right tokens used ─────────────────

def test_no_raw_hex_literal_introduced_in_table_sort_js(js_source):
    """tests/test_design_tokens.py's ratchet only scans templates/*.html, so
    a new JS file is a blind spot for it. This is the equivalent guard for
    this specific file: the arrows must use text-brand/text-faint, not a
    hardcoded colour."""
    hex_literals = re.findall(r"#[0-9A-Fa-f]{3,8}\b", js_source)
    assert not hex_literals, f"raw hex colour(s) found in table-sort.js: {hex_literals}"


def test_active_and_inactive_arrow_use_the_documented_tokens(js_source):
    assert "text-brand" in js_source   # active sort indicator
    assert "text-faint" in js_source   # inactive-but-sortable indicator


def test_svg_arrows_use_currentcolor_not_a_literal_fill(js_source):
    fills = re.findall(r'fill="([^"]*)"', js_source)
    assert fills and all(f == "currentColor" for f in fills)


# ── base.html wiring ─────────────────────────────────────────────────────

@pytest.fixture()
def app_client():
    from app import app as flask_app
    flask_app.config["TESTING"] = True
    return flask_app.test_client()


def test_base_html_loads_table_sort_js_with_a_nonce(app_client):
    r = app_client.get("/login", follow_redirects=True)
    body = r.get_data(as_text=True)
    m = re.search(r'<script[^>]*src="[^"]*js/table-sort\.js"[^>]*>', body)
    assert m, "table-sort.js script tag not found in rendered page"
    assert 'nonce="' in m.group(0), "table-sort.js must carry the CSP nonce like every other inline/loaded script"


def test_table_sort_script_tag_sits_next_to_stepper():
    base_src = BASE_HTML.read_text(encoding="utf-8")
    stepper_idx = base_src.index("js/stepper.js")
    tablesort_idx = base_src.index("js/table-sort.js")
    # "next to" — within the same short tail of the file, not scattered
    # somewhere else in a 900-line template.
    assert 0 < tablesort_idx - stepper_idx < 400


# ── structural wiring: exact per-table, per-column data-sort sequence ──────
#
# Each entry is the expected data-sort value for every <th> in that table's
# header row, IN ORDER, with None for a column left deliberately unsortable
# (an icon-button Actions column, a decorative chevron/expander column, a
# badge-GROUP column with no single value, or a composite chip). A missing
# negative case here is exactly the "guardrail that finds nothing" trap —
# every table below has at least one excluded column asserted as None.

TH_RE = re.compile(r"<th\b([^>]*)>")
SORT_ATTR_RE = re.compile(r'data-sort="([a-z-]+)"')


def _thead_snippet(source: str, table_id: str) -> str:
    marker = f'data-sort-table="{table_id}"'
    start = source.index(marker)
    end = source.index("</thead>", start)
    return source[start:end]


def _sort_sequence(source: str, table_id: str) -> list:
    snippet = _thead_snippet(source, table_id)
    seq = []
    for attrs in TH_RE.findall(snippet):
        m = SORT_ATTR_RE.search(attrs)
        seq.append(m.group(1) if m else None)
    return seq


EXPECTED = {
    ("dashboard.html", "dashboard-restart-results"):
        ["text", "status", "text"],  # Server, Status, Message
    ("monitoring.html", "monitoring-noise-digest"):
        ["text", "text", "number", "number", "number", "text"],  # Server, Type, Score, Fires, Acked, Suggestion
    ("operations.html", "ops-runbooks"):
        ["text", "text", "status", None],  # Name, Category, Type, Actions
    ("operations.html", "ops-config-backups"):
        ["timestamp", "bytes", None],  # Timestamp, DB Size, Actions
    ("partials/server_comparison.html", "compare-live"):
        ["text", "status", "number", "number", "number", "number"],  # Server, Status, CPU, RAM, Disk C, Disk D
    ("partials/server_comparison.html", "compare-stats"):
        ["text", "text", "number", "number", "number", "number", "number", "number"],
    ("partials/server_comparison.html", "compare-hourly"):
        ["text", "text", "number", "number", "status"],
    ("partials/server_comparison.html", "compare-events-common"):
        ["number", "text", "status", "text", None],  # ...Counts per server
    ("partials/server_comparison.html", "compare-events-unique"):
        ["number", "text", "status", "text", None, None],  # ...Present on, Missing on
    ("partials/server_comparison.html", "compare-event-servers"):
        ["text", "number", "status", "timestamp"],
    ("rbac.html", "rbac-acl"):
        ["text", "text", "status", None, None],  # User, Server, Perm, Granted, <revoke>
    ("reports.html", "reports-fleet"):
        [None, "text", "number", "number", None, None, "number", None],  # chevron, Server, Health, Degraded, Main driver, Capacity, Availability, Trend
    ("servers.html", "servers-list"):
        ["text", "text", "status", "text", "number", None],  # Name, Host, Status, Type, Port, Actions
    ("servers.html", "servers-dependencies"):
        ["text", "text", "status", "text", "text", None],
    ("server_detail.html", "server-updates"):
        ["status", "text", "status", "text", "bytes", "status", "text"],
    ("server_detail.html", "server-events"):
        ["timestamp", "status", "text", "number", "text", None],
    ("server_detail.html", "server-logs-windows"):
        [None, "text", "status", "number", "number", "text"],
    ("server_detail.html", "server-logs-firewall"):
        [None, "status", "number", "number", "text"],
}


@pytest.mark.parametrize("key", list(EXPECTED.keys()), ids=lambda k: k[1])
def test_column_sort_types_match_the_documented_decision(key):
    rel_path, table_id = key
    source = (PROJECT_ROOT / "templates" / rel_path).read_text(encoding="utf-8")
    assert _sort_sequence(source, table_id) == EXPECTED[key]


# ── chronological tables must NOT be sortable ──────────────────────────────
#
# Per the design owner: a table that enumerates "what happened, when" (an
# audit trail / execution history) is ordered by time on purpose, and that
# order IS the information. These four must carry no data-sort-table /
# data-sort at all — reclassified away from sortable mid-task; this is the
# regression guard for that decision.

NOT_SORTABLE = [
    ("operations.html", "ops-runbook-executions"),   # Runbook Execution History
    ("operations.html", "ops-audit-log"),            # Audit Trail
    ("server_detail.html", "server-config-changes"),  # config drift history
    ("workflows.html", "workflows-executions"),       # Execution History
]


@pytest.mark.parametrize("rel_path,table_id", NOT_SORTABLE)
def test_chronological_tables_carry_no_sort_table_marker(rel_path, table_id):
    source = (PROJECT_ROOT / "templates" / rel_path).read_text(encoding="utf-8")
    assert f'data-sort-table="{table_id}"' not in source


def test_operations_runbook_execution_history_th_row_has_no_data_sort_attrs():
    """Belt-and-suspenders on the most easily-miscopied case: assert the
    actual <th> row for this table carries zero data-sort attributes, not
    just that the table-level marker is absent."""
    source = (PROJECT_ROOT / "templates" / "operations.html").read_text(encoding="utf-8")
    start = source.index("{{ t.runbook | default('Runbook') }}")
    row_start = source.rindex("<thead", 0, start)
    row_end = source.index("</thead>", start)
    snippet = source[row_start:row_end]
    assert "data-sort" not in snippet


def test_workflows_execution_history_th_row_has_no_data_sort_attrs():
    source = (PROJECT_ROOT / "templates" / "workflows.html").read_text(encoding="utf-8")
    start = source.index("<th class=\"px-4 py-2 text-xs font-semibold text-muted\">Workflow</th>")
    row_start = source.rindex("<thead", 0, start)
    row_end = source.index("</tr></thead>", start)
    snippet = source[row_start:row_end]
    assert "data-sort" not in snippet
