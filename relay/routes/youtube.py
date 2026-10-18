from __future__ import annotations

from tempfile import SpooledTemporaryFile
from typing import Annotated, Literal

from fastapi import File, Form, HTTPException, UploadFile
from google.api_core.exceptions import GoogleAPIError
from google.auth.exceptions import GoogleAuthError
from starlette.datastructures import Headers

from .context import RouteContext


def register_youtube_routes(app, context: RouteContext):
    settings = context.settings
    store = context.store
    protected = context.protected
    valid_account = context.valid_account

    @app.post("/youtube/videos", dependencies=protected, tags=["YouTube"], status_code=201)
    async def upload_video(
        account_id: Annotated[str, Form(min_length=1, max_length=64)],
        title: Annotated[str, Form(min_length=1, max_length=100)],
        file: Annotated[UploadFile | None, File()] = None,
        content_id: Annotated[str | None, Form()] = None,
        description: Annotated[str, Form(max_length=5000)] = "",
        privacy_status: Annotated[Literal["private", "unlisted", "public"], Form()] = "private",
        category_id: Annotated[str, Form(pattern=r"^\d+$")] = "22",
        made_for_kids: Annotated[bool, Form()] = False,
    ):
        """Upload a video using YouTube's resumable session protocol. Defaults to private."""
        if not title.strip() or "<" in title or ">" in title:
            raise HTTPException(422, "Title must contain text and cannot include < or >.")
        temporary_file = None
        content_item = None
        if content_id:
            if file:
                await file.close()
                raise HTTPException(422, "Choose a saved content item or upload a file, not both.")
            item = store.get_content_item(content_id)
            if not item:
                raise HTTPException(404, "Content item not found. Refresh your content library.")
            content_item = item
            if not (
                item["content_type"].startswith("video/")
                or item["content_type"] == "application/octet-stream"
            ):
                raise HTTPException(422, "Choose a video from your content library for YouTube.")
            if not item["size"] or item["size"] > settings.max_upload_bytes:
                raise HTTPException(413, "Video is empty or exceeds MAX_UPLOAD_BYTES.")
            if not settings.instagram_media_bucket:
                raise HTTPException(503, "Content storage is not configured on this service.")
            try:
                from google.cloud import storage

                temporary_file = SpooledTemporaryFile(  # noqa: SIM115 - handed to UploadFile, then closed after upload.
                    max_size=1024 * 1024, mode="w+b"
                )
                storage.Client().bucket(settings.instagram_media_bucket).blob(
                    item["object_name"]
                ).download_to_file(temporary_file)
                temporary_file.seek(0)
                file = UploadFile(
                    filename=item["filename"],
                    file=temporary_file,
                    size=item["size"],
                    headers=Headers({"content-type": item["content_type"]}),
                )
            except (GoogleAPIError, GoogleAuthError, OSError, ValueError):
                if temporary_file is not None:
                    temporary_file.close()
                raise HTTPException(
                    502, "Could not read the selected content from storage."
                ) from None
        if file is None:
            raise HTTPException(422, "Choose a video from your content library.")
        try:
            if not file.size or file.size > settings.max_upload_bytes:
                raise HTTPException(413, "Video is empty or exceeds MAX_UPLOAD_BYTES.")
            mime = file.content_type or "application/octet-stream"
            if not (mime.startswith("video/") or mime == "application/octet-stream"):
                raise HTTPException(
                    422, "Upload a video file (video/* or application/octet-stream)."
                )
            account = await valid_account(account_id, "youtube")
            metadata = {
                "snippet": {"title": title, "description": description, "categoryId": category_id},
                "status": {
                    "privacyStatus": privacy_status,
                    "selfDeclaredMadeForKids": made_for_kids,
                },
            }
            uploaded = await app.state.providers.upload_youtube(
                store.decrypt(account["access_token"]),
                file,
                metadata,
            )
            store.save_youtube_history(
                account["id"],
                uploaded["id"],
                title.strip(),
                description,
                file.filename or "",
                mime,
                content_item["media_url"] if content_item else None,
                uploaded["url"],
                uploaded.get("privacy_status") or privacy_status,
            )
            return uploaded
        finally:
            await file.close()
