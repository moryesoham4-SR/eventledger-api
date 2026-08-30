from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from typing import Optional
from core.database import get_db, execute
from core.auth import get_current_user
from utils.roles import get_event_role, can_access_department, can_edit_department, can_approve_budget
from utils.db_safety import run_safely
from utils.activity import log_activity, ACTION_BUDGET_SUBMITTED, ACTION_BUDGET_APPROVED, ACTION_BUDGET_REJECTED, ACTION_BUDGET_IMPORTED
from utils.email import send_budget_submitted_email, send_budget_status_email, get_super_admin_emails
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
    notes: Optional[str] = ""

class LineItemCreate(BaseModel):
    proposal_id: int
    category: str
    item_name: str
    description: Optional[str] = ""
    quantity: float = 1
    unit: str = "unit"
    unit_price: float
    total_amount: float

class RejectRequest(BaseModel):
    reason: str

def ensure_budget_schema(conn):
    """Ensures budget_proposals and budget_line_items tables and missing columns exist safely."""
    run_safely(conn, lambda: execute(conn, """
        CREATE TABLE IF NOT EXISTS budget_proposals (
            id SERIAL PRIMARY KEY,
            event_id INT NOT NULL,
            department_id INT NOT NULL,
            title VARCHAR(255) NOT NULL,
            notes TEXT,
            status VARCHAR(20) DEFAULT 'draft',
            total_amount NUMERIC(12,2) DEFAULT 0,
            created_by INT,
            submitted_by INT,
            approved_by INT,
            rejected_by INT,
            reject_reason TEXT,
            submitted_at VARCHAR(50),
            approved_at VARCHAR(50),
            rejected_at VARCHAR(50),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """))

    run_safely(conn, lambda: execute(conn, """
        CREATE TABLE IF NOT EXISTS budget_line_items (
            id SERIAL PRIMARY KEY,
            proposal_id INT NOT NULL,
            category VARCHAR(100),
            item_name VARCHAR(255) NOT NULL,
            description TEXT,
            quantity NUMERIC(12,2) DEFAULT 1,
            unit VARCHAR(50) DEFAULT 'unit',
            unit_price NUMERIC(12,2) DEFAULT 0,
            total_amount NUMERIC(12,2) DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """))

    run_safely(conn, lambda: execute(conn, "ALTER TABLE budget_proposals ADD COLUMN IF NOT EXISTS notes TEXT"))
    run_safely(conn, lambda: execute(conn, "ALTER TABLE budget_proposals ADD COLUMN IF NOT EXISTS created_by INT"))
    run_safely(conn, lambda: execute(conn, "ALTER TABLE budget_proposals ADD COLUMN IF NOT EXISTS submitted_by INT"))
    run_safely(conn, lambda: execute(conn, "ALTER TABLE budget_proposals ADD COLUMN IF NOT EXISTS approved_by INT"))
    run_safely(conn, lambda: execute(conn, "ALTER TABLE budget_proposals ADD COLUMN IF NOT EXISTS rejected_by INT"))
    run_safely(conn, lambda: execute(conn, "ALTER TABLE budget_proposals ADD COLUMN IF NOT EXISTS reject_reason TEXT"))

def _get_proposal_or_404(conn, proposal_id):
    ensure_budget_schema(conn)
    cur = execute(conn, "SELECT * FROM budget_proposals WHERE id=%s", (proposal_id,))
    p = cur.fetchone()
    if not p:
        raise HTTPException(status_code=404, detail="Proposal not found")
    return dict(p)

def _notify(conn, user_id, message):
    if not user_id:
        return
    execute(conn, "INSERT INTO notifications (user_id, message, is_read) VALUES (%s,%s,0)", (user_id, message))

def _get_approver_ids(conn, event_id, exclude_user_id=None):
    ids = set()
    cur = execute(conn, "SELECT user_id FROM events WHERE id=%s", (event_id,))
    owner = cur.fetchone()
    if owner:
        ids.add(owner["user_id"])
    cur = execute(conn,
        "SELECT user_id FROM user_event_roles WHERE event_id=%s AND role IN ('event_admin','co_leader','finance_head')",
        (event_id,)
    )
    for row in cur.fetchall():
        ids.add(row["user_id"])

    # Include all Super Admins so Super Admin receives all in-app notifications
    cur_sa = execute(conn, "SELECT id FROM users WHERE is_super_admin=1")
    for r in cur_sa.fetchall():
        ids.add(r["id"])

    ids.discard(exclude_user_id)
    return ids

@router.get("/proposals")
def get_proposals(event_id: int, conn=Depends(get_db), user=Depends(get_current_user)):
    ensure_budget_schema(conn)
    role_ctx = get_event_role(conn, user, event_id)
    if role_ctx["level"] is None:
        raise HTTPException(status_code=403, detail="You don't have access to this event")

    try:
        cur = execute(conn,
            """SELECT p.*, d.name as dept_name,
                      u_app.name as approved_by_name,
                      u_rej.name as rejected_by_name,
                      (SELECT COALESCE(SUM(total_amount), 0) FROM budget_line_items WHERE proposal_id = p.id) as total_amount
               FROM budget_proposals p
               LEFT JOIN departments d ON d.id = p.department_id
               LEFT JOIN users u_app ON u_app.id = p.approved_by
               LEFT JOIN users u_rej ON u_rej.id = p.rejected_by
               WHERE p.event_id = %s
               ORDER BY p.created_at DESC""",
            (event_id,)
        )
        proposals = [dict(r) for r in cur.fetchall()]
    except Exception:
        cur = execute(conn,
            """SELECT p.*, d.name as dept_name FROM budget_proposals p
               LEFT JOIN departments d ON d.id = p.department_id
               WHERE p.event_id = %s
               ORDER BY p.id DESC""",
            (event_id,)
        )
        proposals = [dict(r) for r in cur.fetchall()]

    visible = [p for p in proposals if can_access_department(role_ctx, p["department_id"])]

    for p in visible:
        try:
            cur_items = execute(conn, "SELECT * FROM budget_line_items WHERE proposal_id=%s ORDER BY id", (p["id"],))
            p["line_items"] = [dict(r) for r in cur_items.fetchall()]
        except Exception:
            p["line_items"] = []

    return visible

@router.post("/proposals")
def create_proposal(data: ProposalCreate, conn=Depends(get_db), user=Depends(get_current_user)):
    ensure_budget_schema(conn)
    role_ctx = get_event_role(conn, user, data.event_id)
    if not can_edit_department(role_ctx, data.department_id):
        raise HTTPException(status_code=403, detail="You can't create budget proposals for this department")

    try:
        cur = execute(conn,
            """INSERT INTO budget_proposals (event_id, department_id, title, notes, created_by, submitted_by, status)
               VALUES (%s, %s, %s, %s, %s, %s, 'draft') RETURNING *""",
            (data.event_id, data.department_id, data.title, data.notes or "", user["id"], user["id"])
        )
    except Exception:
        cur = execute(conn,
            """INSERT INTO budget_proposals (event_id, department_id, title, status)
               VALUES (%s, %s, %s, 'draft') RETURNING *""",
            (data.event_id, data.department_id, data.title)
        )

    p = dict(cur.fetchone())
    p["line_items"] = []
    return p

@router.get("/proposals/{proposal_id}")
def get_proposal_detail(proposal_id: int, conn=Depends(get_db), user=Depends(get_current_user)):
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
    try:
        cur2 = execute(conn, "SELECT * FROM budget_line_items WHERE proposal_id=%s", (proposal_id,))
        result["line_items"] = [dict(r) for r in cur2.fetchall()]
    except Exception:
        result["line_items"] = []
    return result

@router.post("/proposals/{proposal_id}/submit")
def submit_proposal(proposal_id: int, conn=Depends(get_db), user=Depends(get_current_user)):
    p = _get_proposal_or_404(conn, proposal_id)
    role_ctx = get_event_role(conn, user, p["event_id"])
    if not can_edit_department(role_ctx, p["department_id"]):
        raise HTTPException(status_code=403, detail="You can't submit this budget proposal")

    now = datetime.datetime.utcnow().isoformat()
    try:
        cur = execute(conn, "SELECT COALESCE(SUM(total_amount),0) as t FROM budget_line_items WHERE proposal_id=%s", (proposal_id,))
        total = float(list(cur.fetchone().values())[0])
    except Exception:
        total = 0.0

    execute(conn,
        "UPDATE budget_proposals SET status='submitted', submitted_at=%s, total_amount=%s WHERE id=%s",
        (now, total, proposal_id)
    )

    def _notify_approvers():
        cur = execute(conn, "SELECT d.name AS dept_name, e.name AS event_name FROM departments d JOIN events e ON e.id=d.event_id WHERE d.id=%s", (p["department_id"],))
        ctx = cur.fetchone()
        dept_title = ctx['dept_name'] if ctx else "Department"
        for uid in _get_approver_ids(conn, p["event_id"], exclude_user_id=user["id"]):
            _notify(conn, uid, f"New budget \"{p['title']}\" from {dept_title} needs your approval — {ctx['event_name'] if ctx else ''}")
            cur_u = execute(conn, "SELECT email FROM users WHERE id=%s", (uid,))
            app_u = cur_u.fetchone()
            if app_u and app_u.get("email"):
                send_budget_submitted_email(app_u["email"], dept_title, p["title"], total, user.get("name") or "Dept Head")
        
        # Always send copy to Super Admins
        for sa_email in get_super_admin_emails(conn):
            send_budget_submitted_email(sa_email, dept_title, p["title"], total, user.get("name") or "Dept Head")

    run_safely(conn, _notify_approvers)
    log_activity(conn, p["event_id"], user["id"], ACTION_BUDGET_SUBMITTED,
                 f"{user['name']} submitted budget \"{p['title']}\" for approval")

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

    # Notify submitting department head
    if p.get("submitted_by") and p["submitted_by"] != user["id"]:
        run_safely(conn, lambda: _notify(conn, p["submitted_by"], f"Your budget \"{p['title']}\" was approved ✅"))
        cur_sub = execute(conn, "SELECT email FROM users WHERE id=%s", (p["submitted_by"],))
        sub_u = cur_sub.fetchone()
        if sub_u and sub_u.get("email"):
            send_budget_status_email(sub_u["email"], p["title"], "approved", user.get("name") or "Finance Lead", "")

    # ALWAYS send copy to all Super Admins!
    for sa_email in get_super_admin_emails(conn):
        send_budget_status_email(sa_email, p["title"], "approved", user.get("name") or "Finance Lead", "")

    log_activity(conn, p["event_id"], user["id"], ACTION_BUDGET_APPROVED,
                 f"{user['name']} approved budget \"{p['title']}\"")
    return {"ok": True, "message": "Budget approved"}

@router.post("/proposals/{proposal_id}/reject")
def reject_proposal(proposal_id: int, data: RejectRequest, conn=Depends(get_db), user=Depends(get_current_user)):
    p = _get_proposal_or_404(conn, proposal_id)
    role_ctx = get_event_role(conn, user, p["event_id"])
    if not can_approve_budget(role_ctx):
        raise HTTPException(status_code=403, detail="Only an event admin or finance role can reject budgets")

    reason_str = (data.reason or "").strip()
    if not reason_str:
        raise HTTPException(status_code=400, detail="A reason for rejecting the budget proposal is compulsory.")

    now = datetime.datetime.utcnow().isoformat()
    execute(conn,
        "UPDATE budget_proposals SET status='rejected', rejected_by=%s, rejected_at=%s, reject_reason=%s WHERE id=%s",
        (user["id"], now, reason_str, proposal_id)
    )

    # Notify submitting department head
    if p.get("submitted_by") and p["submitted_by"] != user["id"]:
        reason_suffix = f": {reason_str}" if reason_str else ""
        run_safely(conn, lambda: _notify(conn, p["submitted_by"], f"Your budget \"{p['title']}\" was rejected{reason_suffix}"))
        cur_sub = execute(conn, "SELECT email FROM users WHERE id=%s", (p["submitted_by"],))
        sub_u = cur_sub.fetchone()
        if sub_u and sub_u.get("email"):
            send_budget_status_email(sub_u["email"], p["title"], "rejected", user.get("name") or "Finance Lead", reason_str)

    # ALWAYS send copy to all Super Admins!
    for sa_email in get_super_admin_emails(conn):
        send_budget_status_email(sa_email, p["title"], "rejected", user.get("name") or "Finance Lead", reason_str)

    log_activity(conn, p["event_id"], user["id"], ACTION_BUDGET_REJECTED,
                 f"{user['name']} rejected budget \"{p['title']}\"" + (f" — {reason_str}" if reason_str else ""))
    return {"ok": True, "message": "Budget rejected"}

@router.post("/line-items")
def add_line_item(data: LineItemCreate, conn=Depends(get_db), user=Depends(get_current_user)):
    ensure_budget_schema(conn)
    p = _get_proposal_or_404(conn, data.proposal_id)
    role_ctx = get_event_role(conn, user, p["event_id"])
    if not can_edit_department(role_ctx, p["department_id"]):
        raise HTTPException(status_code=403, detail="You can't edit this budget proposal")

    cur = execute(conn,
        """INSERT INTO budget_line_items (proposal_id,category,item_name,description,quantity,unit,unit_price,total_amount)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
        (data.proposal_id, data.category, data.item_name, data.description or "",
         data.quantity, data.unit, data.unit_price, data.total_amount)
    )
    return dict(cur.fetchone())

@router.delete("/line-items/{item_id}")
def delete_line_item(item_id: int, conn=Depends(get_db), user=Depends(get_current_user)):
    ensure_budget_schema(conn)
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
