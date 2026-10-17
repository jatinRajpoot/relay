import sqlite3

import pytest
from google.api_core.exceptions import NotFound, PreconditionFailed

from relay.store import Store


class FakeBlob:
    generation = 0
    data = b""

    def reload(self):
        if not self.generation:
            raise NotFound("missing")

    def download_as_bytes(self, if_generation_match):
        assert if_generation_match == self.generation
        return self.data

    def upload_from_string(self, data, content_type, if_generation_match):
        if if_generation_match != self.generation:
            raise PreconditionFailed("stale")
        self.data = data
        self.generation += 1


@pytest.fixture
def cloud_store(settings, monkeypatch):
    from google.cloud import storage

    blob = FakeBlob()
    monkeypatch.setenv("DATABASE_BUCKET", "test-private-bucket")
    monkeypatch.setattr(storage, "Client", lambda: type("Client", (), {
        "bucket": lambda self, name: type("Bucket", (), {
            "blob": lambda self, name: blob,
        })(),
    })())
    return Store(settings.database_path, settings.encryption_key), blob


def test_accounts_survive_loss_of_local_disk(cloud_store, settings):
    store, blob = cloud_store
    store.save_account("youtube", "channel", "Channel", {
        "access_token": "private-token", "expires_in": 3600,
    }, "scope")
    assert b"private-token" not in blob.data
    settings.database_path.unlink()
    restarted = Store(settings.database_path, settings.encryption_key)
    assert restarted.list_accounts()[0]["external_id"] == "channel"


def test_failed_transaction_does_not_change_durable_database(cloud_store):
    store, blob = cloud_store
    before = blob.data
    with pytest.raises(sqlite3.OperationalError), store.connection() as db:
        db.execute("DELETE FROM accounts")
        db.execute("INSERT INTO nonexistent VALUES (1)")
    assert blob.data == before


def test_stale_writer_cannot_overwrite_newer_generation(cloud_store):
    store, blob = cloud_store
    before = blob.data
    with pytest.raises(PreconditionFailed), store.connection() as db:
        db.execute("CREATE TABLE race_test (id INTEGER)")
        blob.generation += 1
    assert blob.data == before
    assert store.list_accounts() == []
