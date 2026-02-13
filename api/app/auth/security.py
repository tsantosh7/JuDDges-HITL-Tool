# app/auth/security.py
from __future__ import annotations

import secrets
from passlib.context import CryptContext

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    return pwd_context.verify(password, password_hash)


def generate_access_code(prefix: str = "TEST") -> str:
    # High entropy, unguessable (safe to DM/email)
    return f"{prefix}_{secrets.token_urlsafe(32)}"


def hash_access_code(code: str) -> str:
    # Same strong hashing; plaintext never stored
    return pwd_context.hash(code)


def verify_access_code(code: str, code_hash: str) -> bool:
    return pwd_context.verify(code, code_hash)
