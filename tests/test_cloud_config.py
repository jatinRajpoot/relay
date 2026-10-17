from relay.config import Settings


def test_loads_separate_secret_mount_without_overriding_runtime_settings(
    settings, tmp_path, monkeypatch,
):
    secret = tmp_path / ".env"
    secret.write_text(
        f"SERVICE_API_KEY={settings.api_key}\n"
        f"TOKEN_ENCRYPTION_KEY={settings.encryption_key}\n"
        "BASE_URL=http://localhost:8000\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("SERVICE_API_KEY", raising=False)
    monkeypatch.delenv("TOKEN_ENCRYPTION_KEY", raising=False)
    monkeypatch.setenv("ENV_FILE", str(secret))
    monkeypatch.setenv("BASE_URL", "https://relay.example.com")
    loaded = Settings.from_env()
    assert loaded.api_key == settings.api_key
    assert loaded.encryption_key == settings.encryption_key
    assert loaded.base_url == "https://relay.example.com"
