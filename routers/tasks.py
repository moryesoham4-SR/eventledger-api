from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional
from core.database import get_db, execute
from core.auth import get_current_user
from utils.roles import get_event_role
from utils.db_safety import run_safely
from utils.activity import log_activity, ACTION_TASK_ASSIGNED, ACTION_TASK_UPDATED
import datetime

router = APIRouter(prefix="/api/tasks", tags=["tasks"])

def ensure_tasks_schema(conn):
    run_safely(conn, lambda: execute(conn, """
        CREATE TABLE IF NOT EXISTS department_tasks (
            id SERIAL PRIMARY KEY,
            event_id INT NOT NULL,
            department_id INT NOT NULL,
            assigned_to_user_id INT,
            assigned_to_name VARCHAR(255),
            title VARCHAR(255) NOT NULL,
            description TEXT,
            deadline VARCHAR(50),
            priority VARCHAR(20) DEFAULT 'medium',
            status VARCHAR(20) DEFAULT 'pending',
            created_by INT,
            created_by_name VARCHAR(255),
            completed_at VARCHAR(50),
            incomplete_reason TEXT,
            demerit_points INT DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """))
    run_safely(conn, lambda: execute(conn, "ALTER TABLE department_tasks ADD COLUMN IF NOT EXISTS created_by_name VARCHAR(255)"))
    run_safely(conn, lambda: execute(conn, "ALTER TABLE department_tasks ADD COLUMN IF NOT EXISTS completed_at VARCHAR(50)"))
    run_safely(conn, lambda: execute(conn, "ALTER TABLE department_tasks ADD COLUMN IF NOT EXISTS incomplete_reason TEXT"))
    run_safely(conn, lambda: execute(conn, "ALTER TABLE department_tasks ADD COLUMN IF NOT EXISTS demerit_points INT DEFAULT 0"))

def is_admin_or_superadmin(user: dict, role_ctx: dict) -> bool:
    if user.get("is_super_admin"):
        return True
    return role_ctx["level"] in ("event_admin", "co_leader")

class TaskCreate(BaseModel):
    event_id: int
    department_id: int
    assigned_to_user_id: Optional[int] = None
    assigned_to_name: Optional[str] = None
    title: str
    description: Optional[str] = ""
    deadline: Optional[str] = ""
    priority: str = "medium"
    status: str = "pending"

class TaskUpdate(BaseModel):
    assigned_to_user_id: Optional[int] = None
    assigned_to_name: Optional[str] = None
    title: Optional[str] = None
    description: Optional[str] = None
    deadline: Optional[str] = None
    priority: Optional[str] = None
    status: Optional[str] = None
    incomplete_reason: Optional[str] = None
    completed_at: Optional[str] = None
    demerit_points: Optional[int] = None

@router.get("/")
def get_tasks(event_id: int, dept_id: Optional[int] = None, conn=Depends(get_db), user=Depends(get_current_user)):
    ensure_tasks_schema(conn)
    role_ctx = get_event_role(conn, user, event_id)
    if role_ctx["level"] is None:
        raise HTTPException(status_code=403, detail="You don't have access to this event")

    query = """
        SELECT t.*, d.name as dept_name, d.color as dept_color,
               u.name as assignee_name, u.email as assignee_email, u.avatar_color as assignee_avatar,
               u_assigner.name as assigner_name
        FROM department_tasks t
        JOIN departments d ON d.id = t.department_id
        LEFT JOIN users u ON u.id = t.assigned_to_user_id
        LEFT JOIN users u_assigner ON u_assigner.id = t.created_by
        WHERE t.event_id = %s
    """
    params = [event_id]

    if role_ctx["level"] in ("dept_head", "volunteer"):
        if role_ctx["dept_id"]:
            query += " AND t.department_id = %s"
            params.append(role_ctx["dept_id"])
        else:
            return []
    elif dept_id:
        query += " AND t.department_id = %s"
        params.append(dept_id)

    query += " ORDER BY t.created_at DESC"
    cur = execute(conn, query, tuple(params))
    return [dict(r) for r in cur.fetchall()]

@router.get("/audit-report")
def get_task_audit_report(event_id: int, conn=Depends(get_db), user=Depends(get_current_user)):
    ensure_tasks_schema(conn)
    role_ctx = get_event_role(conn, user, event_id)
    if role_ctx["level"] is None:
        raise HTTPException(status_code=403, detail="You don't have access to this event")

    query = """
        SELECT t.*, d.name as dept_name, d.color as dept_color,
               u_assignee.name as assignee_name, u_assignee.email as assignee_email,
               u_assigner.name as assigner_name, u_assigner.email as assigner_email
        FROM department_tasks t
        JOIN departments d ON d.id = t.department_id
        LEFT JOIN users u_assignee ON u_assignee.id = t.assigned_to_user_id
        LEFT JOIN users u_assigner ON u_assigner.id = t.created_by
        WHERE t.event_id = %s
        ORDER BY t.created_at DESC
    """
    cur = execute(conn, query, (event_id,))
    rows = [dict(r) for r in cur.fetchall()]

    today_str = datetime.date.today().isoformat()
    for r in rows:
        r["assigner_label"] = r.get("assigner_name") or r.get("created_by_name") or "Super Admin / Event Director"
        r["assignee_label"] = r.get("assignee_name") or r.get("assigned_to_name") or "Unassigned"
        r["is_overdue"] = bool(r.get("deadline") and r["status"] != "completed" and r["deadline"] < today_str)

    return rows

@router.get("/summary")
def get_tasks_summary(event_id: int, conn=Depends(get_db), user=Depends(get_current_user)):
    ensure_tasks_schema(conn)
    role_ctx = get_event_role(conn, user, event_id)
    if role_ctx["level"] is None:
        raise HTTPException(status_code=403, detail="You don't have access to this event")

    cur = execute(conn, """
        SELECT d.id as dept_id, d.name as dept_name, d.head_name, d.color as dept_color,
               COUNT(t.id) as total_given,
               COUNT(CASE WHEN t.status = 'completed' THEN 1 END) as total_completed,
               COUNT(CASE WHEN t.status != 'completed' THEN 1 END) as total_pending
        FROM departments d
        LEFT JOIN department_tasks t ON t.department_id = d.id
        WHERE d.event_id = %s
        GROUP BY d.id, d.name, d.head_name, d.color
        ORDER BY d.name
    """, (event_id,))
    return [dict(r) for r in cur.fetchall()]

@router.post("/")
def create_task(data: TaskCreate, conn=Depends(get_db), user=Depends(get_current_user)):
    ensure_tasks_schema(conn)
    role_ctx = get_event_role(conn, user, data.event_id)
    
    # Authority strictly restricted ONLY to Super Admin or Event Admin / Co-Leader
    if not is_admin_or_superadmin(user, role_ctx):
        raise HTTPException(status_code=403, detail="Only Super Admin or Event Admin can assign work tasks")

    target_user_id = data.assigned_to_user_id
    if not target_user_id and data.assigned_to_name:
        cur_u = execute(conn, "SELECT id FROM users WHERE LOWER(name)=LOWER(%s) OR LOWER(email)=LOWER(%s)",
                        (data.assigned_to_name.strip(), data.assigned_to_name.strip()))
        u_row = cur_u.fetchone()
        if u_row:
            target_user_id = u_row["id"]

    creator_name = user.get("name") or user.get("email") or "Super Admin"

    cur = execute(conn, """
        INSERT INTO department_tasks
        (event_id, department_id, assigned_to_user_id, assigned_to_name, title, description, deadline, priority, status, created_by, created_by_name)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING *
    """, (
        data.event_id, data.department_id, target_user_id, data.assigned_to_name,
        data.title, data.description, data.deadline, data.priority, data.status, user["id"], creator_name
    ))
    task = dict(cur.fetchone())

    # 1. Log to recent activity
    actor_name = user.get("name") or user.get("email") or "Admin"
    assignee_label = data.assigned_to_name or "team member"
    log_activity(conn, data.event_id, user["id"], ACTION_TASK_ASSIGNED, f"{actor_name} assigned work '{data.title}' to {assignee_label}")

    # 2. Dispatch persistent unread notification for target user
    if target_user_id:
        cur_e = execute(conn, "SELECT name FROM events WHERE id=%s", (data.event_id,))
        ev_row = cur_e.fetchone()
        ev_name = ev_row["name"] if ev_row else "Event"
        title_str = "📋 Task Assigned"
        msg = f"WORK ASSIGNED: '{data.title}' (Deadline: {data.deadline or 'TBD'}) for event '{ev_name}'."
        run_safely(conn, lambda: execute(conn, """
            INSERT INTO notifications (user_id, title, message, priority, category, event_id, action_url, is_read)
            VALUES (%s, %s, %s, %s, %s, %s, %s, 0)
        """, (target_user_id, title_str, msg, "info", "task", data.event_id, "/calendar")))

    return task

@router.put("/{task_id}")
def update_task(task_id: int, data: TaskUpdate, conn=Depends(get_db), user=Depends(get_current_user)):
    ensure_tasks_schema(conn)
    cur = execute(conn, "SELECT * FROM department_tasks WHERE id=%s", (task_id,))
    task = cur.fetchone()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    role_ctx = get_event_role(conn, user, task["event_id"])
    can_update = (
        is_admin_or_superadmin(user, role_ctx)
        or task["assigned_to_user_id"] == user["id"]
        or (role_ctx["level"] == "dept_head" and str(role_ctx["dept_id"]) == str(task["department_id"]))
    )

    if not can_update:
        raise HTTPException(status_code=403, detail="You cannot modify this task")

    fields = {k: v for k, v in data.dict().items() if v is not None}
    if not fields:
        raise HTTPException(status_code=400, detail="No fields to update")

    # If status is set to incomplete or overdue, require incomplete_reason
    today_str = datetime.date.today().isoformat()
    is_overdue = bool(task.get("deadline") and task["deadline"] < today_str)

    if fields.get("status") == "incomplete" or (is_overdue and fields.get("status") != "completed"):
        reason = (fields.get("incomplete_reason") or task.get("incomplete_reason") or "").strip()
        if not reason:
            raise HTTPException(status_code=400, detail="An explanation why the task is incomplete or delayed is compulsory.")

    # Record completion date
    if fields.get("status") == "completed" and not fields.get("completed_at"):
        fields["completed_at"] = datetime.datetime.utcnow().isoformat()

    set_clause = ", ".join(f"{k}=%s" for k in fields)
    values = list(fields.values()) + [task_id]
    cur_u = execute(conn, f"UPDATE department_tasks SET {set_clause} WHERE id=%s RETURNING *", values)
    updated_task = dict(cur_u.fetchone())

    if "status" in fields and fields["status"] != task["status"]:
        actor_name = user.get("name") or user.get("email") or "User"
        status_label = fields["status"]
        log_activity(conn, task["event_id"], user["id"], ACTION_TASK_UPDATED, f"{actor_name} marked task '{task['title']}' as {status_label}")

        if task.get("assigned_to_user_id") and task["assigned_to_user_id"] != user["id"]:
            title_str = "🎯 Task Updated"
            msg = f"TASK UPDATE: Task '{task['title']}' was marked as {status_label} by {actor_name}."
            run_safely(conn, lambda: execute(conn, """
                INSERT INTO notifications (user_id, title, message, priority, category, event_id, action_url, is_read)
                VALUES (%s, %s, %s, %s, %s, %s, %s, 0)
            """, (task["assigned_to_user_id"], title_str, msg, "info", "task", task["event_id"], "/calendar")))

    return updated_task

@router.delete("/{task_id}")
def delete_task(task_id: int, conn=Depends(get_db), user=Depends(get_current_user)):
    ensure_tasks_schema(conn)
    cur = execute(conn, "SELECT * FROM department_tasks WHERE id=%s", (task_id,))
    task = cur.fetchone()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    role_ctx = get_event_role(conn, user, task["event_id"])
    if not is_admin_or_superadmin(user, role_ctx):
        raise HTTPException(status_code=403, detail="Only Super Admin or Event Admin can delete work tasks")

    execute(conn, "DELETE FROM department_tasks WHERE id=%s", (task_id,))
    return {"ok": True}
