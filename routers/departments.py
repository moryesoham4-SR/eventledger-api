from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional
from core.database import get_db, execute
from core.auth import get_current_user
from utils.roles import get_event_role, can_manage_departments
from utils.db_safety import run_safely

router = APIRouter(prefix="/api/departments", tags=["departments"])

class DeptCreate(BaseModel):
    event_id: int
    name: str
    head_name: str = ""
    color: str = "#6366f1"

class AssignMemberRequest(BaseModel):
    event_id: int
    user_id: int
    role: str = "volunteer"  # 'co_leader', 'event_admin', 'dept_head', or 'volunteer'

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
        raise HTTPException(status_code=403, detail="Only an Event Lead or Admin can create departments")
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
        raise HTTPException(status_code=403, detail="Only an Event Lead or Admin can delete departments")
    execute(conn, "DELETE FROM departments WHERE id=%s", (dept_id,))
    return {"ok": True}

# ==================== DEPARTMENT TEAM ROSTER ====================

@router.get("/{dept_id}/roster")
def get_department_roster(dept_id: int, conn=Depends(get_db), user=Depends(get_current_user)):
    cur_d = execute(conn, "SELECT * FROM departments WHERE id=%s", (dept_id,))
    dept = cur_d.fetchone()
    if not dept:
        raise HTTPException(status_code=404, detail="Department not found")
    
    event_id = dept["event_id"]
    role_ctx = get_event_role(conn, user, event_id)
    if role_ctx["level"] is None:
        raise HTTPException(status_code=403, detail="You don't have access to this event")

    cur = execute(conn, """
        SELECT u.id, u.name, u.email, u.avatar_color, r.role, r.dept_id
        FROM user_event_roles r
        JOIN users u ON u.id = r.user_id
        WHERE r.event_id = %s AND r.dept_id = %s
    """, (event_id, dept_id))
    members = [dict(r) for r in cur.fetchall()]

    head_user = next((m for m in members if m["role"] == "dept_head"), None)
    coworkers = [m for m in members if m["role"] != "dept_head"]

    return {
        "dept": dict(dept),
        "head": head_user,
        "coworkers": coworkers,
        "all_members": members,
    }

@router.post("/{dept_id}/assign-member")
def assign_department_member(dept_id: int, data: AssignMemberRequest, conn=Depends(get_db), user=Depends(get_current_user)):
    role_ctx = get_event_role(conn, user, data.event_id)
    if not can_manage_departments(role_ctx) and role_ctx["level"] != "dept_head":
        raise HTTPException(status_code=403, detail="Only an Event Lead or Dept Head can assign team members")

    cur_u = execute(conn, "SELECT id, name, email FROM users WHERE id=%s", (data.user_id,))
    target_u = cur_u.fetchone()
    if not target_u:
        raise HTTPException(status_code=404, detail="User not found")

    # If assigning as dept_head, update existing head name in departments table
    if data.role == "dept_head":
        run_safely(conn, lambda: execute(conn, """
            UPDATE user_event_roles SET role='volunteer' WHERE event_id=%s AND dept_id=%s AND role='dept_head'
        """, (data.event_id, dept_id)))
        execute(conn, "UPDATE departments SET head_name=%s WHERE id=%s", (target_u["name"] or target_u["email"], dept_id))

    # CRITICAL FIX: Delete ALL previous role entries for this user on this event so role updates apply IMMEDIATELY!
    execute(conn, "DELETE FROM user_event_roles WHERE user_id=%s AND event_id=%s", (data.user_id, data.event_id))

    execute(conn, """
        INSERT INTO user_event_roles (user_id, event_id, role, dept_id, assigned_by)
        VALUES (%s, %s, %s, %s, %s)
    """, (data.user_id, data.event_id, data.role, dept_id, user["id"]))

    return {"ok": True, "user_name": target_u["name"] or target_u["email"]}

@router.delete("/{dept_id}/members/{user_id}")
def remove_department_member(dept_id: int, user_id: int, conn=Depends(get_db), user=Depends(get_current_user)):
    cur_d = execute(conn, "SELECT event_id FROM departments WHERE id=%s", (dept_id,))
    dept = cur_d.fetchone()
    if not dept:
        raise HTTPException(status_code=404, detail="Department not found")
    
    event_id = dept["event_id"]
    role_ctx = get_event_role(conn, user, event_id)
    if not can_manage_departments(role_ctx) and role_ctx["level"] != "dept_head":
        raise HTTPException(status_code=403, detail="Only an Event Lead or Dept Head can remove team members")

    execute(conn, "DELETE FROM user_event_roles WHERE user_id=%s AND event_id=%s AND dept_id=%s", (user_id, event_id, dept_id))
    return {"ok": True}
