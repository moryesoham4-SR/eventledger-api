from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional
from core.database import get_db, execute
from core.auth import get_current_user
import datetime

router = APIRouter(prefix="/api/budget", tags=["budget"])

class ProposalCreate(BaseModel):
    event_id: int
    department_id: int
    title: str
    notes: str = ""

class LineItemCreate(BaseModel):
    proposal_id: int
    category: str
    item_name: str
    description: str = ""
    quantity: float = 1
    unit: str = "unit"
    unit_price: float
    total_amount: float

class RejectRequest(BaseModel):
    reason: str

@router.get("/proposals")
def get_proposals(event_id: int, conn=Depends(get_db), user=Depends(get_current_user)):
    cur = execute(conn,
        """SELECT p.*, d.name as dept_name, u.name as submitted_by_name
           FROM budget_proposals p
           LEFT JOIN departments d ON d.id=p.department_id
           LEFT JOIN users u ON u.id=p.submitted_by
           WHERE p.event_id=%s ORDER BY p.created_at DESC""",
        (event_id,)
    )
    return [dict(r) for r in cur.fetchall()]

@router.post("/proposals")
def create_proposal(data: ProposalCreate, conn=Depends(get_db), user=Depends(get_current_user)):
    cur = execute(conn,
        "INSERT INTO budget_proposals (event_id,department_id,submitted_by,title,notes,status) VALUES (%s,%s,%s,%s,%s,'draft') RETURNING *",
        (data.event_id, data.department_id, user["id"], data.title, data.notes)
    )
    return dict(cur.fetchone())

@router.get("/proposals/{proposal_id}")
def get_proposal(proposal_id: int, conn=Depends(get_db), user=Depends(get_current_user)):
    cur = execute(conn,
        """SELECT p.*, d.name as dept_name FROM budget_proposals p
           LEFT JOIN departments d ON d.id=p.department_id WHERE p.id=%s""",
        (proposal_id,)
    )
    p = cur.fetchone()
    if not p:
        raise HTTPException(status_code=404, detail="Proposal not found")
    result = dict(p)
    cur2 = execute(conn, "SELECT * FROM budget_line_items WHERE proposal_id=%s", (proposal_id,))
    result["line_items"] = [dict(r) for r in cur2.fetchall()]
    return result

@router.post("/proposals/{proposal_id}/submit")
def submit_proposal(proposal_id: int, conn=Depends(get_db), user=Depends(get_current_user)):
    now = datetime.datetime.utcnow().isoformat()
    cur = execute(conn, "SELECT COALESCE(SUM(total_amount),0) as t FROM budget_line_items WHERE proposal_id=%s", (proposal_id,))
    total = float(list(cur.fetchone().values())[0])
    execute(conn,
        "UPDATE budget_proposals SET status='submitted', submitted_at=%s, total_amount=%s WHERE id=%s",
        (now, total, proposal_id)
    )
    return {"ok": True, "message": "Budget submitted for approval"}

@router.post("/proposals/{proposal_id}/approve")
def approve_proposal(proposal_id: int, conn=Depends(get_db), user=Depends(get_current_user)):
    now = datetime.datetime.utcnow().isoformat()
    execute(conn,
        "UPDATE budget_proposals SET status='approved', approved_by=%s, approved_at=%s WHERE id=%s",
        (user["id"], now, proposal_id)
    )
    return {"ok": True, "message": "Budget approved"}

@router.post("/proposals/{proposal_id}/reject")
def reject_proposal(proposal_id: int, data: RejectRequest, conn=Depends(get_db), user=Depends(get_current_user)):
    now = datetime.datetime.utcnow().isoformat()
    execute(conn,
        "UPDATE budget_proposals SET status='rejected', rejected_by=%s, rejected_at=%s, reject_reason=%s WHERE id=%s",
        (user["id"], now, data.reason, proposal_id)
    )
    return {"ok": True, "message": "Budget rejected"}

@router.post("/line-items")
def add_line_item(data: LineItemCreate, conn=Depends(get_db), user=Depends(get_current_user)):
    cur = execute(conn,
        """INSERT INTO budget_line_items (proposal_id,category,item_name,description,quantity,unit,unit_price,total_amount)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
        (data.proposal_id, data.category, data.item_name, data.description,
         data.quantity, data.unit, data.unit_price, data.total_amount)
    )
    return dict(cur.fetchone())

@router.delete("/line-items/{item_id}")
def delete_line_item(item_id: int, conn=Depends(get_db), user=Depends(get_current_user)):
    execute(conn, "DELETE FROM budget_line_items WHERE id=%s", (item_id,))
    return {"ok": True}
