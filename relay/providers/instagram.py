from datetime import datetime

from fastapi import HTTPException


class InstagramProvider:
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
                    published_at = datetime.fromisoformat(item["timestamp"])
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
