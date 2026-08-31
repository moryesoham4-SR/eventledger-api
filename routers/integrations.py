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
    if not u.startswith("http"):
        if "script.google.com" in u:
            u = "https://" + u.lstrip("/")
        elif u.startswith("AKfy"):
            u = f"https://script.google.com/macros/s/{u}"
        elif u.startswith("dfT-"):
            u = f"https://script.google.com/macros/s/AKfycbyipNRdqLeRN3ttyOK{u}"
        elif "macros/s/" in u:
            u = f"https://script.google.com/{u.lstrip('/')}"
        elif "/exec" in u or len(u) > 30:
            u = f"https://script.google.com/macros/s/{u}"
    
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

    # 2. Fetch Income Records (Estimated & Actual)
    cur_est_inc = execute(conn, "SELECT * FROM estimated_income WHERE event_id=%s ORDER BY id DESC", (data.event_id,))
    est_income_rows = [dict(r) for r in cur_est_inc.fetchall()]

    cur_act_inc = execute(conn, "SELECT * FROM actual_income WHERE event_id=%s ORDER BY id DESC", (data.event_id,))
    act_income_rows = [dict(r) for r in cur_act_inc.fetchall()]

    combined_income = []
    for r in est_income_rows:
        combined_income.append({
            "id": f"EST-{r.get('id')}",
            "type": "Estimated",
            "source": r.get("source", ""),
            "category": r.get("category", "General"),
            "target_amount": float(r.get("amount") or 0),
            "actual_amount": 0,
            "payment_method": "-",
            "status": "Planned",
            "date": "",
            "notes": r.get("notes", "")
        })
    for r in act_income_rows:
        combined_income.append({
            "id": f"ACT-{r.get('id')}",
            "type": "Actual",
            "source": r.get("source", ""),
            "category": r.get("category", "General"),
            "target_amount": 0,
            "actual_amount": float(r.get("amount") or 0),
            "payment_method": r.get("payment_mode", "Cash"),
            "status": "Received",
            "date": str(r.get("received_on") or ""),
            "notes": r.get("notes", "")
        })

    # 3. Fetch Expense Records (Estimated & Actual)
    cur_est_exp = execute(conn, """
        SELECT e.*, d.name as dept_name
        FROM estimated_expenses e
        LEFT JOIN departments d ON d.id = e.department_id
        WHERE e.event_id = %s ORDER BY e.id DESC
    """, (data.event_id,))
    est_expense_rows = [dict(r) for r in cur_est_exp.fetchall()]

    cur_act_exp = execute(conn, """
        SELECT e.*, d.name as dept_name
        FROM actual_expenses e
        LEFT JOIN departments d ON d.id = e.department_id
        WHERE e.event_id = %s ORDER BY e.id DESC
    """, (data.event_id,))
    act_expense_rows = [dict(r) for r in cur_act_exp.fetchall()]

    combined_expenses = []
    for r in est_expense_rows:
        combined_expenses.append({
            "id": f"EST-{r.get('id')}",
            "type": "Estimated",
            "title": r.get("item_name") or r.get("category") or "Item",
            "dept_name": r.get("dept_name") or "General",
            "category": r.get("category", "General"),
            "estimated_cost": float(r.get("amount") or 0),
            "amount": 0,
            "receipt_url": "",
            "payment_method": "-",
            "date": "",
            "notes": r.get("description") or r.get("notes") or ""
        })
    for r in act_expense_rows:
        combined_expenses.append({
            "id": f"ACT-{r.get('id')}",
            "type": "Actual",
            "title": r.get("item_name") or r.get("category") or "Item",
            "dept_name": r.get("dept_name") or "General",
            "category": r.get("category", "General"),
            "estimated_cost": 0,
            "amount": float(r.get("amount") or 0),
            "receipt_url": "",
            "payment_method": r.get("payment_mode", "Cash"),
            "date": str(r.get("paid_on") or ""),
            "notes": r.get("description") or r.get("notes") or ""
        })

    # 4. Fetch Budget Proposals
    cur_prop = execute(conn, """
        SELECT p.*, d.name as dept_name,
               COALESCE((SELECT SUM(COALESCE(li.total_amount, li.unit_price * li.quantity, li.estimated_cost, 0)) 
                         FROM budget_line_items li WHERE li.proposal_id = p.id), 0) as total_amount
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
    total_est_budget = sum(float(r.get("amount") or 0) for r in est_expense_rows) + sum(float(p.get("total_amount") or 0) for p in proposals_list)
    total_act_expenses = sum(float(r.get("amount") or 0) for r in act_expense_rows)
    total_est_income = sum(float(r.get("amount") or 0) for r in est_income_rows) + sum(float(s.get("promised_amount") or 0) for s in sponsors_list)
    total_act_income = sum(float(r.get("amount") or 0) for r in act_income_rows) + sum(float(s.get("amount_received") or 0) for s in sponsors_list)

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
        "income": combined_income,
        "expenses": combined_expenses,
        "proposals": proposals_list,
        "sponsors": sponsors_list,
        "vendors": vendors_list,
    }

    # Dispatch via background thread
    threading.Thread(target=_dispatch_http_post, args=(webhook_url, payload), daemon=True).start()

    run_safely(conn, lambda: execute(conn, "UPDATE event_integrations SET last_synced_at=CURRENT_TIMESTAMP WHERE event_id=%s", (data.event_id,)))

    return {"ok": True, "message": f"Full EventLedger data for {ev_name} synced to Google Sheets! 📊"}
