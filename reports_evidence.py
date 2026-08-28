"""Rendering the evidence report — WP-5.

Three formats over one `evidence.build()` result: PDF for A4, CSV for Excel,
JSON for anything else. The data layer is `evidence.py`; nothing here queries.

A4 GEOMETRY, STATED ONCE AND OBEYED
-----------------------------------
A4 is 595.28 x 841.89 pt. With 20 mm margins the frame is **481.89 pt wide**,
and ReportLab's default cell padding is 6 pt each side. So:

    sum(colWidths) <= 481      and      usable text width = colWidth - 12

Two rules follow, and both were learned the hard way in this same file's older
sibling: a table wider than the frame is drawn anyway — ReportLab neither
shrinks it nor warns — and a plain `str` cell is drawn on ONE line and allowed
to run off the paper, where the viewer clips it silently. Every cell holding
stored text is a wrapping `Paragraph`, and every Paragraph escapes its input,
because a Windows event message containing `<` or `&` would otherwise produce
malformed XML and take the document down.
"""

from __future__ import annotations

import io
import logging

from reports import evidence_csv_writer

logger = logging.getLogger(__name__)

#: A4 minus 20 mm margins each side. Every table below sums to this or less.
FRAME_WIDTH = 481


def _styles():
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib import colors
    base = getSampleStyleSheet()
    return {
        "h1": ParagraphStyle("ev_h1", parent=base["Heading1"], fontSize=16,
                             spaceAfter=10),
        "h2": ParagraphStyle("ev_h2", parent=base["Heading2"], fontSize=12,
                             spaceBefore=14, spaceAfter=6),
        "body": ParagraphStyle("ev_body", parent=base["BodyText"], fontSize=9,
                               leading=12),
        "small": ParagraphStyle("ev_small", parent=base["BodyText"], fontSize=7.5,
                                leading=9.5, textColor=colors.HexColor("#444444")),
        "cell": ParagraphStyle("ev_cell", fontName="Helvetica", fontSize=7.5,
                               leading=9),
        "warn": ParagraphStyle("ev_warn", parent=base["BodyText"], fontSize=9,
                               leading=12, textColor=colors.HexColor("#B45309")),
    }


def _p(text, style):
    """Stored text as a wrapping, escaped Paragraph."""
    from xml.sax.saxutils import escape
    from reportlab.platypus import Paragraph
    return Paragraph(escape(str(text if text is not None else ""))[:400], style)


def _table(data, widths, styles, header=True):
    from reportlab.platypus import Table, TableStyle
    from reportlab.lib import colors
    assert sum(widths) <= FRAME_WIDTH, (
        f"table is {sum(widths)}pt wide; the A4 frame is {FRAME_WIDTH}pt")
    t = Table(data, colWidths=widths, repeatRows=1 if header else 0)
    cmds = [
        ("FONTSIZE", (0, 0), (-1, -1), 7.5),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#D1D5DB")),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]
    if header:
        cmds += [
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1F2937")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ]
    t.setStyle(TableStyle(cmds))
    return t


def generate_evidence_pdf(doc: dict, title: str = "Prism Evidence Report") -> bytes:
    """The evidence report as an A4 PDF.

    Sections are ordered the way a reader needs them: what this document is
    and what it could not see, then the verdict, then the tables that support
    it. An evidence report that opens with tables makes the reader do the
    analysis themselves.
    """
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer,
                                    PageBreak, KeepTogether)

    st = _styles()
    buf = io.BytesIO()
    pdf = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=20 * mm, rightMargin=20 * mm,
        topMargin=18 * mm, bottomMargin=18 * mm,
        title=title,
    )

    el = [Paragraph(title, st["h1"])]
    el += _section_provenance(doc, st)
    el.append(PageBreak())
    el += _section_verdict(doc, st)
    el += _section_posture(doc, st)
    el.append(PageBreak())
    el += _section_auth(doc, st)
    el += _section_firewall(doc, st)
    el.append(PageBreak())
    el += _section_audit(doc, st)

    pdf.build(el, onFirstPage=_page_furniture, onLaterPages=_page_furniture)
    return buf.getvalue()


def _page_furniture(canvas, doc):
    """Page number and footer on EVERY page.

    The older report put its footer in the flowable stream, so it printed once
    — on the last page — and the document had no page numbers at all. A
    ten-page evidence document nobody can cite a page of is hard to use as
    evidence.
    """
    canvas.saveState()
    canvas.setFont("Helvetica", 7.5)
    canvas.setFillGray(0.4)
    canvas.drawString(20 * 2.8346, 12 * 2.8346, "Prism — evidence report")
    canvas.drawRightString(
        canvas._pagesize[0] - 20 * 2.8346, 12 * 2.8346, f"Page {doc.page}")
    canvas.restoreState()


# ── the sections ─────────────────────────────────────────────────────────

def _section_provenance(doc, st):
    from reportlab.platypus import Spacer
    p = doc["provenance"]
    d = p["disclosure"]
    out = [_p(f"Window {p['window_start']} to {p['window_end']} "
              f"({p['window_hours']} hours). Generated {p['generated_at']}.",
              st["body"]), Spacer(1, 6)]

    rows = [["Table", "Rows", "Earliest", "Latest"]]
    for name, t in p["tables"].items():
        rows.append([_p(name, st["cell"]),
                     _p(f"{t['rows']:,}" if t["rows"] is not None else "?", st["cell"]),
                     _p((t["first"] or "")[:19], st["cell"]),
                     _p((t["last"] or "")[:19], st["cell"])])
    out += [_table(rows, [150, 70, 130, 130], st), Spacer(1, 10)]

    out.append(_p("<b>What this report could not see</b>", st["body"]))
    ing = d.get("ingest") or {}
    out.append(_p(
        f"Ingest: {ing.get('payloads_rejected', '?')} payload(s) rejected, "
        f"{ing.get('rows_dropped', '?')} row(s) dropped, "
        f"{ing.get('fields_truncated', '?')} field(s) truncated. "
        "These counters are per process and reset on restart, so a low number "
        "means little on its own.", st["small"]))

    chain = d.get("audit_chain") or {}
    style = st["warn"] if chain.get("state") != "intact" else st["small"]
    out.append(_p(f"<b>Audit chain: {chain.get('state', 'unknown')}.</b> "
                  f"{chain.get('note', '')}", style))
    return out


def _section_verdict(doc, st):
    from reportlab.platypus import Spacer
    v = doc["verdict"]
    out = [_p("Verdict", st["h2"]), _p(v["summary"], st["body"]), Spacer(1, 8)]

    if v.get("attacks"):
        out.append(_p("<b>Sources showing attack shape</b>", st["body"]))
        rows = [["Source", "Servers", "Accounts", "Attempts", "Shape", "First seen"]]
        for a in v["attacks"][:10]:
            rows.append([_p(a["source_ip"], st["cell"]), _p(a["servers"], st["cell"]),
                         _p(a["accounts"], st["cell"]), _p(a["attempts"], st["cell"]),
                         _p(", ".join(a["shapes"]), st["cell"]),
                         _p((a["first_seen"] or "")[:16], st["cell"])])
        out += [_table(rows, [110, 50, 60, 60, 95, 100], st), Spacer(1, 8)]

    faults = v.get("fleet_faults") or []
    if faults:
        out.append(_p("<b>Events across many servers at once</b>", st["body"]))
        rows = [["When", "Source/ID", "Level", "Servers", "vs normal", "Sample"]]
        for f in faults[:12]:
            rows.append([
                _p(f["hour_utc"], st["cell"]),
                _p(f"{f['log_source']}/{f['event_id']}", st["cell"]),
                _p(f["level"], st["cell"]),
                _p(f"{f['servers']}/{f['fleet_size']}", st["cell"]),
                _p(f"{f['spike_multiple']}x", st["cell"]),
                _p(f["sample"][:120], st["cell"]),
            ])
        out += [_table(rows, [72, 78, 52, 48, 48, 183], st), Spacer(1, 8)]
    return out


def _section_posture(doc, st):
    from reportlab.platypus import Spacer
    sp = doc["security_posture"]
    out = [_p("Security posture", st["h2"]),
           _p(f"{sp['total']} servers, as of {sp.get('as_of') or 'unknown'}. "
              f"{sp['with_concerns']} with concerns.", st["body"])]

    if sp.get("collection_warning"):
        out.append(_p(sp["collection_warning"], st["warn"]))
    out.append(_p(sp["limitation"], st["small"]))
    out.append(Spacer(1, 6))

    rows = [["Server", "Defender", "Signatures", "Firewall", "BitLocker", "Concerns"]]
    for s in sp["servers"]:
        def fmt(v, unknown_label="not read"):
            return unknown_label if v is None else ("yes" if v == 1 else
                                                    ("no" if v == 0 else str(v)))
        rows.append([
            _p(s["server"], st["cell"]),
            _p(fmt(s["defender_enabled"]), st["cell"]),
            _p("not read" if s["defender_sig_age_days"] is None
               else f"{s['defender_sig_age_days']}d", st["cell"]),
            _p(fmt(s["firewall_service_running"]), st["cell"]),
            _p("not read" if s["bitlocker_encrypted_pct"] is None
               else f"{s['bitlocker_encrypted_pct']}%", st["cell"]),
            _p("; ".join(s["concerns"]) or "—", st["cell"]),
        ])
    out.append(_table(rows, [80, 55, 60, 55, 55, 176], st))
    return out


def _section_auth(doc, st):
    from reportlab.platypus import Spacer
    a = doc["authentication"]
    out = [_p("Authentication", st["h2"]),
           _p(f"{a['total']} failed logon(s) in the window.", st["body"]),
           Spacer(1, 6)]

    if a["by_reason"]:
        out.append(_p("<b>Why they failed</b>", st["body"]))
        rows = [["Code", "Meaning", "Attempts"]]
        for r in a["by_reason"]:
            rows.append([_p(r["sub_status"], st["cell"]),
                         _p(r["meaning"], st["cell"]),
                         _p(r["attempts"], st["cell"])])
        out += [_table(rows, [90, 290, 60], st), Spacer(1, 8)]

    if a["by_source"]:
        out.append(_p("<b>By source</b>", st["body"]))
        rows = [["Source", "Servers", "Accounts", "Attempts", "First", "Last"]]
        for r in a["by_source"][:20]:
            rows.append([_p(r["source_ip"], st["cell"]), _p(r["servers"], st["cell"]),
                         _p(r["accounts"], st["cell"]), _p(r["attempts"], st["cell"]),
                         _p((r["first_seen"] or "")[:16], st["cell"]),
                         _p((r["last_seen"] or "")[:16], st["cell"])])
        out += [_table(rows, [105, 50, 58, 58, 105, 105], st), Spacer(1, 8)]
    return out


def _section_firewall(doc, st):
    from reportlab.platypus import Spacer
    f = doc["firewall"]
    out = [_p("Firewall changes", st["h2"])]
    if f.get("note"):
        out.append(_p(f["note"], st["warn"]))
        return out
    out.append(_p(f"{f['total_in_window']} policy event(s) in the window.", st["body"]))
    if f["events"]:
        rows = [["When", "Server", "ID", "Meaning", "Detail"]]
        for e in f["events"][:60]:
            rows.append([_p((e["timestamp"] or "")[:19], st["cell"]),
                         _p(e["server"], st["cell"]), _p(e["event_id"], st["cell"]),
                         _p(e["meaning"], st["cell"]),
                         _p((e["message"] or "")[:150], st["cell"])])
        out += [Spacer(1, 6), _table(rows, [95, 80, 35, 110, 161], st)]
    return out


def _section_audit(doc, st):
    from reportlab.platypus import Spacer
    au = doc["audit"]
    out = [_p("Who did what", st["h2"]),
           _p(f"{au['total']} audited action(s) in the window.", st["body"]),
           Spacer(1, 6)]

    if au["by_category"]:
        rows = [["Category", "Actions"]]
        for c in au["by_category"]:
            rows.append([_p(c["category"], st["cell"]), _p(c["actions"], st["cell"])])
        out += [_table(rows, [200, 80], st), Spacer(1, 8)]

    if au["entries"]:
        out.append(_p("<b>Most recent</b>", st["body"]))
        rows = [["When", "User", "Action", "Category", "Detail"]]
        for e in au["entries"][:80]:
            rows.append([_p((e["timestamp"] or "")[:19], st["cell"]),
                         _p(e["username"], st["cell"]), _p(e["action"], st["cell"]),
                         _p(e["category"], st["cell"]),
                         _p((e["details"] or "")[:120], st["cell"])])
        out += [_table(rows, [95, 65, 105, 75, 141], st)]
    return out


# ── the same document as a spreadsheet ───────────────────────────────────

def generate_evidence_csv(doc: dict, ts_fmt=None, ts_label: str = "UTC") -> str:
    """One CSV with every section stacked, each under its own banner.

    A workbook per section would be tidier, but a single CSV is what opens
    without a library on any machine an admin is sitting at. Sections are
    separated by a blank line and a banner row, which Excel shows as a
    heading and a human reads as one.
    """
    fmt = ts_fmt or (lambda v: v)
    out = io.StringIO()
    # NOT csv.writer: this document is the one export built from
    # operator-supplied text — account names, log messages, audit details —
    # so a leading `=` is reachable by anyone who can name a server. The
    # escaping lives in the writer rather than at each `writerow` below,
    # because there are about forty of them.
    w = evidence_csv_writer(out)

    def section(name, header, rows):
        w.writerow([])
        w.writerow([f"== {name} =="])
        w.writerow(header)
        for r in rows:
            w.writerow(r)

    p = doc["provenance"]
    w.writerow(["Prism evidence report"])
    w.writerow(["Window start", fmt(p["window_start"]), f"({ts_label})"])
    w.writerow(["Window end", fmt(p["window_end"]), f"({ts_label})"])
    w.writerow(["Generated", fmt(p["generated_at"]), f"({ts_label})"])
    chain = (p["disclosure"] or {}).get("audit_chain") or {}
    w.writerow(["Audit chain", chain.get("state", "unknown"), chain.get("note", "")])

    section("Data examined", ["Table", "Rows", "Earliest", "Latest"],
            [[n, t["rows"], fmt(t["first"] or ""), fmt(t["last"] or "")]
             for n, t in p["tables"].items()])

    v = doc["verdict"]
    section("Verdict", ["Summary"], [[v["summary"]]])
    section("Attack shape",
            ["Source", "Servers", "Accounts", "Attempts", "Shapes", "First", "Last"],
            [[a["source_ip"], a["servers"], a["accounts"], a["attempts"],
              " ".join(a["shapes"]), fmt(a["first_seen"]), fmt(a["last_seen"])]
             for a in v.get("attacks", [])])
    section("Fleet-wide events",
            ["Hour", "Source", "EventID", "Level", "Servers", "FleetSize",
             "Events", "NormalEvents", "SpikeMultiple", "Sample"],
            [[f["hour_utc"], f["log_source"], f["event_id"], f["level"],
              f["servers"], f["fleet_size"], f["events"], f["normal_events"],
              f["spike_multiple"], f["sample"]]
             for f in v.get("fleet_faults", [])])

    sp = doc["security_posture"]
    section("Security posture (as of, no history)",
            ["Server", "LastChecked", "DefenderEnabled", "RealtimeProtection",
             "SignatureAgeDays", "FirewallService", "FirewallDomain",
             "FirewallPrivate", "FirewallPublic", "BitLockerPct",
             "BitLockerStatus", "Concerns", "NotMeasured"],
            [[s["server"], fmt(s["last_checked"]),
              _tri(s["defender_enabled"]), _tri(s["defender_rt_protection"]),
              "not read" if s["defender_sig_age_days"] is None else s["defender_sig_age_days"],
              _tri(s["firewall_service_running"]), _tri(s["firewall_domain_enabled"]),
              _tri(s["firewall_private_enabled"]), _tri(s["firewall_public_enabled"]),
              "not read" if s["bitlocker_encrypted_pct"] is None else s["bitlocker_encrypted_pct"],
              s["bitlocker_status"] or "not read",
              "; ".join(s["concerns"]), "; ".join(s["unmeasured"])]
             for s in sp["servers"]])

    a = doc["authentication"]
    section("Failed logons by reason", ["SubStatus", "Meaning", "Attempts"],
            [[r["sub_status"], r["meaning"], r["attempts"]] for r in a["by_reason"]])
    section("Failed logons by source",
            ["Source", "Servers", "Accounts", "Attempts", "First", "Last"],
            [[r["source_ip"], r["servers"], r["accounts"], r["attempts"],
              fmt(r["first_seen"]), fmt(r["last_seen"])] for r in a["by_source"]])
    section("Failed logons by account",
            ["Account", "Servers", "Sources", "Attempts", "First", "Last"],
            [[r["account"], r["servers"], r["sources"], r["attempts"],
              fmt(r["first_seen"]), fmt(r["last_seen"])] for r in a["by_account"]])

    f = doc["firewall"]
    section("Firewall policy events",
            ["When", "Server", "Level", "EventID", "Meaning", "Message"],
            [[fmt(e["timestamp"]), e["server"], e["level"], e["event_id"],
              e["meaning"], e["message"]] for e in f["events"]]
            or ([[f.get("note") or "none in window", "", "", "", "", ""]]))

    au = doc["audit"]
    section("Audit trail",
            ["When", "User", "Action", "Category", "Detail", "SourceIP"],
            [[fmt(e["timestamp"]), e["username"], e["action"], e["category"],
              e["details"], e["source_ip"]] for e in au["entries"]])
    section("Procedures executed",
            ["When", "SOP", "By", "Result", "Notes"],
            [[fmt(e["executed_at"]), e["sop_id"], e["executed_by"], e["result"],
              e["notes"]] for e in au["sop_executions"]])

    return out.getvalue()


def _tri(v):
    """yes / no / not read — never a bare 0 that reads as "off".

    The collector stores a failed read and a genuine negative as the same
    value, so `evidence.py` resolves unread fields to None. Printing that as
    `0` here would undo the distinction in the spreadsheet."""
    return "not read" if v is None else ("yes" if v else "no")
