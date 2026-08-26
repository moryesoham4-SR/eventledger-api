from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional
from core.database import get_db, execute
from core.auth import get_current_user, hash_password, verify_password
from utils.roles import get_event_role, is_event_owner_or_super_admin

router = APIRouter(prefix="/api/users", tags=["users"])

def _require_super_admin(user):
    if not user.get("is_super_admin"):
        raise HTTPException(status_code=403, detail="Super Admin only")

class ProfileUpdate(BaseModel):
    name: Optional[str] = None
    avatar_color: Optional[str] = None

class ChangePassword(BaseModel):
    current_password: str
    new_password: str

@router.get("/me")
def get_my_profile(user=Depends(get_current_user)):
    """Your own profile — anyone can see their own info regardless of role."""
    return {
        "id": user["id"], "name": user["name"], "email": user["email"],
        "role": user.get("role"), "org_name": user.get("org_name"),
        "avatar_color": user.get("avatar_color"), "is_super_admin": bool(user.get("is_super_admin")),
        "created_at": user.get("created_at"),
    }

@router.delete("/me")
def delete_my_account(conn=Depends(get_db), user=Depends(get_current_user)):
    """Deletes your own account and all associated user data."""
    user_id = user["id"]
    try:
        execute(conn, "DELETE FROM notifications WHERE user_id=%s", (user_id,))
        execute(conn, "DELETE FROM user_roles WHERE user_id=%s", (user_id,))
        execute(conn, "DELETE FROM users WHERE id=%s", (user_id,))
    except Exception:
        execute(conn, "DELETE FROM users WHERE id=%s", (user_id,))
    return {"ok": True, "message": "Account deleted successfully"}

@router.put("/me")
def update_my_profile(data: ProfileUpdate, conn=Depends(get_db), user=Depends(get_current_user)):
    """Editing your own display name / avatar color — deliberately does NOT
    allow changing email (breaks login lookup) or org_name (an
    organizational identity, not a casual personal setting)."""
    fields = {k: v for k, v in data.dict().items() if v is not None}
    if not fields:
        raise HTTPException(status_code=400, detail="No fields to update")
    set_clause = ", ".join(f"{k}=%s" for k in fields)
    values = list(fields.values()) + [user["id"]]
    cur = execute(conn, f"UPDATE users SET {set_clause} WHERE id=%s RETURNING id,name,email,role,org_name,avatar_color,is_super_admin,created_at", values)
    return dict(cur.fetchone())

@router.post("/me/change-password")
def change_my_password(data: ChangePassword, conn=Depends(get_db), user=Depends(get_current_user)):
    """Self-service password change — requires knowing your CURRENT password,
    unlike the admin-only /reset-password below which deliberately bypasses
    that (for when someone's genuinely locked out)."""
    if not verify_password(data.current_password, user["password"]):
        raise HTTPException(status_code=401, detail="Current password is incorrect")
    execute(conn, "UPDATE users SET password=%s WHERE id=%s", (hash_password(data.new_password), user["id"]))
    return {"ok": True}

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
    """User directory — scoped to the requesting super admin's own
    organization (org_name), not the whole platform. An event's own
    participants can also be listed via /event/{event_id}."""
    _require_super_admin(user)
    cur = execute(conn,
        "SELECT id,name,email,role,is_super_admin,org_name,avatar_color,is_active,created_at FROM users WHERE org_name=%s ORDER BY name",
        (user.get("org_name") or "",)
    )
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
        # Scoped to the admin's own org — not every event on the platform.
        cur = execute(conn,
            """SELECT l.*,u.name as user_name FROM audit_log l
               LEFT JOIN users u ON u.id=l.user_id
               LEFT JOIN events e ON e.id=l.event_id
               LEFT JOIN users owner ON owner.id=e.user_id
               WHERE owner.org_name=%s
               ORDER BY l.created_at DESC LIMIT %s""",
            (user.get("org_name") or "", limit)
        )
    return [dict(r) for r in cur.fetchall()]
