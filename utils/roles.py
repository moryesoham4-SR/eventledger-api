"""
Role-based access control helpers.

Role values used throughout (must match exactly, including in the
"Assign Role" dropdown on the frontend and any rows in user_event_roles):
    "event_admin"   - full control of a specific event (departments, budgets,
                       approvals, everything except deleting the event itself
                       unless they're also the creator)
    "finance_head"  - can see & approve/reject budgets for ALL departments on
                       an event, manage income/expenses; cannot manage
                       departments or delete the event
    "dept_head"     - scoped to their own department on that event: can see,
                       create, and submit budget proposals for it; cannot
                       approve/reject
    "volunteer"     - scoped to their own department, read-only

IMPORTANT — event-level access ("level" below) comes ONLY from:
  1. Owning the event (events.user_id == user.id), or
  2. An explicit row in user_event_roles for that specific event_id.

It never comes automatically from users.is_super_admin, and it never falls
back to a user's global `role`/`org_name` columns. Those are free-text
fields two completely unrelated accounts can easily share (both blank, both
"Test", the same common college/company name) — matching on them was
letting strangers see each other's private event data. is_super_admin is
reserved for platform-wide screens gated separately and explicitly
(the Users directory, audit log — see routers/users.py's
_require_super_admin) and does NOT grant read/write access into someone
else's event. A super admin viewing their OWN events still works fine
because they own those events, same as anyone else.
"""
from core.database import execute

_ROLE_PRIORITY = {"event_admin": 3, "finance_head": 2, "dept_head": 1, "volunteer": 0}


def get_event_role(conn, user: dict, event_id: int) -> dict:
    """Returns {'level': str|None, 'dept_id': int|None} describing this user's
    access on the given event. `level` is None if the user has no access at all."""
    if user and user.get("is_super_admin"):
        return {"level": "event_admin", "dept_id": None}

    cur = execute(conn, "SELECT user_id FROM events WHERE id=%s", (event_id,))
    ev = cur.fetchone()
    if ev and ev["user_id"] == user["id"]:
        return {"level": "event_admin", "dept_id": None}

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
    """The only two parties allowed to delete an event outright: the person
    who created it, or a true platform super admin (moderation power — this
    doesn't expose any data the super admin couldn't already see via the
    Users directory, it just allows removing abusive/erroneous events)."""
    if user.get("is_super_admin"):
        return True
    cur = execute(conn, "SELECT user_id FROM events WHERE id=%s", (event_id,))
    ev = cur.fetchone()
    return bool(ev and ev["user_id"] == user["id"])


def can_manage_departments(role_ctx: dict) -> bool:
    """Create/delete departments — event_admin only."""
    return role_ctx["level"] == "event_admin"


def can_approve_budget(role_ctx: dict) -> bool:
    """Approve/reject a submitted budget proposal — event_admin or finance_head."""
    return role_ctx["level"] in ("event_admin", "finance_head")


def can_access_department(role_ctx: dict, dept_id) -> bool:
    """Can view/act on a given department's data at all."""
    if role_ctx["level"] in ("event_admin", "finance_head"):
        return True
    if role_ctx["level"] in ("dept_head", "volunteer"):
        return role_ctx["dept_id"] is not None and str(role_ctx["dept_id"]) == str(dept_id)
    return False


def can_edit_department(role_ctx: dict, dept_id) -> bool:
    """Create/submit budget proposals & line items for a department —
    event_admin, finance_head, or the dept_head of that specific department."""
    if role_ctx["level"] in ("event_admin", "finance_head"):
        return True
    if role_ctx["level"] == "dept_head":
        return role_ctx["dept_id"] is not None and str(role_ctx["dept_id"]) == str(dept_id)
    return False
