from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional
from core.database import get_db, execute
from core.auth import get_current_user
from utils.roles import get_event_role
from utils.db_safety import run_safely

router = APIRouter(prefix="/api/sponsors", tags=["sponsors"])

class SponsorCreate(BaseModel):
    event_id: int
    name: str
    tier: str = "Bronze"
    contact_name: str = ""
    contact_email: str = ""
    promised_amount: float = 0
    amount: Optional[float] = None  # fallback for backward compatibility
    amount_received: float = 0
    status: str = "confirmed"
    notes: str = ""

class SponsorInstallmentCreate(BaseModel):
    event_id: int
    sponsor_id: int
    installment_name: str
    due_date: Optional[str] = ""
    amount: float
    status: str = "pending"
    notes: Optional[str] = ""

class SponsorInstallmentUpdate(BaseModel):
    installment_name: Optional[str] = None
    due_date: Optional[str] = None
    amount: Optional[float] = None
    status: Optional[str] = None
    received_date: Optional[str] = None
    payment_mode: Optional[str] = None
    reference: Optional[str] = None
    notes: Optional[str] = None


def ensure_sponsor_installments_schema(conn):
    run_safely(conn, lambda: execute(conn, "ALTER TABLE sponsors ADD COLUMN IF NOT EXISTS promised_amount NUMERIC(12,2) DEFAULT 0;"))
    run_safely(conn, lambda: execute(conn, "ALTER TABLE sponsors ADD COLUMN IF NOT EXISTS amount_received NUMERIC(12,2) DEFAULT 0;"))
    run_safely(conn, lambda: execute(conn, """
        CREATE TABLE IF NOT EXISTS sponsor_installments (
            id SERIAL PRIMARY KEY,
            event_id INT NOT NULL,
            sponsor_id INT NOT NULL,
            installment_name VARCHAR(255) NOT NULL,
            due_date VARCHAR(50),
            amount NUMERIC(12,2) NOT NULL DEFAULT 0,
            status VARCHAR(20) DEFAULT 'pending',
            received_date VARCHAR(50),
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
        raise HTTPException(status_code=403, detail="Only an event admin or finance head can manage sponsors")
    return role_ctx


def _sync_sponsor_received_total(conn, sponsor_id):
    """Recalculates total received from installments and updates sponsors table & actual_income."""
    cur_sum = execute(conn, "SELECT COALESCE(SUM(amount), 0) as total FROM sponsor_installments WHERE sponsor_id=%s AND status='received'", (sponsor_id,))
    row = cur_sum.fetchone()
    total_received = float(row["total"]) if row else 0.0
    execute(conn, "UPDATE sponsors SET amount_received=%s WHERE id=%s", (total_received, sponsor_id))


@router.get("/")
def get_sponsors(event_id: int, conn=Depends(get_db), user=Depends(get_current_user)):
    ensure_sponsor_installments_schema(conn)
    _require_event_access(conn, user, event_id)
    cur = execute(conn, "SELECT * FROM sponsors WHERE event_id=%s ORDER BY created_at DESC", (event_id,))
    rows = [dict(r) for r in cur.fetchall()]
    for r in rows:
        # Populate promised_amount fallback
        if not r.get("promised_amount") and r.get("amount"):
            r["promised_amount"] = r["amount"]
    return rows

@router.post("/")
def add_sponsor(data: SponsorCreate, conn=Depends(get_db), user=Depends(get_current_user)):
    ensure_sponsor_installments_schema(conn)
    _require_finance(conn, user, data.event_id)

    total_deal = data.promised_amount if data.promised_amount > 0 else (data.amount or 0)
    initial_received = data.amount_received

    cur = execute(conn,
        """INSERT INTO sponsors (event_id,name,tier,contact_name,contact_email,amount,promised_amount,amount_received,status,notes,income_synced)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,1) RETURNING *""",
        (data.event_id, data.name, data.tier, data.contact_name,
         data.contact_email, total_deal, total_deal, initial_received, data.status, data.notes)
    )
    sponsor = dict(cur.fetchone())

    # ONLY log into actual_income IF money was actually received!
    if initial_received > 0:
        today = "today"
        run_safely(conn, lambda: execute(conn,
            "INSERT INTO actual_income (event_id,source,category,amount,payment_mode,sponsor_id,reference) VALUES (%s,%s,'Sponsor',%s,'Bank Transfer',%s,%s)",
            (data.event_id, f"Sponsor: {data.name} (Initial Deposit)", initial_received, sponsor["id"], f"sponsor_init:{sponsor['id']}")
        ))
        run_safely(conn, lambda: execute(conn,
            """INSERT INTO sponsor_installments (event_id, sponsor_id, installment_name, due_date, amount, status, received_date, payment_mode)
               VALUES (%s, %s, 'Initial Received Deposit', %s, %s, 'received', %s, 'Bank Transfer')""",
            (data.event_id, sponsor["id"], today, initial_received, today)
        ))

    return sponsor

@router.delete("/{sponsor_id}")
def delete_sponsor(sponsor_id: int, conn=Depends(get_db), user=Depends(get_current_user)):
    ensure_sponsor_installments_schema(conn)
    cur = execute(conn, "SELECT event_id FROM sponsors WHERE id=%s", (sponsor_id,))
    sponsor = cur.fetchone()
    if not sponsor:
        raise HTTPException(status_code=404, detail="Sponsor not found")
    _require_finance(conn, user, sponsor["event_id"])
    run_safely(conn, lambda: execute(conn, "DELETE FROM actual_income WHERE sponsor_id=%s", (sponsor_id,)))
    run_safely(conn, lambda: execute(conn, "DELETE FROM sponsor_installments WHERE sponsor_id=%s", (sponsor_id,)))
    execute(conn, "DELETE FROM sponsors WHERE id=%s", (sponsor_id,))
    return {"ok": True}

# ==================== SPONSOR INSTALLMENTS & RECEIVABLES ====================

@router.get("/{sponsor_id}/installments")
def get_sponsor_installments(sponsor_id: int, conn=Depends(get_db), user=Depends(get_current_user)):
    ensure_sponsor_installments_schema(conn)
    cur_s = execute(conn, "SELECT event_id FROM sponsors WHERE id=%s", (sponsor_id,))
    s_row = cur_s.fetchone()
    if not s_row:
        raise HTTPException(status_code=404, detail="Sponsor not found")
    _require_event_access(conn, user, s_row["event_id"])

    cur = execute(conn, "SELECT * FROM sponsor_installments WHERE sponsor_id=%s ORDER BY id ASC", (sponsor_id,))
    return [dict(r) for r in cur.fetchall()]

@router.post("/installments")
def create_sponsor_installment(data: SponsorInstallmentCreate, conn=Depends(get_db), user=Depends(get_current_user)):
    ensure_sponsor_installments_schema(conn)
    _require_finance(conn, user, data.event_id)

    cur = execute(conn, """
        INSERT INTO sponsor_installments
        (event_id, sponsor_id, installment_name, due_date, amount, status, notes)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        RETURNING *
    """, (data.event_id, data.sponsor_id, data.installment_name, data.due_date, data.amount, data.status, data.notes))
    inst = dict(cur.fetchone())

    # If created as received immediately, sync to income
    if data.status == "received":
        execute(conn, """
            INSERT INTO actual_income (event_id, source, category, amount, payment_mode, sponsor_id, reference)
            VALUES (%s, %s, 'Sponsor', %s, 'Bank Transfer', %s, %s)
        """, (data.event_id, f"Sponsor Installment: {data.installment_name}", data.amount, data.sponsor_id, f"sponsor_inst:{inst['id']}"))

    _sync_sponsor_received_total(conn, data.sponsor_id)
    return inst

@router.post("/{sponsor_id}/auto-generate-installments")
def auto_generate_sponsor_installments(sponsor_id: int, conn=Depends(get_db), user=Depends(get_current_user)):
    """Auto-generates standard 50% Advance & 50% Final Settlement sponsor installments."""
    ensure_sponsor_installments_schema(conn)
    cur_s = execute(conn, "SELECT * FROM sponsors WHERE id=%s", (sponsor_id,))
    s = cur_s.fetchone()
    if not s:
        raise HTTPException(status_code=404, detail="Sponsor not found")
    _require_finance(conn, user, s["event_id"])

    total_deal = float(s.get("promised_amount") or s.get("amount") or 0)
    if total_deal <= 0:
        raise HTTPException(status_code=400, detail="Sponsor committed deal amount must be greater than 0")

    # Clean existing pending installments & income entries tied to installments
    execute(conn, "DELETE FROM sponsor_installments WHERE sponsor_id=%s", (sponsor_id,))
    execute(conn, "DELETE FROM actual_income WHERE sponsor_id=%s", (sponsor_id,))

    m1_amt = round(total_deal * 0.50, 2)
    m2_amt = round(total_deal - m1_amt, 2)

    installments = [
        (s["event_id"], sponsor_id, "50% Advance Deposit Payment", "", m1_amt, "pending", "Initial commitment deposit"),
        (s["event_id"], sponsor_id, "50% Final Post-Event Settlement", "", m2_amt, "pending", "Final installment post event"),
    ]

    inserted = []
    for inst in installments:
        cur_i = execute(conn, """
            INSERT INTO sponsor_installments
            (event_id, sponsor_id, installment_name, due_date, amount, status, notes)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING *
        """, inst)
        inserted.append(dict(cur_i.fetchone()))

    _sync_sponsor_received_total(conn, sponsor_id)
    return inserted

@router.put("/installments/{installment_id}")
def update_sponsor_installment(installment_id: int, data: SponsorInstallmentUpdate, conn=Depends(get_db), user=Depends(get_current_user)):
    ensure_sponsor_installments_schema(conn)
    cur_i = execute(conn, "SELECT * FROM sponsor_installments WHERE id=%s", (installment_id,))
    inst = cur_i.fetchone()
    if not inst:
        raise HTTPException(status_code=404, detail="Installment not found")
    _require_finance(conn, user, inst["event_id"])

    old_status = inst["status"]
    fields = {k: v for k, v in data.dict().items() if v is not None}
    if not fields:
        raise HTTPException(status_code=400, detail="No fields to update")

    set_clause = ", ".join(f"{k}=%s" for k in fields)
    values = list(fields.values()) + [installment_id]
    cur_u = execute(conn, f"UPDATE sponsor_installments SET {set_clause} WHERE id=%s RETURNING *", values)
    updated_inst = dict(cur_u.fetchone())

    new_status = updated_inst["status"]
    sponsor_id = updated_inst["sponsor_id"]
    event_id = updated_inst["event_id"]
    amount = updated_inst["amount"]
    inst_name = updated_inst["installment_name"]
    pay_mode = updated_inst.get("payment_mode") or "Bank Transfer"
    ref_tag = f"sponsor_inst:{installment_id}"

    # Sync with actual_income!
    if old_status != "received" and new_status == "received":
        # Log to income!
        execute(conn, """
            INSERT INTO actual_income (event_id, source, category, amount, payment_mode, sponsor_id, reference)
            VALUES (%s, %s, 'Sponsor', %s, %s, %s, %s)
        """, (event_id, f"Sponsor: {inst_name}", amount, pay_mode, sponsor_id, ref_tag))
    elif old_status == "received" and new_status != "received":
        # Remove from income!
        execute(conn, "DELETE FROM actual_income WHERE reference=%s", (ref_tag,))

    _sync_sponsor_received_total(conn, sponsor_id)
    return updated_inst

@router.delete("/installments/{installment_id}")
def delete_sponsor_installment(installment_id: int, conn=Depends(get_db), user=Depends(get_current_user)):
    ensure_sponsor_installments_schema(conn)
    cur_i = execute(conn, "SELECT * FROM sponsor_installments WHERE id=%s", (installment_id,))
    inst = cur_i.fetchone()
    if not inst:
        raise HTTPException(status_code=404, detail="Installment not found")
    _require_finance(conn, user, inst["event_id"])

    ref_tag = f"sponsor_inst:{installment_id}"
    execute(conn, "DELETE FROM actual_income WHERE reference=%s", (ref_tag,))
    execute(conn, "DELETE FROM sponsor_installments WHERE id=%s", (installment_id,))
    _sync_sponsor_received_total(conn, inst["sponsor_id"])
    return {"ok": True}

@router.get("/events/{event_id}/receivables-schedule")
def get_event_sponsor_receivables(event_id: int, conn=Depends(get_db), user=Depends(get_current_user)):
    ensure_sponsor_installments_schema(conn)
    _require_event_access(conn, user, event_id)

    cur = execute(conn, """
        SELECT i.*, s.name as sponsor_name, s.tier as sponsor_tier
        FROM sponsor_installments i
        JOIN sponsors s ON s.id = i.sponsor_id
        WHERE i.event_id = %s
        ORDER BY CASE WHEN i.due_date = '' OR i.due_date IS NULL THEN '9999-99-99' ELSE i.due_date END ASC, i.id ASC
    """, (event_id,))
    return [dict(r) for r in cur.fetchall()]
