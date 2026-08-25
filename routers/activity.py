"""
Read-only activity feed for an event — who did what, when. Entries are
written by other routers via utils/activity.log_activity; this module just
exposes them.
"""
from fastapi import APIRouter, Depends, HTTPException
from core.database import get_db, execute
from core.auth import get_current_user
from utils.roles import get_event_role

router = APIRouter(prefix="/api/events", tags=["activity"])


@router.get("/{event_id}/activity")
def get_activity(event_id: int, limit: int = 50, conn=Depends(get_db), user=Depends(get_current_user)):
    role_ctx = get_event_role(conn, user, event_id)
    if role_ctx["level"] is None:
        raise HTTPException(status_code=403, detail="You don't have access to this event")

    limit = max(1, min(limit, 200))
    cur = execute(conn, """
        SELECT a.id, a.action, a.description, a.created_at, u.name AS user_name
        FROM activity_log a
        LEFT JOIN users u ON u.id = a.user_id
        WHERE a.event_id=%s
        ORDER BY a.created_at DESC
        LIMIT %s
    """, (event_id, limit))
    return [dict(r) for r in cur.fetchall()]
