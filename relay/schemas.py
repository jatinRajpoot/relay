import re
from typing import Literal

from pydantic import BaseModel, Field, HttpUrl, field_validator

Provider = Literal["youtube", "instagram"]


class InstagramPost(BaseModel):
    account_id: str = Field(min_length=1, max_length=64)
    media_type: Literal["IMAGE", "REELS"]
    media_url: HttpUrl
    caption: str = Field(default="", max_length=2200)

    @field_validator("media_url")
    @classmethod
    def public_https_url(cls, value):
        if value.scheme != "https" or value.username or value.password or value.fragment:
            raise ValueError("Use a publicly accessible HTTPS media URL without credentials.")
        return value


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
