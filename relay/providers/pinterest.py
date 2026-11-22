from __future__ import annotations

from fastapi import HTTPException


class PinterestProvider:
    async def list_pinterest_boards(self, token: str) -> list[dict]:
        """Return all boards belonging to the connected Pinterest user."""
        data = self.payload(
            await self.request(
                "GET",
                f"{self.pinterest_url}/boards",
                params={"page_size": 100},
                headers={"Authorization": f"Bearer {token}"},
            )
        )
        items = data.get("items") or []
        return [
            {
                "id": str(board.get("id", "")),
                "name": board.get("name", "Board"),
                "pin_count": self.nonnegative_int(board.get("pin_count")),
                "privacy": board.get("privacy", "PUBLIC"),
            }
            for board in items
            if board.get("id")
        ]

    async def create_pinterest_board(
        self, token: str, name: str, description: str = "", privacy: str = "PUBLIC"
    ) -> dict:
        """Create a new board for saving pins."""
        if not name.strip():
            raise HTTPException(422, "Board name cannot be blank.")
        body = {
            "name": name.strip(),
            "description": (description or "").strip()[:500],
            "privacy": privacy if privacy in {"PUBLIC", "SECRET", "PROTECTED"} else "PUBLIC",
        }
        data = self.payload(
            await self.request(
                "POST",
                f"{self.pinterest_url}/boards",
                json=body,
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            )
        )
        board_id = self.required_id(data)
        return {
            "id": board_id,
            "name": data.get("name", name),
            "privacy": data.get("privacy", privacy),
        }

    async def create_pinterest_pin(
        self,
        token: str,
        board_id: str,
        title: str,
        description: str,
        media_url: str,
        link: str | None = None,
        alt_text: str | None = None,
    ) -> dict:
        """Create a Pin on the given board using an image URL."""
        if not board_id:
            raise HTTPException(422, "board_id is required to create a Pin.")
        body = {
            "board_id": str(board_id),
            "title": (title or "").strip()[:100],
            "description": (description or "").strip()[:800],
            "media_source": {
                "source_type": "image_url",
                "url": str(media_url),
            },
        }
        if link:
            body["link"] = str(link)
        if alt_text:
            body["alt_text"] = (alt_text or "").strip()[:500]

        data = self.payload(
            await self.request(
                "POST",
                f"{self.pinterest_url}/pins",
                json=body,
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            )
        )
        pin_id = self.required_id(data)
        return {
            "id": pin_id,
            "url": f"https://www.pinterest.com/pin/{pin_id}/",
            "title": data.get("title") or title,
            "board_id": str(board_id),
        }

    async def get_pinterest_pin(self, token: str, pin_id: str) -> dict:
        """Fetch details of a single pin."""
        data = self.payload(
            await self.request(
                "GET",
                f"{self.pinterest_url}/pins/{pin_id}",
                headers={"Authorization": f"Bearer {token}"},
            )
        )
        return data

    async def pinterest_analytics(
        self, external_id: str, token: str, start_date: str, end_date: str
    ) -> dict:
        """Return profile metrics and board summary for reporting period."""
        headers = {"Authorization": f"Bearer {token}"}
        profile = self.payload(
            await self.request(
                "GET",
                f"{self.pinterest_url}/user_account",
                headers=headers,
            )
        )
        followers = self.nonnegative_int(profile.get("follower_count"))
        pins = self.nonnegative_int(profile.get("pin_count"))
        boards_count = self.nonnegative_int(profile.get("board_count"))
        monthly_views = self.nonnegative_int(profile.get("monthly_views"))

        impressions = 0
        saves = 0
        pin_clicks = 0
        try:
            analytics_resp = self.payload(
                await self.request(
                    "GET",
                    f"{self.pinterest_url}/user_account/analytics",
                    params={
                        "start_date": start_date,
                        "end_date": end_date,
                        "columns": "IMPRESSION,SAVE,PIN_CLICK",
                    },
                    headers=headers,
                )
            )
            all_metrics = analytics_resp.get("all") or analytics_resp.get("summary_metrics") or {}
            impressions = self.nonnegative_int(all_metrics.get("IMPRESSION") or all_metrics.get("impression"))
            saves = self.nonnegative_int(all_metrics.get("SAVE") or all_metrics.get("save"))
            pin_clicks = self.nonnegative_int(all_metrics.get("PIN_CLICK") or all_metrics.get("pin_click"))
        except HTTPException:
            # Analytics endpoint requires business account; gracefully default to profile views
            impressions = monthly_views

        boards = []
        try:
            boards = await self.list_pinterest_boards(token)
        except HTTPException:
            pass

        top_posts = [
            {
                "id": b["id"],
                "title": f"Board: {b['name']}",
                "url": f"https://www.pinterest.com/",
                "views": b.get("pin_count", 0),
                "likes": 0,
                "comments": 0,
            }
            for b in boards[:5]
        ]

        return {
            "channel": {
                "followers": followers,
                "pins": pins,
                "boards": boards_count or len(boards),
                "monthly_views": monthly_views,
            },
            "period": {
                "posts": pins,
                "impressions": impressions,
                "saves": saves,
                "pin_clicks": pin_clicks,
            },
            "top_posts": top_posts,
        }
