from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional, List
from core.database import get_db, execute
from core.auth import get_current_user
from utils.roles import get_event_role
from utils.db_safety import run_safely

router = APIRouter(prefix="/api/sponsorship", tags=["sponsorship"])

class TierCreate(BaseModel):
    tier_name: str
    min_amount: float = 0
    badge_color: str = "indigo"
    description: str = ""

class DeliverableCreate(BaseModel):
    title: str
    notes: str = ""

def _ensure_tables(conn):
    sql_tiers = """
    CREATE TABLE IF NOT EXISTS sponsorship_tiers (
        id SERIAL PRIMARY KEY,
        event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
        tier_name VARCHAR(100) NOT NULL,
        min_amount NUMERIC(12, 2) DEFAULT 0,
        badge_color VARCHAR(50) DEFAULT 'indigo',
        description TEXT DEFAULT '',
        created_at TIMESTAMP DEFAULT NOW()
    );
    """
    sql_deliverables = """
    CREATE TABLE IF NOT EXISTS sponsor_deliverables (
        id SERIAL PRIMARY KEY,
        sponsor_id INTEGER NOT NULL REFERENCES sponsors(id) ON DELETE CASCADE,
        event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
        title VARCHAR(255) NOT NULL,
        status VARCHAR(50) DEFAULT 'pending',
        notes TEXT DEFAULT '',
        created_at TIMESTAMP DEFAULT NOW()
    );
    """
    run_safely(conn, lambda: execute(conn, sql_tiers))
    run_safely(conn, lambda: execute(conn, sql_deliverables))

def _require_event_access(conn, user, event_id: int):
    _ensure_tables(conn)
    role_ctx = get_event_role(conn, user, event_id)
    if role_ctx["level"] is None:
        raise HTTPException(status_code=403, detail="You don't have access to this event")
    return role_ctx

def _require_finance(conn, user, event_id: int):
    role_ctx = _require_event_access(conn, user, event_id)
    if role_ctx["level"] not in ("event_admin", "finance_head"):
        raise HTTPException(status_code=403, detail="Only an event admin or finance head can manage sponsorship packages")
    return role_ctx

def _seed_default_tiers_if_empty(conn, event_id: int):
    cur = execute(conn, "SELECT COUNT(*) as count FROM sponsorship_tiers WHERE event_id=%s", (event_id,))
    count = cur.fetchone()["count"]
    if count == 0:
        defaults = [
            ("Title Sponsor", 100000, "amber", "Main event title sponsor with maximum brand prominence"),
            ("Gold Sponsor", 50000, "yellow", "Gold tier sponsor with stage branding and stall allocation"),
            ("Silver Sponsor", 25000, "slate", "Silver tier sponsor with logo on flex banners and website"),
            ("Bronze Sponsor", 10000, "amber-700", "Bronze tier sponsor with social media promotion"),
        ]
        for name, amt, color, desc in defaults:
            execute(
                conn,
                "INSERT INTO sponsorship_tiers (event_id, tier_name, min_amount, badge_color, description) VALUES (%s, %s, %s, %s, %s)",
                (event_id, name, amt, color, desc)
            )

@router.get("/events/{event_id}/tiers")
def list_sponsorship_tiers(event_id: int, conn=Depends(get_db), user=Depends(get_current_user)):
    _require_event_access(conn, user, event_id)
    _seed_default_tiers_if_empty(conn, event_id)
    cur = execute(conn, "SELECT * FROM sponsorship_tiers WHERE event_id=%s ORDER BY min_amount DESC", (event_id,))
    return [dict(r) for r in cur.fetchall()]

@router.post("/events/{event_id}/tiers")
def create_sponsorship_tier(event_id: int, data: TierCreate, conn=Depends(get_db), user=Depends(get_current_user)):
    _require_finance(conn, user, event_id)
    cur = execute(
        conn,
        """INSERT INTO sponsorship_tiers (event_id, tier_name, min_amount, badge_color, description)
           VALUES (%s, %s, %s, %s, %s) RETURNING *""",
        (event_id, data.tier_name, data.min_amount, data.badge_color, data.description)
    )
    return dict(cur.fetchone())

@router.delete("/tiers/{tier_id}")
def delete_sponsorship_tier(tier_id: int, conn=Depends(get_db), user=Depends(get_current_user)):
    _ensure_tables(conn)
    cur = execute(conn, "SELECT event_id FROM sponsorship_tiers WHERE id=%s", (tier_id,))
    row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Sponsorship tier not found")
    _require_finance(conn, user, row["event_id"])
    execute(conn, "DELETE FROM sponsorship_tiers WHERE id=%s", (tier_id,))
    return {"ok": True}

@router.get("/sponsors/{sponsor_id}/deliverables")
def list_sponsor_deliverables(sponsor_id: int, conn=Depends(get_db), user=Depends(get_current_user)):
    _ensure_tables(conn)
    cur = execute(conn, "SELECT event_id FROM sponsors WHERE id=%s", (sponsor_id,))
    sponsor = cur.fetchone()
    if not sponsor:
        raise HTTPException(status_code=404, detail="Sponsor not found")
    _require_event_access(conn, user, sponsor["event_id"])

    cur = execute(conn, "SELECT * FROM sponsor_deliverables WHERE sponsor_id=%s ORDER BY created_at ASC", (sponsor_id,))
    return [dict(r) for r in cur.fetchall()]

@router.post("/sponsors/{sponsor_id}/deliverables")
def add_sponsor_deliverable(sponsor_id: int, data: DeliverableCreate, conn=Depends(get_db), user=Depends(get_current_user)):
    _ensure_tables(conn)
    cur = execute(conn, "SELECT event_id FROM sponsors WHERE id=%s", (sponsor_id,))
    sponsor = cur.fetchone()
    if not sponsor:
        raise HTTPException(status_code=404, detail="Sponsor not found")
    _require_finance(conn, user, sponsor["event_id"])

    cur = execute(
        conn,
        """INSERT INTO sponsor_deliverables (sponsor_id, event_id, title, notes, status)
           VALUES (%s, %s, %s, %s, 'pending') RETURNING *""",
        (sponsor_id, sponsor["event_id"], data.title, data.notes)
    )
    return dict(cur.fetchone())

@router.post("/deliverables/{deliverable_id}/toggle")
def toggle_deliverable_status(deliverable_id: int, conn=Depends(get_db), user=Depends(get_current_user)):
    _ensure_tables(conn)
    cur = execute(conn, "SELECT * FROM sponsor_deliverables WHERE id=%s", (deliverable_id,))
    item = cur.fetchone()
    if not item:
        raise HTTPException(status_code=404, detail="Deliverable not found")
    item = dict(item)
    _require_finance(conn, user, item["event_id"])

    new_status = "completed" if item["status"] == "pending" else "pending"
    cur = execute(conn, "UPDATE sponsor_deliverables SET status=%s WHERE id=%s RETURNING *", (new_status, deliverable_id))
    return dict(cur.fetchone())

@router.delete("/deliverables/{deliverable_id}")
def delete_deliverable(deliverable_id: int, conn=Depends(get_db), user=Depends(get_current_user)):
    _ensure_tables(conn)
    cur = execute(conn, "SELECT event_id FROM sponsor_deliverables WHERE id=%s", (deliverable_id,))
    item = cur.fetchone()
    if not item:
        raise HTTPException(status_code=404, detail="Deliverable not found")
    _require_finance(conn, user, item["event_id"])

    execute(conn, "DELETE FROM sponsor_deliverables WHERE id=%s", (deliverable_id,))
    return {"ok": True}
