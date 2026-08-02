from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from core.database import get_db, execute
from core.auth import get_current_user
from utils.roles import get_event_role, can_manage_departments

router = APIRouter(prefix="/api/departments", tags=["departments"])

class DeptCreate(BaseModel):
    event_id: int
    name: str
    head_name: str = ""
    color: str = "#6366f1"

@router.get("/")
def get_departments(event_id: int, conn=Depends(get_db), user=Depends(get_current_user)):
    role_ctx = get_event_role(conn, user, event_id)
    if role_ctx["level"] is None:
        raise HTTPException(status_code=403, detail="You don't have access to this event")
    cur = execute(conn, "SELECT * FROM departments WHERE event_id=%s ORDER BY name", (event_id,))
    return [dict(r) for r in cur.fetchall()]

@router.post("/")
def create_department(data: DeptCreate, conn=Depends(get_db), user=Depends(get_current_user)):
    role_ctx = get_event_role(conn, user, data.event_id)
    if not can_manage_departments(role_ctx):
        raise HTTPException(status_code=403, detail="Only an event admin can create departments")
    cur = execute(conn,
        "INSERT INTO departments (event_id,name,head_name,color) VALUES (%s,%s,%s,%s) RETURNING *",
        (data.event_id, data.name, data.head_name, data.color)
    )
    return dict(cur.fetchone())

@router.delete("/{dept_id}")
def delete_department(dept_id: int, conn=Depends(get_db), user=Depends(get_current_user)):
    cur = execute(conn, "SELECT event_id FROM departments WHERE id=%s", (dept_id,))
    dept = cur.fetchone()
    if not dept:
        raise HTTPException(status_code=404, detail="Department not found")
    role_ctx = get_event_role(conn, user, dept["event_id"])
    if not can_manage_departments(role_ctx):
        raise HTTPException(status_code=403, detail="Only an event admin can delete departments")
    execute(conn, "DELETE FROM departments WHERE id=%s", (dept_id,))
    return {"ok": True}
