from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional
from core.database import get_db, execute
from core.auth import get_current_user
from utils.roles import get_event_role

router = APIRouter(prefix="/api/certificates", tags=["certificates"])

class CertificateRequest(BaseModel):
    event_id: int
    user_name: str
    user_role: str = "Co-Worker / Volunteer"
    department_name: str = "Event Operations"
    award_title: str = "Certificate of Appreciation"
    citation: str = "In recognition of outstanding dedication, leadership, and exemplary service."
    signatory_title_1: str = "Event Admin / Lead"
    signatory_name_1: str = "Event Director"
    signatory_title_2: str = "Faculty Coordinator"
    signatory_name_2: str = "Dean of Student Affairs"
    issue_date: Optional[str] = ""

@router.post("/generate")
def generate_certificate_payload(data: CertificateRequest, conn=Depends(get_db), user=Depends(get_current_user)):
    role_ctx = get_event_role(conn, user, data.event_id)
    if role_ctx["level"] is None:
        raise HTTPException(status_code=403, detail="You don't have access to this event")

    # Fetch event info safely using correct DB column 'name'
    cur_e = execute(conn, "SELECT name, start_date, certificates_enabled FROM events WHERE id=%s", (data.event_id,))
    event_info = cur_e.fetchone() or {}

    is_admin = role_ctx["level"] in ("co_leader", "event_admin") or bool(user.get("is_super_admin"))
    certificates_enabled = bool(event_info.get("certificates_enabled"))

    if not certificates_enabled and not is_admin:
        raise HTTPException(status_code=403, detail="Certificate downloads have not been unlocked by the Event Director yet.")

    event_name = event_info.get("name") or "Event Fest 2026"
    start_date = event_info.get("start_date") or "2026"
    issue_date = data.issue_date or start_date

    return {
        "event_title": event_name,
        "organization_name": user.get("org_name") or "Event Management Board",
        "recipient_name": data.user_name,
        "recipient_role": data.user_role,
        "department_name": data.department_name,
        "award_title": data.award_title,
        "citation": data.citation,
        "signatory_1": {"title": data.signatory_title_1, "name": data.signatory_name_1},
        "signatory_2": {"title": data.signatory_title_2, "name": data.signatory_name_2},
        "issue_date": issue_date,
        "certificate_id": f"CERT-{data.event_id}-{user['id']}-882",
    }
