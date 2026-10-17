import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from cryptography.fernet import Fernet
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Settings:
    api_key: str = field(repr=False)
    encryption_key: str = field(repr=False)
    youtube_client_id: str = field(repr=False)
    youtube_client_secret: str = field(repr=False)
    instagram_app_id: str = field(repr=False)
    instagram_app_secret: str = field(repr=False)
    base_url: str = "http://localhost:8000"
    database_path: Path = ROOT / "data/relay.sqlite3"
    instagram_api_version: str = "v25.0"
    max_upload_bytes: int = 2 * 1024**3
    instagram_media_bucket: str = ""
    youtube_redirect_uri: str = ""
    instagram_redirect_uri: str = ""

    def __post_init__(self):
        if len(self.api_key) < 32:
            raise ValueError("SERVICE_API_KEY must be at least 32 characters. Run setup_local.py.")
        try:
            Fernet(self.encryption_key.encode())
        except (ValueError, TypeError):
            raise ValueError("Invalid TOKEN_ENCRYPTION_KEY. Run setup_local.py.") from None
        if not re.fullmatch(r"v\d+\.\d+", self.instagram_api_version):
            raise ValueError("INSTAGRAM_API_VERSION must look like v25.0.")
        if self.max_upload_bytes <= 0:
            raise ValueError("MAX_UPLOAD_BYTES must be positive.")
        for url in [self.base_url, self.redirect_uri("youtube"), self.redirect_uri("instagram")]:
            parsed = urlsplit(url)
            if (
                not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.query
                or parsed.fragment
                or (
                    parsed.scheme != "https"
                    and not (
                        parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1"}
                    )
                )
            ):
                raise ValueError("Callback/base URLs must be HTTPS (HTTP allowed on localhost).")

    def redirect_uri(self, provider: str) -> str:
        override = getattr(self, f"{provider}_redirect_uri")
        return override or f"{self.base_url.rstrip('/')}/oauth/{provider}/callback"

    def credentials(self, provider: str) -> tuple[str, str]:
        if provider == "youtube":
            return self.youtube_client_id, self.youtube_client_secret
        return self.instagram_app_id, self.instagram_app_secret

    def oauth_setup_issue(self, provider: str) -> str | None:
        callback = urlsplit(self.redirect_uri(provider))
        base = urlsplit(self.base_url)
        if provider == "instagram" and (
            callback.scheme != "https" or callback.hostname in {"localhost", "127.0.0.1"}
        ):
            return (
                "Set up a public HTTPS address for Instagram first. Set BASE_URL to that address, "
                "register its /oauth/instagram/callback URL in Meta's Instagram Login settings, "
                "then open this workspace through the same HTTPS address."
            )
        if (callback.scheme, callback.netloc) != (base.scheme, base.netloc):
            return "The callback and BASE_URL must use the same origin so the sign-in cookie can be verified."
        if callback.path != f"/oauth/{provider}/callback":
            return f"The callback path must be /oauth/{provider}/callback."
        return None

    @classmethod
    def from_env(cls):
        load_dotenv(os.getenv("ENV_FILE", str(ROOT / ".env")))
        database_path = Path(os.getenv("DATABASE_PATH", "data/relay.sqlite3"))
        return cls(
            api_key=os.getenv("SERVICE_API_KEY", ""),
            encryption_key=os.getenv("TOKEN_ENCRYPTION_KEY", ""),
            youtube_client_id=os.getenv("YOUTUBE_CLIENT_ID", ""),
            youtube_client_secret=os.getenv("YOUTUBE_CLIENT_SECRET", ""),
            instagram_app_id=os.getenv("INSTAGRAM_APP_ID", ""),
            instagram_app_secret=os.getenv("INSTAGRAM_APP_SECRET", ""),
            base_url=os.getenv("BASE_URL", "http://localhost:8000").rstrip("/"),
            database_path=database_path if database_path.is_absolute() else ROOT / database_path,
            instagram_api_version=os.getenv("INSTAGRAM_API_VERSION", "v25.0"),
            max_upload_bytes=int(os.getenv("MAX_UPLOAD_BYTES", str(2 * 1024**3))),
            instagram_media_bucket=os.getenv("INSTAGRAM_MEDIA_BUCKET", "").strip(),
            youtube_redirect_uri=os.getenv("YOUTUBE_REDIRECT_URI", ""),
            instagram_redirect_uri=os.getenv("INSTAGRAM_REDIRECT_URI", ""),
        )
