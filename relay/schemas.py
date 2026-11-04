import re
from typing import Literal

from pydantic import BaseModel, Field, HttpUrl, field_validator

Provider = Literal["youtube", "instagram"]


IDEMPOTENCY_PATTERN = r"^[A-Za-z0-9._:-]{8,128}$"


def normalize_idempotency_key(value):
    """Accept a client-supplied stable key, or ``None`` for legacy compatibility."""
    if value is None:
        return None
    value = value.strip()
    if not re.fullmatch(IDEMPOTENCY_PATTERN[1:-1], value):
        raise ValueError(
            "Idempotency-Key must be 8-128 characters of letters, digits, '.', '_', ':' or '-'."
        )
    return value


class InstagramPost(BaseModel):
    account_id: str = Field(min_length=1, max_length=64)
    media_type: Literal["IMAGE", "REELS"]
    media_url: HttpUrl
    caption: str = Field(default="", max_length=2200)
    idempotency_key: str | None = Field(default=None, max_length=128)

    @field_validator("media_url")
    @classmethod
    def public_https_url(cls, value):
        if value.scheme != "https" or value.username or value.password or value.fragment:
            raise ValueError("Use a publicly accessible HTTPS media URL without credentials.")
        return value

    @field_validator("idempotency_key")
    @classmethod
    def valid_idempotency_key(cls, value):
        return normalize_idempotency_key(value)


class PublishRequest(BaseModel):
    """Optional body for an explicit Instagram publish confirmation."""

    idempotency_key: str | None = Field(default=None, max_length=128)

    @field_validator("idempotency_key")
    @classmethod
    def valid_idempotency_key(cls, value):
        return normalize_idempotency_key(value)


class UnlockRequest(BaseModel):
    service_key: str = Field(min_length=1, max_length=1024, repr=False)


class LoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=128, repr=False)

    @field_validator("email")
    @classmethod
    def normalized_email(cls, value):
        value = value.strip().lower()
        if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value):
            raise ValueError("Enter a valid email address.")
        return value


class WorkspaceRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)

    @field_validator("name")
    @classmethod
    def trimmed_name(cls, value):
        if not value.strip():
            raise ValueError("Enter a name.")
        return value.strip()


class SignupRequest(LoginRequest):
    password: str = Field(min_length=12, max_length=128, repr=False)
    name: str = Field(min_length=1, max_length=80)
    workspace_name: str = Field(default="My workspace", min_length=1, max_length=80)

    @field_validator("name", "workspace_name")
    @classmethod
    def trimmed_name(cls, value):
        return WorkspaceRequest.trimmed_name(value)
