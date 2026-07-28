from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from typing import Optional
from core.database import get_db, execute
from core.auth import get_current_user
from utils.roles import get_event_role, can_access_department, can_edit_department, can_approve_budget
import datetime
import io
import openpyxl
from openpyxl.utils import get_column_letter

router = APIRouter(prefix="/api/budget", tags=["budget"])

EXPORT_HEADERS = [
    "Department", "Proposal Title", "Status", "Category", "Item Name",
    "Description", "Quantity", "Unit", "Unit Price", "Total Amount",
]

class ProposalCreate(BaseModel):
    event_id: int
    department_id: int
    title: str
    notes: str = ""

class LineItemCreate(BaseModel):
    proposal_id: int
    category: str
    item_name: str
    description: str = ""
    quantity: float = 1
    unit: str = "unit"
    unit_price: float
    total_amount: float

class RejectRequest(BaseModel):
    reason: str


def _get_proposal_or_404(conn, proposal_id):
    cur = execute(conn, "SELECT * FROM budget_proposals WHERE id=%s", (proposal_id,))
    p = cur.fetchone()
    if not p:
        raise HTTPException(status_code=404, detail="Proposal not found")
    return dict(p)


def _notify(conn, user_id, message):
    """Persists a notification so it's waiting for someone next time they log
    in, even if they weren't online when the triggering action happened."""
    if not user_id:
        return
    execute(conn, "INSERT INTO notifications (user_id, message, is_read) VALUES (%s,%s,0)", (user_id, message))


def _get_approver_ids(conn, event_id, exclude_user_id=None):
    """Everyone who can approve budgets on this event: the event owner, plus
    anyone explicitly assigned event_admin/finance_head for it."""
    ids = set()
    cur = execute(conn, "SELECT user_id FROM events WHERE id=%s", (event_id,))
    owner = cur.fetchone()
    if owner:
        ids.add(owner["user_id"])
    cur = execute(conn,
        "SELECT user_id FROM user_event_roles WHERE event_id=%s AND role IN ('event_admin','finance_head')",
        (event_id,)
    )
    for row in cur.fetchall():
        ids.add(row["user_id"])
    ids.discard(exclude_user_id)
    return ids


@router.get("/proposals")
def get_proposals(event_id: int, conn=Depends(get_db), user=Depends(get_current_user)):
    role_ctx = get_event_role(conn, user, event_id)
    if role_ctx["level"] is None:
        raise HTTPException(status_code=403, detail="You don't have access to this event")

    if role_ctx["level"] in ("dept_head", "volunteer"):
        cur = execute(conn,
            """SELECT p.*, d.name as dept_name, u.name as submitted_by_name
               FROM budget_proposals p
               LEFT JOIN departments d ON d.id=p.department_id
               LEFT JOIN users u ON u.id=p.submitted_by
               WHERE p.event_id=%s AND p.department_id=%s ORDER BY p.created_at DESC""",
            (event_id, role_ctx["dept_id"])
        )
    else:
        cur = execute(conn,
            """SELECT p.*, d.name as dept_name, u.name as submitted_by_name
               FROM budget_proposals p
               LEFT JOIN departments d ON d.id=p.department_id
               LEFT JOIN users u ON u.id=p.submitted_by
               WHERE p.event_id=%s ORDER BY p.created_at DESC""",
            (event_id,)
        )
    return [dict(r) for r in cur.fetchall()]

@router.post("/proposals")
def create_proposal(data: ProposalCreate, conn=Depends(get_db), user=Depends(get_current_user)):
    role_ctx = get_event_role(conn, user, data.event_id)
    if not can_edit_department(role_ctx, data.department_id):
        raise HTTPException(status_code=403, detail="You can't create a budget proposal for this department")
    cur = execute(conn,
        "INSERT INTO budget_proposals (event_id,department_id,submitted_by,title,notes,status) VALUES (%s,%s,%s,%s,%s,'draft') RETURNING *",
        (data.event_id, data.department_id, user["id"], data.title, data.notes)
    )
    return dict(cur.fetchone())

@router.get("/proposals/{proposal_id}")
def get_proposal(proposal_id: int, conn=Depends(get_db), user=Depends(get_current_user)):
    p = _get_proposal_or_404(conn, proposal_id)
    role_ctx = get_event_role(conn, user, p["event_id"])
    if not can_access_department(role_ctx, p["department_id"]):
        raise HTTPException(status_code=403, detail="You don't have access to this budget proposal")

    cur = execute(conn,
        """SELECT p.*, d.name as dept_name FROM budget_proposals p
           LEFT JOIN departments d ON d.id=p.department_id WHERE p.id=%s""",
        (proposal_id,)
    )
    result = dict(cur.fetchone())
    cur2 = execute(conn, "SELECT * FROM budget_line_items WHERE proposal_id=%s", (proposal_id,))
    result["line_items"] = [dict(r) for r in cur2.fetchall()]
    return result

@router.post("/proposals/{proposal_id}/submit")
def submit_proposal(proposal_id: int, conn=Depends(get_db), user=Depends(get_current_user)):
    p = _get_proposal_or_404(conn, proposal_id)
    role_ctx = get_event_role(conn, user, p["event_id"])
    if not can_edit_department(role_ctx, p["department_id"]):
        raise HTTPException(status_code=403, detail="You can't submit this budget proposal")

    now = datetime.datetime.utcnow().isoformat()
    cur = execute(conn, "SELECT COALESCE(SUM(total_amount),0) as t FROM budget_line_items WHERE proposal_id=%s", (proposal_id,))
    total = float(list(cur.fetchone().values())[0])
    execute(conn,
        "UPDATE budget_proposals SET status='submitted', submitted_at=%s, total_amount=%s WHERE id=%s",
        (now, total, proposal_id)
    )

    cur = execute(conn, "SELECT d.name AS dept_name, e.name AS event_name FROM departments d JOIN events e ON e.id=d.event_id WHERE d.id=%s", (p["department_id"],))
    ctx = cur.fetchone()
    for uid in _get_approver_ids(conn, p["event_id"], exclude_user_id=user["id"]):
        _notify(conn, uid, f"New budget \"{p['title']}\" from {ctx['dept_name']} needs your approval — {ctx['event_name']}")

    return {"ok": True, "message": "Budget submitted for approval"}

@router.post("/proposals/{proposal_id}/approve")
def approve_proposal(proposal_id: int, conn=Depends(get_db), user=Depends(get_current_user)):
    p = _get_proposal_or_404(conn, proposal_id)
    role_ctx = get_event_role(conn, user, p["event_id"])
    if not can_approve_budget(role_ctx):
        raise HTTPException(status_code=403, detail="Only an event admin or finance role can approve budgets")

    now = datetime.datetime.utcnow().isoformat()
    execute(conn,
        "UPDATE budget_proposals SET status='approved', approved_by=%s, approved_at=%s WHERE id=%s",
        (user["id"], now, proposal_id)
    )
    if p.get("submitted_by") and p["submitted_by"] != user["id"]:
        _notify(conn, p["submitted_by"], f"Your budget \"{p['title']}\" was approved ✅")
    return {"ok": True, "message": "Budget approved"}

@router.post("/proposals/{proposal_id}/reject")
def reject_proposal(proposal_id: int, data: RejectRequest, conn=Depends(get_db), user=Depends(get_current_user)):
    p = _get_proposal_or_404(conn, proposal_id)
    role_ctx = get_event_role(conn, user, p["event_id"])
    if not can_approve_budget(role_ctx):
        raise HTTPException(status_code=403, detail="Only an event admin or finance role can reject budgets")

    now = datetime.datetime.utcnow().isoformat()
    execute(conn,
        "UPDATE budget_proposals SET status='rejected', rejected_by=%s, rejected_at=%s, reject_reason=%s WHERE id=%s",
        (user["id"], now, data.reason, proposal_id)
    )
    if p.get("submitted_by") and p["submitted_by"] != user["id"]:
        reason_suffix = f": {data.reason}" if data.reason else ""
        _notify(conn, p["submitted_by"], f"Your budget \"{p['title']}\" was rejected{reason_suffix}")
    return {"ok": True, "message": "Budget rejected"}

@router.post("/line-items")
def add_line_item(data: LineItemCreate, conn=Depends(get_db), user=Depends(get_current_user)):
    p = _get_proposal_or_404(conn, data.proposal_id)
    role_ctx = get_event_role(conn, user, p["event_id"])
    if not can_edit_department(role_ctx, p["department_id"]):
        raise HTTPException(status_code=403, detail="You can't edit this budget proposal")

    cur = execute(conn,
        """INSERT INTO budget_line_items (proposal_id,category,item_name,description,quantity,unit,unit_price,total_amount)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
        (data.proposal_id, data.category, data.item_name, data.description,
         data.quantity, data.unit, data.unit_price, data.total_amount)
    )
    return dict(cur.fetchone())

@router.delete("/line-items/{item_id}")
def delete_line_item(item_id: int, conn=Depends(get_db), user=Depends(get_current_user)):
    cur = execute(conn, "SELECT proposal_id FROM budget_line_items WHERE id=%s", (item_id,))
    li = cur.fetchone()
    if not li:
        raise HTTPException(status_code=404, detail="Line item not found")
    p = _get_proposal_or_404(conn, li["proposal_id"])
    role_ctx = get_event_role(conn, user, p["event_id"])
    if not can_edit_department(role_ctx, p["department_id"]):
        raise HTTPException(status_code=403, detail="You can't edit this budget proposal")

    execute(conn, "DELETE FROM budget_line_items WHERE id=%s", (item_id,))
    return {"ok": True}

@router.get("/export")
def export_budget(event_id: int, conn=Depends(get_db), user=Depends(get_current_user)):
    """Downloads every proposal (scoped the same way the list view is —
    dept_head/volunteer only see their own department) as an .xlsx workbook."""
    role_ctx = get_event_role(conn, user, event_id)
    if role_ctx["level"] is None:
        raise HTTPException(status_code=403, detail="You don't have access to this event")

    base_query = """
        SELECT d.name AS dept_name, p.title, p.status, li.category, li.item_name,
               li.description, li.quantity, li.unit, li.unit_price, li.total_amount
        FROM budget_proposals p
        JOIN departments d ON d.id = p.department_id
        LEFT JOIN budget_line_items li ON li.proposal_id = p.id
        WHERE p.event_id = %s {dept_filter}
        ORDER BY d.name, p.title, li.id
    """
    if role_ctx["level"] in ("dept_head", "volunteer"):
        cur = execute(conn, base_query.format(dept_filter="AND p.department_id = %s"), (event_id, role_ctx["dept_id"]))
    else:
        cur = execute(conn, base_query.format(dept_filter=""), (event_id,))
    rows = cur.fetchall()

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Budget"
    ws.append(EXPORT_HEADERS)
    for col in range(1, len(EXPORT_HEADERS) + 1):
        ws.cell(row=1, column=col).font = openpyxl.styles.Font(bold=True)
    for r in rows:
        ws.append([
            r["dept_name"], r["title"], r["status"], r["category"], r["item_name"],
            r["description"], r["quantity"], r["unit"], r["unit_price"], r["total_amount"],
        ])
    for i, header in enumerate(EXPORT_HEADERS, 1):
        ws.column_dimensions[get_column_letter(i)].width = max(14, len(header) + 2)

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename=budget_export_event_{event_id}.xlsx"},
    )

@router.post("/import")
async def import_budget(
    event_id: int = Form(...),
    file: UploadFile = File(...),
    conn=Depends(get_db),
    user=Depends(get_current_user),
):
    """Bulk-creates proposals + line items from an .xlsx with the same
    columns as /export. Rows are grouped into one proposal per unique
    (Department, Proposal Title) pair; every imported proposal starts as
    a draft regardless of the Status column, so it still goes through the
    normal submit/approve flow. Department names must already exist on
    this event (created via the Departments page) — unmatched rows are
    reported back rather than silently skipped or auto-creating a department."""
    role_ctx = get_event_role(conn, user, event_id)
    if role_ctx["level"] not in ("event_admin", "finance_head"):
        raise HTTPException(status_code=403, detail="Only an event admin or finance head can import a budget")

    contents = await file.read()
    try:
        wb = openpyxl.load_workbook(io.BytesIO(contents), data_only=True)
    except Exception:
        raise HTTPException(status_code=400, detail="Couldn't read that file — is it a valid .xlsx?")
    ws = wb.active

    cur = execute(conn, "SELECT id, name FROM departments WHERE event_id=%s", (event_id,))
    dept_map = {row["name"].strip().lower(): row["id"] for row in cur.fetchall()}

    proposal_cache = {}
    created_proposals = 0
    created_items = 0
    errors = []

    for idx, row in enumerate(ws.iter_rows(min_row=2, values_only=True), start=2):
        if not row or not any(row):
            continue
        padded = (list(row) + [None] * 10)[:10]
        dept_name, title, _status, category, item_name, description, quantity, unit, unit_price, total_amount = padded

        if not dept_name or not title:
            errors.append(f"Row {idx}: missing department or proposal title")
            continue
        dept_id = dept_map.get(str(dept_name).strip().lower())
        if not dept_id:
            errors.append(f"Row {idx}: no department named '{dept_name}' on this event")
            continue

        key = (dept_id, str(title).strip())
        if key not in proposal_cache:
            cur = execute(
                conn,
                "INSERT INTO budget_proposals (event_id,department_id,submitted_by,title,notes,status) VALUES (%s,%s,%s,%s,'',  'draft') RETURNING id",
                (event_id, dept_id, user["id"], str(title).strip()),
            )
            proposal_cache[key] = cur.fetchone()["id"]
            created_proposals += 1
        proposal_id = proposal_cache[key]

        if category or item_name:
            try:
                qty = float(quantity) if quantity not in (None, "") else 1
                price = float(unit_price) if unit_price not in (None, "") else 0
            except (TypeError, ValueError):
                errors.append(f"Row {idx}: quantity/unit price must be numbers")
                continue
            total = float(total_amount) if total_amount not in (None, "") else qty * price
            execute(
                conn,
                """INSERT INTO budget_line_items (proposal_id,category,item_name,description,quantity,unit,unit_price,total_amount)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                (proposal_id, category or "", item_name or "", description or "", qty, unit or "unit", price, total),
            )
            created_items += 1

    return {
        "ok": True,
        "proposals_created": created_proposals,
        "line_items_created": created_items,
        "errors": errors,
    }
