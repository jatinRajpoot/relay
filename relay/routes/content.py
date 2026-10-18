from __future__ import annotations

import mimetypes
import secrets
from typing import Annotated
from urllib.parse import quote

from fastapi import File, HTTPException, UploadFile
from google.api_core.exceptions import GoogleAPIError
from google.auth.exceptions import GoogleAuthError

from .context import RouteContext


def register_content_routes(app, context: RouteContext):
    settings = context.settings
    store = context.store
    protected = context.protected

    async def save_content_file(file: UploadFile):
        if not settings.instagram_media_bucket:
            raise HTTPException(503, "Instagram media storage is not configured on this service.")
        media_type = (file.content_type or "").lower()
        if (
            media_type != "image/jpeg"
            and not media_type.startswith("video/")
            and media_type != "application/octet-stream"
        ):
            raise HTTPException(422, "Choose a JPEG image or a video file.")
        if not file.size or file.size > settings.max_upload_bytes:
            raise HTTPException(413, "Media is empty or exceeds MAX_UPLOAD_BYTES.")
        extension = mimetypes.guess_extension(media_type) or ".bin"
        if extension == ".jpe":
            extension = ".jpg"
        object_name = f"content/{secrets.token_urlsafe(24)}{extension}"
        filename = (file.filename or "").replace("\\", "/").rsplit("/", 1)[-1].strip()
        filename = "".join(character for character in filename if character.isprintable())[:255]
        filename = filename or f"Untitled{extension}"
        try:
            from google.cloud import storage

            blob = storage.Client().bucket(settings.instagram_media_bucket).blob(object_name)
            blob.upload_from_file(
                file.file,
                rewind=True,
                size=file.size,
                content_type=media_type,
                if_generation_match=0,
            )
        except (GoogleAPIError, GoogleAuthError, OSError, ValueError):
            raise HTTPException(502, "Could not upload media to Google Cloud Storage.") from None
        media_url = (
            f"https://storage.googleapis.com/{quote(settings.instagram_media_bucket, safe='')}/"
            f"{quote(object_name, safe='/')}"
        )
        try:
            item = store.create_content_item(
                filename, media_type, file.size, object_name, media_url
            )
        except Exception:  # noqa: BLE001 - clean up the uploaded object for any DB failure.
            try:
                blob.delete()
            except Exception:  # noqa: BLE001, S110 - cleanup is best effort after the DB failure.
                pass
            raise HTTPException(
                502, "Could not save the uploaded media to your content library."
            ) from None
        return item

    def public_content_item(item):
        return {
            "id": item["id"],
            "filename": item["filename"],
            "content_type": item["content_type"],
            "size": item["size"],
            "media_url": item["media_url"],
            "created_at": item["created_at"],
            "instagram_compatible": item["content_type"] in {"image/jpeg", "video/mp4"},
            "youtube_compatible": item["content_type"].startswith("video/")
            or item["content_type"] == "application/octet-stream",
        }

    @app.get("/content", dependencies=protected, tags=["Content"])
    async def list_content():
        return {"items": [public_content_item(item) for item in store.list_content_items()]}

    @app.post("/content", dependencies=protected, tags=["Content"], status_code=201)
    async def upload_content(file: Annotated[UploadFile, File()]):
        """Save a JPEG or video file in the workspace media library."""
        try:
            item = await save_content_file(file)
            return public_content_item(item)
        finally:
            await file.close()

    @app.post("/instagram/media", dependencies=protected, tags=["Instagram"], status_code=201)
    async def upload_instagram_media(file: Annotated[UploadFile, File()]):
        """Upload JPEG or MP4 media to the configured public GCS bucket for Instagram to fetch."""
        try:
            if (file.content_type or "").lower() not in {"image/jpeg", "video/mp4"}:
                raise HTTPException(422, "Choose a JPEG image or an MP4 video for Instagram.")
            item = await save_content_file(file)
        finally:
            await file.close()
        return {
            "media_url": item["media_url"],
            "media_type": "IMAGE" if item["content_type"] == "image/jpeg" else "REELS",
            "content_id": item["id"],
        }
