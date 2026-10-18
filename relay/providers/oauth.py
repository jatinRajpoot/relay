import base64
import hashlib
from urllib.parse import urlencode

from fastapi import HTTPException

YOUTUBE_SCOPES = (
    "https://www.googleapis.com/auth/youtube.upload "
    "https://www.googleapis.com/auth/youtube.readonly"
)
INSTAGRAM_SCOPES = "instagram_business_basic,instagram_business_content_publish"


class OAuthProvider:
    async def account_avatar(self, provider, external_id, token):
        if provider == "youtube":
            data = self.payload(
                await self.request(
                    "GET",
                    "https://www.googleapis.com/youtube/v3/channels",
                    params={"part": "snippet", "id": external_id},
                    headers={"Authorization": f"Bearer {token}"},
                )
            )
            channels = data.get("items") or []
            return self.youtube_avatar(channels[0].get("snippet")) if channels else None
        data = self.payload(
            await self.request(
                "GET",
                f"{self.instagram_url}/me",
                params={"fields": "profile_picture_url"},
                headers={"Authorization": f"Bearer {token}"},
            )
        )
        return data.get("profile_picture_url")

    def authorization_url(self, provider, state, verifier):
        client_id, client_secret = self.settings.credentials(provider)
        if not client_id or not client_secret:
            raise HTTPException(503, f"Configure {provider} client credentials in .env first.")
        params = {
            "client_id": client_id,
            "redirect_uri": self.settings.redirect_uri(provider),
            "response_type": "code",
            "state": state,
        }
        if provider == "youtube":
            challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            params.update(
                scope=YOUTUBE_SCOPES,
                access_type="offline",
                prompt="consent select_account",
                include_granted_scopes="true",
                code_challenge=challenge.decode().rstrip("="),
                code_challenge_method="S256",
            )
            endpoint = "https://accounts.google.com/o/oauth2/v2/auth"
        else:
            params.update(scope=INSTAGRAM_SCOPES, enable_fb_login="0", force_authentication="1")
            endpoint = "https://www.instagram.com/oauth/authorize"
        return endpoint + "?" + urlencode(params)

    async def exchange(self, provider, code, verifier):
        client_id, client_secret = self.settings.credentials(provider)
        form = {
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.settings.redirect_uri(provider),
        }
        if provider == "youtube":
            form["code_verifier"] = verifier
            tokens = self.tokens(
                self.payload(
                    await self.request(
                        "POST",
                        "https://oauth2.googleapis.com/token",
                        data=form,
                    )
                )
            )
            granted = tokens.get("scope", "")
            if not set(YOUTUBE_SCOPES.split()).issubset(set(granted.split())):
                raise HTTPException(409, "Grant both YouTube upload and channel-read permissions.")
            profile = self.payload(
                await self.request(
                    "GET",
                    "https://www.googleapis.com/youtube/v3/channels",
                    params={"part": "snippet", "mine": "true"},
                    headers={"Authorization": f"Bearer {tokens['access_token']}"},
                )
            )
            channels = profile.get("items", [])
            if len(channels) != 1:
                raise HTTPException(
                    409, "Select an account with one YouTube channel and reconnect."
                )
            channel = channels[0]
            snippet = channel.get("snippet") or {}
            avatar_url = self.youtube_avatar(snippet)
            return (
                tokens,
                self.required_id(channel),
                snippet.get("title", "YouTube"),
                granted,
                avatar_url,
            )

        # Meta documents multipart form data for the authorization-code exchange.
        short = self.payload(
            await self.request(
                "POST",
                "https://api.instagram.com/oauth/access_token",
                files={key: (None, value) for key, value in form.items()},
            )
        )
        # Some Meta responses wrap the short-lived token in a one-item data list.
        if "data" in short:
            if (
                "access_token" in short
                or not isinstance(short["data"], list)
                or len(short["data"]) != 1
            ):
                raise HTTPException(502, "Provider returned ambiguous token data.")
            short = short["data"][0]
        if not isinstance(short, dict) or not short.get("access_token"):
            raise HTTPException(502, "Instagram did not return an access token.")
        permissions = short.get("permissions")
        if permissions is not None:
            granted = set(permissions.split(",") if isinstance(permissions, str) else permissions)
            if not set(INSTAGRAM_SCOPES.split(",")).issubset(granted):
                raise HTTPException(409, "Grant both Instagram basic and publishing permissions.")
        tokens = self.tokens(
            self.payload(
                await self.request(
                    "GET",
                    "https://graph.instagram.com/access_token",
                    params={
                        "grant_type": "ig_exchange_token",
                        "client_secret": client_secret,
                        "access_token": short["access_token"],
                    },
                )
            )
        )
        profile = self.payload(
            await self.request(
                "GET",
                f"{self.instagram_url}/me",
                params={"fields": "user_id,username,profile_picture_url"},
                headers={"Authorization": f"Bearer {tokens['access_token']}"},
            )
        )
        return (
            tokens,
            self.required_id(profile, "user_id"),
            profile.get("username", "Instagram"),
            INSTAGRAM_SCOPES,
            profile.get("profile_picture_url"),
        )

    async def refresh(self, provider, access_token, refresh_token):
        if provider == "youtube":
            if not refresh_token:
                raise HTTPException(409, "No refresh token stored. Reconnect YouTube with consent.")
            client_id, client_secret = self.settings.credentials(provider)
            response = await self.request(
                "POST",
                "https://oauth2.googleapis.com/token",
                data={
                    "grant_type": "refresh_token",
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "refresh_token": refresh_token,
                },
            )
        else:
            response = await self.request(
                "GET",
                "https://graph.instagram.com/refresh_access_token",
                params={"grant_type": "ig_refresh_token", "access_token": access_token},
            )
        return self.tokens(self.payload(response))
