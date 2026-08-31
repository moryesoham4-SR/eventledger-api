from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional
from core.database import get_db, execute
from core.auth import get_current_user
from utils.roles import get_event_role, is_event_owner_or_super_admin
from utils.db_safety import run_safely
from utils.google_sheets import GOOGLE_APPS_SCRIPT_TEMPLATE, _dispatch_http_post
import threading

router = APIRouter(prefix="/api/integrations", tags=["integrations"])

class GoogleSheetsConfig(BaseModel):
    event_id: int
    webhook_url: str
    is_auto_sync_enabled: bool = True

class SyncAllRequest(BaseModel):
    event_id: int
    webhook_url: Optional[str] = None

def ensure_integrations_schema(conn):
    run_safely(conn, lambda: execute(conn, """
        CREATE TABLE IF NOT EXISTS event_integrations (
            id SERIAL PRIMARY KEY,
            event_id INT NOT NULL UNIQUE,
            google_sheets_webhook_url TEXT DEFAULT '',
            is_auto_sync_enabled BOOLEAN DEFAULT TRUE,
            last_synced_at TIMESTAMP
        )
    """))

def _require_admin_or_super_admin(conn, user, event_id: int):
    if user.get("is_super_admin"):
        return
    role_ctx = get_event_role(conn, user, event_id)
    if role_ctx["level"] is None:
        raise HTTPException(status_code=403, detail="You don't have access to this event")
    if not (role_ctx["level"] in ("co_leader", "event_admin", "finance_head", "dept_head") or is_event_owner_or_super_admin(conn, user, event_id)):
        raise HTTPException(status_code=403, detail="Only Event leaders and team members can configure integrations.")

@router.get("/google-sheets")
def get_google_sheets_config(event_id: int, conn=Depends(get_db), user=Depends(get_current_user)):
    ensure_integrations_schema(conn)
    _require_admin_or_super_admin(conn, user, event_id)

    cur = execute(conn, "SELECT * FROM event_integrations WHERE event_id=%s", (event_id,))
    row = cur.fetchone()

    return {
        "event_id": event_id,
        "webhook_url": row["google_sheets_webhook_url"] if row else "",
        "is_auto_sync_enabled": bool(row["is_auto_sync_enabled"]) if row else True,
        "last_synced_at": row["last_synced_at"] if row else None,
        "script_template": GOOGLE_APPS_SCRIPT_TEMPLATE,
    }

@router.post("/google-sheets")
def save_google_sheets_config(data: GoogleSheetsConfig, conn=Depends(get_db), user=Depends(get_current_user)):
    ensure_integrations_schema(conn)
    _require_admin_or_super_admin(conn, user, data.event_id)

    clean_url = data.webhook_url.strip()

    execute(conn, """
        INSERT INTO event_integrations (event_id, google_sheets_webhook_url, is_auto_sync_enabled)
        VALUES (%s, %s, %s)
        ON CONFLICT (event_id) DO UPDATE SET
            google_sheets_webhook_url = EXCLUDED.google_sheets_webhook_url,
            is_auto_sync_enabled = EXCLUDED.is_auto_sync_enabled
    """, (data.event_id, clean_url, data.is_auto_sync_enabled))

    return {"ok": True, "message": "Google Sheets live sync settings updated!"}

@router.post("/google-sheets/sync-all")
def trigger_sync_all(data: SyncAllRequest, conn=Depends(get_db), user=Depends(get_current_user)):
    ensure_integrations_schema(conn)
    _require_admin_or_super_admin(conn, user, data.event_id)

    # Auto-save webhook_url if provided in sync request
    if data.webhook_url and data.webhook_url.strip():
        clean_url = data.webhook_url.strip()
        execute(conn, """
            INSERT INTO event_integrations (event_id, google_sheets_webhook_url, is_auto_sync_enabled)
            VALUES (%s, %s, TRUE)
            ON CONFLICT (event_id) DO UPDATE SET
                google_sheets_webhook_url = EXCLUDED.google_sheets_webhook_url
        """, (data.event_id, clean_url))

    cur_cfg = execute(conn, "SELECT google_sheets_webhook_url FROM event_integrations WHERE event_id=%s", (data.event_id,))
    row_cfg = cur_cfg.fetchone()
    if not row_cfg or not row_cfg.get("google_sheets_webhook_url"):
        raise HTTPException(status_code=400, detail="Please configure a Google Sheets Webhook URL first.")

    webhook_url = row_cfg["google_sheets_webhook_url"].strip()

    # 1. Fetch Event Info
    cur_e = execute(conn, "SELECT name FROM events WHERE id=%s", (data.event_id,))
    ev = cur_e.fetchone()
    ev_name = ev["name"] if ev else "Event"

    # 2. Fetch Income Records
    cur_inc = execute(conn, "SELECT * FROM income WHERE event_id=%s ORDER BY date DESC", (data.event_id,))
    income_list = [dict(r) for r in cur_inc.fetchall()]

    # 3. Fetch Expense Records
    cur_exp = execute(conn, """
        SELECT e.*, d.name as dept_name
        FROM actual_expenses e
        LEFT JOIN departments d ON d.id = e.department_id
        WHERE e.event_id = %s ORDER BY e.date DESC
    """, (data.event_id,))
    expense_list = [dict(r) for r in cur_exp.fetchall()]

    # 4. Fetch Budget Proposals
    cur_prop = execute(conn, """
        SELECT p.*, d.name as dept_name
        FROM budget_proposals p
        LEFT JOIN departments d ON d.id = p.department_id
        WHERE p.event_id = %s ORDER BY p.id DESC
    """, (data.event_id,))
    proposals_list = [dict(r) for r in cur_prop.fetchall()]

    # 5. Fetch Sponsors
    cur_sp = execute(conn, "SELECT * FROM sponsors WHERE event_id=%s ORDER BY id DESC", (data.event_id,))
    sponsors_list = [dict(r) for r in cur_sp.fetchall()]

    # 6. Fetch Vendors
    cur_v = execute(conn, "SELECT * FROM vendors WHERE event_id=%s ORDER BY id DESC", (data.event_id,))
    vendors_list = [dict(r) for r in cur_v.fetchall()]

    # Calculate Totals for Summary
    total_est_budget = sum(float(p.get("total_amount") or 0) for p in proposals_list)
    total_act_expenses = sum(float(e.get("amount") or 0) for e in expense_list)
    total_act_income = sum(float(i.get("amount") or 0) for i in income_list)
    total_est_income = sum(float(s.get("committed_amount") or 0) for s in sponsors_list) + total_act_income

    payload = {
        "action": "sync_all",
        "event_id": data.event_id,
        "event_name": ev_name,
        "summary": {
            "total_estimated_budget": total_est_budget,
            "total_actual_expenses": total_act_expenses,
            "total_estimated_income": total_est_income,
            "total_actual_income": total_act_income,
        },
        "income": income_list,
        "expenses": expense_list,
        "proposals": proposals_list,
        "sponsors": sponsors_list,
        "vendors": vendors_list,
    }

    # Dispatch via background thread
    threading.Thread(target=_dispatch_http_post, args=(webhook_url, payload), daemon=True).start()

    execute(conn, "UPDATE event_integrations SET last_synced_at=CURRENT_TIMESTAMP WHERE event_id=%s", (data.event_id,))

    return {"ok": True, "message": "Full EventLedger auto-sync dispatched to Google Sheets! 📊"}
