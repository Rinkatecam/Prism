"""The PDF uses the Prism display face for headings, and degrades safely.

Background: ReportLab cannot read WOFF2 — registering the vendored
static/vendor/fonts/chakra-petch-*.woff2 raises
``TTFError: Not a recognized TrueType font: version=0x774F4632``. The .ttf files
beside them are the SAME faces converted with fontTools, so no new font was
introduced and the vendored OFL.txt still covers them.

Scope is deliberate: display face for the title and section headings only. Body
copy and every table stay on Helvetica, because the subset is 252 glyphs with no
verifiable ``tnum`` and Chakra Petch's squared bowls make 0/8, 5/6 and 1/7
confusable at the 8-9pt the tables run at. A misread digit in an availability
figure is a factual error in a document someone signs off on.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import reports

FONT_DIR = Path(__file__).resolve().parent.parent / "static" / "vendor" / "fonts"


@pytest.fixture(autouse=True)
def _reset_font_cache():
    """_register_brand_fonts memoises into a module global."""
    reports._BRAND_FONTS_READY = None
    yield
    reports._BRAND_FONTS_READY = None


def test_both_ttfs_are_vendored():
    for n in ("ChakraPetch-SemiBold.ttf", "ChakraPetch-Medium.ttf"):
        p = FONT_DIR / n
        assert p.is_file(), f"{n} missing — PDF headings would silently fall back"
        assert p.stat().st_size > 10_000, f"{n} looks truncated"


def test_ofl_licence_is_present_alongside_them():
    """The repo is going public; the converted TTFs are covered by the same OFL
    that already ships with the woff2 sources."""
    assert (FONT_DIR / "OFL.txt").is_file()


def test_the_ttfs_are_real_truetype_not_renamed_woff2():
    """The exact failure this work exists to fix. WOFF2 starts 'wOF2'."""
    for n in ("ChakraPetch-SemiBold.ttf", "ChakraPetch-Medium.ttf"):
        head = (FONT_DIR / n).read_bytes()[:4]
        assert head != b"wOF2", f"{n} is still WOFF2 — ReportLab will reject it"
        assert head in (b"\x00\x01\x00\x00", b"true", b"ttcf"), head


def test_registers_the_brand_face():
    assert reports._register_brand_fonts() == "ChakraPetch-SemiBold"


def test_registration_is_idempotent():
    """ReportLab's font registry is process-global; re-registering is waste."""
    a = reports._register_brand_fonts()
    b = reports._register_brand_fonts()
    assert a == b == "ChakraPetch-SemiBold"


def test_the_registered_font_can_measure_text():
    """Registration succeeding is not enough — the metrics must load, which is
    where a corrupt conversion would surface."""
    reports._register_brand_fonts()
    from reportlab.pdfbase import pdfmetrics
    w = pdfmetrics.stringWidth("PRISM Fleet Report", "ChakraPetch-SemiBold", 34)
    assert w > 0


def test_falls_back_to_helvetica_when_the_font_is_missing(tmp_path, monkeypatch):
    """A missing or unreadable font must never break report generation — the
    PDF endpoint is also driven headlessly by scheduled_reports.py."""
    monkeypatch.setattr(reports, "__file__", str(tmp_path / "reports.py"))
    assert reports._register_brand_fonts() == "Helvetica-Bold"


def test_generated_pdf_embeds_the_brand_face(tmp_path):
    from config_manager import ConfigManager
    from database import Database
    from i18n import get_translations

    db = Database(str(tmp_path / "t.db"))
    cfg = ConfigManager(str(tmp_path / "config.json"))
    pdf = reports.generate_pdf_report(db, cfg, get_translations("en"))

    assert pdf[:5] == b"%PDF-", "not a PDF"
    assert b"ChakraPetch" in pdf, "brand face not referenced in the output"
    assert b"Helvetica" in pdf, "body copy should still be Helvetica"


def test_pdf_still_generates_without_the_brand_face(tmp_path, monkeypatch):
    """The fallback must produce a working document, not just a font name."""
    from config_manager import ConfigManager
    from database import Database
    from i18n import get_translations

    monkeypatch.setattr(reports, "__file__", str(tmp_path / "reports.py"))
    db = Database(str(tmp_path / "t2.db"))
    cfg = ConfigManager(str(tmp_path / "config2.json"))
    pdf = reports.generate_pdf_report(db, cfg, get_translations("en"))
    assert pdf[:5] == b"%PDF-"
    assert b"ChakraPetch" not in pdf
