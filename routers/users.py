from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional
from core.database import get_db, execute
from core.auth import get_current_user, hash_password, verify_password
from utils.roles import get_event_role, is_event_owner_or_super_admin
from utils.db_safety import run_safely
from utils.email import send_team_invite_email
import re

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

class InviteMemberRequest(BaseModel):
    event_id: int
    email: str
    role: str = "volunteer"
    dept_id: Optional[int] = None
    name: Optional[str] = None
    password: Optional[str] = None

class RoleAssign(BaseModel):
    user_id: int
    event_id: Optional[int] = None
    role: str
    dept_id: Optional[int] = None

class PasswordReset(BaseModel):
    user_id: int
    new_password: str

def validate_and_clean_email(raw_email: str) -> str:
    cleaned = raw_email.strip().lower()
    email_regex = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"
    if not re.match(email_regex, cleaned):
        raise HTTPException(status_code=400, detail="Invalid email format")
    return cleaned

@router.get("/me")
def get_my_profile(user=Depends(get_current_user)):
    return {
        "id": user["id"], "name": user["name"], "email": user["email"],
        "role": user.get("role"), "org_name": user.get("org_name"),
        "avatar_color": user.get("avatar_color"), "is_super_admin": bool(user.get("is_super_admin")),
        "created_at": user.get("created_at"),
    }

@router.delete("/me")
def delete_my_account(conn=Depends(get_db), user=Depends(get_current_user)):
    user_id = user["id"]
    run_safely(conn, lambda: execute(conn, "DELETE FROM notifications WHERE user_id=%s", (user_id,)))
    run_safely(conn, lambda: execute(conn, "DELETE FROM user_event_roles WHERE user_id=%s", (user_id,)))
    run_safely(conn, lambda: execute(conn, "DELETE FROM user_roles WHERE user_id=%s", (user_id,)))
    run_safely(conn, lambda: execute(conn, "DELETE FROM audit_log WHERE user_id=%s", (user_id,)))
    run_safely(conn, lambda: execute(conn, "DELETE FROM events WHERE user_id=%s", (user_id,)))
    run_safely(conn, lambda: execute(conn, "UPDATE events SET user_id=NULL WHERE user_id=%s", (user_id,)))

    try:
        execute(conn, "DELETE FROM users WHERE id=%s", (user_id,))
    except Exception:
        execute(conn, "UPDATE users SET is_active=0, email=CONCAT(email, '_deleted_', id) WHERE id=%s", (user_id,))
    return {"ok": True, "message": "Account deleted successfully"}

@router.delete("/{user_id}")
def delete_user_by_admin(user_id: int, conn=Depends(get_db), user=Depends(get_current_user)):
    _require_super_admin(user)
    if user_id == user["id"]:
        raise HTTPException(status_code=400, detail="You cannot delete your own account from here.")

    run_safely(conn, lambda: execute(conn, "DELETE FROM notifications WHERE user_id=%s", (user_id,)))
    run_safely(conn, lambda: execute(conn, "DELETE FROM user_event_roles WHERE user_id=%s", (user_id,)))
    run_safely(conn, lambda: execute(conn, "DELETE FROM user_roles WHERE user_id=%s", (user_id,)))
    run_safely(conn, lambda: execute(conn, "DELETE FROM audit_log WHERE user_id=%s", (user_id,)))

    try:
        execute(conn, "DELETE FROM users WHERE id=%s", (user_id,))
    except Exception:
        execute(conn, "UPDATE users SET is_active=0, email=CONCAT(email, '_deleted_', id) WHERE id=%s", (user_id,))

    return {"ok": True, "message": "User deleted successfully"}

@router.put("/me")
def update_my_profile(data: ProfileUpdate, conn=Depends(get_db), user=Depends(get_current_user)):
    fields = []
    values = []
    if data.name is not None:
        fields.append("name=%s")
        values.append(data.name.strip())
    if data.avatar_color is not None:
        fields.append("avatar_color=%s")
        values.append(data.avatar_color)
    if not fields:
        return {"ok": True, "message": "No changes"}
    values.append(user["id"])
    cur = execute(conn, f"UPDATE users SET {','.join(fields)} WHERE id=%s RETURNING id,name,email,role,org_name,avatar_color,is_super_admin", values)
    return dict(cur.fetchone())

@router.post("/me/change-password")
def change_password(data: ChangePassword, conn=Depends(get_db), user=Depends(get_current_user)):
    cur = execute(conn, "SELECT password FROM users WHERE id=%s", (user["id"],))
    u = cur.fetchone()
    if not u or not verify_password(data.current_password, u["password"]):
        raise HTTPException(status_code=400, detail="Current password is incorrect")
    execute(conn, "UPDATE users SET password=%s WHERE id=%s", (hash_password(data.new_password), user["id"]))
    return {"ok": True, "message": "Password changed successfully"}

@router.get("/my-role")
def get_my_role(event_id: int, conn=Depends(get_db), user=Depends(get_current_user)):
    role_ctx = get_event_role(conn, user, event_id)
    return {
        "level": role_ctx["level"],
        "dept_id": role_ctx["dept_id"],
        "can_manage_departments": role_ctx["level"] in ("co_leader", "event_admin"),
        "can_manage_invites": role_ctx["level"] in ("co_leader", "event_admin"),
        "can_manage_tasks": role_ctx["level"] in ("co_leader", "event_admin"),
        "can_approve_budget": role_ctx["level"] in ("co_leader", "finance_head"),
        "is_super_admin": bool(user.get("is_super_admin")),
    }

@router.post("/invite-member")
def invite_member(data: InviteMemberRequest, conn=Depends(get_db), user=Depends(get_current_user)):
    role_ctx = get_event_role(conn, user, data.event_id)
    if not (role_ctx["level"] in ("co_leader", "event_admin") or is_event_owner_or_super_admin(conn, user, data.event_id)):
        raise HTTPException(status_code=403, detail="Only Event Lead and Co-Leader can invite team members")
    
    target_email = validate_and_clean_email(data.email)
    cur = execute(conn, "SELECT * FROM users WHERE email=%s", (target_email,))
    target_user = cur.fetchone()
    
    if not target_user:
        name_str = data.name.strip() if data.name else target_email.split("@")[0].capitalize()
        pwd_to_use = hash_password(data.password) if (data.password and data.password.strip()) else hash_password(f"invite_{target_email}_secret")
        cur = execute(
            conn,
            "INSERT INTO users (name, email, password, role, is_super_admin, org_name) VALUES (%s, %s, %s, %s, %s, %s) RETURNING *",
            (name_str, target_email, pwd_to_use, data.role, 0, user.get("org_name") or "Event Team")
        )
        target_user = cur.fetchone()
    else:
        execute(conn, "UPDATE users SET role=%s WHERE id=%s", (data.role, target_user["id"]))
    
    target_id = target_user["id"]

    run_safely(conn, lambda: execute(conn, "DELETE FROM user_event_roles WHERE user_id=%s AND event_id=%s", (target_id, data.event_id)))

    run_safely(conn, lambda: execute(conn, """
        INSERT INTO user_event_roles (user_id, event_id, role, dept_id, assigned_by)
        VALUES (%s, %s, %s, %s, %s)
    """, (target_id, data.event_id, data.role, data.dept_id, user["id"])))

    cur_e = execute(conn, "SELECT name FROM events WHERE id=%s", (data.event_id,))
    ev_row = cur_e.fetchone()
    ev_name = ev_row["name"] if ev_row else "Event"
    role_title = data.role.replace("_", " ").title()

    notif_msg = f"🎉 TEAM INVITE: You've been invited as '{role_title}' for '{ev_name}' by {user['name']}!"
    run_safely(conn, lambda: execute(conn, """
        INSERT INTO notifications (user_id, message, priority, category, event_id, action_url)
        VALUES (%s, %s, %s, %s, %s, %s)
    """, (target_id, notif_msg, "info", "general", data.event_id, "/dashboard")))

    # Dispatch rich HTML email notification in background
    try:
        send_team_invite_email(target_email, target_user.get("name") or "Team Member", role_title, ev_name)
    except Exception as err:
        print(f"Error sending invite email: {err}")

    return {"ok": True, "message": f"Successfully invited {target_email} to team!"}

@router.get("/")
def get_users(event_id: Optional[int] = None, conn=Depends(get_db), user=Depends(get_current_user)):
    _require_super_admin(user)
    if event_id:
        cur = execute(conn, """
            SELECT u.id, u.name, u.email, COALESCE(r.role, u.role) as role, r.dept_id, d.name as dept_name, u.is_super_admin, u.org_name, u.avatar_color, u.is_active, u.created_at
            FROM users u
            LEFT JOIN user_event_roles r ON r.user_id = u.id AND r.event_id = %s
            LEFT JOIN departments d ON d.id = r.dept_id
            WHERE u.org_name = %s
            ORDER BY u.name
        """, (event_id, user.get("org_name") or ""))
    else:
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
        """SELECT u.id,u.name,u.email,u.avatar_color,r.role,r.dept_id, d.name as dept_name
           FROM user_event_roles r JOIN users u ON u.id=r.user_id
           LEFT JOIN departments d ON d.id = r.dept_id
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

    # 1. Update the user's role in the main users table so Users directory reflects it instantly!
    execute(conn, "UPDATE users SET role=%s WHERE id=%s", (data.role, data.user_id))

    # 2. Delete any old user_event_roles for this event_id if provided
    if data.event_id is not None:
        execute(conn, "DELETE FROM user_event_roles WHERE user_id=%s AND event_id=%s", (data.user_id, data.event_id))
        execute(conn,
            """INSERT INTO user_event_roles (user_id,event_id,role,dept_id,assigned_by)
               VALUES (%s,%s,%s,%s,%s)""",
            (data.user_id, data.event_id, data.role, data.dept_id, user["id"])
        )

        cur_u = execute(conn, "SELECT email, name FROM users WHERE id=%s", (data.user_id,))
        target_u = cur_u.fetchone()
        cur_e = execute(conn, "SELECT name FROM events WHERE id=%s", (data.event_id,))
        ev_row = cur_e.fetchone()
        if target_u and target_u.get("email") and ev_row:
            try:
                send_team_invite_email(target_u["email"], target_u.get("name") or "Team Member", data.role.replace("_", " ").title(), ev_row["name"])
            except Exception as err:
                print(f"Error sending role update email: {err}")

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
