from fastapi import APIRouter, Depends
from pydantic import BaseModel
from typing import Optional
from core.database import get_db, execute
from core.auth import get_current_user

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

@router.get("/")
def get_sponsors(event_id: int, conn=Depends(get_db), user=Depends(get_current_user)):
    cur = execute(conn, "SELECT * FROM sponsors WHERE event_id=%s ORDER BY created_at DESC", (event_id,))
    return [dict(r) for r in cur.fetchall()]

@router.post("/")
def add_sponsor(data: SponsorCreate, conn=Depends(get_db), user=Depends(get_current_user)):
    cur = execute(conn,
        """INSERT INTO sponsors (event_id,name,tier,contact_name,contact_email,amount,status,notes,income_synced)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,1) RETURNING *""",
        (data.event_id, data.name, data.tier, data.contact_name,
         data.contact_email, data.amount, data.status, data.notes)
    )
    sponsor = dict(cur.fetchone())
    # Auto-sync to actual income
    execute(conn,
        """INSERT INTO actual_income (event_id,source,category,amount,payment_mode,sponsor_id)
           VALUES (%s,%s,'Sponsor',%s,'Bank Transfer',%s)""",
        (data.event_id, f"Sponsor: {data.name}", data.amount, sponsor["id"])
    )
    return sponsor

@router.delete("/{sponsor_id}")
def delete_sponsor(sponsor_id: int, conn=Depends(get_db), user=Depends(get_current_user)):
    execute(conn, "DELETE FROM actual_income WHERE sponsor_id=%s", (sponsor_id,))
    execute(conn, "DELETE FROM sponsors WHERE id=%s", (sponsor_id,))
    return {"ok": True}
