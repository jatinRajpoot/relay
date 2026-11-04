from .accounts import AccountStore
from .attempts import AttemptStore
from .content import ContentStore
from .core import StorageError, StoreCore, digest
from .oauth import OAuthStateStore
from .publishing import PublishingStore


class Store(
    StoreCore, OAuthStateStore, AccountStore, PublishingStore, AttemptStore, ContentStore
):
    """Workspace-scoped SQLite persistence with encrypted credentials."""


__all__ = ["StorageError", "Store", "digest"]
