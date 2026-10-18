from urllib.parse import urlsplit

import httpx
from fastapi import HTTPException, UploadFile


class YouTubeProvider:
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
