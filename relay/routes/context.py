from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import HTTPException

from relay.config import Settings
from relay.identity import Identity
from relay.store import Store


class CredentialsUnreadable(HTTPException):
    """A stored credential cannot be decrypted.

    This happens after a rotated ``TOKEN_ENCRYPTION_KEY`` or when a database is
    restored under a different key. It is a local, definite non-effect: nothing
    was sent to the provider. The only remedy is reconnecting the channel, so
    routes classify it as ``reconnect`` rather than a retryable refusal — a
    resend with the same credential would fail identically forever.
    """

    def __init__(self):
        super().__init__(409, "Stored credentials can't be read. Reconnect this account.")


@dataclass(frozen=True, slots=True)
class RouteContext:
    settings: Settings
    store: Store
    identity: Identity
    frontend: Path
    session_cookie: str
    session_marker: bytes
    protected: list[Any]
    legacy_session: Callable[..., bool]
    user_session: Callable[..., Any]
    bootstrap_session: Callable[..., Any]
    has_session: Callable[..., bool]
    same_origin: Callable[..., None]
    valid_account: Callable[..., Any]
    account_token: Callable[..., Any]
    post_or_404: Callable[..., Any]
    sync_post: Callable[..., Any]
