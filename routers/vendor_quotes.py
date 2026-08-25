from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional
from core.database import get_db, execute
from core.auth import get_current_user
from utils.roles import get_event_role
from utils.db_safety import run_safely

router = APIRouter(prefix="/api/budget/proposals", tags=["vendor-quotes"])

class VendorQuoteCreate(BaseModel):
    vendor_name: str
    contact_info: str = ""
    quote_amount: float
    deliverables: str = ""
    terms: str = ""
    notes: str = ""

def _ensure_table(conn):
    sql = """
    CREATE TABLE IF NOT EXISTS vendor_quotes (
        id SERIAL PRIMARY KEY,
        event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
        proposal_id INTEGER NOT NULL REFERENCES budget_proposals(id) ON DELETE CASCADE,
        vendor_name VARCHAR(255) NOT NULL,
        contact_info VARCHAR(255) DEFAULT '',
        quote_amount NUMERIC(12, 2) NOT NULL,
        deliverables TEXT DEFAULT '',
        terms TEXT DEFAULT '',
        notes TEXT DEFAULT '',
        is_selected BOOLEAN DEFAULT FALSE,
        created_at TIMESTAMP DEFAULT NOW()
    );
    """
    run_safely(conn, lambda: execute(conn, sql))

def _get_proposal_and_check_access(conn, user, proposal_id: int):
    _ensure_table(conn)
    cur = execute(conn, "SELECT * FROM budget_proposals WHERE id=%s", (proposal_id,))
    proposal = cur.fetchone()
    if not proposal:
        raise HTTPException(status_code=404, detail="Proposal not found")
    proposal = dict(proposal)
    role_ctx = get_event_role(conn, user, proposal["event_id"])
    if role_ctx["level"] is None:
        raise HTTPException(status_code=403, detail="You don't have access to this event")
    return proposal, role_ctx

@router.get("/{proposal_id}/quotes")
def list_vendor_quotes(proposal_id: int, conn=Depends(get_db), user=Depends(get_current_user)):
    _get_proposal_and_check_access(conn, user, proposal_id)
    cur = execute(conn, "SELECT * FROM vendor_quotes WHERE proposal_id=%s ORDER BY created_at ASC", (proposal_id,))
    return [dict(r) for r in cur.fetchall()]

@router.post("/{proposal_id}/quotes")
def add_vendor_quote(proposal_id: int, data: VendorQuoteCreate, conn=Depends(get_db), user=Depends(get_current_user)):
    proposal, role = _get_proposal_and_check_access(conn, user, proposal_id)
    
    # Check current count of quotes (max 3 allowed)
    cur = execute(conn, "SELECT COUNT(*) as count FROM vendor_quotes WHERE proposal_id=%s", (proposal_id,))
    count = cur.fetchone()["count"]
    if count >= 5:
        raise HTTPException(status_code=400, detail="Maximum 5 vendor quotes per proposal allowed")

    # If first quote, auto select it as initial default
    is_first = (count == 0)

    cur = execute(conn,
        """INSERT INTO vendor_quotes (event_id, proposal_id, vendor_name, contact_info, quote_amount, deliverables, terms, notes, is_selected)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING *""",
        (proposal["event_id"], proposal_id, data.vendor_name, data.contact_info,
         data.quote_amount, data.deliverables, data.terms, data.notes, is_first)
    )
    quote = dict(cur.fetchone())
    return quote

@router.post("/{proposal_id}/quotes/{quote_id}/select")
def select_vendor_quote(proposal_id: int, quote_id: int, conn=Depends(get_db), user=Depends(get_current_user)):
    proposal, role = _get_proposal_and_check_access(conn, user, proposal_id)
    
    # Unselect all quotes for this proposal first
    execute(conn, "UPDATE vendor_quotes SET is_selected=FALSE WHERE proposal_id=%s", (proposal_id,))
    
    # Select target quote
    cur = execute(conn, "UPDATE vendor_quotes SET is_selected=TRUE WHERE id=%s AND proposal_id=%s RETURNING *", (quote_id, proposal_id))
    selected = cur.fetchone()
    if not selected:
        raise HTTPException(status_code=404, detail="Vendor quote not found")
    
    return dict(selected)

@router.delete("/{proposal_id}/quotes/{quote_id}")
def delete_vendor_quote(proposal_id: int, quote_id: int, conn=Depends(get_db), user=Depends(get_current_user)):
    _get_proposal_and_check_access(conn, user, proposal_id)
    execute(conn, "DELETE FROM vendor_quotes WHERE id=%s AND proposal_id=%s", (quote_id, proposal_id))
    return {"ok": True}
