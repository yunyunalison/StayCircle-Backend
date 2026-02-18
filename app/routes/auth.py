# Authentication and authorization endpoints and helpers.
# Provides JWT issuance/verification, role guards, and login/signup with basic rate limits.
from __future__ import annotations

import os
import time
from typing import Optional

import jwt
from fastapi import APIRouter, Depends, HTTPException, Header, status
from passlib.context import CryptContext
from sqlalchemy.orm import Session

from ..db import get_db
from .. import models, schemas
from ..rate_limit import rate_limit

# Router namespace for auth endpoints
router = APIRouter()

# JWT configuration (demo-ready; rotate secrets and tighten TTLs for production)
JWT_SECRET: str = os.getenv("STAYCIRCLE_JWT_SECRET", "dev-secret-change-me")
JWT_ALG: str = "HS256"
JWT_TTL_SECONDS: int = 60 * 60 * 24 * 7  # 7 days
# bcrypt_sha256 avoids bcrypt's 72-byte limit and handles Unicode safely
pwd_context = CryptContext(schemes=["bcrypt_sha256"], deprecated="auto")


# Helpers
def hash_password(password: str) -> str:
    """Hash a plaintext password using bcrypt_sha256."""
    return pwd_context.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    """Verify a plaintext password against its bcrypt_sha256 hash."""
    return pwd_context.verify(password, password_hash)


def create_access_token(*, user: models.User) -> str:
    """Create a signed JWT access token for the given user."""
    now = int(time.time())
    payload = {
        "sub": str(user.id),
        "email": user.email,
        "role": user.role,
        "iat": now,
        "exp": now + JWT_TTL_SECONDS,
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALG)


def decode_token(token: str) -> dict:
    """Decode and validate a JWT, raising a 401 on expiry or invalid signatures."""
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALG])
    except jwt.ExpiredSignatureError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token expired") from exc
    except jwt.InvalidTokenError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token") from exc


# Dependencies
def bearer_token_from_auth_header(authorization: Optional[str]) -> str:
    """Extract the Bearer token from the Authorization header."""
    if not authorization:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authorization header missing")
    parts = authorization.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid Authorization header")
    return parts[1]


def get_current_user(
    db: Session = Depends(get_db),
    authorization: Optional[str] = Header(default=None, alias="Authorization"),
) -> models.User:
    """Return the authenticated user or raise 401."""
    token = bearer_token_from_auth_header(authorization)
    payload = decode_token(token)
    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token payload")
    user = db.get(models.User, int(user_id))
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")
    return user


def get_current_user_optional(
    db: Session = Depends(get_db),
    authorization: Optional[str] = Header(default=None, alias="Authorization"),
) -> Optional[models.User]:
    """
    Return the current user if a valid Bearer token is present; otherwise None.

    Useful for public endpoints that vary behavior when the caller is authenticated.
    """
    if not authorization:
        return None
    try:
        token = bearer_token_from_auth_header(authorization)
        payload = decode_token(token)
        user_id = payload.get("sub")
        if not user_id:
            return None
        user = db.get(models.User, int(user_id))
        return user
    except HTTPException:
        # Treat invalid/missing tokens as anonymous for optional auth
        return None


# Dependency: enforce landlord role
def require_landlord(user: models.User = Depends(get_current_user)) -> models.User:
    if user.role != "landlord":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Landlord role required")
    return user


# Dependency: enforce tenant role
def require_tenant(user: models.User = Depends(get_current_user)) -> models.User:
    if user.role != "tenant":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Tenant role required")
    return user


# Routes
@router.post("/auth/signup", response_model=schemas.TokenResponse, status_code=status.HTTP_201_CREATED, dependencies=[Depends(rate_limit("signup"))])
def signup(payload: schemas.UserCreate, db: Session = Depends(get_db)) -> schemas.TokenResponse:
    """Register a user and return an access token."""
    # Determine role (default handled by schema)
    role = payload.role or "tenant"

    # Normalize email via schema validator; enforce uniqueness
    email = payload.email
    existing = db.query(models.User).filter(models.User.email == email).first()
    if existing:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Email already registered")

    user = models.User(
        email=email,
        password_hash=hash_password(payload.password),
        role=role,
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    token = create_access_token(user=user)
    return schemas.TokenResponse(
        access_token=token,
        user=schemas.UserRead.model_validate(user),
    )


@router.post("/auth/login", response_model=schemas.TokenResponse, dependencies=[Depends(rate_limit("login"))])
def login(payload: schemas.LoginRequest, db: Session = Depends(get_db)) -> schemas.TokenResponse:
    """Authenticate a user and return a fresh access token."""
    email = payload.email
    user = db.query(models.User).filter(models.User.email == email).first()
    if not user or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")

    token = create_access_token(user=user)
    return schemas.TokenResponse(
        access_token=token,
        user=schemas.UserRead.model_validate(user),
    )
