from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
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
    amount: float = 0
    status: str = "confirmed"
    notes: str = ""


def _require_event_access(conn, user, event_id):
    role_ctx = get_event_role(conn, user, event_id)
    if role_ctx["level"] is None:
        raise HTTPException(status_code=403, detail="You don't have access to this event")
    return role_ctx


def _require_finance(conn, user, event_id):
    """Signing/removing a sponsor is a finance-level action, same as vendors and income."""
    role_ctx = get_event_role(conn, user, event_id)
    if role_ctx["level"] not in ("event_admin", "finance_head"):
        raise HTTPException(status_code=403, detail="Only an event admin or finance head can manage sponsors")
    return role_ctx


@router.get("/")
def get_sponsors(event_id: int, conn=Depends(get_db), user=Depends(get_current_user)):
    _require_event_access(conn, user, event_id)
    cur = execute(conn, "SELECT * FROM sponsors WHERE event_id=%s ORDER BY created_at DESC", (event_id,))
    return [dict(r) for r in cur.fetchall()]

@router.post("/")
def add_sponsor(data: SponsorCreate, conn=Depends(get_db), user=Depends(get_current_user)):
    _require_finance(conn, user, data.event_id)
    cur = execute(conn,
        """INSERT INTO sponsors (event_id,name,tier,contact_name,contact_email,amount,status,notes,income_synced)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,1) RETURNING *""",
        (data.event_id, data.name, data.tier, data.contact_name,
         data.contact_email, data.amount, data.status, data.notes)
    )
    sponsor = dict(cur.fetchone())
    # Wrapped in run_safely: if this sync insert ever fails for an
    # unexpected reason, the sponsor itself is still created successfully
    # rather than the whole request rolling back (PostgreSQL aborts the
    # entire transaction on any statement failure — see utils/db_safety.py).
    run_safely(conn, lambda: execute(conn,
        "INSERT INTO actual_income (event_id,source,category,amount,payment_mode,sponsor_id) VALUES (%s,%s,'Sponsor',%s,'Bank Transfer',%s)",
        (data.event_id, f"Sponsor: {data.name}", data.amount, sponsor["id"])
    ))
    return sponsor

@router.delete("/{sponsor_id}")
def delete_sponsor(sponsor_id: int, conn=Depends(get_db), user=Depends(get_current_user)):
    cur = execute(conn, "SELECT event_id FROM sponsors WHERE id=%s", (sponsor_id,))
    sponsor = cur.fetchone()
    if not sponsor:
        raise HTTPException(status_code=404, detail="Sponsor not found")
    _require_finance(conn, user, sponsor["event_id"])
    run_safely(conn, lambda: execute(conn, "DELETE FROM actual_income WHERE sponsor_id=%s", (sponsor_id,)))
    execute(conn, "DELETE FROM sponsors WHERE id=%s", (sponsor_id,))
    return {"ok": True}
