from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from core.database import get_db, execute
from core.auth import get_current_user
from utils.roles import get_event_role
from utils.db_safety import run_safely
from utils.email import send_certificates_unlocked_email
from routers.reimbursements import ensure_reimbursements_schema

router = APIRouter(prefix="/api/leaderboard", tags=["leaderboard"])

def Number(val):
    try:
        return int(val or 0)
    except:
        return 0

def ensure_certificates_schema(conn):
    run_safely(conn, lambda: execute(conn, "ALTER TABLE events ADD COLUMN certificates_enabled BOOLEAN DEFAULT FALSE"))

class ToggleCertificatesRequest(BaseModel):
    enabled: bool

@router.post("/{event_id}/toggle-certificates")
def toggle_certificate_issuance(event_id: int, data: ToggleCertificatesRequest, conn=Depends(get_db), user=Depends(get_current_user)):
    role_ctx = get_event_role(conn, user, event_id)
    if not (role_ctx["level"] in ("co_leader", "event_admin") or user.get("is_super_admin")):
        raise HTTPException(status_code=403, detail="Only Event Lead and Co-Leader can toggle certificate downloads")

    ensure_certificates_schema(conn)
    execute(conn, "UPDATE events SET certificates_enabled=%s WHERE id=%s", (data.enabled, event_id))

    if data.enabled:
        cur_e = execute(conn, "SELECT name FROM events WHERE id=%s", (event_id,))
        ev_row = cur_e.fetchone()
        ev_name = ev_row["name"] if ev_row else "Event Fest 2026"

        cur_team = execute(conn, """
            SELECT DISTINCT u.email, u.name FROM user_event_roles r
            JOIN users u ON u.id = r.user_id
            WHERE r.event_id = %s
        """, (event_id,))
        for t in cur_team.fetchall():
            if t.get("email"):
                send_certificates_unlocked_email(t["email"], t.get("name") or "Team Member", ev_name)

    return {"ok": True, "certificates_enabled": data.enabled}

@router.get("/")
def get_event_leaderboard(event_id: int, conn=Depends(get_db), user=Depends(get_current_user)):
    role_ctx = get_event_role(conn, user, event_id)
    if role_ctx["level"] is None:
        raise HTTPException(status_code=403, detail="You don't have access to this event")

    ensure_reimbursements_schema(conn)
    ensure_certificates_schema(conn)

    cur_e_info = execute(conn, "SELECT certificates_enabled FROM events WHERE id=%s", (event_id,))
    e_row = cur_e_info.fetchone()
    certificates_enabled = bool(e_row.get("certificates_enabled")) if e_row else False

    # Fetch departments
    try:
        cur_d = execute(conn, "SELECT * FROM departments WHERE event_id=%s ORDER BY name", (event_id,))
        departments = [dict(r) for r in cur_d.fetchall()]
    except Exception:
        departments = []

    # Fetch work tasks safely
    tasks = []
    try:
        cur_t = execute(conn, "SELECT * FROM work_tasks WHERE event_id=%s", (event_id,))
        tasks = [dict(r) for r in cur_t.fetchall()]
    except Exception:
        tasks = []

    # Fetch actual expenses safely
    expenses_by_dept = {}
    try:
        cur_e = execute(conn, "SELECT department_id, SUM(amount) as total_spent FROM actual_expenses WHERE event_id=%s GROUP BY department_id", (event_id,))
        for r in cur_e.fetchall():
            if r["department_id"]:
                expenses_by_dept[r["department_id"]] = float(r["total_spent"] or 0)
    except Exception:
        expenses_by_dept = {}

    # Fetch budget items safely
    budget_by_dept = {}
    try:
        cur_b = execute(conn, "SELECT department_id, SUM(amount) as total_budget FROM budget_proposals WHERE event_id=%s GROUP BY department_id", (event_id,))
        for r in cur_b.fetchall():
            if r["department_id"]:
                budget_by_dept[r["department_id"]] = float(r["total_budget"] or 0)
    except Exception:
        budget_by_dept = {}

    # Fetch reimbursement claims safely
    claims_by_dept = {}
    try:
        cur_c = execute(conn, "SELECT department_id, COUNT(*) as total_claims, SUM(CASE WHEN finance_status='paid_out' THEN 1 ELSE 0 END) as paid_claims FROM expense_reimbursements WHERE event_id=%s GROUP BY department_id", (event_id,))
        for r in cur_c.fetchall():
            if r["department_id"]:
                claims_by_dept[r["department_id"]] = {
                    "total": Number(r["total_claims"]),
                    "paid": Number(r["paid_claims"])
                }
    except Exception:
        claims_by_dept = {}

    # Calculate Efficiency XP Score for each department
    dept_leaderboard = []
    for d in departments:
        dept_id = d["id"]
        d_tasks = [t for t in tasks if Number(t.get("department_id")) == dept_id]
        total_t = len(d_tasks)
        completed_t = sum(1 for t in d_tasks if t.get("status") == "completed")
        task_completion_pct = round((completed_t / total_t * 100) if total_t > 0 else 100)

        allocated_b = budget_by_dept.get(dept_id, 0)
        spent_e = expenses_by_dept.get(dept_id, 0)
        if allocated_b > 0:
            budget_eff_pct = round(max(0, min(100, (1 - (spent_e / allocated_b)) * 100 + 50)))
        else:
            budget_eff_pct = 100

        c_info = claims_by_dept.get(dept_id, {"total": 0, "paid": 0})
        reimbursement_compliance_pct = round((c_info["paid"] / c_info["total"] * 100) if c_info["total"] > 0 else 100)

        xp_score = round((task_completion_pct * 0.5) + (budget_eff_pct * 0.3) + (reimbursement_compliance_pct * 0.2))

        dept_leaderboard.append({
            "dept_id": dept_id,
            "dept_name": d["name"],
            "head_name": d["head_name"] or "Unassigned",
            "color": d["color"] or "#6366f1",
            "completed_tasks": completed_t,
            "total_tasks": total_t,
            "task_completion_pct": task_completion_pct,
            "budget_allocated": allocated_b,
            "actual_spent": spent_e,
            "budget_efficiency": budget_eff_pct,
            "xp_score": xp_score,
        })

    dept_leaderboard.sort(key=lambda x: x["xp_score"], reverse=True)

    for i, d in enumerate(dept_leaderboard):
        d["rank"] = i + 1
        d["badge"] = "🥇 1st" if i == 0 else "🥈 2nd" if i == 1 else "🥉 3rd" if i == 2 else f"#{i+1}"

    # Fetch Active Volunteers / Team Members
    volunteers = []
    seen_names = set()
    try:
        cur_v = execute(conn, """
            SELECT u.id, u.name, u.email, u.avatar_color, r.role, r.dept_id, d.name as dept_name
            FROM user_event_roles r
            JOIN users u ON u.id = r.user_id
            LEFT JOIN departments d ON d.id = r.dept_id
            WHERE r.event_id = %s
            ORDER BY u.name
        """, (event_id,))
        for r in cur_v.fetchall():
            row = dict(r)
            v_name = (row.get("name") or row.get("email") or "").strip()
            seen_names.add(v_name.lower())
            volunteers.append(row)
    except Exception:
        volunteers = []

    # Synthesize Department Heads defined in departments table if not in user_event_roles yet
    synth_id = 99000
    for d in departments:
        h_name = (d.get("head_name") or "").strip()
        if h_name and h_name.lower() not in seen_names and h_name.lower() != "unassigned":
            seen_names.add(h_name.lower())
            synth_id += 1
            volunteers.append({
                "id": synth_id,
                "name": h_name,
                "email": f"{h_name.lower().replace(' ', '.')}@eventledger.internal",
                "avatar_color": d.get("color") or "#6366f1",
                "role": "dept_head",
                "dept_id": d["id"],
                "dept_name": d["name"],
            })

    # Include current requesting user if not in roster
    user_name = (user.get("name") or user.get("email") or "").strip()
    if user_name and user_name.lower() not in seen_names:
        volunteers.append({
            "id": user["id"],
            "name": user_name,
            "email": user.get("email") or "",
            "avatar_color": user.get("avatar_color") or "#6366f1",
            "role": "event_admin",
            "dept_id": None,
            "dept_name": "Event Management",
        })

    for v in volunteers:
        uid = v["id"]
        v_tasks = [t for t in tasks if Number(t.get("assigned_to_user_id")) == uid]
        v["tasks_assigned"] = len(v_tasks)
        v["tasks_completed"] = sum(1 for t in v_tasks if t.get("status") == "completed")

    return {
        "event_id": event_id,
        "certificates_enabled": certificates_enabled,
        "departments": dept_leaderboard,
        "volunteers": volunteers,
    }
