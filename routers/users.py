from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional
from core.database import get_db, execute
from core.auth import get_current_user, hash_password

router = APIRouter(prefix="/api/users", tags=["users"])

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
    cur = execute(conn, "SELECT id,name,email,role,is_super_admin,org_name,avatar_color,is_active,created_at FROM users ORDER BY name")
    return [dict(r) for r in cur.fetchall()]

@router.get("/event/{event_id}")
def get_event_users(event_id: int, conn=Depends(get_db), user=Depends(get_current_user)):
    cur = execute(conn,
        """SELECT u.id,u.name,u.email,u.avatar_color,r.role,r.dept_id
           FROM user_event_roles r JOIN users u ON u.id=r.user_id
           WHERE r.event_id=%s""",
        (event_id,)
    )
    return [dict(r) for r in cur.fetchall()]

@router.post("/assign-role")
def assign_role(data: RoleAssign, conn=Depends(get_db), user=Depends(get_current_user)):
    execute(conn,
        """INSERT INTO user_event_roles (user_id,event_id,role,dept_id,assigned_by)
           VALUES (%s,%s,%s,%s,%s)
           ON CONFLICT (user_id,event_id,role,dept_id) DO NOTHING""",
        (data.user_id, data.event_id, data.role, data.dept_id, user["id"])
    )
    return {"ok": True}

@router.post("/reset-password")
def reset_password(data: PasswordReset, conn=Depends(get_db), user=Depends(get_current_user)):
    if not user.get("is_super_admin"):
        raise HTTPException(status_code=403, detail="Super Admin only")
    execute(conn, "UPDATE users SET password=%s WHERE id=%s", (hash_password(data.new_password), data.user_id))
    return {"ok": True}

@router.get("/audit-log")
def get_audit_log(event_id: Optional[int] = None, limit: int = 100, conn=Depends(get_db), user=Depends(get_current_user)):
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
