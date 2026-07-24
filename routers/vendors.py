from fastapi import APIRouter, Depends
from pydantic import BaseModel
from core.database import get_db, execute
from core.auth import get_current_user

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

@router.get("/")
def get_vendors(event_id: int, conn=Depends(get_db), user=Depends(get_current_user)):
    cur = execute(conn, "SELECT * FROM vendors WHERE event_id=%s ORDER BY created_at DESC", (event_id,))
    return [dict(r) for r in cur.fetchall()]

@router.post("/")
def add_vendor(data: VendorCreate, conn=Depends(get_db), user=Depends(get_current_user)):
    cur = execute(conn,
        """INSERT INTO vendors (event_id,name,category,contact_name,contact_email,contract_value,status,notes)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
        (data.event_id, data.name, data.category, data.contact_name,
         data.contact_email, data.contract_value, data.status, data.notes)
    )
    return dict(cur.fetchone())

@router.delete("/{vendor_id}")
def delete_vendor(vendor_id: int, conn=Depends(get_db), user=Depends(get_current_user)):
    execute(conn, "DELETE FROM vendors WHERE id=%s", (vendor_id,))
    return {"ok": True}
