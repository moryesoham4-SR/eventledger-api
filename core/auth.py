from datetime import datetime, timedelta
from typing import Optional
from jose import JWTError, jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from core.config import settings
from core.database import get_db, execute
from passlib.context import CryptContext
import hashlib

# Passwords are hashed with bcrypt (salted, deliberately slow — the right
# choice for password storage). Older accounts created before this change
# have an unsalted SHA-256 hash instead; verify_password() still accepts
# those so nobody gets locked out, and routers/auth.py's login endpoint
# transparently re-hashes them to bcrypt on next successful login, so every
# account migrates to the stronger scheme as people log in rather than
# needing a one-time mass password reset.
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
bearer_scheme = HTTPBearer()

def hash_password(password: str) -> str:
    return pwd_context.hash(password)

def is_legacy_hash(hashed: str) -> bool:
    """True for the old unsalted-SHA-256 scheme, False for bcrypt."""
    return not hashed.startswith(("$2a$", "$2b$", "$2y$"))

def verify_password(plain: str, hashed: str) -> bool:
    if is_legacy_hash(hashed):
        return hashlib.sha256(plain.encode()).hexdigest() == hashed
    return pwd_context.verify(plain, hashed)

def create_access_token(data: dict, expires_delta: Optional[timedelta] = None):
    to_encode = data.copy()
    expire = datetime.utcnow() + (expires_delta or timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES))
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, settings.SECRET_KEY, algorithm=settings.ALGORITHM)

def decode_token(token: str) -> dict:
    try:
        return jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
    except JWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )

def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    conn=Depends(get_db)
):
    payload = decode_token(credentials.credentials)
    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid token")
    cur = execute(conn, "SELECT * FROM users WHERE id=%s AND is_active=1", (int(user_id),))
    user = cur.fetchone()
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    return dict(user)
