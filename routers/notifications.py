from fastapi import APIRouter, Depends
from core.database import get_db, execute
from core.auth import get_current_user
from utils.db_safety import run_safely

router = APIRouter(prefix="/api/notifications", tags=["notifications"])

def ensure_notifications_schema(conn):
    run_safely(conn, lambda: execute(conn, "ALTER TABLE notifications ADD COLUMN IF NOT EXISTS priority VARCHAR(20) DEFAULT 'info'"))
    run_safely(conn, lambda: execute(conn, "ALTER TABLE notifications ADD COLUMN IF NOT EXISTS category VARCHAR(30) DEFAULT 'general'"))
    run_safely(conn, lambda: execute(conn, "ALTER TABLE notifications ADD COLUMN IF NOT EXISTS event_id INT"))
    run_safely(conn, lambda: execute(conn, "ALTER TABLE notifications ADD COLUMN IF NOT EXISTS action_url VARCHAR(255)"))

@router.get("/")
def get_notifications(conn=Depends(get_db), user=Depends(get_current_user)):
    ensure_notifications_schema(conn)
    cur = execute(conn,
        "SELECT * FROM notifications WHERE user_id=%s ORDER BY created_at DESC LIMIT 50",
        (user["id"],)
    )
    return [dict(r) for r in cur.fetchall()]

@router.get("/unread-count")
def unread_count(conn=Depends(get_db), user=Depends(get_current_user)):
    ensure_notifications_schema(conn)
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

@router.post("/generate-alerts")
def generate_alerts(event_id: int, conn=Depends(get_db), user=Depends(get_current_user)):
    ensure_notifications_schema(conn)
    user_id = user["id"]
    new_alerts_count = 0

    # 1. Department Budget Overruns
    try:
        cur = execute(conn, """
            SELECT d.id, d.name,
                   COALESCE((SELECT SUM(total_amount) FROM budget_proposals WHERE department_id = d.id AND status = 'approved'), 0) as allocated,
                   COALESCE((SELECT SUM(amount) FROM actual_expenses WHERE department_id = d.id AND status != 'rejected'), 0) as spent
            FROM departments d
            WHERE d.event_id = %s
        """, (event_id,))
        depts = cur.fetchall()

        for dept in depts:
            allocated = float(dept["allocated"] or 0)
            spent = float(dept["spent"] or 0)
            if allocated > 0:
                pct = (spent / allocated) * 100
                if pct >= 100:
                    msg = f"🚨 CRITICAL OVERRUN: Department '{dept['name']}' has exceeded its budget ({pct:.1f}% spent - ₹{spent:,.2f} / ₹{allocated:,.2f})!"
                    check = execute(conn, "SELECT id FROM notifications WHERE user_id=%s AND message=%s AND is_read=0", (user_id, msg))
                    if not check.fetchone():
                        execute(conn, """
                            INSERT INTO notifications (user_id, message, priority, category, event_id, action_url)
                            VALUES (%s, %s, %s, %s, %s, %s)
                        """, (user_id, msg, "critical", "overrun", event_id, "/budget"))
                        new_alerts_count += 1
                elif pct >= 85:
                    msg = f"⚠️ BUDGET WARNING: Department '{dept['name']}' is approaching budget limit ({pct:.1f}% spent - ₹{spent:,.2f} / ₹{allocated:,.2f})."
                    check = execute(conn, "SELECT id FROM notifications WHERE user_id=%s AND message=%s AND is_read=0", (user_id, msg))
                    if not check.fetchone():
                        execute(conn, """
                            INSERT INTO notifications (user_id, message, priority, category, event_id, action_url)
                            VALUES (%s, %s, %s, %s, %s, %s)
                        """, (user_id, msg, "warning", "overrun", event_id, "/budget"))
                        new_alerts_count += 1
    except Exception as err:
        conn.rollback()
        print(f"Error checking budget overruns: {err}")

    # 2. Vendor Payment Deadlines
    try:
        cur_v = execute(conn, """
            SELECT id, name, contract_value, status
            FROM vendors
            WHERE event_id = %s AND status = 'pending'
        """, (event_id,))
        vendors = cur_v.fetchall()

        for v in vendors:
            msg = f"⏰ VENDOR PAYMENT DUE: Vendor '{v['name']}' payout of ₹{float(v['contract_value'] or 0):,.2f} is pending."
            check = execute(conn, "SELECT id FROM notifications WHERE user_id=%s AND message=%s AND is_read=0", (user_id, msg))
            if not check.fetchone():
                execute(conn, """
                    INSERT INTO notifications (user_id, message, priority, category, event_id, action_url)
                    VALUES (%s, %s, %s, %s, %s, %s)
                """, (user_id, msg, "warning", "deadline", event_id, "/vendors"))
                new_alerts_count += 1
    except Exception as err:
        conn.rollback()
        print(f"Error checking vendor deadlines: {err}")

    # 3. High-Value Unapproved Budget Proposals
    try:
        cur_e = execute(conn, """
            SELECT id, title, total_amount as amount
            FROM budget_proposals
            WHERE event_id = %s AND status = 'submitted' AND total_amount >= 10000
        """, (event_id,))
        high_expenses = cur_e.fetchall()

        for exp in high_expenses:
            msg = f"💸 UNAPPROVED BUDGET: High-value proposal '{exp['title']}' (₹{float(exp['amount']):,.2f}) requires approval."
            check = execute(conn, "SELECT id FROM notifications WHERE user_id=%s AND message=%s AND is_read=0", (user_id, msg))
            if not check.fetchone():
                execute(conn, """
                    INSERT INTO notifications (user_id, message, priority, category, event_id, action_url)
                    VALUES (%s, %s, %s, %s, %s, %s)
                """, (user_id, msg, "info", "expense", event_id, "/budget"))
                new_alerts_count += 1
    except Exception as err:
        conn.rollback()
        print(f"Error checking high-value proposals: {err}")

    return {"ok": True, "new_alerts": new_alerts_count}
