from .core import ProviderCore
from .instagram import InstagramProvider
from .oauth import INSTAGRAM_SCOPES, YOUTUBE_SCOPES, OAuthProvider
from .youtube import YouTubeProvider


class Providers(ProviderCore, OAuthProvider, InstagramProvider, YouTubeProvider):
    """Provider operations shared by OAuth, publishing, and analytics routes."""


__all__ = ["INSTAGRAM_SCOPES", "YOUTUBE_SCOPES", "Providers"]
