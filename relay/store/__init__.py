from .accounts import AccountStore
from .content import ContentStore
from .core import StoreCore, digest
from .oauth import OAuthStateStore
from .publishing import PublishingStore


class Store(StoreCore, OAuthStateStore, AccountStore, PublishingStore, ContentStore):
    """Workspace-scoped SQLite persistence with encrypted credentials."""


__all__ = ["Store", "digest"]
