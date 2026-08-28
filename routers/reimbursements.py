from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional
from core.database import get_db, execute
from core.auth import get_current_user
from utils.db_safety import run_safely
from utils.roles import get_event_role

router = APIRouter(prefix="/api/reimbursements", tags=["reimbursements"])

class ClaimCreate(BaseModel):
    event_id: int
    department_id: int
    item_name: str
    category: str = "General"
    amount: float
    receipt_url: Optional[str] = ""
    notes: Optional[str] = ""

class DeptApprovalRequest(BaseModel):
    status: str  # 'approved' or 'rejected'
    notes: Optional[str] = ""

class FinancePayoutRequest(BaseModel):
    status: str  # 'paid_out' or 'rejected'
    payment_mode: str = "UPI"
    payout_reference: Optional[str] = ""
    notes: Optional[str] = ""


def ensure_reimbursements_schema(conn):
    run_safely(conn, lambda: execute(conn, """
        CREATE TABLE IF NOT EXISTS expense_reimbursements (
            id SERIAL PRIMARY KEY,
            event_id INT NOT NULL,
            department_id INT NOT NULL,
            claimed_by_user_id INT NOT NULL,
            claimed_by_name VARCHAR(255),
            item_name VARCHAR(255) NOT NULL,
            category VARCHAR(100) DEFAULT 'General',
            amount NUMERIC(12,2) NOT NULL DEFAULT 0,
            receipt_url TEXT,
            notes TEXT,
            dept_head_status VARCHAR(20) DEFAULT 'pending',
            dept_head_notes TEXT,
            finance_status VARCHAR(20) DEFAULT 'pending',
            payment_mode VARCHAR(50) DEFAULT 'UPI',
            payout_reference VARCHAR(100),
            paid_out_date VARCHAR(50),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """))


def _require_event_access(conn, user, event_id):
    role_ctx = get_event_role(conn, user, event_id)
    if role_ctx["level"] is None:
        raise HTTPException(status_code=403, detail="You don't have access to this event")
    return role_ctx


@router.get("/")
def get_reimbursements(event_id: int, conn=Depends(get_db), user=Depends(get_current_user)):
    ensure_reimbursements_schema(conn)
    role_ctx = _require_event_access(conn, user, event_id)

    query = """
        SELECT r.*, d.name as dept_name, d.color as dept_color, u.email as user_email
        FROM expense_reimbursements r
        JOIN departments d ON d.id = r.department_id
        JOIN users u ON u.id = r.claimed_by_user_id
        WHERE r.event_id = %s
    """
    params = [event_id]

    # Scoped visibility: dept_head and volunteers see their own department claims
    if role_ctx["level"] in ("dept_head", "volunteer") and role_ctx["dept_id"]:
        query += " AND r.department_id = %s"
        params.append(role_ctx["dept_id"])

    query += " ORDER BY r.created_at DESC"
    cur = execute(conn, query, params)
    return [dict(row) for row in cur.fetchall()]


@router.post("/")
def submit_reimbursement(data: ClaimCreate, conn=Depends(get_db), user=Depends(get_current_user)):
    ensure_reimbursements_schema(conn)
    _require_event_access(conn, user, data.event_id)

    user_name = user.get("name") or user.get("email") or "Co-Worker"

    cur = execute(conn, """
        INSERT INTO expense_reimbursements
        (event_id, department_id, claimed_by_user_id, claimed_by_name, item_name, category, amount, receipt_url, notes)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING *
    """, (data.event_id, data.department_id, user["id"], user_name, data.item_name, data.category, data.amount, data.receipt_url, data.notes))
    
    claim = dict(cur.fetchone())

    # Send notification to event admins & dept head
    notif_msg = f"📥 REIMBURSEMENT CLAIM: {user_name} submitted ₹{data.amount} for '{data.item_name}'"
    run_safely(conn, lambda: execute(conn, """
        INSERT INTO notifications (user_id, message, priority, category, event_id, action_url)
        VALUES (%s, %s, 'info', 'general', %s, '/expenses')
    """, (user["id"], notif_msg, data.event_id)))

    return claim


@router.put("/{claim_id}/dept-approval")
def dept_head_approve_claim(claim_id: int, data: DeptApprovalRequest, conn=Depends(get_db), user=Depends(get_current_user)):
    ensure_reimbursements_schema(conn)
    cur_c = execute(conn, "SELECT * FROM expense_reimbursements WHERE id=%s", (claim_id,))
    claim = cur_c.fetchone()
    if not claim:
        raise HTTPException(status_code=404, detail="Claim not found")

    role_ctx = _require_event_access(conn, user, claim["event_id"])
    if role_ctx["level"] not in ("event_admin", "finance_head", "dept_head"):
        raise HTTPException(status_code=403, detail="Only a Dept Head or Event Admin can verify department claims")

    if role_ctx["level"] == "dept_head" and String(role_ctx["dept_id"]) != String(claim["department_id"]):
        raise HTTPException(status_code=403, detail="You can only verify claims for your own department")

    cur = execute(conn, """
        UPDATE expense_reimbursements
        SET dept_head_status=%s, dept_head_notes=%s
        WHERE id=%s
        RETURNING *
    """, (data.status, data.notes, claim_id))
    
    updated = dict(cur.fetchone())

    # Notify claiming co-worker
    notif_msg = f"🏢 DEPT HEAD REVIEW: Your claim for '{claim['item_name']}' was marked '{data.status.upper()}' by Dept Head"
    run_safely(conn, lambda: execute(conn, """
        INSERT INTO notifications (user_id, message, priority, category, event_id, action_url)
        VALUES (%s, %s, 'info', 'general', %s, '/expenses')
    """, (claim["claimed_by_user_id"], notif_msg, claim["event_id"])))

    return updated


@router.put("/{claim_id}/finance-payout")
def finance_head_payout_claim(claim_id: int, data: FinancePayoutRequest, conn=Depends(get_db), user=Depends(get_current_user)):
    ensure_reimbursements_schema(conn)
    cur_c = execute(conn, "SELECT * FROM expense_reimbursements WHERE id=%s", (claim_id,))
    claim = cur_c.fetchone()
    if not claim:
        raise HTTPException(status_code=404, detail="Claim not found")

    role_ctx = _require_event_access(conn, user, claim["event_id"])
    if role_ctx["level"] not in ("event_admin", "finance_head"):
        raise HTTPException(status_code=403, detail="Only Finance Head or Event Admin can approve final payouts")

    today = "today"
    cur = execute(conn, """
        UPDATE expense_reimbursements
        SET finance_status=%s, payment_mode=%s, payout_reference=%s, paid_out_date=%s
        WHERE id=%s
        RETURNING *
    """, (data.status, data.payment_mode, data.payout_reference, today if data.status == "paid_out" else None, claim_id))
    
    updated = dict(cur.fetchone())

    # CRITICAL: ONLY WHEN FINANCE HEAD APPROVES & PAYS OUT -> LOG INTO ACTUAL_EXPENSES LEDGER!
    if data.status == "paid_out":
        ref_tag = f"reimbursement:{claim_id}"
        desc = f"Reimbursement payout to {claim['claimed_by_name']} (Ref: {data.payout_reference or 'N/A'})"
        run_safely(conn, lambda: execute(conn, """
            INSERT INTO actual_expenses
            (event_id, department_id, category, item_name, description, quantity, unit, amount, payment_mode, status, reference, notes)
            VALUES (%s, %s, %s, %s, %s, 1, 'unit', %s, %s, 'paid', %s, %s)
        """, (claim["event_id"], claim["department_id"], claim["category"], f"Reimbursement: {claim['item_name']}",
              desc, claim["amount"], data.payment_mode, ref_tag, f"Paid out to {claim['claimed_by_name']}")))

    elif data.status != "paid_out":
        # If toggled back from paid_out, remove from actual_expenses
        ref_tag = f"reimbursement:{claim_id}"
        run_safely(conn, lambda: execute(conn, "DELETE FROM actual_expenses WHERE reference=%s", (ref_tag,)))

    # Notify claiming co-worker
    notif_msg = f"💰 FINANCE PAYOUT: Your ₹{claim['amount']} claim for '{claim['item_name']}' was PAID OUT by Finance Head!"
    run_safely(conn, lambda: execute(conn, """
        INSERT INTO notifications (user_id, message, priority, category, event_id, action_url)
        VALUES (%s, %s, 'info', 'general', %s, '/expenses')
    """, (claim["claimed_by_user_id"], notif_msg, claim["event_id"])))

    return updated


@router.delete("/{claim_id}")
def delete_reimbursement(claim_id: int, conn=Depends(get_db), user=Depends(get_current_user)):
    ensure_reimbursements_schema(conn)
    cur_c = execute(conn, "SELECT * FROM expense_reimbursements WHERE id=%s", (claim_id,))
    claim = cur_c.fetchone()
    if not claim:
        raise HTTPException(status_code=404, detail="Claim not found")

    role_ctx = _require_event_access(conn, user, claim["event_id"])
    is_owner = claim["claimed_by_user_id"] == user["id"]
    is_admin = role_ctx["level"] in ("event_admin", "finance_head")

    if not is_owner and not is_admin:
        raise HTTPException(status_code=403, detail="You can only delete your own reimbursement claims")

    ref_tag = f"reimbursement:{claim_id}"
    run_safely(conn, lambda: execute(conn, "DELETE FROM actual_expenses WHERE reference=%s", (ref_tag,)))
    execute(conn, "DELETE FROM expense_reimbursements WHERE id=%s", (claim_id,))
    return {"ok": True}
