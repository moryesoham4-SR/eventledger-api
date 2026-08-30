from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional, List
from core.database import get_db, execute
from core.auth import get_current_user
from utils.roles import get_event_role, is_event_owner_or_super_admin
from utils.db_safety import run_safely

router = APIRouter(prefix="/api/sandbox-planning", tags=["sandbox-planning"])

class ScenarioCreate(BaseModel):
    event_id: int
    title: str
    description: str = ""
    projected_income: float = 0.0
    notes: str = ""

class SandboxDeptCreate(BaseModel):
    scenario_id: int
    name: str
    color: str = "#6366f1"

class SandboxItemCreate(BaseModel):
    sandbox_dept_id: int
    item_name: str
    amount: float = 0.0
    notes: str = ""

class MergeToMainRequest(BaseModel):
    event_id: int
    scenario_id: int

def ensure_sandbox_schema(conn):
    run_safely(conn, lambda: execute(conn, """
        CREATE TABLE IF NOT EXISTS sandbox_scenarios (
            id SERIAL PRIMARY KEY,
            event_id INT NOT NULL,
            title VARCHAR(255) NOT NULL,
            description TEXT DEFAULT '',
            projected_income NUMERIC(12,2) DEFAULT 0.00,
            notes TEXT DEFAULT '',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """))

    run_safely(conn, lambda: execute(conn, """
        CREATE TABLE IF NOT EXISTS sandbox_departments (
            id SERIAL PRIMARY KEY,
            scenario_id INT NOT NULL,
            name VARCHAR(255) NOT NULL,
            color VARCHAR(50) DEFAULT '#6366f1',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """))

    run_safely(conn, lambda: execute(conn, """
        CREATE TABLE IF NOT EXISTS sandbox_items (
            id SERIAL PRIMARY KEY,
            sandbox_dept_id INT NOT NULL,
            item_name VARCHAR(255) NOT NULL,
            amount NUMERIC(12,2) DEFAULT 0.00,
            notes TEXT DEFAULT '',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """))

def _require_co_head_or_super_admin(conn, user, event_id: int):
    if user.get("is_super_admin"):
        return
    role_ctx = get_event_role(conn, user, event_id)
    if not (role_ctx["level"] in ("co_leader", "event_admin") or is_event_owner_or_super_admin(conn, user, event_id)):
        raise HTTPException(status_code=403, detail="Mini EventLedger Sandbox is restricted to Super Admins, Event Leads, and Co-Heads.")

@router.get("/")
def get_sandbox_data(event_id: int, conn=Depends(get_db), user=Depends(get_current_user)):
    ensure_sandbox_schema(conn)
    _require_co_head_or_super_admin(conn, user, event_id)

    cur_s = execute(conn, "SELECT * FROM sandbox_scenarios WHERE event_id=%s ORDER BY id ASC", (event_id,))
    scenarios = [dict(r) for r in cur_s.fetchall()]

    scenario_list = []
    for sc in scenarios:
        sc_id = sc["id"]
        cur_d = execute(conn, "SELECT * FROM sandbox_departments WHERE scenario_id=%s ORDER BY id ASC", (sc_id,))
        departments = [dict(r) for r in cur_d.fetchall()]

        dept_list = []
        for d in departments:
            d_id = d["id"]
            cur_i = execute(conn, "SELECT * FROM sandbox_items WHERE sandbox_dept_id=%s ORDER BY id ASC", (d_id,))
            items = [dict(r) for r in cur_i.fetchall()]
            dept_list.append({**d, "items": items})

        scenario_list.append({**sc, "departments": dept_list})

    return {"scenarios": scenario_list}

@router.post("/scenario")
def create_scenario(data: ScenarioCreate, conn=Depends(get_db), user=Depends(get_current_user)):
    ensure_sandbox_schema(conn)
    _require_co_head_or_super_admin(conn, user, data.event_id)

    cur = execute(conn, """
        INSERT INTO sandbox_scenarios (event_id, title, description, projected_income, notes)
        VALUES (%s, %s, %s, %s, %s)
        RETURNING *
    """, (data.event_id, data.title, data.description, data.projected_income, data.notes))

    scenario = dict(cur.fetchone())

    # Auto-create default sandbox departments for quick planning
    default_depts = [
        ("Logistics & Stage", "#6366f1"),
        ("Art & Decor", "#ec4899"),
        ("Technical & Sound", "#8b5cf6"),
        ("Food & Catering", "#f59e0b"),
    ]
    for d_name, d_color in default_depts:
        execute(conn, """
            INSERT INTO sandbox_departments (scenario_id, name, color)
            VALUES (%s, %s, %s)
        """, (scenario["id"], d_name, d_color))

    return scenario

@router.delete("/scenario/{scenario_id}")
def delete_scenario(scenario_id: int, conn=Depends(get_db), user=Depends(get_current_user)):
    ensure_sandbox_schema(conn)
    cur_s = execute(conn, "SELECT event_id FROM sandbox_scenarios WHERE id=%s", (scenario_id,))
    sc = cur_s.fetchone()
    if not sc:
        raise HTTPException(status_code=404, detail="Scenario not found")

    _require_co_head_or_super_admin(conn, user, sc["event_id"])

    # Cascade delete items, depts, scenario
    cur_d = execute(conn, "SELECT id FROM sandbox_departments WHERE scenario_id=%s", (scenario_id,))
    depts = cur_d.fetchall()
    for d in depts:
        execute(conn, "DELETE FROM sandbox_items WHERE sandbox_dept_id=%s", (d["id"],))

    execute(conn, "DELETE FROM sandbox_departments WHERE scenario_id=%s", (scenario_id,))
    execute(conn, "DELETE FROM sandbox_scenarios WHERE id=%s", (scenario_id,))

    return {"ok": True}

@router.post("/department")
def create_sandbox_department(data: SandboxDeptCreate, conn=Depends(get_db), user=Depends(get_current_user)):
    ensure_sandbox_schema(conn)
    cur_s = execute(conn, "SELECT event_id FROM sandbox_scenarios WHERE id=%s", (data.scenario_id,))
    sc = cur_s.fetchone()
    if not sc:
        raise HTTPException(status_code=404, detail="Scenario not found")

    _require_co_head_or_super_admin(conn, user, sc["event_id"])

    cur = execute(conn, """
        INSERT INTO sandbox_departments (scenario_id, name, color)
        VALUES (%s, %s, %s)
        RETURNING *
    """, (data.scenario_id, data.name, data.color))

    return dict(cur.fetchone())

@router.delete("/department/{dept_id}")
def delete_sandbox_department(dept_id: int, conn=Depends(get_db), user=Depends(get_current_user)):
    ensure_sandbox_schema(conn)
    cur_d = execute(conn, """
        SELECT d.id, s.event_id FROM sandbox_departments d
        JOIN sandbox_scenarios s ON s.id = d.scenario_id
        WHERE d.id = %s
    """, (dept_id,))
    d = cur_d.fetchone()
    if not d:
        raise HTTPException(status_code=404, detail="Sandbox department not found")

    _require_co_head_or_super_admin(conn, user, d["event_id"])

    execute(conn, "DELETE FROM sandbox_items WHERE sandbox_dept_id=%s", (dept_id,))
    execute(conn, "DELETE FROM sandbox_departments WHERE id=%s", (dept_id,))
    return {"ok": True}

@router.post("/item")
def create_sandbox_item(data: SandboxItemCreate, conn=Depends(get_db), user=Depends(get_current_user)):
    ensure_sandbox_schema(conn)
    cur_d = execute(conn, """
        SELECT d.id, s.event_id FROM sandbox_departments d
        JOIN sandbox_scenarios s ON s.id = d.scenario_id
        WHERE d.id = %s
    """, (data.sandbox_dept_id,))
    d = cur_d.fetchone()
    if not d:
        raise HTTPException(status_code=404, detail="Sandbox department not found")

    _require_co_head_or_super_admin(conn, user, d["event_id"])

    cur = execute(conn, """
        INSERT INTO sandbox_items (sandbox_dept_id, item_name, amount, notes)
        VALUES (%s, %s, %s, %s)
        RETURNING *
    """, (data.sandbox_dept_id, data.item_name, data.amount, data.notes))

    return dict(cur.fetchone())

@router.delete("/item/{item_id}")
def delete_sandbox_item(item_id: int, conn=Depends(get_db), user=Depends(get_current_user)):
    ensure_sandbox_schema(conn)
    cur_i = execute(conn, """
        SELECT i.id, s.event_id FROM sandbox_items i
        JOIN sandbox_departments d ON d.id = i.sandbox_dept_id
        JOIN sandbox_scenarios s ON s.id = d.scenario_id
        WHERE i.id = %s
    """, (item_id,))
    i = cur_i.fetchone()
    if not i:
        raise HTTPException(status_code=404, detail="Sandbox item not found")

    _require_co_head_or_super_admin(conn, user, i["event_id"])
    execute(conn, "DELETE FROM sandbox_items WHERE id=%s", (item_id,))
    return {"ok": True}

@router.post("/merge-to-main")
def merge_to_main_event(data: MergeToMainRequest, conn=Depends(get_db), user=Depends(get_current_user)):
    ensure_sandbox_schema(conn)
    _require_co_head_or_super_admin(conn, user, data.event_id)

    cur_s = execute(conn, "SELECT * FROM sandbox_scenarios WHERE id=%s AND event_id=%s", (data.scenario_id, data.event_id))
    sc = cur_s.fetchone()
    if not sc:
        raise HTTPException(status_code=404, detail="Scenario not found")

    cur_d = execute(conn, "SELECT * FROM sandbox_departments WHERE scenario_id=%s", (data.scenario_id,))
    sandbox_depts = [dict(r) for r in cur_d.fetchall()]

    merged_count = 0
    for sd in sandbox_depts:
        # Check if department already exists in live main event
        cur_exist = execute(conn, "SELECT id FROM departments WHERE event_id=%s AND LOWER(name)=%s", (data.event_id, sd["name"].strip().lower()))
        real_d = cur_exist.fetchone()

        if not real_d:
            cur_new = execute(conn, """
                INSERT INTO departments (event_id, name, budget, color)
                VALUES (%s, %s, %s, %s)
                RETURNING id
            """, (data.event_id, sd["name"], 0, sd["color"]))
            real_dept_id = cur_new.fetchone()["id"]
        else:
            real_dept_id = real_d["id"]

        # Fetch sandbox items for this department and create corresponding budget proposals in main event
        cur_items = execute(conn, "SELECT * FROM sandbox_items WHERE sandbox_dept_id=%s", (sd["id"],))
        items = [dict(r) for r in cur_items.fetchall()]

        for it in items:
            execute(conn, """
                INSERT INTO budget_proposals (event_id, department_id, title, description, quantity, unit_cost, total_amount, status, created_by)
                VALUES (%s, %s, %s, %s, 1, %s, %s, 'approved', %s)
            """, (data.event_id, real_dept_id, it["item_name"], it["notes"] or f"Merged from Sandbox: {sc['title']}", it["amount"], it["amount"], user["id"]))
            merged_count += 1

    return {"ok": True, "message": f"Successfully promoted {len(sandbox_depts)} departments and {merged_count} items to main event!"}
