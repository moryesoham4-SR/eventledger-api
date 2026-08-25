from fastapi import APIRouter, Depends
from core.database import get_db, execute
from core.auth import get_current_user

router = APIRouter(prefix="/api/notifications", tags=["notifications"])

@router.get("/")
def get_notifications(conn=Depends(get_db), user=Depends(get_current_user)):
    cur = execute(conn,
        "SELECT * FROM notifications WHERE user_id=%s ORDER BY created_at DESC LIMIT 50",
        (user["id"],)
    )
    return [dict(r) for r in cur.fetchall()]

@router.get("/unread-count")
def unread_count(conn=Depends(get_db), user=Depends(get_current_user)):
    cur = execute(conn,
        "SELECT COUNT(*) as count FROM notifications WHERE user_id=%s AND is_read=0",
        (user["id"],)
    )
    return {"count": list(cur.fetchone().values())[0]}

@router.post("/{notif_id}/read")
def mark_read(notif_id: int, conn=Depends(get_db), user=Depends(get_current_user)):
    execute(conn, "UPDATE notifications SET is_read=1 WHERE id=%s AND user_id=%s", (notif_id, user["id"]))
    return {"ok": True}

@router.post("/read-all")
def mark_all_read(conn=Depends(get_db), user=Depends(get_current_user)):
    execute(conn, "UPDATE notifications SET is_read=1 WHERE user_id=%s", (user["id"],))
    return {"ok": True}
