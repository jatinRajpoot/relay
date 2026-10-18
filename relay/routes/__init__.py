from .accounts import register_accounts_routes
from .content import register_content_routes
from .identity import register_identity_routes
from .instagram import register_instagram_routes
from .oauth import register_oauth_routes
from .service import register_service_routes
from .youtube import register_youtube_routes

__all__ = [
    "register_accounts_routes", "register_content_routes", "register_identity_routes",
    "register_instagram_routes", "register_oauth_routes", "register_service_routes",
    "register_youtube_routes",
]
