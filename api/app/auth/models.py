# app/auth/models.py
from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from sqlalchemy import String, Boolean, DateTime, Integer, ForeignKey
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base

from sqlalchemy import Text  # add
from sqlalchemy.orm import relationship  # optional

class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)

    email: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    username: Mapped[str] = mapped_column(String(80), unique=True, index=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)

    # admin / reviewer / viewer
    role: Mapped[str] = mapped_column(String(32), default="viewer", nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # "free", "trial", "pro_monthly", "pro_yearly", "lifetime"
    plan: Mapped[str] = mapped_column(String(32), default="free", nullable=False)
    access_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    stripe_customer_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    stripe_subscription_id: Mapped[str | None] = mapped_column(String(128), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow, nullable=False)


class AccessCode(Base):
    """
    Admin-issued access code for user testing.

    Secure design:
    - store ONLY a hash (code_hash); never store the plaintext code
    - one-time use by default
    - expires_at enforced
    - optional allowed_email to prevent sharing
    """
    __tablename__ = "access_codes"

    id: Mapped[str] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)

    code_hash: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)

    purpose: Mapped[str] = mapped_column(String(64), default="testing_bypass", nullable=False)
    plan_granted: Mapped[str] = mapped_column(String(32), default="trial", nullable=False)
    days_granted: Mapped[int] = mapped_column(Integer, default=7, nullable=False)

    allowed_email: Mapped[str | None] = mapped_column(String(255), nullable=True)

    max_uses: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    uses: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    issued_by_user_id: Mapped[str | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    redeemed_by_user_id: Mapped[str | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    redeemed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow, nullable=False)



class PasswordResetToken(Base):
    __tablename__ = "password_reset_tokens"

    id: Mapped[str] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)

    user_id: Mapped[str] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)

    token_hash: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    used_ip: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # optional:
    # user = relationship("User", lazy="joined")
