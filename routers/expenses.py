from fastapi import APIRouter, Depends
from pydantic import BaseModel
from typing import Optional
from core.database import get_db, execute
from core.auth import get_current_user

router = APIRouter(prefix="/api/expenses", tags=["expenses"])

class EstExpenseCreate(BaseModel):
    event_id: int
    department_id: Optional[int] = None
    category: str
    item_name: str = ""
    description: str = ""
    quantity: float = 1
    unit: str = "unit"
    amount: float
    notes: str = ""

class ActExpenseCreate(BaseModel):
    event_id: int
    department_id: Optional[int] = None
    category: str
    item_name: str = ""
    description: str = ""
    quantity: float = 1
    unit: str = "unit"
    amount: float
    paid_on: str = ""
    payment_mode: str = "Cash"
    status: str = "paid"
    reference: str = ""
    notes: str = ""

@router.get("/estimated")
def get_estimated_expenses(event_id: int, dept_id: Optional[int] = None, conn=Depends(get_db), user=Depends(get_current_user)):
    if dept_id:
        cur = execute(conn, "SELECT * FROM estimated_expenses WHERE event_id=%s AND department_id=%s ORDER BY created_at DESC", (event_id, dept_id))
    else:
        cur = execute(conn, "SELECT * FROM estimated_expenses WHERE event_id=%s ORDER BY created_at DESC", (event_id,))
    return [dict(r) for r in cur.fetchall()]

@router.post("/estimated")
def add_estimated_expense(data: EstExpenseCreate, conn=Depends(get_db), user=Depends(get_current_user)):
    cur = execute(conn,
        """INSERT INTO estimated_expenses (event_id,department_id,category,item_name,description,quantity,unit,amount,notes)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
        (data.event_id, data.department_id, data.category, data.item_name,
         data.description, data.quantity, data.unit, data.amount, data.notes)
    )
    return dict(cur.fetchone())

@router.delete("/estimated/{item_id}")
def delete_estimated_expense(item_id: int, conn=Depends(get_db), user=Depends(get_current_user)):
    execute(conn, "DELETE FROM estimated_expenses WHERE id=%s", (item_id,))
    return {"ok": True}

@router.get("/actual")
def get_actual_expenses(event_id: int, dept_id: Optional[int] = None, conn=Depends(get_db), user=Depends(get_current_user)):
    if dept_id:
        cur = execute(conn, "SELECT * FROM actual_expenses WHERE event_id=%s AND department_id=%s ORDER BY created_at DESC", (event_id, dept_id))
    else:
        cur = execute(conn, "SELECT * FROM actual_expenses WHERE event_id=%s ORDER BY created_at DESC", (event_id,))
    return [dict(r) for r in cur.fetchall()]

@router.post("/actual")
def add_actual_expense(data: ActExpenseCreate, conn=Depends(get_db), user=Depends(get_current_user)):
    cur = execute(conn,
        """INSERT INTO actual_expenses (event_id,department_id,category,item_name,description,
           quantity,unit,amount,paid_on,payment_mode,status,reference,notes)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
        (data.event_id, data.department_id, data.category, data.item_name, data.description,
         data.quantity, data.unit, data.amount, data.paid_on, data.payment_mode,
         data.status, data.reference, data.notes)
    )
    return dict(cur.fetchone())

@router.delete("/actual/{item_id}")
def delete_actual_expense(item_id: int, conn=Depends(get_db), user=Depends(get_current_user)):
    execute(conn, "DELETE FROM actual_expenses WHERE id=%s", (item_id,))
    return {"ok": True}
