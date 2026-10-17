import pytest
from cryptography.fernet import Fernet

from relay.config import Settings


@pytest.fixture
def settings(tmp_path):
    return Settings(
        api_key="test-service-key-with-at-least-32-characters",
        encryption_key=Fernet.generate_key().decode(),
        youtube_client_id="test-google-client",
        youtube_client_secret="test-google-secret",
        instagram_app_id="test-ig-client",
        instagram_app_secret="test-ig-secret",
        database_path=tmp_path / "tokens.sqlite3",
    )
