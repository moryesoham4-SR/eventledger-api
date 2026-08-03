from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional
from core.database import get_db, execute
from core.auth import get_current_user
from utils.roles import get_event_role, can_access_department, can_edit_department

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


def _require_event_access(conn, user, event_id):
    role_ctx = get_event_role(conn, user, event_id)
    if role_ctx["level"] is None:
        raise HTTPException(status_code=403, detail="You don't have access to this event")
    return role_ctx


def _effective_dept_filter(role_ctx, requested_dept_id):
    """dept_head/volunteer can only ever see their own department's expenses,
    no matter what dept_id they pass in — event_admin/finance_head can see
    any department (or all, if none specified)."""
    if role_ctx["level"] in ("dept_head", "volunteer"):
        return role_ctx["dept_id"]
    return requested_dept_id


def _get_expense_or_404(conn, table, item_id):
    cur = execute(conn, f"SELECT * FROM {table} WHERE id=%s", (item_id,))
    row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Not found")
    return dict(row)


@router.get("/estimated")
def get_estimated_expenses(event_id: int, dept_id: Optional[int] = None, conn=Depends(get_db), user=Depends(get_current_user)):
    role_ctx = _require_event_access(conn, user, event_id)
    effective_dept = _effective_dept_filter(role_ctx, dept_id)
    if effective_dept:
        cur = execute(conn, "SELECT * FROM estimated_expenses WHERE event_id=%s AND department_id=%s ORDER BY created_at DESC", (event_id, effective_dept))
    else:
        cur = execute(conn, "SELECT * FROM estimated_expenses WHERE event_id=%s ORDER BY created_at DESC", (event_id,))
    return [dict(r) for r in cur.fetchall()]

@router.post("/estimated")
def add_estimated_expense(data: EstExpenseCreate, conn=Depends(get_db), user=Depends(get_current_user)):
    role_ctx = get_event_role(conn, user, data.event_id)
    if not can_edit_department(role_ctx, data.department_id):
        raise HTTPException(status_code=403, detail="You can't add expenses for this department")
    cur = execute(conn,
        """INSERT INTO estimated_expenses (event_id,department_id,category,item_name,description,quantity,unit,amount,notes)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
        (data.event_id, data.department_id, data.category, data.item_name,
         data.description, data.quantity, data.unit, data.amount, data.notes)
    )
    return dict(cur.fetchone())

@router.delete("/estimated/{item_id}")
def delete_estimated_expense(item_id: int, conn=Depends(get_db), user=Depends(get_current_user)):
    item = _get_expense_or_404(conn, "estimated_expenses", item_id)
    role_ctx = get_event_role(conn, user, item["event_id"])
    if not can_edit_department(role_ctx, item["department_id"]):
        raise HTTPException(status_code=403, detail="You can't delete this expense")
    execute(conn, "DELETE FROM estimated_expenses WHERE id=%s", (item_id,))
    return {"ok": True}

@router.get("/actual")
def get_actual_expenses(event_id: int, dept_id: Optional[int] = None, conn=Depends(get_db), user=Depends(get_current_user)):
    role_ctx = _require_event_access(conn, user, event_id)
    effective_dept = _effective_dept_filter(role_ctx, dept_id)
    if effective_dept:
        cur = execute(conn, "SELECT * FROM actual_expenses WHERE event_id=%s AND department_id=%s ORDER BY created_at DESC", (event_id, effective_dept))
    else:
        cur = execute(conn, "SELECT * FROM actual_expenses WHERE event_id=%s ORDER BY created_at DESC", (event_id,))
    return [dict(r) for r in cur.fetchall()]

@router.post("/actual")
def add_actual_expense(data: ActExpenseCreate, conn=Depends(get_db), user=Depends(get_current_user)):
    role_ctx = get_event_role(conn, user, data.event_id)
    if not can_edit_department(role_ctx, data.department_id):
        raise HTTPException(status_code=403, detail="You can't add expenses for this department")
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
    item = _get_expense_or_404(conn, "actual_expenses", item_id)
    role_ctx = get_event_role(conn, user, item["event_id"])
    if not can_edit_department(role_ctx, item["department_id"]):
        raise HTTPException(status_code=403, detail="You can't delete this expense")
    execute(conn, "DELETE FROM actual_expenses WHERE id=%s", (item_id,))
    return {"ok": True}
