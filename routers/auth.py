from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from core.database import get_db, execute
from core.auth import verify_password, create_access_token, hash_password, is_legacy_hash

router = APIRouter(prefix="/api/auth", tags=["auth"])

class LoginRequest(BaseModel):
    email: str
    password: str

class RegisterRequest(BaseModel):
    name: str
    email: str
    password: str
    org_name: str = ""

@router.post("/login")
def login(data: LoginRequest, conn=Depends(get_db)):
    cur = execute(conn, "SELECT * FROM users WHERE email=%s AND is_active=1", (data.email.lower(),))
    user = cur.fetchone()
    if not user or not verify_password(data.password, user["password"]):
        raise HTTPException(status_code=401, detail="Invalid email or password")

    # Quietly migrate this account off the old unsalted SHA-256 scheme now
    # that we know the plaintext password matched it.
    if is_legacy_hash(user["password"]):
        execute(conn, "UPDATE users SET password=%s WHERE id=%s", (hash_password(data.password), user["id"]))

    token = create_access_token({"sub": str(user["id"])})
    return {
        "access_token": token,
        "token_type": "bearer",
        "user": {
            "id": user["id"],
            "name": user["name"],
            "email": user["email"],
            "role": user["role"],
            "is_super_admin": user["is_super_admin"],
            "org_name": user["org_name"],
            "avatar_color": user["avatar_color"],
        }
    }

@router.post("/register")
def register(data: RegisterRequest, conn=Depends(get_db)):
    cur = execute(conn, "SELECT id FROM users WHERE email=%s", (data.email.lower(),))
    if cur.fetchone():
        raise HTTPException(status_code=400, detail="Email already registered")
    cur = execute(conn,
        "INSERT INTO users (name,email,password,role,is_super_admin,org_name) VALUES (%s,%s,%s,%s,%s,%s) RETURNING id",
        (data.name, data.email.lower(), hash_password(data.password), "event_admin", 0, data.org_name)
    )
    user_id = cur.fetchone()["id"]
    token = create_access_token({"sub": str(user_id)})
    return {"access_token": token, "token_type": "bearer", "user_id": user_id}
