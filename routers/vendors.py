from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from core.database import get_db, execute
from core.auth import get_current_user
from utils.db_safety import run_safely
from utils.roles import get_event_role

router = APIRouter(prefix="/api/vendors", tags=["vendors"])

class VendorCreate(BaseModel):
    event_id: int
    name: str
    category: str = "Other"
    contact_name: str = ""
    contact_email: str = ""
    contract_value: float = 0
    status: str = "active"
    notes: str = ""


def _require_event_access(conn, user, event_id):
    """Vendors aren't department-specific, so viewing just requires being on
    the event at all."""
    role_ctx = get_event_role(conn, user, event_id)
    if role_ctx["level"] is None:
        raise HTTPException(status_code=403, detail="You don't have access to this event")
    return role_ctx


def _require_finance(conn, user, event_id):
    """Signing/removing a vendor contract is a finance-level action."""
    role_ctx = get_event_role(conn, user, event_id)
    if role_ctx["level"] not in ("event_admin", "finance_head"):
        raise HTTPException(status_code=403, detail="Only an event admin or finance head can manage vendors")
    return role_ctx


@router.get("/")
def get_vendors(event_id: int, conn=Depends(get_db), user=Depends(get_current_user)):
    _require_event_access(conn, user, event_id)
    cur = execute(conn, "SELECT * FROM vendors WHERE event_id=%s ORDER BY created_at DESC", (event_id,))
    return [dict(r) for r in cur.fetchall()]

@router.post("/")
def add_vendor(data: VendorCreate, conn=Depends(get_db), user=Depends(get_current_user)):
    _require_finance(conn, user, data.event_id)
    cur = execute(conn,
        """INSERT INTO vendors (event_id,name,category,contact_name,contact_email,contract_value,status,notes)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
        (data.event_id, data.name, data.category, data.contact_name,
         data.contact_email, data.contract_value, data.status, data.notes)
    )
    vendor = dict(cur.fetchone())

    # A vendor contract is money the event will PAY OUT — the mirror image
    # of a sponsor (money coming IN, synced to actual_income). Record it as
    # an actual expense. Wrapped in run_safely so if this fails for any
    # reason, the vendor itself is still created successfully — matched on
    # delete via a "vendor:{id}" marker in `reference` rather than a
    # dedicated FK column, since unlike sponsors/actual_income (which
    # already had a sponsor_id column before this change), we can't confirm
    # actual_expenses has an equivalent vendor_id column.
    if data.contract_value:
        def _sync():
            execute(conn,
                """INSERT INTO actual_expenses (event_id,category,item_name,description,quantity,unit,amount,payment_mode,status,reference,notes)
                   VALUES (%s,'Vendor',%s,%s,1,'unit',%s,'Bank Transfer','paid',%s,%s)""",
                (data.event_id, data.name, f"Vendor contract: {data.name}", data.contract_value,
                 f"vendor:{vendor['id']}", f"Auto-synced from vendor #{vendor['id']}")
            )
        run_safely(conn, _sync)

    return vendor

@router.delete("/{vendor_id}")
def delete_vendor(vendor_id: int, conn=Depends(get_db), user=Depends(get_current_user)):
    cur = execute(conn, "SELECT event_id FROM vendors WHERE id=%s", (vendor_id,))
    vendor = cur.fetchone()
    if not vendor:
        raise HTTPException(status_code=404, detail="Vendor not found")
    _require_finance(conn, user, vendor["event_id"])
    run_safely(conn, lambda: execute(conn, "DELETE FROM actual_expenses WHERE reference=%s", (f"vendor:{vendor_id}",)))
    execute(conn, "DELETE FROM vendors WHERE id=%s", (vendor_id,))
    return {"ok": True}
