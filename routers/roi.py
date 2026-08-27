from fastapi import APIRouter, Depends, HTTPException
from core.database import get_db, execute
from core.auth import get_current_user
from utils.roles import get_event_role

router = APIRouter(prefix="/api/events", tags=["roi"])

@router.get("/{event_id}/roi-forecast")
def get_roi_forecast(event_id: int, ticket_price: float = 500.0, expected_tickets: int = 200, conn=Depends(get_db), user=Depends(get_current_user)):
    role_ctx = get_event_role(conn, user, event_id)
    if role_ctx["level"] is None:
        raise HTTPException(status_code=403, detail="You don't have access to this event")

    cur_e = execute(conn, "SELECT name, currency FROM events WHERE id=%s", (event_id,))
    event = cur_e.fetchone()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")

    currency = event.get("currency") or "INR"

    # 1. Total Expenses (Actual + Allocated Dept Budgets)
    cur_exp = execute(conn, "SELECT COALESCE(SUM(amount), 0) as total FROM actual_expenses WHERE event_id=%s AND status != 'rejected'", (event_id,))
    act_exp = float(cur_exp.fetchone()["total"] or 0)

    cur_dept = execute(conn, "SELECT COALESCE(SUM(allocated_budget), 0) as total FROM departments WHERE event_id=%s", (event_id,))
    allocated_budgets = float(cur_dept.fetchone()["total"] or 0)

    total_expense_load = max(act_exp, allocated_budgets)

    # 2. Total Sponsorship Income
    cur_spons = execute(conn, "SELECT COALESCE(SUM(amount), 0) as total FROM sponsors WHERE event_id=%s AND status = 'confirmed'", (event_id,))
    sponsorship_revenue = float(cur_spons.fetchone()["total"] or 0)

    # 3. Direct Actual Income
    cur_inc = execute(conn, "SELECT COALESCE(SUM(amount), 0) as total FROM actual_income WHERE event_id=%s", (event_id,))
    actual_income = float(cur_inc.fetchone()["total"] or 0)

    # 4. Projected Ticket Revenue
    projected_ticket_revenue = ticket_price * expected_tickets
    total_projected_revenue = actual_income + sponsorship_revenue + projected_ticket_revenue

    # 5. Financial Metrics
    net_profit = total_projected_revenue - total_expense_load
    profit_margin_pct = round((net_profit / total_projected_revenue * 100), 1) if total_projected_revenue > 0 else 0.0

    # Break-even calculation
    uncovered_costs = max(0.0, total_expense_load - sponsorship_revenue - actual_income)
    break_even_attendees = int(uncovered_costs / ticket_price) if ticket_price > 0 else 0

    sponsorship_coverage_pct = round((sponsorship_revenue / total_expense_load * 100), 1) if total_expense_load > 0 else 0.0

    # 6. AI Insights & Risk Evaluation
    ai_risk = "LOW"
    ai_recommendation = ""

    if net_profit < 0:
        ai_risk = "HIGH"
        ai_recommendation = f"🚨 Deficit Warning: Event is currently at a loss of {currency} {abs(net_profit):,.2f}. Increase ticket sales to at least {break_even_attendees} attendees or secure {currency} {uncovered_costs:,.2f} in additional sponsorships."
    elif profit_margin_pct < 15:
        ai_risk = "MODERATE"
        ai_recommendation = f"⚠️ Thin Margin: Profit margin is only {profit_margin_pct}%. Consider increasing ticket prices by 10% to build a financial safety cushion."
    else:
        ai_risk = "OPTIMAL"
        ai_recommendation = f"✅ Healthy Financials: Event exhibits a strong {profit_margin_pct}% profit margin. Sponsorship covers {sponsorship_coverage_pct}% of total costs."

    return {
        "event_id": event_id,
        "currency": currency,
        "total_expense_load": total_expense_load,
        "actual_income": actual_income,
        "sponsorship_revenue": sponsorship_revenue,
        "projected_ticket_revenue": projected_ticket_revenue,
        "total_projected_revenue": total_projected_revenue,
        "net_profit": net_profit,
        "profit_margin_pct": profit_margin_pct,
        "break_even_attendees": break_even_attendees,
        "sponsorship_coverage_pct": sponsorship_coverage_pct,
        "ai_risk": ai_risk,
        "ai_recommendation": ai_recommendation
    }
