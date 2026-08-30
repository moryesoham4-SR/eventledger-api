from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional
from core.database import get_db, execute
from core.auth import get_current_user
from utils.db_safety import run_safely
from utils.roles import get_event_role
from utils.email import send_claim_submitted_email, send_claim_verified_email, send_claim_paid_email, get_super_admin_emails

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

    # Get department title
    cur_d = execute(conn, "SELECT name FROM departments WHERE id=%s", (data.department_id,))
    d_row = cur_d.fetchone()
    dept_title = d_row["name"] if d_row else "Department"

    # Find Dept Head, Finance Head & Event Admins to notify in DB & via Email
    cur_heads = execute(conn, """
        SELECT DISTINCT r.user_id, u.email FROM user_event_roles r
        JOIN users u ON u.id = r.user_id
        WHERE r.event_id=%s AND (r.role IN ('event_admin', 'co_leader', 'finance_head') OR (r.role='dept_head' AND r.dept_id=%s))
    """, (data.event_id, data.department_id))
    head_rows = cur_heads.fetchall()

    notif_msg = f"📥 REIMBURSEMENT CLAIM: {user_name} submitted ₹{data.amount:,.2f} for '{data.item_name}' in {dept_title}"
    for h in head_rows:
        run_safely(conn, lambda hid=h["user_id"]: execute(conn, """
            INSERT INTO notifications (user_id, message, priority, category, event_id, action_url)
            VALUES (%s, %s, 'info', 'general', %s, '/expenses')
        """, (hid, notif_msg, data.event_id)))

        if h.get("email"):
            send_claim_submitted_email(h["email"], user_name, data.amount, dept_title, data.item_name)

    # Always broadcast email copy to all Super Admins
    for sa_email in get_super_admin_emails(conn):
        send_claim_submitted_email(sa_email, user_name, data.amount, dept_title, data.item_name)

    return claim

@router.put("/{claim_id}/dept-approval")
def dept_head_approve_claim(claim_id: int, data: DeptApprovalRequest, conn=Depends(get_db), user=Depends(get_current_user)):
    ensure_reimbursements_schema(conn)
    cur_c = execute(conn, "SELECT * FROM expense_reimbursements WHERE id=%s", (claim_id,))
    claim = cur_c.fetchone()
    if not claim:
        raise HTTPException(status_code=404, detail="Claim not found")

    role_ctx = _require_event_access(conn, user, claim["event_id"])
    if role_ctx["level"] not in ("co_leader", "event_admin", "finance_head", "dept_head"):
        raise HTTPException(status_code=403, detail="Only a Dept Head or Event Admin can verify department claims")

    if role_ctx["level"] == "dept_head" and str(role_ctx["dept_id"]) != str(claim["department_id"]):
        raise HTTPException(status_code=403, detail="You can only verify claims for your own department")

    notes_str = (data.notes or "").strip()
    if data.status.lower() == "rejected" and not notes_str:
        raise HTTPException(status_code=400, detail="A reason for rejecting the reimbursement claim is compulsory.")

    cur = execute(conn, """
        UPDATE expense_reimbursements
        SET dept_head_status=%s, dept_head_notes=%s
        WHERE id=%s
        RETURNING *
    """, (data.status, notes_str, claim_id))
    
    updated = dict(cur.fetchone())

    # Get department title
    cur_d = execute(conn, "SELECT name FROM departments WHERE id=%s", (claim["department_id"],))
    d_row = cur_d.fetchone()
    dept_title = d_row["name"] if d_row else "Department"

    # Notify Finance Heads & Event Admins when verified by Dept Head via DB & Email
    cur_fin = execute(conn, """
        SELECT DISTINCT r.user_id, u.email FROM user_event_roles r
        JOIN users u ON u.id = r.user_id
        WHERE r.event_id=%s AND r.role IN ('event_admin', 'co_leader', 'finance_head')
    """, (claim["event_id"],))
    fin_rows = cur_fin.fetchall()

    fin_notif = f"🏢 DEPT HEAD VERIFIED: Claim ₹{claim['amount']:,.2f} for '{claim['item_name']}' verified by Dept Head. Ready for Finance Payout!"
    for f in fin_rows:
        run_safely(conn, lambda fid=f["user_id"]: execute(conn, """
            INSERT INTO notifications (user_id, message, priority, category, event_id, action_url)
            VALUES (%s, %s, 'info', 'general', %s, '/expenses')
        """, (fid, fin_notif, claim["event_id"])))

        if f.get("email"):
            send_claim_verified_email(f["email"], claim["claimed_by_name"], float(claim["amount"]), dept_title, claim["item_name"])

    # Always broadcast email copy to all Super Admins
    for sa_email in get_super_admin_emails(conn):
        send_claim_verified_email(sa_email, claim["claimed_by_name"], float(claim["amount"]), dept_title, claim["item_name"])

    # Notify claiming co-worker
    coworker_notif = f"🏢 DEPT HEAD REVIEW: Your claim for '{claim['item_name']}' was marked '{data.status.upper()}' by Dept Head!"
    run_safely(conn, lambda: execute(conn, """
        INSERT INTO notifications (user_id, message, priority, category, event_id, action_url)
        VALUES (%s, %s, 'info', 'general', %s, '/expenses')
    """, (claim["claimed_by_user_id"], coworker_notif, claim["event_id"])))

    return updated

@router.put("/{claim_id}/finance-payout")
def finance_head_payout_claim(claim_id: int, data: FinancePayoutRequest, conn=Depends(get_db), user=Depends(get_current_user)):
    ensure_reimbursements_schema(conn)
    cur_c = execute(conn, "SELECT * FROM expense_reimbursements WHERE id=%s", (claim_id,))
    claim = cur_c.fetchone()
    if not claim:
        raise HTTPException(status_code=404, detail="Claim not found")

    role_ctx = _require_event_access(conn, user, claim["event_id"])
    if role_ctx["level"] not in ("co_leader", "event_admin", "finance_head"):
        raise HTTPException(status_code=403, detail="Only Finance Head or Event Admin can approve final payouts")

    notes_str = (data.notes or "").strip()
    if data.status.lower() == "rejected" and not notes_str:
        raise HTTPException(status_code=400, detail="A reason for rejecting the claim payout is compulsory.")

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
        """, (claim["event_id"], claim["department_id"], claim["category"], claim["item_name"], desc, claim["amount"], data.payment_mode, ref_tag, notes_str)))

    # Notify claiming co-worker via DB & Email
    payout_notif = f"💸 FINANCE PAYOUT: Your claim for '{claim['item_name']}' (₹{claim['amount']:,.2f}) was marked '{data.status.upper()}'! Ref: {data.payout_reference or 'N/A'}"
    run_safely(conn, lambda: execute(conn, """
        INSERT INTO notifications (user_id, message, priority, category, event_id, action_url)
        VALUES (%s, %s, 'info', 'general', %s, '/expenses')
    """, (claim["claimed_by_user_id"], payout_notif, claim["event_id"])))

    cur_u = execute(conn, "SELECT email, name FROM users WHERE id=%s", (claim["claimed_by_user_id"],))
    c_u = cur_u.fetchone()
    if c_u and c_u.get("email") and data.status == "paid_out":
        send_claim_paid_email(c_u["email"], c_u.get("name") or claim["claimed_by_name"], float(claim["amount"]), claim["item_name"], data.payout_reference or "")

    # Always broadcast email copy to all Super Admins
    if data.status == "paid_out":
        for sa_email in get_super_admin_emails(conn):
            send_claim_paid_email(sa_email, claim["claimed_by_name"], float(claim["amount"]), claim["item_name"], data.payout_reference or "")

    return updated
