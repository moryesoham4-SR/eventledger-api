from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional
from core.database import get_db, execute
from core.auth import get_current_user
from utils.roles import get_event_role
from utils.google_sheets import sync_event_data_to_sheets

router = APIRouter(prefix="/api/income", tags=["income"])

class EstIncomeCreate(BaseModel):
    event_id: int
    source: str
    category: str = "Other"
    amount: float
    notes: str = ""

class ActIncomeCreate(BaseModel):
    event_id: int
    source: str
    category: str = "Other"
    amount: float
    received_on: str = ""
    payment_mode: str = "Cash"
    reference: str = ""
    notes: str = ""


def _require_event_access(conn, user, event_id):
    """Income isn't department-specific, so viewing it just requires being
    on the event at all — everyone from a volunteer up can see the event's
    income for context, same as the Dashboard's overall totals already show."""
    role_ctx = get_event_role(conn, user, event_id)
    if role_ctx["level"] is None:
        raise HTTPException(status_code=403, detail="You don't have access to this event")
    return role_ctx


def _require_finance(conn, user, event_id):
    """Adding/deleting income IS a finance-level action — a dept_head has no
    reason to be recording event-wide income, so this is admin/finance only."""
    role_ctx = get_event_role(conn, user, event_id)
    if role_ctx["level"] not in ("event_admin", "finance_head"):
        raise HTTPException(status_code=403, detail="Only an event admin or finance head can manage income")
    return role_ctx


def _get_income_or_404(conn, table, item_id):
    cur = execute(conn, f"SELECT * FROM {table} WHERE id=%s", (item_id,))
    row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Not found")
    return dict(row)


@router.get("/estimated")
def get_estimated_income(event_id: int, conn=Depends(get_db), user=Depends(get_current_user)):
    _require_event_access(conn, user, event_id)
    cur = execute(conn, "SELECT * FROM estimated_income WHERE event_id=%s ORDER BY created_at DESC", (event_id,))
    return [dict(r) for r in cur.fetchall()]

@router.post("/estimated")
def add_estimated_income(data: EstIncomeCreate, conn=Depends(get_db), user=Depends(get_current_user)):
    _require_finance(conn, user, data.event_id)
    cur = execute(conn,
        "INSERT INTO estimated_income (event_id,source,category,amount,notes) VALUES (%s,%s,%s,%s,%s) RETURNING *",
        (data.event_id, data.source, data.category, data.amount, data.notes)
    )
    res = dict(cur.fetchone())
    sync_event_data_to_sheets(conn, data.event_id, "create", "income", res)
    return res

@router.delete("/estimated/{item_id}")
def delete_estimated_income(item_id: int, conn=Depends(get_db), user=Depends(get_current_user)):
    item = _get_income_or_404(conn, "estimated_income", item_id)
    _require_finance(conn, user, item["event_id"])
    execute(conn, "DELETE FROM estimated_income WHERE id=%s", (item_id,))
    sync_event_data_to_sheets(conn, item["event_id"], "delete", "income", {"id": item_id})
    return {"ok": True}

@router.get("/actual")
def get_actual_income(event_id: int, conn=Depends(get_db), user=Depends(get_current_user)):
    _require_event_access(conn, user, event_id)
    cur = execute(conn, "SELECT * FROM actual_income WHERE event_id=%s ORDER BY created_at DESC", (event_id,))
    return [dict(r) for r in cur.fetchall()]

@router.post("/actual")
def add_actual_income(data: ActIncomeCreate, conn=Depends(get_db), user=Depends(get_current_user)):
    _require_finance(conn, user, data.event_id)
    cur = execute(conn,
        """INSERT INTO actual_income (event_id,source,category,amount,received_on,payment_mode,reference,notes)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
        (data.event_id, data.source, data.category, data.amount,
         data.received_on, data.payment_mode, data.reference, data.notes)
    )
    res = dict(cur.fetchone())
    sync_event_data_to_sheets(conn, data.event_id, "create", "income", res)
    return res

@router.delete("/actual/{item_id}")
def delete_actual_income(item_id: int, conn=Depends(get_db), user=Depends(get_current_user)):
    item = _get_income_or_404(conn, "actual_income", item_id)
    _require_finance(conn, user, item["event_id"])
    execute(conn, "DELETE FROM actual_income WHERE id=%s", (item_id,))
    sync_event_data_to_sheets(conn, item["event_id"], "delete", "income", {"id": item_id})
    return {"ok": True}
