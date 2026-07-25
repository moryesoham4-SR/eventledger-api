from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional
from core.database import get_db, execute
from core.auth import get_current_user
from utils.roles import get_event_role, is_event_owner_or_super_admin

router = APIRouter(prefix="/api/events", tags=["events"])

class EventCreate(BaseModel):
    name: str
    description: str = ""
    venue: str = ""
    start_date: str = ""
    end_date: str = ""
    expected_attendees: int = 0
    currency: str = "INR"

class EventUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    venue: Optional[str] = None
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    expected_attendees: Optional[int] = None
    status: Optional[str] = None
    phase: Optional[str] = None
    currency: Optional[str] = None

@router.get("/")
def get_events(conn=Depends(get_db), user=Depends(get_current_user)):
    if user.get("is_super_admin"):
        cur = execute(conn, "SELECT * FROM events ORDER BY created_at DESC")
    else:
        cur = execute(conn,
            """SELECT DISTINCT e.* FROM events e
               LEFT JOIN user_event_roles r ON r.event_id=e.id AND r.user_id=%s
               WHERE e.user_id=%s OR r.user_id=%s
               ORDER BY e.created_at DESC""",
            (user["id"], user["id"], user["id"])
        )
    return [dict(r) for r in cur.fetchall()]

@router.post("/")
def create_event(data: EventCreate, conn=Depends(get_db), user=Depends(get_current_user)):
    cur = execute(conn,
        """INSERT INTO events (user_id,name,description,venue,start_date,end_date,
           expected_attendees,currency,status,phase)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'active','planning') RETURNING *""",
        (user["id"], data.name, data.description, data.venue,
         data.start_date, data.end_date, data.expected_attendees, data.currency)
    )
    return dict(cur.fetchone())

@router.get("/{event_id}")
def get_event(event_id: int, conn=Depends(get_db), user=Depends(get_current_user)):
    cur = execute(conn, "SELECT * FROM events WHERE id=%s", (event_id,))
    event = cur.fetchone()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    role_ctx = get_event_role(conn, user, event_id)
    if role_ctx["level"] is None:
        raise HTTPException(status_code=403, detail="You don't have access to this event")
    return dict(event)

@router.put("/{event_id}")
def update_event(event_id: int, data: EventUpdate, conn=Depends(get_db), user=Depends(get_current_user)):
    role_ctx = get_event_role(conn, user, event_id)
    if role_ctx["level"] != "event_admin":
        raise HTTPException(status_code=403, detail="Only this event's admin can edit it")
    fields = {k: v for k, v in data.dict().items() if v is not None}
    if not fields:
        raise HTTPException(status_code=400, detail="No fields to update")
    set_clause = ", ".join(f"{k}=%s" for k in fields)
    values = list(fields.values()) + [event_id]
    cur = execute(conn, f"UPDATE events SET {set_clause} WHERE id=%s RETURNING *", values)
    return dict(cur.fetchone())

@router.delete("/{event_id}")
def delete_event(event_id: int, conn=Depends(get_db), user=Depends(get_current_user)):
    # Deliberately stricter than "event_admin": an assigned event_admin role
    # can run the event day-to-day, but only the creator or a platform super
    # admin can delete it outright.
    if not is_event_owner_or_super_admin(conn, user, event_id):
        raise HTTPException(status_code=403, detail="Only the event's creator or a Super Admin can delete it")
    execute(conn, "DELETE FROM events WHERE id=%s", (event_id,))
    return {"ok": True}

@router.get("/{event_id}/summary")
def get_event_summary(event_id: int, conn=Depends(get_db), user=Depends(get_current_user)):
    role_ctx = get_event_role(conn, user, event_id)
    if role_ctx["level"] is None:
        raise HTTPException(status_code=403, detail="You don't have access to this event")

    def q(sql, p):
        cur = execute(conn, sql, p)
        row = cur.fetchone()
        return float(list(row.values())[0]) if row else 0.0
    est_inc = q("SELECT COALESCE(SUM(amount),0) FROM estimated_income WHERE event_id=%s", (event_id,))
    act_inc = q("SELECT COALESCE(SUM(amount),0) FROM actual_income WHERE event_id=%s", (event_id,))
    est_exp = q("SELECT COALESCE(SUM(amount),0) FROM estimated_expenses WHERE event_id=%s", (event_id,))
    act_exp = q("SELECT COALESCE(SUM(amount),0) FROM actual_expenses WHERE event_id=%s", (event_id,))
    return {
        "est_income": est_inc, "act_income": act_inc,
        "est_expense": est_exp, "act_expense": act_exp,
        "profit": act_inc - act_exp,
        "variance": est_exp - act_exp,
        "budget_utilization": round(act_exp / est_exp * 100, 1) if est_exp else 0,
    }
