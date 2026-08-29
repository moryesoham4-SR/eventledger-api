"""
Role-based access control helpers.

Role values used throughout:
    "co_leader"     - FULL Event Authority (Co-Lead / Vice President / Co-Host) — full control
                       of budgets, approvals, team roles, and departments.
    "event_admin"   - Event Manager — manages departments, assigns work tasks, monitors
                       operations, verifies claims.
    "finance_head"  - Finance Lead — approves/rejects budgets, manages income/expenses & payouts.
    "dept_head"     - Department Head — scoped to their department: submits budgets & verifies claims.
    "volunteer"     - Co-Worker / Volunteer — scoped to department, completes tasks & files claims.
"""
from core.database import execute

_ROLE_PRIORITY = {
    "co_leader": 4,
    "event_admin": 3,
    "finance_head": 2,
    "dept_head": 1,
    "volunteer": 0
}

def get_event_role(conn, user: dict, event_id: int) -> dict:
    """Returns {'level': str|None, 'dept_id': int|None} describing this user's access on the given event."""
    if user and user.get("is_super_admin"):
        return {"level": "co_leader", "dept_id": None}

    cur = execute(conn, "SELECT user_id FROM events WHERE id=%s", (event_id,))
    ev = cur.fetchone()
    if ev and ev["user_id"] == user["id"]:
        return {"level": "co_leader", "dept_id": None}

    cur = execute(
        conn,
        "SELECT role, dept_id FROM user_event_roles WHERE user_id=%s AND event_id=%s",
        (user["id"], event_id),
    )
    rows = cur.fetchall()
    if not rows:
        return {"level": None, "dept_id": None}

    best = max(rows, key=lambda r: _ROLE_PRIORITY.get(r["role"], -1))
    return {"level": best["role"], "dept_id": best["dept_id"]}

def is_event_owner_or_super_admin(conn, user: dict, event_id: int) -> bool:
    if user.get("is_super_admin"):
        return True
    cur = execute(conn, "SELECT user_id FROM events WHERE id=%s", (event_id,))
    ev = cur.fetchone()
    return bool(ev and ev["user_id"] == user["id"])

def can_manage_departments(role_ctx: dict) -> bool:
    """Create/delete departments — co_leader or event_admin."""
    return role_ctx["level"] in ("co_leader", "event_admin")

def can_approve_budget(role_ctx: dict) -> bool:
    """Approve/reject a submitted budget proposal — co_leader or finance_head."""
    return role_ctx["level"] in ("co_leader", "finance_head")

def can_access_department(role_ctx: dict, dept_id) -> bool:
    if role_ctx["level"] in ("co_leader", "event_admin", "finance_head"):
        return True
    if role_ctx["level"] in ("dept_head", "volunteer"):
        return role_ctx["dept_id"] is not None and str(role_ctx["dept_id"]) == str(dept_id)
    return False

def can_edit_department(role_ctx: dict, dept_id) -> bool:
    if role_ctx["level"] in ("co_leader", "event_admin", "finance_head"):
        return True
    if role_ctx["level"] == "dept_head":
        return role_ctx["dept_id"] is not None and str(role_ctx["dept_id"]) == str(dept_id)
    return False
