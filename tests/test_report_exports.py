"""The report exports, as files someone actually opens — WP-5.

The owner asked for reports that "fit on A4 pages or the Excel". Measured
first, because two of the three things that looked wrong were not:

  * **The PDF page SIZE was already A4** — that part needed nothing.

    But my first check here reported the CONTENT as fitting too, and it did
    not. It measured rendered text positions with pypdf, saw a maximum of
    473pt against 595pt, and passed — while two tables were 510pt wide in a
    481.90pt frame and an event Message column overflowed by 122pt, past the
    edge of the paper.

    The check could not have found it: text drawn outside the MediaBox is
    clipped by the VIEWER, and extraction does not report what was clipped.
    ReportLab does not warn either — `doc.build()` completes cleanly on a
    document with 122pt of clipped text. Source geometry is the instrument
    that works, and it is what these tests use now.

  * **Formula injection was not happening.** A first pass counted 4,130
    dangerous cells by flagging every leading `-`; `-1.0` is a negative
    number, not a formula. The real count is zero. The guard exists anyway,
    because the evidence report carries operator-supplied text where a leading
    `=` is reachable by anyone who can name a server.

  * **Two things genuinely were wrong**, and both broke the file for its
    reader rather than for the code:

      1. No UTF-8 BOM. Excel on Windows opens a BOM-less UTF-8 CSV as CP1252,
         so every umlaut, accent and Japanese character is mojibake. Prism
         ships five locales; four are affected.

      2. Timestamps as `2026-08-25T12:06:54Z`. Excel parses that as TEXT, so
         the operator cannot sort or filter by time — most of what a
         spreadsheet is for. They were also UTC, while this project's rule is
         that displayed timestamps use the configured timezone, and a CSV
         handed to management is display.

WHAT THIS FILE IS BLIND TO: whether the CONTENT is the right content. That is
the reorganisation question, and it is separate.
"""

from __future__ import annotations

import csv
import io
import pathlib
import re

import pytest


@pytest.fixture(scope="module")
def client():
    import app as prism_app
    prism_app.app.config["TESTING"] = True
    return prism_app.app.test_client()


_CSV_ROUTES = [
    "/api/reports/csv/metrics?hours=2",
    "/api/reports/csv/events",
    "/api/reports/csv/fleet?hours=24",
]


# ── the two real defects ──────────────────────────────────────────────────

@pytest.mark.parametrize("route", _CSV_ROUTES)
def test_every_csv_starts_with_a_utf8_bom(client, route):
    """Without it Excel reads the file as CP1252. Every one of the five
    locales except English contains characters this destroys."""
    r = client.get(route)
    assert r.status_code == 200, route
    assert r.get_data()[:3] == b"\xef\xbb\xbf", (
        f"{route} has no UTF-8 BOM; Excel will show mojibake")


@pytest.mark.parametrize("route", _CSV_ROUTES)
def test_every_csv_declares_utf8_exactly_once(client, route):
    """`mimetype="text/csv; charset=utf-8"` produced the charset TWICE,
    because Flask appends its own."""
    ctype = client.get(route).headers.get("Content-Type", "")
    assert "charset=utf-8" in ctype.lower(), f"{route}: {ctype}"
    assert ctype.lower().count("charset") == 1, f"{route} declares charset twice: {ctype}"


@pytest.mark.parametrize("route", _CSV_ROUTES[:2])
def test_timestamps_are_a_shape_excel_parses_as_a_date(client, route):
    """`2026-08-25T12:06:54Z` lands in Excel as text: no sorting, no
    filtering, no date arithmetic. `YYYY-MM-DD HH:MM:SS` parses."""
    rows = _rows(client, route)
    header, body = rows[0], rows[1:]
    if not body:
        pytest.skip("no data rows in this window")
    for i, name in enumerate(header):
        if not name.lower().startswith("timestamp"):
            continue
        values = [r[i] for r in body[:50] if len(r) > i and r[i]]
        assert values, f"{route}: the timestamp column is empty"
        bad = [v for v in values
               if not re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", v)]
        assert not bad, (
            f"{route}: timestamps Excel will treat as text: {bad[:3]}")


@pytest.mark.parametrize("route", _CSV_ROUTES[:2])
def test_the_timestamp_column_names_its_timezone(client, route):
    """A bare "Timestamp" on evidence is ambiguous, and the same file may be
    read in another country. The zone is in the header because the cell
    itself cannot carry it once Excel has parsed it as a local datetime."""
    header = _rows(client, route)[0]
    stamped = [h for h in header if h.lower().startswith("timestamp")]
    assert stamped, f"{route} has no timestamp column"
    for h in stamped:
        assert re.search(r"\(.+/.+\)|\(UTC\)", h), (
            f"{route}: {h!r} does not name its timezone")


@pytest.mark.parametrize("route", _CSV_ROUTES)
def test_no_cell_would_be_evaluated_as_a_formula(client, route):
    """Measured as zero today. Kept because the evidence report will carry
    account names and log messages, where a leading `=` is reachable by
    anyone who can name a server.

    `-` is deliberately not treated as dangerous on its own: `-1.0` is a
    negative number, and flagging it produced 4,130 false positives on the
    first pass."""
    for row in _rows(client, route)[1:]:
        for cell in row:
            if cell[:1] in ("=", "+", "@", "\t", "\r"):
                assert cell.startswith("'"), (
                    f"{route}: {cell[:40]!r} would be evaluated by Excel")


# ── what was already right, and must stay right ──────────────────────────

def test_the_pdf_is_a4():
    """Measured before assuming: it was A4 all along. The owner's "fit on A4"
    turned out to be about the CSVs, not this."""
    pytest.importorskip("pypdf")
    from pypdf import PdfReader
    import app as prism_app
    from database import Database
    from config_manager import ConfigManager
    from i18n import get_translations
    from reports import generate_pdf_report

    pdf = generate_pdf_report(Database(), ConfigManager(), get_translations("en"))
    reader = PdfReader(io.BytesIO(pdf))
    assert reader.pages, "the PDF has no pages"
    for page in reader.pages[:5]:
        width = float(page.mediabox.width)
        height = float(page.mediabox.height)
        assert abs(width - 595.276) < 2 and abs(height - 841.89) < 2, (
            f"page is {width:.0f}x{height:.0f}pt, which is not A4")


A4_WIDTH = 595.28
A4_MARGIN = 56.69          # 20*mm, reports.py
CELL_PADDING = 6           # ReportLab default LEFTPADDING/RIGHTPADDING
FRAME = A4_WIDTH - 2 * A4_MARGIN


def test_no_table_is_wider_than_the_page():
    """Checked against the SOURCE, not the rendered file.

    The first version of this test measured text positions in the built PDF
    with pypdf and passed — while two tables were 510 pt wide in a 481.90 pt
    frame. Text drawn outside the MediaBox is clipped by the VIEWER, and
    extraction cannot report what was clipped, so a render-position check is
    structurally unable to see this. ReportLab does not warn either:
    `doc.build()` completes cleanly on a document with 122 pt of clipped
    text."""
    import re
    src = (pathlib.Path(__file__).resolve().parent.parent / "reports.py").read_text(encoding="utf-8")
    over = []
    for m in re.finditer(r"colWidths\s*=\s*\[([^\]]+)\]", src):
        try:
            widths = [float(eval(v.strip(), {"__builtins__": {}}, {}))
                      for v in m.group(1).split(",") if v.strip()]
        except Exception:
            continue          # a computed width this scan cannot evaluate
        if sum(widths) > FRAME:
            over.append(f"line {src[:m.start()].count(chr(10)) + 1}: "
                        f"{sum(widths):.0f}pt > {FRAME:.0f}pt")
    assert not over, "tables wider than the printable frame:\n  " + "\n  ".join(over)


def test_no_stored_text_is_placed_in_a_plain_string_cell():
    """A plain `str` in a ReportLab table is drawn on ONE line and runs off
    the page. Anything holding host- or operator-supplied text has to be a
    wrapping Paragraph.

    The event Message column truncated by CHARACTERS (`[:80]`) and was sized
    in POINTS (210): 80 characters of Helvetica-8 is 320 pt into 198 pt of
    usable width."""
    import re
    src = (pathlib.Path(__file__).resolve().parent.parent / "reports.py").read_text(encoding="utf-8")
    offenders = re.findall(r'\.get\("message", ""\)(?:\s*or\s*""\s*)?\)?\[:\d+\]', src)
    assert not offenders, (
        f"stored message text truncated by character count into a fixed-width "
        f"column: {offenders}")


def test_paragraph_cells_escape_what_they_wrap():
    """The risk the wrapping fix introduces. A Paragraph parses its input as
    markup, so a Windows event message containing `<` or `&` produces
    malformed XML and takes the document down — reachable by any host that
    logs an XML fragment."""
    src = (pathlib.Path(__file__).resolve().parent.parent / "reports.py").read_text(encoding="utf-8")
    assert "def _cell(" in src, "the wrapping cell helper is gone"
    body = src[src.index("def _cell("):]
    body = body[:body.index("\n\n\n")] if "\n\n\n" in body else body[:800]
    assert "escape(" in body, (
        "_cell does not escape its input; a message containing '<' or '&' "
        "would produce malformed XML")


def test_a_message_containing_markup_does_not_break_the_document():
    """Exercised, not just asserted about."""
    from reports import _cell, _msg_style
    para = _cell('crash <injected> & "quoted" tag', _msg_style(8))
    text = getattr(para, "text", "")
    assert "&lt;" in text and "&amp;" in text, (
        f"markup reached the Paragraph unescaped: {text[:80]!r}")


def test_every_sparkline_fits_inside_its_own_cell():
    """Each drawing sat at 100 pt inside a 100 pt column that also has 6 pt of
    padding on each side, so every sparkline overhung its cell by 12 pt."""
    import re
    src = (pathlib.Path(__file__).resolve().parent.parent / "reports.py").read_text(encoding="utf-8")
    # CALL sites only: `def _draw_sparkline(values, width=120, ...)` is a
    # default no caller uses, and matching it reported a 120pt drawing that
    # is never actually drawn.
    calls = re.findall(r"(?<!def )_draw_sparkline\([^)]*width=(\d+)", src)
    draw_widths = {int(w) for w in calls}
    spark_cols = re.findall(r"colWidths=\[81, (\d+),", src)
    assert draw_widths and spark_cols, "the sparkline tables changed shape"
    narrowest_col = min(int(c) for c in spark_cols)
    widest_draw = max(draw_widths)
    assert widest_draw + 2 * CELL_PADDING <= narrowest_col, (
        f"a {widest_draw}pt drawing plus {2*CELL_PADDING}pt padding does not "
        f"fit a {narrowest_col}pt column")


# ── helpers ───────────────────────────────────────────────────────────────

def _rows(client, route) -> list[list[str]]:
    body = client.get(route).get_data().decode("utf-8-sig")
    return list(csv.reader(io.StringIO(body)))
