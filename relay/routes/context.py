from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from relay.config import Settings
from relay.identity import Identity
from relay.store import Store


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
    has_session: Callable[..., bool]
    same_origin: Callable[..., None]
    valid_account: Callable[..., Any]
    post_or_404: Callable[..., Any]
    sync_post: Callable[..., Any]
