from .core import ProviderCore, ProviderError
from .instagram import InstagramProvider
from .oauth import INSTAGRAM_SCOPES, PINTEREST_SCOPES, YOUTUBE_SCOPES, OAuthProvider
from .pinterest import PinterestProvider
from .youtube import YouTubeProvider


class Providers(
    ProviderCore, OAuthProvider, InstagramProvider, YouTubeProvider, PinterestProvider
):
    """Provider operations shared by OAuth, publishing, and analytics routes."""


__all__ = [
    "INSTAGRAM_SCOPES",
    "PINTEREST_SCOPES",
    "YOUTUBE_SCOPES",
    "ProviderError",
    "Providers",
]
