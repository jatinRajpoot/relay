"""Add missing local service secrets to .env without printing or replacing credentials."""

import secrets
from pathlib import Path

from cryptography.fernet import Fernet
from dotenv import dotenv_values, set_key

root = Path(__file__).resolve().parent.parent
env_path = root / ".env"
env_path.touch(exist_ok=True)
values = dotenv_values(env_path)
for name, value in {
    "SERVICE_API_KEY": secrets.token_urlsafe(48),
    "TOKEN_ENCRYPTION_KEY": Fernet.generate_key().decode(),
    "BASE_URL": "http://localhost:8000",
    "DATABASE_PATH": "data/relay.sqlite3",
    "INSTAGRAM_API_VERSION": "v25.0",
}.items():
    if not values.get(name):
        set_key(env_path, name, value)
        print(f"Configured {name} in .env")
print("Ready. Keep .env private and back up TOKEN_ENCRYPTION_KEY with the database.")
