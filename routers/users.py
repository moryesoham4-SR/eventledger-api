from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional
from core.database import get_db, execute
from core.auth import get_current_user, hash_password
from utils.roles import get_event_role, is_event_owner_or_super_admin

router = APIRouter(prefix="/api/users", tags=["users"])

def _require_super_admin(user):
    if not user.get("is_super_admin"):
        raise HTTPException(status_code=403, detail="Super Admin only")

@router.get("/my-role")
def my_role(event_id: int, conn=Depends(get_db), user=Depends(get_current_user)):
    """Tells the frontend what this user can do on a given event, so it can
    show/hide actions (approve, delete department, etc.) accordingly.
    The backend enforces these independently — this is for UI convenience only."""
    role_ctx = get_event_role(conn, user, event_id)
    return {
        "level": role_ctx["level"],  # "event_admin" | "finance_head" | "dept_head" | "volunteer" | None
        "dept_id": role_ctx["dept_id"],
        "can_manage_departments": role_ctx["level"] == "event_admin",
        "can_approve_budget": role_ctx["level"] in ("event_admin", "finance_head"),
        "is_super_admin": bool(user.get("is_super_admin")),
    }

class RoleAssign(BaseModel):
    user_id: int
    event_id: Optional[int] = None
    role: str
    dept_id: Optional[int] = None

class PasswordReset(BaseModel):
    user_id: int
    new_password: str

@router.get("/")
def get_users(conn=Depends(get_db), user=Depends(get_current_user)):
    """Full user directory — platform-wide, so this is restricted to super admins.
    An event's own participants can be listed via /event/{event_id} instead."""
    _require_super_admin(user)
    cur = execute(conn, "SELECT id,name,email,role,is_super_admin,org_name,avatar_color,is_active,created_at FROM users ORDER BY name")
    return [dict(r) for r in cur.fetchall()]

@router.get("/event/{event_id}")
def get_event_users(event_id: int, conn=Depends(get_db), user=Depends(get_current_user)):
    role_ctx = get_event_role(conn, user, event_id)
    if role_ctx["level"] is None:
        raise HTTPException(status_code=403, detail="You don't have access to this event")
    cur = execute(conn,
        """SELECT u.id,u.name,u.email,u.avatar_color,r.role,r.dept_id
           FROM user_event_roles r JOIN users u ON u.id=r.user_id
           WHERE r.event_id=%s""",
        (event_id,)
    )
    return [dict(r) for r in cur.fetchall()]

@router.post("/assign-role")
def assign_role(data: RoleAssign, conn=Depends(get_db), user=Depends(get_current_user)):
    if data.event_id is not None:
        if not is_event_owner_or_super_admin(conn, user, data.event_id):
            raise HTTPException(status_code=403, detail="Only this event's admin can assign roles on it")
    else:
        _require_super_admin(user)

    execute(conn,
        """INSERT INTO user_event_roles (user_id,event_id,role,dept_id,assigned_by)
           VALUES (%s,%s,%s,%s,%s)
           ON CONFLICT (user_id,event_id,role,dept_id) DO NOTHING""",
        (data.user_id, data.event_id, data.role, data.dept_id, user["id"])
    )
    return {"ok": True}

@router.post("/reset-password")
def reset_password(data: PasswordReset, conn=Depends(get_db), user=Depends(get_current_user)):
    _require_super_admin(user)
    execute(conn, "UPDATE users SET password=%s WHERE id=%s", (hash_password(data.new_password), data.user_id))
    return {"ok": True}

@router.get("/audit-log")
def get_audit_log(event_id: Optional[int] = None, limit: int = 100, conn=Depends(get_db), user=Depends(get_current_user)):
    _require_super_admin(user)
    if event_id:
        cur = execute(conn,
            """SELECT l.*,u.name as user_name FROM audit_log l
               LEFT JOIN users u ON u.id=l.user_id
               WHERE l.event_id=%s ORDER BY l.created_at DESC LIMIT %s""",
            (event_id, limit)
        )
    else:
        cur = execute(conn,
            """SELECT l.*,u.name as user_name FROM audit_log l
               LEFT JOIN users u ON u.id=l.user_id
               ORDER BY l.created_at DESC LIMIT %s""",
            (limit,)
        )
    return [dict(r) for r in cur.fetchall()]
