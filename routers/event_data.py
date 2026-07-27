"""
Whole-event export/import: one .xlsx workbook covering everything on an
event — Departments, Budget Proposals + line items, Estimated/Actual
Expenses, Estimated/Actual Income, Vendors, and Sponsors — as separate
sheets, so someone can back up, edit offline, or clone the financial setup
of an event in one file instead of doing it screen by screen.

Restricted to event_admin/finance_head (same bar as budget import) since
it can create departments and touches every financial table on the event.
"""
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from fastapi.responses import StreamingResponse
from core.database import get_db, execute
from core.auth import get_current_user
from utils.roles import get_event_role
import io
import openpyxl
from openpyxl.utils import get_column_letter

router = APIRouter(prefix="/api/events", tags=["event-data"])

SHEETS = {
    "Departments": ["Name", "Head Name", "Color"],
    "Budget Proposals": [
        "Department", "Proposal Title", "Status", "Category", "Item Name",
        "Description", "Quantity", "Unit", "Unit Price", "Total Amount",
    ],
    "Estimated Expenses": [
        "Department", "Category", "Item Name", "Description", "Quantity", "Unit", "Amount", "Notes",
    ],
    "Actual Expenses": [
        "Department", "Category", "Item Name", "Description", "Quantity", "Unit", "Amount",
        "Paid On", "Payment Mode", "Status", "Reference", "Notes",
    ],
    "Estimated Income": ["Source", "Category", "Amount", "Notes"],
    "Actual Income": ["Source", "Category", "Amount", "Received On", "Payment Mode", "Reference", "Notes"],
    "Vendors": ["Name", "Category", "Contact Name", "Contact Email", "Contract Value", "Status", "Notes"],
    "Sponsors": ["Name", "Tier", "Contact Name", "Contact Email", "Amount", "Status", "Notes"],
}


def _require_admin_or_finance(conn, user, event_id):
    role_ctx = get_event_role(conn, user, event_id)
    if role_ctx["level"] not in ("event_admin", "finance_head"):
        raise HTTPException(status_code=403, detail="Only an event admin or finance head can do this")
    return role_ctx


def _write_sheet(wb, name, headers, rows):
    ws = wb.create_sheet(name)
    ws.append(headers)
    for col in range(1, len(headers) + 1):
        ws.cell(row=1, column=col).font = openpyxl.styles.Font(bold=True)
    for row in rows:
        ws.append(row)
    for i, h in enumerate(headers, 1):
        ws.column_dimensions[get_column_letter(i)].width = max(14, len(h) + 2)


@router.get("/{event_id}/export")
def export_event(event_id: int, conn=Depends(get_db), user=Depends(get_current_user)):
    _require_admin_or_finance(conn, user, event_id)

    wb = openpyxl.Workbook()
    wb.remove(wb.active)  # drop the default blank sheet

    cur = execute(conn, "SELECT name, head_name, color FROM departments WHERE event_id=%s ORDER BY name", (event_id,))
    _write_sheet(wb, "Departments", SHEETS["Departments"], [[r["name"], r["head_name"], r["color"]] for r in cur.fetchall()])

    cur = execute(conn, """
        SELECT d.name AS dept_name, p.title, p.status, li.category, li.item_name,
               li.description, li.quantity, li.unit, li.unit_price, li.total_amount
        FROM budget_proposals p
        JOIN departments d ON d.id = p.department_id
        LEFT JOIN budget_line_items li ON li.proposal_id = p.id
        WHERE p.event_id=%s ORDER BY d.name, p.title, li.id
    """, (event_id,))
    _write_sheet(wb, "Budget Proposals", SHEETS["Budget Proposals"], [
        [r["dept_name"], r["title"], r["status"], r["category"], r["item_name"],
         r["description"], r["quantity"], r["unit"], r["unit_price"], r["total_amount"]]
        for r in cur.fetchall()
    ])

    cur = execute(conn, """
        SELECT d.name AS dept_name, e.category, e.item_name, e.description, e.quantity, e.unit, e.amount, e.notes
        FROM estimated_expenses e LEFT JOIN departments d ON d.id = e.department_id
        WHERE e.event_id=%s ORDER BY e.created_at
    """, (event_id,))
    _write_sheet(wb, "Estimated Expenses", SHEETS["Estimated Expenses"], [
        [r["dept_name"], r["category"], r["item_name"], r["description"], r["quantity"], r["unit"], r["amount"], r["notes"]]
        for r in cur.fetchall()
    ])

    cur = execute(conn, """
        SELECT d.name AS dept_name, e.category, e.item_name, e.description, e.quantity, e.unit, e.amount,
               e.paid_on, e.payment_mode, e.status, e.reference, e.notes
        FROM actual_expenses e LEFT JOIN departments d ON d.id = e.department_id
        WHERE e.event_id=%s ORDER BY e.created_at
    """, (event_id,))
    _write_sheet(wb, "Actual Expenses", SHEETS["Actual Expenses"], [
        [r["dept_name"], r["category"], r["item_name"], r["description"], r["quantity"], r["unit"], r["amount"],
         r["paid_on"], r["payment_mode"], r["status"], r["reference"], r["notes"]]
        for r in cur.fetchall()
    ])

    cur = execute(conn, "SELECT source, category, amount, notes FROM estimated_income WHERE event_id=%s ORDER BY created_at", (event_id,))
    _write_sheet(wb, "Estimated Income", SHEETS["Estimated Income"], [[r["source"], r["category"], r["amount"], r["notes"]] for r in cur.fetchall()])

    cur = execute(conn, """
        SELECT source, category, amount, received_on, payment_mode, reference, notes
        FROM actual_income WHERE event_id=%s ORDER BY created_at
    """, (event_id,))
    _write_sheet(wb, "Actual Income", SHEETS["Actual Income"], [
        [r["source"], r["category"], r["amount"], r["received_on"], r["payment_mode"], r["reference"], r["notes"]]
        for r in cur.fetchall()
    ])

    cur = execute(conn, """
        SELECT name, category, contact_name, contact_email, contract_value, status, notes
        FROM vendors WHERE event_id=%s ORDER BY created_at
    """, (event_id,))
    _write_sheet(wb, "Vendors", SHEETS["Vendors"], [
        [r["name"], r["category"], r["contact_name"], r["contact_email"], r["contract_value"], r["status"], r["notes"]]
        for r in cur.fetchall()
    ])

    cur = execute(conn, """
        SELECT name, tier, contact_name, contact_email, amount, status, notes
        FROM sponsors WHERE event_id=%s ORDER BY created_at
    """, (event_id,))
    _write_sheet(wb, "Sponsors", SHEETS["Sponsors"], [
        [r["name"], r["tier"], r["contact_name"], r["contact_email"], r["amount"], r["status"], r["notes"]]
        for r in cur.fetchall()
    ])

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename=event_export_{event_id}.xlsx"},
    )


@router.post("/{event_id}/import")
async def import_event(event_id: int, file: UploadFile = File(...), conn=Depends(get_db), user=Depends(get_current_user)):
    _require_admin_or_finance(conn, user, event_id)

    contents = await file.read()
    try:
        wb = openpyxl.load_workbook(io.BytesIO(contents), data_only=True)
    except Exception:
        raise HTTPException(status_code=400, detail="Couldn't read that file — is it a valid .xlsx?")

    def rows_of(sheet_name):
        if sheet_name not in wb.sheetnames:
            return []
        ws = wb[sheet_name]
        return [r for r in ws.iter_rows(min_row=2, values_only=True) if r and any(r)]

    summary = {"departments_created": 0, "budget_proposals_created": 0, "budget_items_created": 0,
               "expenses_created": 0, "income_created": 0, "vendors_created": 0, "sponsors_created": 0}
    errors = []

    # 1. Departments first — everything else may reference a department by name.
    cur = execute(conn, "SELECT id, name FROM departments WHERE event_id=%s", (event_id,))
    dept_map = {row["name"].strip().lower(): row["id"] for row in cur.fetchall()}

    for idx, row in enumerate(rows_of("Departments"), start=2):
        name, head_name, color = (list(row) + [None] * 3)[:3]
        if not name:
            continue
        key = str(name).strip().lower()
        if key in dept_map:
            continue
        cur = execute(conn, "INSERT INTO departments (event_id,name,head_name,color) VALUES (%s,%s,%s,%s) RETURNING id",
                      (event_id, str(name).strip(), head_name or "", color or "#6366f1"))
        dept_map[key] = cur.fetchone()["id"]
        summary["departments_created"] += 1

    def dept_id_for(name, row_label):
        if not name:
            return None, True
        did = dept_map.get(str(name).strip().lower())
        if not did:
            errors.append(f"{row_label}: no department named '{name}'")
            return None, False
        return did, True

    # 2. Budget proposals + line items
    proposal_cache = {}
    for idx, row in enumerate(rows_of("Budget Proposals"), start=2):
        dept_name, title, _status, category, item_name, description, quantity, unit, unit_price, total_amount = (list(row) + [None] * 10)[:10]
        if not dept_name or not title:
            errors.append(f"Budget Proposals row {idx}: missing department or title")
            continue
        did, ok = dept_id_for(dept_name, f"Budget Proposals row {idx}")
        if not ok:
            continue
        key = (did, str(title).strip())
        if key not in proposal_cache:
            cur = execute(conn,
                "INSERT INTO budget_proposals (event_id,department_id,submitted_by,title,notes,status) VALUES (%s,%s,%s,%s,'','draft') RETURNING id",
                (event_id, did, user["id"], str(title).strip()))
            proposal_cache[key] = cur.fetchone()["id"]
            summary["budget_proposals_created"] += 1
        proposal_id = proposal_cache[key]
        if category or item_name:
            try:
                qty = float(quantity) if quantity not in (None, "") else 1
                price = float(unit_price) if unit_price not in (None, "") else 0
            except (TypeError, ValueError):
                errors.append(f"Budget Proposals row {idx}: quantity/unit price must be numbers")
                continue
            total = float(total_amount) if total_amount not in (None, "") else qty * price
            execute(conn,
                """INSERT INTO budget_line_items (proposal_id,category,item_name,description,quantity,unit,unit_price,total_amount)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                (proposal_id, category or "", item_name or "", description or "", qty, unit or "unit", price, total))
            summary["budget_items_created"] += 1

    # 3. Estimated expenses
    for idx, row in enumerate(rows_of("Estimated Expenses"), start=2):
        dept_name, category, item_name, description, quantity, unit, amount, notes = (list(row) + [None] * 8)[:8]
        if not category or amount in (None, ""):
            errors.append(f"Estimated Expenses row {idx}: missing category or amount")
            continue
        did, ok = dept_id_for(dept_name, f"Estimated Expenses row {idx}")
        if not ok:
            continue
        try:
            qty = float(quantity) if quantity not in (None, "") else 1
            amt = float(amount)
        except (TypeError, ValueError):
            errors.append(f"Estimated Expenses row {idx}: quantity/amount must be numbers")
            continue
        execute(conn,
            """INSERT INTO estimated_expenses (event_id,department_id,category,item_name,description,quantity,unit,amount,notes)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (event_id, did, category, item_name or "", description or "", qty, unit or "unit", amt, notes or ""))
        summary["expenses_created"] += 1

    # 4. Actual expenses
    for idx, row in enumerate(rows_of("Actual Expenses"), start=2):
        dept_name, category, item_name, description, quantity, unit, amount, paid_on, payment_mode, status, reference, notes = (list(row) + [None] * 12)[:12]
        if not category or amount in (None, ""):
            errors.append(f"Actual Expenses row {idx}: missing category or amount")
            continue
        did, ok = dept_id_for(dept_name, f"Actual Expenses row {idx}")
        if not ok:
            continue
        try:
            qty = float(quantity) if quantity not in (None, "") else 1
            amt = float(amount)
        except (TypeError, ValueError):
            errors.append(f"Actual Expenses row {idx}: quantity/amount must be numbers")
            continue
        execute(conn,
            """INSERT INTO actual_expenses (event_id,department_id,category,item_name,description,quantity,unit,amount,paid_on,payment_mode,status,reference,notes)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (event_id, did, category, item_name or "", description or "", qty, unit or "unit", amt,
             paid_on or "", payment_mode or "Cash", status or "paid", reference or "", notes or ""))
        summary["expenses_created"] += 1

    # 5. Estimated income
    for idx, row in enumerate(rows_of("Estimated Income"), start=2):
        source, category, amount, notes = (list(row) + [None] * 4)[:4]
        if not source or amount in (None, ""):
            errors.append(f"Estimated Income row {idx}: missing source or amount")
            continue
        try:
            amt = float(amount)
        except (TypeError, ValueError):
            errors.append(f"Estimated Income row {idx}: amount must be a number")
            continue
        execute(conn, "INSERT INTO estimated_income (event_id,source,category,amount,notes) VALUES (%s,%s,%s,%s,%s)",
                (event_id, source, category or "Other", amt, notes or ""))
        summary["income_created"] += 1

    # 6. Actual income
    for idx, row in enumerate(rows_of("Actual Income"), start=2):
        source, category, amount, received_on, payment_mode, reference, notes = (list(row) + [None] * 7)[:7]
        if not source or amount in (None, ""):
            errors.append(f"Actual Income row {idx}: missing source or amount")
            continue
        try:
            amt = float(amount)
        except (TypeError, ValueError):
            errors.append(f"Actual Income row {idx}: amount must be a number")
            continue
        execute(conn,
            "INSERT INTO actual_income (event_id,source,category,amount,received_on,payment_mode,reference,notes) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
            (event_id, source, category or "Other", amt, received_on or "", payment_mode or "Cash", reference or "", notes or ""))
        summary["income_created"] += 1

    # 7. Vendors
    for idx, row in enumerate(rows_of("Vendors"), start=2):
        name, category, contact_name, contact_email, contract_value, status, notes = (list(row) + [None] * 7)[:7]
        if not name:
            errors.append(f"Vendors row {idx}: missing name")
            continue
        try:
            cv = float(contract_value) if contract_value not in (None, "") else 0
        except (TypeError, ValueError):
            cv = 0
        execute(conn,
            "INSERT INTO vendors (event_id,name,category,contact_name,contact_email,contract_value,status,notes) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
            (event_id, name, category or "Other", contact_name or "", contact_email or "", cv, status or "active", notes or ""))
        summary["vendors_created"] += 1

    # 8. Sponsors (also syncs to actual_income, same as the normal create-sponsor endpoint)
    for idx, row in enumerate(rows_of("Sponsors"), start=2):
        name, tier, contact_name, contact_email, amount, status, notes = (list(row) + [None] * 7)[:7]
        if not name:
            errors.append(f"Sponsors row {idx}: missing name")
            continue
        try:
            amt = float(amount) if amount not in (None, "") else 0
        except (TypeError, ValueError):
            amt = 0
        cur = execute(conn,
            "INSERT INTO sponsors (event_id,name,tier,contact_name,contact_email,amount,status,notes,income_synced) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,1) RETURNING id",
            (event_id, name, tier or "Bronze", contact_name or "", contact_email or "", amt, status or "confirmed", notes or ""))
        sponsor_id = cur.fetchone()["id"]
        execute(conn,
            "INSERT INTO actual_income (event_id,source,category,amount,payment_mode,sponsor_id) VALUES (%s,%s,'Sponsor',%s,'Bank Transfer',%s)",
            (event_id, f"Sponsor: {name}", amt, sponsor_id))
        summary["sponsors_created"] += 1

    return {"ok": True, **summary, "errors": errors}
