from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from core.database import get_db, execute
from core.auth import hash_password, verify_password, create_access_token

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
        "INSERT INTO users (name, email, password, role, is_super_admin, org_name) VALUES (%s,%s,%s,%s,%s,%s) RETURNING id",
        (data.name, data.email.lower(), hash_password(data.password), "super_admin", 1, data.org_name)
    )
    user_id = cur.fetchone()["id"]
    token = create_access_token({"sub": str(user_id)})
    return {"access_token": token, "token_type": "bearer", "user_id": user_id}

@router.get("/me")
def get_me(conn=Depends(get_db), current_user=Depends(lambda: None)):
    return current_user
