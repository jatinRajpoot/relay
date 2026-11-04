import httpx
from fastapi import HTTPException

from relay.config import Settings

# Meta Graph API rate-limit error codes. These arrive as HTTP 400, not 429, so a
# status-code-only classifier would record a routine rate limit as a permanent
# rejection.
_META_RATE_LIMIT_CODES = {4, 17, 32, 613}
# Google APIs report quota/rate exhaustion as HTTP 403 with a reason string.
_GOOGLE_RATE_LIMIT_REASONS = {
    "quotaExceeded",
    "dailyLimitExceeded",
    "rateLimitExceeded",
    "userRateLimitExceeded",
    "RATE_LIMIT_EXCEEDED",
}
_AUTH_CODES = {190}
_MAX_RETRY_AFTER = 3600


def _error_details(response):
    """Extract only the small, non-secret parts of a provider error body.

    Provider messages and URLs can contain credentials, so callers never forward
    or log the body itself; only a numeric code and a reason string are read.
    """
    try:
        data = response.json()
    except ValueError:
        return None, None
    if not isinstance(data, dict):
        return None, None
    error = data.get("error")
    if isinstance(error, str):
        return None, error
    if not isinstance(error, dict):
        return None, None
    code = error.get("code")
    reason = error.get("reason") or error.get("type")
    errors = error.get("errors")
    if isinstance(errors, list) and errors and isinstance(errors[0], dict):
        reason = errors[0].get("reason") or reason
    return code, reason


def _retry_after(response):
    """A bounded, validated Retry-After in seconds, or ``None``.

    Only the integer-seconds form is accepted. An HTTP-date or garbage value is
    ignored rather than guessed at, so the API never promises a cooldown the
    provider did not state.
    """
    value = response.headers.get("retry-after")
    if not value:
        return None
    try:
        seconds = int(value.strip())
    except (TypeError, ValueError):
        return None
    if seconds < 0 or seconds > _MAX_RETRY_AFTER:
        return None
    return seconds


class ProviderError(HTTPException):
    """A failed provider call, classified by what is known about its effect.

    ``kind`` is the contract callers switch on:

    ``ambiguous``    the provider may have acted; never retry automatically
    ``rate_limited`` the provider definitely did not act; retry after a delay
    ``auth``         the credential is unusable; reconnect before retrying
    ``rejected``     the provider answered with a definite refusal

    ``ambiguous`` is kept as a derived property so existing callers keep working.
    """

    def __init__(self, status_code: int, detail: str, *, kind: str = "rejected",
                 retry_after: int | None = None):
        headers = None
        if kind == "rate_limited" and retry_after is not None:
            headers = {"Retry-After": str(retry_after)}
        super().__init__(status_code, detail, headers=headers)
        self.kind = kind
        self.retry_after = retry_after

    @property
    def ambiguous(self) -> bool:
        return self.kind == "ambiguous"


class ProviderCore:
    def __init__(self, settings: Settings, client: httpx.AsyncClient):
        self.settings = settings
        self.client = client
        self.instagram_url = f"https://graph.instagram.com/{settings.instagram_api_version}"

    async def request(self, method, url, **kwargs):
        try:
            response = await self.client.request(method, url, **kwargs)
        except httpx.RequestError:
            # The provider may have received and acted on the request before the
            # connection broke, so the outcome is unknown rather than a failure.
            raise ProviderError(
                502,
                "Provider connection failed. Check status before retrying uploads.",
                kind="ambiguous",
            ) from None
        if response.is_error or response.is_redirect:
            status = response.status_code
            # Ambiguity is decided from the status line *first*, before the body is
            # read. A 408, a 5xx, or a redirect can all follow a request the provider
            # already acted on. A quota- or auth-shaped body on such a reply is an
            # upstream condition echoed by an edge/gateway, not proof that nothing
            # happened. Reading the body first would turn "may have published" into
            # "definitely did not", which is the one classification that permits a
            # duplicate resend.
            if status == 408:
                # A timeout is not proof the provider did nothing. Keep it uncertain.
                raise ProviderError(
                    502,
                    "Provider request timed out. Check status before retrying.",
                    kind="ambiguous",
                )
            if status >= 500 or response.is_redirect:
                raise ProviderError(
                    502,
                    f"Provider request failed (HTTP {status}).",
                    kind="ambiguous",
                )
            # Only a definite 4xx reply may be classified from its body: the provider
            # answered, so it provably refused the request before acting.
            code, reason = _error_details(response)
            if reason == "invalid_grant" or code in _AUTH_CODES or status == 401:
                raise ProviderError(
                    409,
                    "Provider authorization expired or was revoked. Reconnect account.",
                    kind="auth",
                )
            if (
                status == 429
                or code in _META_RATE_LIMIT_CODES
                or reason in _GOOGLE_RATE_LIMIT_REASONS
            ):
                # A rate limit is a definite non-effect: the provider refused the
                # request before acting. It is retryable, but only after a wait.
                raise ProviderError(
                    429,
                    "The provider rate-limited this request. Wait a few minutes, then try again.",
                    kind="rate_limited",
                    retry_after=_retry_after(response),
                )
            raise ProviderError(
                502,
                f"Provider request failed (HTTP {status}).",
                kind="rejected",
            )
        return response

    @staticmethod
    def payload(response):
        try:
            data = response.json()
        except ValueError:
            # A 2xx reply we cannot read: the remote side may already have acted on it.
            raise ProviderError(
                502, "Provider returned invalid JSON.", kind="ambiguous"
            ) from None
        if not isinstance(data, dict) or "error" in data:
            raise ProviderError(
                502, "Provider returned an invalid response.", kind="ambiguous"
            )
        return data

    @staticmethod
    def tokens(data):
        try:
            valid = (
                isinstance(data.get("access_token"), str)
                and bool(data["access_token"])
                and int(data["expires_in"]) > 0
            )
        except (ValueError, TypeError, KeyError):
            valid = False
        if not valid:
            raise ProviderError(502, "Provider returned incomplete token data.")
        return data

    @staticmethod
    def required_id(data, key="id"):
        value = data.get(key)
        if not isinstance(value, (str, int)) or not str(value):
            # The call succeeded but we cannot identify the created resource.
            raise ProviderError(
                502, "Provider did not return a resource identifier.", kind="ambiguous"
            )
        return str(value)

    @staticmethod
    def nonnegative_int(value):
        try:
            return max(0, int(value))
        except (TypeError, ValueError, OverflowError):
            return 0
