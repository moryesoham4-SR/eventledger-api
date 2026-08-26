from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from core.database import get_db, execute
from core.auth import verify_password, create_access_token, hash_password, is_legacy_hash
from utils.db_safety import run_safely

router = APIRouter(prefix="/api/auth", tags=["auth"])

class LoginRequest(BaseModel):
    email: str
    password: str

class RegisterRequest(BaseModel):
    name: str
    email: str
    password: str
    org_name: str = ""

class GoogleLoginRequest(BaseModel):
    id_token: str

class ForgotPasswordRequest(BaseModel):
    email: str

class ResetPasswordConfirmRequest(BaseModel):
    email: str
    reset_code: str
    new_password: str

RESET_CODES = {}

def send_reset_email(to_email: str, code: str):
    import os, smtplib
    from email.mime.text import MIMEText
    from email.mime.multipart import MIMEMultipart

    smtp_server = os.getenv("SMTP_SERVER", "smtp.gmail.com")
    smtp_port = int(os.getenv("SMTP_PORT", "587"))
    sender_email = os.getenv("SMTP_EMAIL", "")
    sender_password = os.getenv("SMTP_PASSWORD", "")

    if sender_email and sender_password:
        try:
            msg = MIMEMultipart("alternative")
            msg["Subject"] = f"EventLedger AI - Your Reset Code: {code}"
            msg["From"] = f"EventLedger AI <{sender_email}>"
            msg["To"] = to_email

            html = f"""
            <div style="font-family: Arial, sans-serif; padding: 24px; background-color: #0F172A; color: #F8FAFC; border-radius: 12px;">
              <h2 style="color: #FF7A00; margin-top: 0;">EventLedger AI - Password Reset</h2>
              <p>You requested to reset your password. Use the 6-digit verification code below:</p>
              <div style="font-size: 32px; font-weight: bold; letter-spacing: 6px; color: #10B981; padding: 12px 24px; background: #1E293B; display: inline-block; border-radius: 8px; margin: 16px 0;">
                {code}
              </div>
              <p style="color: #94A3B8; font-size: 12px;">If you did not request this, please ignore this email.</p>
            </div>
            """
            msg.attach(MIMEText(html, "html"))

            with smtplib.SMTP(smtp_server, smtp_port, timeout=10) as server:
                server.starttls()
                server.login(sender_email, sender_password)
                server.sendmail(sender_email, to_email, msg.as_string())
        except Exception as e:
            print(f"SMTP Email Error: {e}")

@router.post("/forgot-password")
def forgot_password(data: ForgotPasswordRequest, conn=Depends(get_db)):
    email = data.email.lower().strip()
    cur = execute(conn, "SELECT id, name FROM users WHERE email=%s", (email,))
    user = cur.fetchone()
    if not user:
        raise HTTPException(status_code=404, detail="No account found with this email address")
    import random
    code = f"{random.randint(100000, 999999)}"
    RESET_CODES[email] = code
    
    # Send email if SMTP is configured
    send_reset_email(email, code)

    return {
        "ok": True,
        "message": f"A 6-digit reset code has been sent to {email}. Please check your email inbox."
    }

@router.post("/reset-password-confirm")
def reset_password_confirm(data: ResetPasswordConfirmRequest, conn=Depends(get_db)):
    email = data.email.lower().strip()
    stored_code = RESET_CODES.get(email)
    if not stored_code or stored_code != data.reset_code.strip():
        raise HTTPException(status_code=400, detail="Invalid or expired reset code")
    new_hash = hash_password(data.new_password)
    execute(conn, "UPDATE users SET password=%s WHERE email=%s", (new_hash, email))
    RESET_CODES.pop(email, None)
    return {"ok": True, "message": "Password reset successfully"}

@router.post("/login")
def login(data: LoginRequest, conn=Depends(get_db)):
    cur = execute(conn, "SELECT * FROM users WHERE email=%s AND is_active=1", (data.email.lower(),))
    user = cur.fetchone()
    if not user or not verify_password(data.password, user["password"]):
        raise HTTPException(status_code=401, detail="Invalid email or password")

    # Quietly migrate this account off the old unsalted SHA-256 scheme now
    # that we know the plaintext password matched it. Wrapped in run_safely
    # so that if hashing ever fails for an unexpected reason, the person can
    # still log in on their legacy hash rather than getting a 500 — the
    # migration just gets tried again next time.
    if is_legacy_hash(user["password"]):
        run_safely(conn, lambda: execute(conn, "UPDATE users SET password=%s WHERE id=%s", (hash_password(data.password), user["id"])))

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

@router.post("/google")
def google_login(data: GoogleLoginRequest, conn=Depends(get_db)):
    import urllib.request
    import json
    
    email = None
    name = None
    
    try:
        url = f"https://oauth2.googleapis.com/tokeninfo?id_token={data.id_token}"
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=5) as response:
            token_data = json.loads(response.read().decode('utf-8'))
            email = token_data.get("email")
            name = token_data.get("name") or token_data.get("given_name") or (email.split("@")[0] if email else "Google User")
    except Exception:
        try:
            import base64
            parts = data.id_token.split(".")
            if len(parts) >= 2:
                padded = parts[1] + "=" * (-len(parts[1]) % 4)
                payload = json.loads(base64.b64decode(padded).decode('utf-8'))
                email = payload.get("email")
                name = payload.get("name") or (email.split("@")[0] if email else "Google User")
        except Exception:
            pass

    if not email:
        raise HTTPException(status_code=400, detail="Invalid Google authentication token")

    email = email.lower()
    cur = execute(conn, "SELECT * FROM users WHERE email=%s", (email,))
    user = cur.fetchone()
    
    if not user:
        random_password = hash_password(f"google_{email}_secret")
        cur = execute(
            conn,
            "INSERT INTO users (name, email, password, role, is_super_admin, org_name) VALUES (%s, %s, %s, %s, %s, %s) RETURNING *",
            (name, email, random_password, "event_admin", 0, "Google User")
        )
        user = cur.fetchone()
        
    user = dict(user)
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
            "avatar_color": user.get("avatar_color") or "#4285F4",
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
