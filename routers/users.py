from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional
from core.database import get_db, execute
from core.auth import get_current_user, hash_password, verify_password
from utils.roles import get_event_role, is_event_owner_or_super_admin
from utils.db_safety import run_safely

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
    run_safely(conn, lambda: execute(conn, "DELETE FROM notifications WHERE user_id=%s", (user_id,)))
    run_safely(conn, lambda: execute(conn, "DELETE FROM user_event_roles WHERE user_id=%s", (user_id,)))
    run_safely(conn, lambda: execute(conn, "DELETE FROM user_roles WHERE user_id=%s", (user_id,)))
    run_safely(conn, lambda: execute(conn, "DELETE FROM audit_log WHERE user_id=%s", (user_id,)))
    
    # Try deleting events owned by this user or nullifying owner reference
    run_safely(conn, lambda: execute(conn, "DELETE FROM events WHERE user_id=%s", (user_id,)))
    run_safely(conn, lambda: execute(conn, "UPDATE events SET user_id=NULL WHERE user_id=%s", (user_id,)))

    try:
        execute(conn, "DELETE FROM users WHERE id=%s", (user_id,))
    except Exception:
        execute(conn, "UPDATE users SET is_active=0, email=CONCAT(email, '_deleted_', id) WHERE id=%s", (user_id,))
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

class InviteMemberRequest(BaseModel):
    email: str
    name: Optional[str] = None
    role: str
    dept_id: Optional[int] = None
    event_id: int

class PasswordReset(BaseModel):
    user_id: int
    new_password: str

def send_invite_email(to_email: str, inviter_name: str, role_title: str, event_name: str):
    import os, smtplib
    from email.mime.text import MIMEText
    from email.mime.multipart import MIMEMultipart

    sender_email = os.getenv("SMTP_EMAIL", "moryesoham4@gmail.com").strip()
    sender_password = os.getenv("SMTP_PASSWORD", "nbpcyvdiqbnbyvwj").replace(" ", "").strip()

    if not sender_email or not sender_password:
        return

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"🎉 You've been invited to '{event_name}' on EventLedger AI"
    msg["From"] = f"EventLedger AI <{sender_email}>"
    msg["To"] = to_email

    html = f"""
    <div style="font-family: Arial, sans-serif; padding: 24px; background-color: #0F172A; color: #F8FAFC; border-radius: 12px; max-width: 500px;">
      <h2 style="color: #FF7A00; margin-top: 0;">EventLedger AI — Team Invitation</h2>
      <p style="font-size: 15px;"><strong>{inviter_name}</strong> invited you to collaborate as <strong>{role_title}</strong> for the event: <strong>{event_name}</strong>.</p>
      
      <div style="background-color: #1E293B; padding: 16px; border-radius: 8px; margin: 20px 0;">
        <h4 style="color: #10B981; margin-top: 0; margin-bottom: 8px;">🚀 How to Log In (Choose Either):</h4>
        <p style="margin: 4px 0; font-size: 13px;"><strong>Option 1:</strong> Click <strong>"Sign in with Google"</strong> using this email ({to_email}) for instant 1-click access.</p>
        <p style="margin: 4px 0; font-size: 13px;"><strong>Option 2:</strong> Click <strong>"Forgot Password?"</strong> on the login page to set your own password using the instant verification code.</p>
      </div>

      <a href="https://eventledger-web.vercel.app/login" style="display: inline-block; background-color: #2563EB; color: #ffffff; padding: 12px 24px; text-decoration: none; border-radius: 8px; font-weight: bold; font-size: 14px;">Open EventLedger AI →</a>
    </div>
    """
    msg.attach(MIMEText(html, "html"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=10) as server:
            server.login(sender_email, sender_password)
            server.sendmail(sender_email, to_email, msg.as_string())
    except Exception:
        try:
            with smtplib.SMTP("smtp.gmail.com", 587, timeout=10) as server:
                server.starttls()
                server.login(sender_email, sender_password)
                server.sendmail(sender_email, to_email, msg.as_string())
        except Exception:
            pass

import re

EMAIL_REGEX = re.compile(r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$")

def validate_and_clean_email(email_str: str) -> str:
    if not email_str:
        raise HTTPException(status_code=400, detail="Email address is required")
    cleaned = email_str.lower().strip()
    if not EMAIL_REGEX.match(cleaned):
        raise HTTPException(
            status_code=400,
            detail="Invalid email format. Please enter a complete email address with '@' and domain (e.g. name@gmail.com)"
        )
    return cleaned

@router.get("/event-team/{event_id}")
def get_event_team(event_id: int, conn=Depends(get_db), user=Depends(get_current_user)):
    cur = execute(conn, """
        SELECT u.id, u.name, u.email, u.avatar_color, r.role, r.dept_id, d.name as dept_name
        FROM user_event_roles r
        JOIN users u ON u.id = r.user_id
        LEFT JOIN departments d ON d.id = r.dept_id
        WHERE r.event_id = %s
        ORDER BY u.name
    """, (event_id,))
    return [dict(row) for row in cur.fetchall()]

@router.post("/invite-member")
def invite_member(data: InviteMemberRequest, conn=Depends(get_db), user=Depends(get_current_user)):
    if not is_event_owner_or_super_admin(conn, user, data.event_id):
        raise HTTPException(status_code=403, detail="Only event admins can invite team members")
    
    target_email = validate_and_clean_email(data.email)
    cur = execute(conn, "SELECT * FROM users WHERE email=%s", (target_email,))
    target_user = cur.fetchone()
    
    if not target_user:
        name_str = data.name.strip() if data.name else target_email.split("@")[0].capitalize()
        random_pwd = hash_password(f"invite_{target_email}_secret")
        cur = execute(
            conn,
            "INSERT INTO users (name, email, password, role, is_super_admin, org_name) VALUES (%s, %s, %s, %s, %s, %s) RETURNING *",
            (name_str, target_email, random_pwd, "event_admin", 0, user.get("org_name") or "Event Team")
        )
        target_user = cur.fetchone()
    
    target_id = target_user["id"]

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

    # Dispatch email invitation
    try:
        send_invite_email(target_email, user.get("name") or "Event Admin", role_title, ev_name)
    except Exception as err:
        print(f"Error sending invite email: {err}")

    return {"ok": True, "message": f"Successfully invited {target_email} to team!"}

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
           VALUES (%s,%s,%s,%s,%s)""",
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
