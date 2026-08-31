from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional
from core.database import get_db, execute
from core.auth import get_current_user
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
            event_id INT NOT NULL,
            google_sheets_webhook_url TEXT DEFAULT '',
            is_auto_sync_enabled BOOLEAN DEFAULT TRUE,
            last_synced_at TIMESTAMP
        )
    """))
    run_safely(conn, lambda: execute(conn, """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_event_integrations_event_id ON event_integrations (event_id)
    """))

def _clean_webhook_url(url: str) -> str:
    if not url:
        return ""
    u = url.strip()
    # If it is a script.google.com Web App URL and missing /exec, auto-append /exec
    if "script.google.com/macros/s/" in u and not u.endswith("/exec"):
        u = u.rstrip("/") + "/exec"
    return u

def _upsert_webhook_url(conn, event_id: int, webhook_url: str, is_auto_sync: bool = True):
    cur = execute(conn, "SELECT id FROM event_integrations WHERE event_id=%s", (event_id,))
    row = cur.fetchone()
    if row:
        execute(conn, """
            UPDATE event_integrations 
            SET google_sheets_webhook_url=%s, is_auto_sync_enabled=%s 
            WHERE event_id=%s
        """, (webhook_url, is_auto_sync, event_id))
    else:
        execute(conn, """
            INSERT INTO event_integrations (event_id, google_sheets_webhook_url, is_auto_sync_enabled)
            VALUES (%s, %s, %s)
        """, (event_id, webhook_url, is_auto_sync))

@router.get("/google-sheets")
def get_google_sheets_config(event_id: int, conn=Depends(get_db), user=Depends(get_current_user)):
    ensure_integrations_schema(conn)

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

    clean_url = _clean_webhook_url(data.webhook_url)
    _upsert_webhook_url(conn, data.event_id, clean_url, data.is_auto_sync_enabled)

    return {"ok": True, "message": "Google Sheets live sync settings updated!"}

@router.post("/google-sheets/sync-all")
def trigger_sync_all(data: SyncAllRequest, conn=Depends(get_db), user=Depends(get_current_user)):
    ensure_integrations_schema(conn)

    webhook_url = _clean_webhook_url(data.webhook_url or "")

    if webhook_url:
        _upsert_webhook_url(conn, data.event_id, webhook_url, True)
    else:
        cur_cfg = execute(conn, "SELECT google_sheets_webhook_url FROM event_integrations WHERE event_id=%s", (data.event_id,))
        row_cfg = cur_cfg.fetchone()
        if row_cfg and row_cfg.get("google_sheets_webhook_url"):
            webhook_url = _clean_webhook_url(row_cfg["google_sheets_webhook_url"])

    if not webhook_url or not webhook_url.startswith("http"):
        raise HTTPException(status_code=400, detail="Please configure a Google Sheets Webhook URL first.")

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

    run_safely(conn, lambda: execute(conn, "UPDATE event_integrations SET last_synced_at=CURRENT_TIMESTAMP WHERE event_id=%s", (data.event_id,)))

    return {"ok": True, "message": "Full EventLedger auto-sync dispatched to Google Sheets! 📊"}
