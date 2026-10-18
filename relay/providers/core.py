import httpx
from fastapi import HTTPException

from relay.config import Settings


class ProviderCore:
    def __init__(self, settings: Settings, client: httpx.AsyncClient):
        self.settings = settings
        self.client = client
        self.instagram_url = f"https://graph.instagram.com/{settings.instagram_api_version}"

    async def request(self, method, url, **kwargs):
        try:
            response = await self.client.request(method, url, **kwargs)
        except httpx.RequestError:
            raise HTTPException(
                502, "Provider connection failed. Check status before retrying uploads."
            ) from None
        if response.is_error or response.is_redirect:
            # Provider messages and URLs can contain credentials; never forward or log them.
            try:
                error = response.json().get("error")
            except (ValueError, AttributeError):
                error = None
            if error == "invalid_grant" or (isinstance(error, dict) and error.get("code") == 190):
                raise HTTPException(
                    409, "Provider authorization expired or was revoked. Reconnect account."
                )
            if response.status_code == 401:
                raise HTTPException(409, "Provider rejected authorization. Reconnect account.")
            raise HTTPException(502, f"Provider request failed (HTTP {response.status_code}).")
        return response

    @staticmethod
    def payload(response):
        try:
            data = response.json()
        except ValueError:
            raise HTTPException(502, "Provider returned invalid JSON.") from None
        if not isinstance(data, dict) or "error" in data:
            raise HTTPException(502, "Provider returned an invalid response.")
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
            raise HTTPException(502, "Provider returned incomplete token data.")
        return data

    @staticmethod
    def required_id(data, key="id"):
        value = data.get(key)
        if not isinstance(value, (str, int)) or not str(value):
            raise HTTPException(502, "Provider did not return a resource identifier.")
        return str(value)

    @staticmethod
    def nonnegative_int(value):
        try:
            return max(0, int(value))
        except (TypeError, ValueError, OverflowError):
            return 0
