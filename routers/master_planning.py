from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional, List
from core.database import get_db, execute
from core.auth import get_current_user
from utils.roles import get_event_role, is_event_owner_or_super_admin
from utils.db_safety import run_safely

router = APIRouter(prefix="/api/master-planning", tags=["master-planning"])

class StrategyUpdate(BaseModel):
    event_id: int
    master_vision: str = ""
    target_audience: str = ""
    key_objectives: str = ""
    notes: str = ""

class BudgetPlanItem(BaseModel):
    department_id: int
    plan_a_amount: float = 0.0
    plan_b_amount: float = 0.0
    notes: str = ""

class SaveBudgetPlansRequest(BaseModel):
    event_id: int
    items: List[BudgetPlanItem]

class MilestoneCreate(BaseModel):
    event_id: int
    phase: str = "Planning"  # 'Concept', 'Planning', 'Procurement', 'Marketing', 'Execution'
    title: str
    target_date: str = ""
    assigned_co_head: str = ""
    is_completed: bool = False

class MilestoneUpdate(BaseModel):
    is_completed: Optional[bool] = None
    title: Optional[str] = None
    phase: Optional[str] = None
    target_date: Optional[str] = None

class RiskCreate(BaseModel):
    event_id: int
    risk_title: str
    severity: str = "medium"  # 'low', 'medium', 'high', 'critical'
    mitigation_plan_b: str = ""
    backup_vendor: str = ""
    emergency_contact: str = ""

def ensure_master_planning_schema(conn):
    run_safely(conn, lambda: execute(conn, """
        CREATE TABLE IF NOT EXISTS event_master_plans (
            id SERIAL PRIMARY KEY,
            event_id INT NOT NULL UNIQUE,
            master_vision TEXT DEFAULT '',
            target_audience TEXT DEFAULT '',
            key_objectives TEXT DEFAULT '',
            notes TEXT DEFAULT '',
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """))

    run_safely(conn, lambda: execute(conn, """
        CREATE TABLE IF NOT EXISTS event_budget_plans (
            id SERIAL PRIMARY KEY,
            event_id INT NOT NULL,
            department_id INT NOT NULL,
            plan_a_amount NUMERIC(12,2) DEFAULT 0.00,
            plan_b_amount NUMERIC(12,2) DEFAULT 0.00,
            notes TEXT DEFAULT '',
            CONSTRAINT uq_event_dept_plan UNIQUE (event_id, department_id)
        )
    """))

    run_safely(conn, lambda: execute(conn, """
        CREATE TABLE IF NOT EXISTS event_milestones (
            id SERIAL PRIMARY KEY,
            event_id INT NOT NULL,
            phase VARCHAR(100) DEFAULT 'Planning',
            title VARCHAR(255) NOT NULL,
            target_date VARCHAR(100) DEFAULT '',
            assigned_co_head VARCHAR(255) DEFAULT '',
            is_completed BOOLEAN DEFAULT FALSE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """))

    run_safely(conn, lambda: execute(conn, """
        CREATE TABLE IF NOT EXISTS event_risk_register (
            id SERIAL PRIMARY KEY,
            event_id INT NOT NULL,
            risk_title VARCHAR(255) NOT NULL,
            severity VARCHAR(50) DEFAULT 'medium',
            mitigation_plan_b TEXT DEFAULT '',
            backup_vendor VARCHAR(255) DEFAULT '',
            emergency_contact VARCHAR(255) DEFAULT '',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """))

def _require_co_head_or_super_admin(conn, user, event_id: int):
    if user.get("is_super_admin"):
        return
    role_ctx = get_event_role(conn, user, event_id)
    if not (role_ctx["level"] in ("co_leader", "event_admin") or is_event_owner_or_super_admin(conn, user, event_id)):
        raise HTTPException(status_code=403, detail="Master & Backup Planning mode is restricted to Super Admins, Event Leads, and Co-Heads.")

@router.get("/")
def get_master_plan(event_id: int, conn=Depends(get_db), user=Depends(get_current_user)):
    ensure_master_planning_schema(conn)
    _require_co_head_or_super_admin(conn, user, event_id)

    # Fetch strategy
    cur_s = execute(conn, "SELECT * FROM event_master_plans WHERE event_id=%s", (event_id,))
    strategy = cur_s.fetchone()

    # Fetch budget plans (Plan A & B) per department
    cur_b = execute(conn, """
        SELECT bp.*, d.name as dept_name, d.color as dept_color
        FROM event_budget_plans bp
        JOIN departments d ON d.id = bp.department_id
        WHERE bp.event_id = %s
        ORDER BY d.name
    """, (event_id,))
    budget_plans = [dict(r) for r in cur_b.fetchall()]

    # Fetch milestones
    cur_m = execute(conn, "SELECT * FROM event_milestones WHERE event_id=%s ORDER BY id ASC", (event_id,))
    milestones = [dict(r) for r in cur_m.fetchall()]

    # Fetch risk register
    cur_r = execute(conn, "SELECT * FROM event_risk_register WHERE event_id=%s ORDER BY id DESC", (event_id,))
    risks = [dict(r) for r in cur_r.fetchall()]

    return {
        "strategy": dict(strategy) if strategy else {
            "event_id": event_id, "master_vision": "", "target_audience": "", "key_objectives": "", "notes": ""
        },
        "budget_plans": budget_plans,
        "milestones": milestones,
        "risks": risks,
    }

@router.post("/strategy")
def update_strategy(data: StrategyUpdate, conn=Depends(get_db), user=Depends(get_current_user)):
    ensure_master_planning_schema(conn)
    _require_co_head_or_super_admin(conn, user, data.event_id)

    cur = execute(conn, """
        INSERT INTO event_master_plans (event_id, master_vision, target_audience, key_objectives, notes, updated_at)
        VALUES (%s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
        ON CONFLICT (event_id) DO UPDATE SET
            master_vision = EXCLUDED.master_vision,
            target_audience = EXCLUDED.target_audience,
            key_objectives = EXCLUDED.key_objectives,
            notes = EXCLUDED.notes,
            updated_at = CURRENT_TIMESTAMP
        RETURNING *
    """, (data.event_id, data.master_vision, data.target_audience, data.key_objectives, data.notes))

    return dict(cur.fetchone())

@router.post("/budget-plan")
def save_budget_plans(data: SaveBudgetPlansRequest, conn=Depends(get_db), user=Depends(get_current_user)):
    ensure_master_planning_schema(conn)
    _require_co_head_or_super_admin(conn, user, data.event_id)

    for item in data.items:
        execute(conn, """
            INSERT INTO event_budget_plans (event_id, department_id, plan_a_amount, plan_b_amount, notes)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (event_id, department_id) DO UPDATE SET
                plan_a_amount = EXCLUDED.plan_a_amount,
                plan_b_amount = EXCLUDED.plan_b_amount,
                notes = EXCLUDED.notes
        """, (data.event_id, item.department_id, item.plan_a_amount, item.plan_b_amount, item.notes))

    return {"ok": True, "message": "Master Plan A vs Plan B budgets updated successfully!"}

@router.post("/milestone")
def create_milestone(data: MilestoneCreate, conn=Depends(get_db), user=Depends(get_current_user)):
    ensure_master_planning_schema(conn)
    _require_co_head_or_super_admin(conn, user, data.event_id)

    cur = execute(conn, """
        INSERT INTO event_milestones (event_id, phase, title, target_date, assigned_co_head, is_completed)
        VALUES (%s, %s, %s, %s, %s, %s)
        RETURNING *
    """, (data.event_id, data.phase, data.title, data.target_date, data.assigned_co_head, data.is_completed))

    return dict(cur.fetchone())

@router.put("/milestone/{milestone_id}")
def update_milestone(milestone_id: int, data: MilestoneUpdate, conn=Depends(get_db), user=Depends(get_current_user)):
    ensure_master_planning_schema(conn)
    cur_m = execute(conn, "SELECT event_id FROM event_milestones WHERE id=%s", (milestone_id,))
    m = cur_m.fetchone()
    if not m:
        raise HTTPException(status_code=404, detail="Milestone not found")

    _require_co_head_or_super_admin(conn, user, m["event_id"])

    fields = []
    values = []
    if data.is_completed is not None:
        fields.append("is_completed = %s")
        values.append(data.is_completed)
    if data.title is not None:
        fields.append("title = %s")
        values.append(data.title)
    if data.phase is not None:
        fields.append("phase = %s")
        values.append(data.phase)
    if data.target_date is not None:
        fields.append("target_date = %s")
        values.append(data.target_date)

    if fields:
        values.append(milestone_id)
        execute(conn, f"UPDATE event_milestones SET {','.join(fields)} WHERE id=%s", values)

    return {"ok": True}

@router.delete("/milestone/{milestone_id}")
def delete_milestone(milestone_id: int, conn=Depends(get_db), user=Depends(get_current_user)):
    ensure_master_planning_schema(conn)
    cur_m = execute(conn, "SELECT event_id FROM event_milestones WHERE id=%s", (milestone_id,))
    m = cur_m.fetchone()
    if not m:
        raise HTTPException(status_code=404, detail="Milestone not found")

    _require_co_head_or_super_admin(conn, user, m["event_id"])
    execute(conn, "DELETE FROM event_milestones WHERE id=%s", (milestone_id,))
    return {"ok": True}

@router.post("/risk")
def create_risk(data: RiskCreate, conn=Depends(get_db), user=Depends(get_current_user)):
    ensure_master_planning_schema(conn)
    _require_co_head_or_super_admin(conn, user, data.event_id)

    cur = execute(conn, """
        INSERT INTO event_risk_register (event_id, risk_title, severity, mitigation_plan_b, backup_vendor, emergency_contact)
        VALUES (%s, %s, %s, %s, %s, %s)
        RETURNING *
    """, (data.event_id, data.risk_title, data.severity, data.mitigation_plan_b, data.backup_vendor, data.emergency_contact))

    return dict(cur.fetchone())

@router.delete("/risk/{risk_id}")
def delete_risk(risk_id: int, conn=Depends(get_db), user=Depends(get_current_user)):
    ensure_master_planning_schema(conn)
    cur_r = execute(conn, "SELECT event_id FROM event_risk_register WHERE id=%s", (risk_id,))
    r = cur_r.fetchone()
    if not r:
        raise HTTPException(status_code=404, detail="Risk item not found")

    _require_co_head_or_super_admin(conn, user, r["event_id"])
    execute(conn, "DELETE FROM event_risk_register WHERE id=%s", (risk_id,))
    return {"ok": True}
