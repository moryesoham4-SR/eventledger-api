"""
Role-based access control helpers.

Permission model per event:
  - Super admins (users.is_super_admin) — full access everywhere.
  - Event owner (events.user_id == user.id) — full "admin" access on that event.
  - user_event_roles.role for that event:
      "admin"     — full access on that event (departments, budgets, everything)
      "finance"   — can see/approve/reject budgets for ALL departments,
                    manage income/expenses, but cannot create/delete departments
      "dept_head" — scoped to their own department: can see/create/submit budget
                    proposals for that department only, cannot approve/reject
      "volunteer" — scoped to their own department, read-only
  - No role for that event and not owner/super admin — no access.
"""
from core.database import execute

_ROLE_PRIORITY = {"admin": 3, "finance": 2, "dept_head": 1, "volunteer": 0}


def get_event_role(conn, user: dict, event_id: int) -> dict:
    """Returns {'level': str|None, 'dept_id': int|None} describing this user's
    access on the given event. `level` is None if the user has no access at all."""
    if user.get("is_super_admin"):
        return {"level": "admin", "dept_id": None}

    cur = execute(conn, "SELECT user_id FROM events WHERE id=%s", (event_id,))
    ev = cur.fetchone()
    if ev and ev["user_id"] == user["id"]:
        return {"level": "admin", "dept_id": None}

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


def can_manage_departments(role_ctx: dict) -> bool:
    """Create/delete departments — admin only."""
    return role_ctx["level"] == "admin"


def can_approve_budget(role_ctx: dict) -> bool:
    """Approve/reject a submitted budget proposal — admin or finance only."""
    return role_ctx["level"] in ("admin", "finance")


def can_access_department(role_ctx: dict, dept_id) -> bool:
    """Can view/act on a given department's data at all."""
    if role_ctx["level"] in ("admin", "finance"):
        return True
    if role_ctx["level"] in ("dept_head", "volunteer"):
        return role_ctx["dept_id"] is not None and str(role_ctx["dept_id"]) == str(dept_id)
    return False


def can_edit_department(role_ctx: dict, dept_id) -> bool:
    """Create/submit budget proposals & line items for a department —
    admin, finance, or the dept_head of that specific department."""
    if role_ctx["level"] in ("admin", "finance"):
        return True
    if role_ctx["level"] == "dept_head":
        return role_ctx["dept_id"] is not None and str(role_ctx["dept_id"]) == str(dept_id)
    return False
