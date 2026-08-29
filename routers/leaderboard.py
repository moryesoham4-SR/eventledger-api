from fastapi import APIRouter, Depends, HTTPException
from core.database import get_db, execute
from core.auth import get_current_user
from utils.roles import get_event_role

router = APIRouter(prefix="/api/leaderboard", tags=["leaderboard"])

@router.get("/")
def get_event_leaderboard(event_id: int, conn=Depends(get_db), user=Depends(get_current_user)):
    role_ctx = get_event_role(conn, user, event_id)
    if role_ctx["level"] is None:
        raise HTTPException(status_code=403, detail="You don't have access to this event")

    # Fetch departments
    cur_d = execute(conn, "SELECT * FROM departments WHERE event_id=%s ORDER BY name", (event_id,))
    departments = [dict(r) for r in cur_d.fetchall()]

    # Fetch tasks
    cur_t = execute(conn, "SELECT * FROM work_tasks WHERE event_id=%s", (event_id,))
    tasks = [dict(r) for r in cur_t.fetchall()]

    # Fetch actual expenses
    cur_e = execute(conn, "SELECT department_id, SUM(amount) as total_spent FROM actual_expenses WHERE event_id=%s GROUP BY department_id", (event_id,))
    expenses_by_dept = {r["department_id"]: float(r["total_spent"] or 0) for r in cur_e.fetchall()}

    # Fetch budget items
    cur_b = execute(conn, "SELECT department_id, SUM(amount) as total_budget FROM budget_proposals WHERE event_id=%s GROUP BY department_id", (event_id,))
    budget_by_dept = {r["department_id"]: float(r["total_budget"] or 0) for r in cur_b.fetchall()}

    # Fetch reimbursement claims
    cur_r = execute(conn, "SELECT department_id, COUNT(*) as total_claims, SUM(CASE WHEN dept_head_status='approved' THEN 1 ELSE 0 END) as approved_claims FROM expense_reimbursements WHERE event_id=%s GROUP BY department_id", (event_id,))
    claims_by_dept = {r["department_id"]: dict(r) for r in cur_r.fetchall()}

    dept_leaderboard = []

    for d in departments:
        did = d["id"]
        d_tasks = [t for t in tasks if Number(t.get("department_id") or 0) == did]
        total_tasks = len(d_tasks)
        completed_tasks = sum(1 for t in d_tasks if t.get("status") == "completed")
        task_completion_pct = round((completed_tasks / total_tasks * 100) if total_tasks > 0 else 100.0, 1)

        allocated = budget_by_dept.get(did, 0.0)
        spent = expenses_by_dept.get(did, 0.0)
        if allocated > 0:
            variance_pct = ((allocated - spent) / allocated) * 100
            budget_efficiency = max(0.0, min(100.0, 100.0 + variance_pct))
        else:
            budget_efficiency = 100.0 if spent == 0 else 50.0

        c_info = claims_by_dept.get(did, {"total_claims": 0, "approved_claims": 0})
        t_claims = c_info["total_claims"]
        a_claims = c_info["approved_claims"]
        reimbursement_pct = round((a_claims / t_claims * 100) if t_claims > 0 else 100.0, 1)

        # Efficiency Score formula (0-100 XP)
        xp_score = round((task_completion_pct * 0.5) + (budget_efficiency * 0.3) + (reimbursement_pct * 0.2), 1)

        dept_leaderboard.append({
            "dept_id": did,
            "dept_name": d["name"],
            "head_name": d.get("head_name") or "Unassigned",
            "color": d.get("color") or "#6366f1",
            "xp_score": xp_score,
            "total_tasks": total_tasks,
            "completed_tasks": completed_tasks,
            "task_completion_pct": task_completion_pct,
            "allocated_budget": allocated,
            "actual_spent": spent,
            "budget_efficiency": round(budget_efficiency, 1),
            "reimbursement_compliance": reimbursement_pct,
        })

    def get_xp(item):
        return item["xp_score"]

    dept_leaderboard.sort(key=get_xp, reverse=True)

    # Assign ranks
    for idx, d in enumerate(dept_leaderboard):
        d["rank"] = idx + 1
        d["badge"] = "🥇 Gold" if idx == 0 else "🥈 Silver" if idx == 1 else "🥉 Bronze" if idx == 2 else f"#{idx + 1}"

    # Fetch team volunteer roster with completion stats
    cur_v = execute(conn, """
        SELECT u.id, u.name, u.email, u.avatar_color, r.role, r.dept_id, d.name as dept_name
        FROM user_event_roles r
        JOIN users u ON u.id = r.user_id
        LEFT JOIN departments d ON d.id = r.dept_id
        WHERE r.event_id = %s
    """, (event_id,))
    volunteers = [dict(r) for r in cur_v.fetchall()]

    for v in volunteers:
        uid = v["id"]
        v_tasks = [t for t in tasks if Number(t.get("assigned_to_user_id") or 0) == uid]
        v["tasks_assigned"] = len(v_tasks)
        v["tasks_completed"] = sum(1 for t in v_tasks if t.get("status") == "completed")

    return {
        "event_id": event_id,
        "departments": dept_leaderboard,
        "volunteers": volunteers,
    }

def Number(val):
    try:
        return int(val)
    except:
        return 0
