from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional
from core.database import get_db, execute
from core.auth import get_current_user
from utils.db_safety import run_safely
from utils.roles import get_event_role

router = APIRouter(prefix="/api/vendors", tags=["vendors"])

class VendorCreate(BaseModel):
    event_id: int
    name: str
    category: str = "Other"
    contact_name: str = ""
    contact_email: str = ""
    contract_value: float = 0
    status: str = "active"
    notes: str = ""

class MilestoneCreate(BaseModel):
    event_id: int
    vendor_id: int
    milestone_name: str
    due_date: Optional[str] = ""
    amount: float
    status: str = "pending"
    notes: Optional[str] = ""

class MilestoneUpdate(BaseModel):
    milestone_name: Optional[str] = None
    due_date: Optional[str] = None
    amount: Optional[float] = None
    status: Optional[str] = None
    paid_date: Optional[str] = None
    payment_mode: Optional[str] = None
    reference: Optional[str] = None
    notes: Optional[str] = None


def ensure_vendor_milestones_schema(conn):
    run_safely(conn, lambda: execute(conn, """
        CREATE TABLE IF NOT EXISTS vendor_payment_milestones (
            id SERIAL PRIMARY KEY,
            event_id INT NOT NULL,
            vendor_id INT NOT NULL,
            milestone_name VARCHAR(255) NOT NULL,
            due_date VARCHAR(50),
            amount NUMERIC(12,2) NOT NULL DEFAULT 0,
            status VARCHAR(20) DEFAULT 'pending',
            paid_date VARCHAR(50),
            payment_mode VARCHAR(50) DEFAULT 'Bank Transfer',
            reference VARCHAR(100),
            notes TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """))


def _require_event_access(conn, user, event_id):
    role_ctx = get_event_role(conn, user, event_id)
    if role_ctx["level"] is None:
        raise HTTPException(status_code=403, detail="You don't have access to this event")
    return role_ctx


def _require_finance(conn, user, event_id):
    role_ctx = get_event_role(conn, user, event_id)
    if role_ctx["level"] not in ("event_admin", "finance_head"):
        raise HTTPException(status_code=403, detail="Only an event admin or finance head can manage vendors")
    return role_ctx


@router.get("/")
def get_vendors(event_id: int, conn=Depends(get_db), user=Depends(get_current_user)):
    _require_event_access(conn, user, event_id)
    cur = execute(conn, "SELECT * FROM vendors WHERE event_id=%s ORDER BY created_at DESC", (event_id,))
    return [dict(r) for r in cur.fetchall()]

@router.post("/")
def add_vendor(data: VendorCreate, conn=Depends(get_db), user=Depends(get_current_user)):
    _require_finance(conn, user, data.event_id)
    cur = execute(conn,
        """INSERT INTO vendors (event_id,name,category,contact_name,contact_email,contract_value,status,notes)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
        (data.event_id, data.name, data.category, data.contact_name,
         data.contact_email, data.contract_value, data.status, data.notes)
    )
    vendor = dict(cur.fetchone())

    if data.contract_value:
        def _sync():
            execute(conn,
                """INSERT INTO actual_expenses (event_id,category,item_name,description,quantity,unit,amount,payment_mode,status,reference,notes)
                   VALUES (%s,'Vendor',%s,%s,1,'unit',%s,'Bank Transfer','paid',%s,%s)""",
                (data.event_id, data.name, f"Vendor contract: {data.name}", data.contract_value,
                 f"vendor:{vendor['id']}", f"Auto-synced from vendor #{vendor['id']}")
            )
        run_safely(conn, _sync)

    return vendor

@router.delete("/{vendor_id}")
def delete_vendor(vendor_id: int, conn=Depends(get_db), user=Depends(get_current_user)):
    cur = execute(conn, "SELECT event_id FROM vendors WHERE id=%s", (vendor_id,))
    vendor = cur.fetchone()
    if not vendor:
        raise HTTPException(status_code=404, detail="Vendor not found")
    _require_finance(conn, user, vendor["event_id"])
    run_safely(conn, lambda: execute(conn, "DELETE FROM actual_expenses WHERE reference=%s", (f"vendor:{vendor_id}",)))
    run_safely(conn, lambda: execute(conn, "DELETE FROM vendor_payment_milestones WHERE vendor_id=%s", (vendor_id,)))
    execute(conn, "DELETE FROM vendors WHERE id=%s", (vendor_id,))
    return {"ok": True}

# ==================== VENDOR PAYMENT MILESTONES ====================

@router.get("/{vendor_id}/milestones")
def get_vendor_milestones(vendor_id: int, conn=Depends(get_db), user=Depends(get_current_user)):
    ensure_vendor_milestones_schema(conn)
    cur_v = execute(conn, "SELECT event_id FROM vendors WHERE id=%s", (vendor_id,))
    v_row = cur_v.fetchone()
    if not v_row:
        raise HTTPException(status_code=404, detail="Vendor not found")
    _require_event_access(conn, user, v_row["event_id"])

    cur = execute(conn, "SELECT * FROM vendor_payment_milestones WHERE vendor_id=%s ORDER BY id ASC", (vendor_id,))
    return [dict(r) for r in cur.fetchall()]

@router.post("/milestones")
def create_vendor_milestone(data: MilestoneCreate, conn=Depends(get_db), user=Depends(get_current_user)):
    ensure_vendor_milestones_schema(conn)
    _require_finance(conn, user, data.event_id)

    cur = execute(conn, """
        INSERT INTO vendor_payment_milestones
        (event_id, vendor_id, milestone_name, due_date, amount, status, notes)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        RETURNING *
    """, (data.event_id, data.vendor_id, data.milestone_name, data.due_date, data.amount, data.status, data.notes))
    return dict(cur.fetchone())

@router.post("/{vendor_id}/auto-generate-milestones")
def auto_generate_milestones(vendor_id: int, conn=Depends(get_db), user=Depends(get_current_user)):
    """Auto-generates standard 30% Advance, 50% On Setup, 20% Post-Event Settlement milestones."""
    ensure_vendor_milestones_schema(conn)
    cur_v = execute(conn, "SELECT * FROM vendors WHERE id=%s", (vendor_id,))
    v = cur_v.fetchone()
    if not v:
        raise HTTPException(status_code=404, detail="Vendor not found")
    _require_finance(conn, user, v["event_id"])

    contract_val = float(v["contract_value"] or 0)
    if contract_val <= 0:
        raise HTTPException(status_code=400, detail="Vendor contract value must be greater than 0 to generate milestones")

    # Clear existing milestones
    execute(conn, "DELETE FROM vendor_payment_milestones WHERE vendor_id=%s", (vendor_id,))

    m1_amt = round(contract_val * 0.30, 2)
    m2_amt = round(contract_val * 0.50, 2)
    m3_amt = round(contract_val - m1_amt - m2_amt, 2)

    milestones = [
        (v["event_id"], vendor_id, "30% Advance Payment on Contract Signing", "", m1_amt, "pending", "Initial deposit"),
        (v["event_id"], vendor_id, "50% Delivery & Setup Payment", "", m2_amt, "pending", "Stage setup & equipment delivery"),
        (v["event_id"], vendor_id, "20% Final Post-Event Settlement", "", m3_amt, "pending", "Final sign-off post event"),
    ]

    inserted = []
    for m in milestones:
        cur_i = execute(conn, """
            INSERT INTO vendor_payment_milestones
            (event_id, vendor_id, milestone_name, due_date, amount, status, notes)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING *
        """, m)
        inserted.append(dict(cur_i.fetchone()))

    return inserted

@router.put("/milestones/{milestone_id}")
def update_vendor_milestone(milestone_id: int, data: MilestoneUpdate, conn=Depends(get_db), user=Depends(get_current_user)):
    ensure_vendor_milestones_schema(conn)
    cur_m = execute(conn, "SELECT * FROM vendor_payment_milestones WHERE id=%s", (milestone_id,))
    m_row = cur_m.fetchone()
    if not m_row:
        raise HTTPException(status_code=404, detail="Milestone not found")
    _require_finance(conn, user, m_row["event_id"])

    fields = {k: v for k, v in data.dict().items() if v is not None}
    if not fields:
        raise HTTPException(status_code=400, detail="No fields to update")

    set_clause = ", ".join(f"{k}=%s" for k in fields)
    values = list(fields.values()) + [milestone_id]
    cur_u = execute(conn, f"UPDATE vendor_payment_milestones SET {set_clause} WHERE id=%s RETURNING *", values)
    return dict(cur_u.fetchone())

@router.delete("/milestones/{milestone_id}")
def delete_vendor_milestone(milestone_id: int, conn=Depends(get_db), user=Depends(get_current_user)):
    ensure_vendor_milestones_schema(conn)
    cur_m = execute(conn, "SELECT event_id FROM vendor_payment_milestones WHERE id=%s", (milestone_id,))
    m_row = cur_m.fetchone()
    if not m_row:
        raise HTTPException(status_code=404, detail="Milestone not found")
    _require_finance(conn, user, m_row["event_id"])

    execute(conn, "DELETE FROM vendor_payment_milestones WHERE id=%s", (milestone_id,))
    return {"ok": True}

@router.get("/events/{event_id}/milestones-schedule")
def get_event_milestones_schedule(event_id: int, conn=Depends(get_db), user=Depends(get_current_user)):
    ensure_vendor_milestones_schema(conn)
    _require_event_access(conn, user, event_id)

    cur = execute(conn, """
        SELECT m.*, v.name as vendor_name, v.category as vendor_category
        FROM vendor_payment_milestones m
        JOIN vendors v ON v.id = m.vendor_id
        WHERE m.event_id = %s
        ORDER BY CASE WHEN m.due_date = '' OR m.due_date IS NULL THEN '9999-99-99' ELSE m.due_date END ASC, m.id ASC
    """, (event_id,))
    return [dict(r) for r in cur.fetchall()]
