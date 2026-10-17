import base64
import hashlib
from datetime import datetime
from urllib.parse import urlencode, urlsplit

import httpx
from fastapi import HTTPException, UploadFile

from relay.config import Settings

YOUTUBE_SCOPES = (
    "https://www.googleapis.com/auth/youtube.upload "
    "https://www.googleapis.com/auth/youtube.readonly"
)
INSTAGRAM_SCOPES = "instagram_business_basic,instagram_business_content_publish"


class Providers:
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
    def youtube_avatar(snippet):
        thumbnails = (snippet or {}).get("thumbnails") or {}
        return next(
            (
                thumbnails[size].get("url")
                for size in ("medium", "high", "default")
                if isinstance(thumbnails.get(size), dict) and thumbnails[size].get("url")
            ),
            None,
        )

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

    async def create_instagram_post(self, external_id, token, media_type, media_url, caption):
        form = {"caption": caption}
        if media_type == "REELS":
            form.update(media_type="REELS", video_url=media_url, share_to_feed="true")
        else:
            form["image_url"] = media_url
        data = self.payload(
            await self.request(
                "POST",
                f"{self.instagram_url}/{external_id}/media",
                data=form,
                headers={"Authorization": f"Bearer {token}"},
            )
        )
        return self.required_id(data)

    async def instagram_status(self, container_id, token):
        data = self.payload(
            await self.request(
                "GET",
                f"{self.instagram_url}/{container_id}",
                params={"fields": "status_code"},
                headers={"Authorization": f"Bearer {token}"},
            )
        )
        status = data.get("status_code")
        if status not in {"FINISHED", "IN_PROGRESS", "ERROR", "EXPIRED", "PUBLISHED"}:
            raise HTTPException(502, "Instagram returned an unknown processing status.")
        return status

    async def publish_instagram(self, external_id, token, container_id):
        data = self.payload(
            await self.request(
                "POST",
                f"{self.instagram_url}/{external_id}/media_publish",
                data={"creation_id": container_id},
                headers={"Authorization": f"Bearer {token}"},
            )
        )
        return self.required_id(data)

    async def instagram_media(self, external_id, token, start_date):
        """Return recent media and public interaction counts for one Instagram profile."""
        items = []
        after = None
        for _ in range(5):
            params = {
                "fields": "id,caption,media_type,timestamp,permalink,like_count,comments_count",
                "limit": 100,
            }
            if after:
                params["after"] = after
            data = self.payload(
                await self.request(
                    "GET",
                    f"{self.instagram_url}/{external_id}/media",
                    params=params,
                    headers={"Authorization": f"Bearer {token}"},
                )
            )
            page = data.get("data")
            if not isinstance(page, list):
                raise HTTPException(502, "Instagram returned an invalid media list.")
            reached_start = False
            for item in page:
                try:
                    published_at = datetime.fromisoformat(
                        item["timestamp"].replace("Z", "+00:00")
                    )
                except (KeyError, TypeError, ValueError):
                    continue
                if published_at.date().isoformat() < start_date:
                    reached_start = True
                    continue
                items.append(
                    {
                        "id": str(item.get("id", "")),
                        "title": (item.get("caption") or "Instagram post")[:160],
                        "media_type": item.get("media_type", "POST"),
                        "url": item.get("permalink"),
                        "published_at": published_at.isoformat(),
                        "likes": self.nonnegative_int(item.get("like_count")),
                        "comments": self.nonnegative_int(item.get("comments_count")),
                    }
                )
            paging = data.get("paging") or {}
            cursors = paging.get("cursors") or {}
            next_after = cursors.get("after")
            if reached_start or not paging.get("next") or not next_after or len(page) < 100:
                break
            after = next_after
        return items

    @staticmethod
    def nonnegative_int(value):
        try:
            return max(0, int(value))
        except (TypeError, ValueError, OverflowError):
            return 0

    async def youtube_analytics(self, external_id, token, start_date, end_date):
        """Return channel totals and top videos for a reporting period."""
        headers = {"Authorization": f"Bearer {token}"}
        channel = self.payload(
            await self.request(
                "GET",
                "https://www.googleapis.com/youtube/v3/channels",
                params={"part": "statistics", "id": external_id},
                headers=headers,
            )
        )
        channel_items = channel.get("items") or []
        if not channel_items:
            raise HTTPException(409, "YouTube channel is unavailable. Reconnect the account.")
        statistics = channel_items[0].get("statistics") or {}

        async def report(metrics, **extra):
            params = {
                "ids": "channel==MINE",
                "startDate": start_date,
                "endDate": end_date,
                "metrics": metrics,
                **extra,
            }
            return self.payload(
                await self.request(
                    "GET",
                    "https://youtubeanalytics.googleapis.com/v2/reports",
                    params=params,
                    headers=headers,
                )
            )

        summary = await report("views,likes,comments,subscribersGained,estimatedMinutesWatched")
        summary_row = (summary.get("rows") or [[0, 0, 0, 0, 0]])[0]
        top_report = await report(
            "views,likes,comments", dimensions="video", sort="-views", maxResults=5
        )
        video_rows = top_report.get("rows") or []
        video_ids = [str(row[0]) for row in video_rows if row and row[0]]
        video_titles = {}
        if video_ids:
            videos = self.payload(
                await self.request(
                    "GET",
                    "https://www.googleapis.com/youtube/v3/videos",
                    params={"part": "snippet", "id": ",".join(video_ids)},
                    headers=headers,
                )
            )
            video_titles = {
                item.get("id"): (item.get("snippet") or {}).get("title", "YouTube video")
                for item in videos.get("items", [])
            }
        return {
            "channel": {
                "views": self.nonnegative_int(statistics.get("viewCount")),
                "subscribers": self.nonnegative_int(statistics["subscriberCount"])
                if "subscriberCount" in statistics
                else None,
                "videos": self.nonnegative_int(statistics.get("videoCount")),
            },
            "period": {
                "views": self.nonnegative_int(summary_row[0] if len(summary_row) > 0 else 0),
                "likes": self.nonnegative_int(summary_row[1] if len(summary_row) > 1 else 0),
                "comments": self.nonnegative_int(summary_row[2] if len(summary_row) > 2 else 0),
                "subscribers_gained": self.nonnegative_int(summary_row[3] if len(summary_row) > 3 else 0),
                "watch_minutes": self.nonnegative_int(summary_row[4] if len(summary_row) > 4 else 0),
            },
            "top_posts": [
                {
                    "id": str(row[0]),
                    "title": video_titles.get(str(row[0]), "YouTube video"),
                    "url": f"https://www.youtube.com/watch?v={row[0]}",
                    "views": self.nonnegative_int(row[1] if len(row) > 1 else 0),
                    "likes": self.nonnegative_int(row[2] if len(row) > 2 else 0),
                    "comments": self.nonnegative_int(row[3] if len(row) > 3 else 0),
                }
                for row in video_rows
                if row
            ],
        }

    async def upload_youtube(self, token, file: UploadFile, metadata):
        content_type = file.content_type or "application/octet-stream"
        headers = {"Authorization": f"Bearer {token}"}
        response = await self.request(
            "POST",
            "https://www.googleapis.com/upload/youtube/v3/videos",
            params={"uploadType": "resumable", "part": "snippet,status"},
            json=metadata,
            headers=headers
            | {"X-Upload-Content-Length": str(file.size), "X-Upload-Content-Type": content_type},
        )
        upload_url = response.headers.get("location", "")
        parsed = urlsplit(upload_url)
        if (
            parsed.scheme != "https"
            or parsed.netloc != "www.googleapis.com"
            or parsed.path != "/upload/youtube/v3/videos"
            or parsed.fragment
        ):
            raise HTTPException(502, "YouTube returned an invalid upload session URL.")

        async def chunks():
            while chunk := await file.read(1024 * 1024):
                yield chunk

        # Stream the spooled file, keeping memory bounded even for large videos.
        await file.seek(0)
        result = self.payload(
            await self.request(
                "PUT",
                upload_url,
                content=chunks(),
                timeout=httpx.Timeout(600, connect=30),
                headers=headers | {"Content-Length": str(file.size), "Content-Type": content_type},
            )
        )
        video_id = self.required_id(result)
        return {
            "id": video_id,
            "url": f"https://www.youtube.com/watch?v={video_id}",
            "privacy_status": result.get("status", {}).get("privacyStatus"),
        }
