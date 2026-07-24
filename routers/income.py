from fastapi import APIRouter, Depends
from pydantic import BaseModel
from typing import Optional
from core.database import get_db, execute
from core.auth import get_current_user

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

@router.get("/estimated")
def get_estimated_income(event_id: int, conn=Depends(get_db), user=Depends(get_current_user)):
    cur = execute(conn, "SELECT * FROM estimated_income WHERE event_id=%s ORDER BY created_at DESC", (event_id,))
    return [dict(r) for r in cur.fetchall()]

@router.post("/estimated")
def add_estimated_income(data: EstIncomeCreate, conn=Depends(get_db), user=Depends(get_current_user)):
    cur = execute(conn,
        "INSERT INTO estimated_income (event_id,source,category,amount,notes) VALUES (%s,%s,%s,%s,%s) RETURNING *",
        (data.event_id, data.source, data.category, data.amount, data.notes)
    )
    return dict(cur.fetchone())

@router.delete("/estimated/{item_id}")
def delete_estimated_income(item_id: int, conn=Depends(get_db), user=Depends(get_current_user)):
    execute(conn, "DELETE FROM estimated_income WHERE id=%s", (item_id,))
    return {"ok": True}

@router.get("/actual")
def get_actual_income(event_id: int, conn=Depends(get_db), user=Depends(get_current_user)):
    cur = execute(conn, "SELECT * FROM actual_income WHERE event_id=%s ORDER BY created_at DESC", (event_id,))
    return [dict(r) for r in cur.fetchall()]

@router.post("/actual")
def add_actual_income(data: ActIncomeCreate, conn=Depends(get_db), user=Depends(get_current_user)):
    cur = execute(conn,
        """INSERT INTO actual_income (event_id,source,category,amount,received_on,payment_mode,reference,notes)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
        (data.event_id, data.source, data.category, data.amount,
         data.received_on, data.payment_mode, data.reference, data.notes)
    )
    return dict(cur.fetchone())

@router.delete("/actual/{item_id}")
def delete_actual_income(item_id: int, conn=Depends(get_db), user=Depends(get_current_user)):
    execute(conn, "DELETE FROM actual_income WHERE id=%s", (item_id,))
    return {"ok": True}
