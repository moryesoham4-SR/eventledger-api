"""
Event PDF report — a single downloadable PDF summarizing an event's
financials: overview, income vs. expense summary, department budget
breakdown, actual expenses, actual income, vendors, and sponsors.

Uses reportlab (already a dependency) so no new packages are needed.
Same access rule as the JSON summary endpoint: anyone with access to the
event can pull the report (view-only — this generates a PDF, it doesn't
write anything).
"""
import io
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak,
)

from core.database import get_db, execute
from core.auth import get_current_user
from utils.roles import get_event_role

router = APIRouter(prefix="/api/events", tags=["reports"])

CURRENCY_SYMBOLS = {"INR": "Rs. ", "USD": "$", "EUR": "\u20ac", "GBP": "\u00a3"}


def _money(amount, currency):
    symbol = CURRENCY_SYMBOLS.get(currency, currency + " ")
    return f"{symbol}{amount:,.2f}"


def _require_access(conn, user, event_id):
    role_ctx = get_event_role(conn, user, event_id)
    if role_ctx["level"] is None:
        raise HTTPException(status_code=403, detail="You don't have access to this event")
    return role_ctx


def _fetch_all(conn, sql, params):
    cur = execute(conn, sql, params)
    return [dict(r) for r in cur.fetchall()]


def _styles():
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(
        name="ReportTitle", parent=styles["Title"], fontSize=22, spaceAfter=4,
        textColor=colors.HexColor("#1a1a2e"),
    ))
    styles.add(ParagraphStyle(
        name="ReportSubtitle", parent=styles["Normal"], fontSize=10,
        textColor=colors.HexColor("#666677"), spaceAfter=16,
    ))
    styles.add(ParagraphStyle(
        name="SectionHeading", parent=styles["Heading2"], fontSize=13,
        spaceBefore=18, spaceAfter=8, textColor=colors.HexColor("#1a1a2e"),
    ))
    styles.add(ParagraphStyle(
        name="SmallNote", parent=styles["Normal"], fontSize=8,
        textColor=colors.HexColor("#888899"),
    ))
    return styles


def _summary_table_style(header_bg="#312e81"):
    return TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor(header_bg)),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 8.5),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f5f5fa")]),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#dddde5")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
    ])


def _empty_row(n):
    return [["No records."] + [""] * (n - 1)]


@router.get("/{event_id}/report.pdf")
def get_event_report_pdf(event_id: int, conn=Depends(get_db), user=Depends(get_current_user)):
    _require_access(conn, user, event_id)

    cur = execute(conn, "SELECT * FROM events WHERE id=%s", (event_id,))
    event = cur.fetchone()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    event = dict(event)
    currency = event.get("currency") or "INR"

    def q(sql, p):
        row = execute(conn, sql, p).fetchone()
        return float(list(row.values())[0]) if row else 0.0

    est_inc = q("SELECT COALESCE(SUM(amount),0) FROM estimated_income WHERE event_id=%s", (event_id,))
    act_inc = q("SELECT COALESCE(SUM(amount),0) FROM actual_income WHERE event_id=%s", (event_id,))
    est_exp = q("SELECT COALESCE(SUM(amount),0) FROM estimated_expenses WHERE event_id=%s", (event_id,))
    act_exp = q("SELECT COALESCE(SUM(amount),0) FROM actual_expenses WHERE event_id=%s", (event_id,))
    profit = act_inc - act_exp
    utilization = round(act_exp / est_exp * 100, 1) if est_exp else 0

    departments = _fetch_all(conn, "SELECT * FROM departments WHERE event_id=%s ORDER BY name", (event_id,))

    dept_budget = _fetch_all(conn, """
        SELECT d.name AS dept_name,
               COALESCE(SUM(ee.amount), 0) AS estimated,
               COALESCE((SELECT SUM(ae.amount) FROM actual_expenses ae WHERE ae.department_id = d.id), 0) AS actual
        FROM departments d
        LEFT JOIN estimated_expenses ee ON ee.department_id = d.id
        WHERE d.event_id=%s
        GROUP BY d.id, d.name
        ORDER BY d.name
    """, (event_id,))

    actual_expenses = _fetch_all(conn, """
        SELECT d.name AS dept_name, e.category, e.item_name, e.amount, e.paid_on, e.status
        FROM actual_expenses e LEFT JOIN departments d ON d.id = e.department_id
        WHERE e.event_id=%s ORDER BY e.created_at
    """, (event_id,))

    actual_income = _fetch_all(conn, """
        SELECT source, category, amount, received_on, payment_mode
        FROM actual_income WHERE event_id=%s ORDER BY created_at
    """, (event_id,))

    vendors = _fetch_all(conn, """
        SELECT name, category, contact_name, contract_value, status
        FROM vendors WHERE event_id=%s ORDER BY created_at
    """, (event_id,))

    sponsors = _fetch_all(conn, """
        SELECT name, tier, amount, status
        FROM sponsors WHERE event_id=%s ORDER BY created_at
    """, (event_id,))

    # ---- build the PDF ----
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        topMargin=18 * mm, bottomMargin=16 * mm, leftMargin=16 * mm, rightMargin=16 * mm,
        title=f"{event['name']} — Event Report",
    )
    styles = _styles()
    story = []

    # Header
    story.append(Paragraph(event["name"], styles["ReportTitle"]))
    meta_bits = []
    if event.get("venue"):
        meta_bits.append(event["venue"])
    if event.get("start_date"):
        date_str = event["start_date"]
        if event.get("end_date") and event["end_date"] != event["start_date"]:
            date_str += f" – {event['end_date']}"
        meta_bits.append(date_str)
    if event.get("expected_attendees"):
        meta_bits.append(f"{event['expected_attendees']} expected attendees")
    story.append(Paragraph(" &nbsp;·&nbsp; ".join(meta_bits) or "&nbsp;", styles["ReportSubtitle"]))
    story.append(Paragraph(
        f"Generated {datetime.now().strftime('%d %b %Y, %H:%M')}", styles["SmallNote"]
    ))

    # Financial overview
    story.append(Paragraph("Financial Overview", styles["SectionHeading"]))
    overview_rows = [
        ["Metric", "Estimated", "Actual"],
        ["Income", _money(est_inc, currency), _money(act_inc, currency)],
        ["Expense", _money(est_exp, currency), _money(act_exp, currency)],
        ["Net Position", "", _money(profit, currency)],
        ["Budget Utilization", "", f"{utilization}%"],
    ]
    t = Table(overview_rows, colWidths=[60 * mm, 55 * mm, 55 * mm])
    t.setStyle(_summary_table_style())
    story.append(t)

    # Department budget breakdown
    story.append(Paragraph("Budget by Department", styles["SectionHeading"]))
    if dept_budget:
        rows = [["Department", "Estimated Expense", "Actual Expense", "Variance"]]
        for d in dept_budget:
            variance = float(d["estimated"]) - float(d["actual"])
            rows.append([
                d["dept_name"], _money(d["estimated"], currency),
                _money(d["actual"], currency), _money(variance, currency),
            ])
        t = Table(rows, colWidths=[55 * mm, 40 * mm, 40 * mm, 35 * mm])
        t.setStyle(_summary_table_style())
        story.append(t)
    else:
        story.append(Paragraph("No departments set up yet.", styles["Normal"]))

    # Actual expenses
    story.append(Paragraph("Actual Expenses", styles["SectionHeading"]))
    if actual_expenses:
        rows = [["Department", "Category", "Item", "Amount", "Paid On", "Status"]]
        for e in actual_expenses:
            rows.append([
                e["dept_name"] or "—", e["category"] or "—", e["item_name"] or "—",
                _money(e["amount"], currency), e["paid_on"] or "—", (e["status"] or "—").title(),
            ])
        t = Table(rows, colWidths=[30 * mm, 25 * mm, 40 * mm, 28 * mm, 25 * mm, 22 * mm], repeatRows=1)
        t.setStyle(_summary_table_style("#7c2d12"))
        story.append(t)
    else:
        story.append(Paragraph("No actual expenses recorded yet.", styles["Normal"]))

    story.append(PageBreak())

    # Actual income
    story.append(Paragraph("Actual Income", styles["SectionHeading"]))
    if actual_income:
        rows = [["Source", "Category", "Amount", "Received On", "Payment Mode"]]
        for i in actual_income:
            rows.append([
                i["source"] or "—", i["category"] or "—", _money(i["amount"], currency),
                i["received_on"] or "—", i["payment_mode"] or "—",
            ])
        t = Table(rows, colWidths=[42 * mm, 28 * mm, 30 * mm, 30 * mm, 40 * mm], repeatRows=1)
        t.setStyle(_summary_table_style("#166534"))
        story.append(t)
    else:
        story.append(Paragraph("No actual income recorded yet.", styles["Normal"]))

    # Vendors
    story.append(Paragraph("Vendors", styles["SectionHeading"]))
    if vendors:
        rows = [["Name", "Category", "Contact", "Contract Value", "Status"]]
        for v in vendors:
            rows.append([
                v["name"], v["category"] or "—", v["contact_name"] or "—",
                _money(v["contract_value"] or 0, currency), (v["status"] or "—").title(),
            ])
        t = Table(rows, colWidths=[40 * mm, 30 * mm, 35 * mm, 32 * mm, 33 * mm], repeatRows=1)
        t.setStyle(_summary_table_style())
        story.append(t)
    else:
        story.append(Paragraph("No vendors added yet.", styles["Normal"]))

    # Sponsors
    story.append(Paragraph("Sponsors", styles["SectionHeading"]))
    if sponsors:
        rows = [["Name", "Tier", "Amount", "Status"]]
        for s in sponsors:
            rows.append([s["name"], s["tier"] or "—", _money(s["amount"] or 0, currency), (s["status"] or "—").title()])
        t = Table(rows, colWidths=[55 * mm, 35 * mm, 40 * mm, 40 * mm], repeatRows=1)
        t.setStyle(_summary_table_style())
        story.append(t)
    else:
        story.append(Paragraph("No sponsors added yet.", styles["Normal"]))

    story.append(Spacer(1, 16))
    story.append(Paragraph("Generated by EventLedger AI", styles["SmallNote"]))

    doc.build(story)
    buf.seek(0)

    safe_name = "".join(c if c.isalnum() or c in " -_" else "" for c in event["name"]).strip() or "event"
    filename = f"{safe_name.replace(' ', '_')}_report.pdf"

    return StreamingResponse(
        buf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
