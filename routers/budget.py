from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional
from core.database import get_db, execute
from core.auth import get_current_user
from utils.roles import get_event_role, can_access_department, can_edit_department, can_approve_budget
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


def _get_proposal_or_404(conn, proposal_id):
    cur = execute(conn, "SELECT * FROM budget_proposals WHERE id=%s", (proposal_id,))
    p = cur.fetchone()
    if not p:
        raise HTTPException(status_code=404, detail="Proposal not found")
    return dict(p)


@router.get("/proposals")
def get_proposals(event_id: int, conn=Depends(get_db), user=Depends(get_current_user)):
    role_ctx = get_event_role(conn, user, event_id)
    if role_ctx["level"] is None:
        raise HTTPException(status_code=403, detail="You don't have access to this event")

    if role_ctx["level"] in ("dept_head", "volunteer"):
        cur = execute(conn,
            """SELECT p.*, d.name as dept_name, u.name as submitted_by_name
               FROM budget_proposals p
               LEFT JOIN departments d ON d.id=p.department_id
               LEFT JOIN users u ON u.id=p.submitted_by
               WHERE p.event_id=%s AND p.department_id=%s ORDER BY p.created_at DESC""",
            (event_id, role_ctx["dept_id"])
        )
    else:
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
    role_ctx = get_event_role(conn, user, data.event_id)
    if not can_edit_department(role_ctx, data.department_id):
        raise HTTPException(status_code=403, detail="You can't create a budget proposal for this department")
    cur = execute(conn,
        "INSERT INTO budget_proposals (event_id,department_id,submitted_by,title,notes,status) VALUES (%s,%s,%s,%s,%s,'draft') RETURNING *",
        (data.event_id, data.department_id, user["id"], data.title, data.notes)
    )
    return dict(cur.fetchone())

@router.get("/proposals/{proposal_id}")
def get_proposal(proposal_id: int, conn=Depends(get_db), user=Depends(get_current_user)):
    p = _get_proposal_or_404(conn, proposal_id)
    role_ctx = get_event_role(conn, user, p["event_id"])
    if not can_access_department(role_ctx, p["department_id"]):
        raise HTTPException(status_code=403, detail="You don't have access to this budget proposal")

    cur = execute(conn,
        """SELECT p.*, d.name as dept_name FROM budget_proposals p
           LEFT JOIN departments d ON d.id=p.department_id WHERE p.id=%s""",
        (proposal_id,)
    )
    result = dict(cur.fetchone())
    cur2 = execute(conn, "SELECT * FROM budget_line_items WHERE proposal_id=%s", (proposal_id,))
    result["line_items"] = [dict(r) for r in cur2.fetchall()]
    return result

@router.post("/proposals/{proposal_id}/submit")
def submit_proposal(proposal_id: int, conn=Depends(get_db), user=Depends(get_current_user)):
    p = _get_proposal_or_404(conn, proposal_id)
    role_ctx = get_event_role(conn, user, p["event_id"])
    if not can_edit_department(role_ctx, p["department_id"]):
        raise HTTPException(status_code=403, detail="You can't submit this budget proposal")

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
    p = _get_proposal_or_404(conn, proposal_id)
    role_ctx = get_event_role(conn, user, p["event_id"])
    if not can_approve_budget(role_ctx):
        raise HTTPException(status_code=403, detail="Only an event admin or finance role can approve budgets")

    now = datetime.datetime.utcnow().isoformat()
    execute(conn,
        "UPDATE budget_proposals SET status='approved', approved_by=%s, approved_at=%s WHERE id=%s",
        (user["id"], now, proposal_id)
    )
    return {"ok": True, "message": "Budget approved"}

@router.post("/proposals/{proposal_id}/reject")
def reject_proposal(proposal_id: int, data: RejectRequest, conn=Depends(get_db), user=Depends(get_current_user)):
    p = _get_proposal_or_404(conn, proposal_id)
    role_ctx = get_event_role(conn, user, p["event_id"])
    if not can_approve_budget(role_ctx):
        raise HTTPException(status_code=403, detail="Only an event admin or finance role can reject budgets")

    now = datetime.datetime.utcnow().isoformat()
    execute(conn,
        "UPDATE budget_proposals SET status='rejected', rejected_by=%s, rejected_at=%s, reject_reason=%s WHERE id=%s",
        (user["id"], now, data.reason, proposal_id)
    )
    return {"ok": True, "message": "Budget rejected"}

@router.post("/line-items")
def add_line_item(data: LineItemCreate, conn=Depends(get_db), user=Depends(get_current_user)):
    p = _get_proposal_or_404(conn, data.proposal_id)
    role_ctx = get_event_role(conn, user, p["event_id"])
    if not can_edit_department(role_ctx, p["department_id"]):
        raise HTTPException(status_code=403, detail="You can't edit this budget proposal")

    cur = execute(conn,
        """INSERT INTO budget_line_items (proposal_id,category,item_name,description,quantity,unit,unit_price,total_amount)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
        (data.proposal_id, data.category, data.item_name, data.description,
         data.quantity, data.unit, data.unit_price, data.total_amount)
    )
    return dict(cur.fetchone())

@router.delete("/line-items/{item_id}")
def delete_line_item(item_id: int, conn=Depends(get_db), user=Depends(get_current_user)):
    cur = execute(conn, "SELECT proposal_id FROM budget_line_items WHERE id=%s", (item_id,))
    li = cur.fetchone()
    if not li:
        raise HTTPException(status_code=404, detail="Line item not found")
    p = _get_proposal_or_404(conn, li["proposal_id"])
    role_ctx = get_event_role(conn, user, p["event_id"])
    if not can_edit_department(role_ctx, p["department_id"]):
        raise HTTPException(status_code=403, detail="You can't edit this budget proposal")

    execute(conn, "DELETE FROM budget_line_items WHERE id=%s", (item_id,))
    return {"ok": True}
