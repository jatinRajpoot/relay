from __future__ import annotations

import hashlib
import sqlite3
import time
from tempfile import SpooledTemporaryFile
from typing import Annotated, Literal

from fastapi import File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse
from google.api_core.exceptions import GoogleAPIError
from google.auth.exceptions import GoogleAuthError
from starlette.datastructures import Headers

from relay.providers import ProviderError
from relay.schemas import normalize_idempotency_key
from relay.store import StorageError
from relay.store.attempts import fingerprint, retry_metadata, safe_error

from .context import CredentialsUnreadable, RouteContext

IDEMPOTENCY_HEADER = "Idempotency-Key"
_KEY_REUSED = "This request key was already used for a different upload. Start a new upload."


def _resolve_key(header_value, form_value):
    """Merge the header and form key, refusing an ambiguous or malformed pair.

    The form field is length-bounded by FastAPI but not pattern-validated, and the
    header is not validated at all, so both are checked here: an invalid key must
    be a 422, never an unhandled ``ValueError`` (a 500).
    """
    try:
        header_key = normalize_idempotency_key(header_value)
        form_key = normalize_idempotency_key(form_value)
    except ValueError as error:
        raise HTTPException(422, str(error)) from None
    if header_key and form_key and header_key != form_key:
        raise HTTPException(422, "Send the idempotency key once, as a header or in the form.")
    return header_key or form_key


async def _content_digest(file):
    """SHA-256 of the exact bytes that will be uploaded, then rewind.

    Filename, size, and MIME type are not an identity: two different videos can
    share all three. The digest is what makes a reused idempotency key with
    different bytes a *different* request (409) instead of a silent replay of the
    first upload. The file is spooled, so this is a second bounded read pass, and
    the handle is left at position 0 for the upload itself.
    """
    digest = hashlib.sha256()
    try:
        await file.seek(0)
        while chunk := await file.read(1024 * 1024):
            digest.update(chunk)
    finally:
        await file.seek(0)
    return digest.hexdigest()


def register_youtube_routes(app, context: RouteContext):
    settings = context.settings
    store = context.store
    protected = context.protected
    valid_account = context.valid_account
    account_token = context.account_token

    def duplicate_response(attempt):
        """Answer a repeated key from the durable receipt. Never re-uploads."""
        if attempt["status"] == "published" and attempt["provider_ref"]:
            return JSONResponse(
                status_code=201,
                content={
                    "id": attempt["provider_ref"],
                    "url": attempt["provider_url"]
                    or f"https://www.youtube.com/watch?v={attempt['provider_ref']}",
                    "privacy_status": attempt["privacy_status"],
                    "attempt": store.public_attempt(attempt),
                },
            )
        return JSONResponse(
            status_code=202,
            content={
                "attempt": store.public_attempt(attempt),
                "detail": "A previous upload with this key is still being resolved.",
            },
        )

    def retry_gate(attempt):
        if attempt["retry_kind"] == "reconnect":
            raise HTTPException(409, "Reconnect this account before uploading again.")
        remaining = max(1, (attempt["retry_at"] or 0) - int(time.time()))
        raise HTTPException(
            429,
            "The provider asked us to wait before retrying. Try again shortly.",
            headers={"Retry-After": str(remaining)},
        )

    def require_retry_open(attempt):
        """The one gate an explicit upload retry passes, keyed or not."""
        if store.reconnect_required(attempt):
            raise HTTPException(409, "Reconnect this account before uploading again.")
        if not store.retry_is_open(attempt):
            retry_gate(attempt)

    def repeated_key(attempt):
        """Resolve a repeated upload key without implicitly re-uploading.

        ``None`` means the receipt records a definite refusal that may be retried
        now; the caller re-arms that same row. Any other state answers from the
        ledger, so a duplicate click can never start a second upload.
        """
        if attempt["status"] == "failed":
            require_retry_open(attempt)
            return None
        return duplicate_response(attempt)

    @app.post("/youtube/videos", dependencies=protected, tags=["YouTube"], status_code=201)
    async def upload_video(
        request: Request,
        account_id: Annotated[str, Form(min_length=1, max_length=64)],
        title: Annotated[str, Form(min_length=1, max_length=100)],
        file: Annotated[UploadFile | None, File()] = None,
        content_id: Annotated[str | None, Form()] = None,
        description: Annotated[str, Form(max_length=5000)] = "",
        privacy_status: Annotated[Literal["private", "unlisted", "public"], Form()] = "private",
        category_id: Annotated[str, Form(pattern=r"^\d+$")] = "22",
        made_for_kids: Annotated[bool, Form()] = False,
        idempotency_key: Annotated[str | None, Form(max_length=128)] = None,
    ):
        """Upload a video using YouTube's resumable session protocol. Defaults to private."""
        if settings.local_media_only:
            raise HTTPException(
                409,
                "Local preview mode stores files on this computer. YouTube publishing is disabled.",
            )
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
            key = _resolve_key(request.headers.get(IDEMPOTENCY_HEADER), idempotency_key)
            # The fingerprint binds the key to the exact semantic upload, including
            # the bytes themselves. Filename/size/MIME alone let two different
            # videos collide, which would answer the second one with the first
            # one's receipt. Hashing is only needed when a key is actually used.
            request_fingerprint = fingerprint(
                {
                    "operation": "youtube.upload",
                    "account_id": account["id"],
                    "title": title.strip(),
                    "description": description,
                    "privacy_status": privacy_status,
                    "category_id": category_id,
                    "made_for_kids": bool(made_for_kids),
                    "content_id": content_id,
                    "filename": file.filename or "",
                    "size": file.size,
                    "content_type": mime,
                    "sha256": await _content_digest(file) if key else None,
                }
            )
            if key:
                existing = store.find_attempt_by_key("youtube", account["id"], key)
                if existing:
                    if existing["request_fingerprint"] != request_fingerprint:
                        raise HTTPException(409, _KEY_REUSED)
                    answer = repeated_key(existing)
                    if answer is not None:
                        return answer
                    if not store.reopen_failed_attempt(existing["id"]):
                        raise HTTPException(409, "This upload is already being processed.")
                    attempt = store.get_attempt(existing["id"])
                else:
                    attempt = None
            else:
                attempt = None

            metadata = {
                "snippet": {"title": title, "description": description, "categoryId": category_id},
                "status": {
                    "privacyStatus": privacy_status,
                    "selfDeclaredMadeForKids": made_for_kids,
                },
            }
            if attempt is None:
                # The receipt is durable before the upload starts. An upload can run
                # for minutes; if the process dies mid-stream the row is the only
                # evidence that YouTube may hold a video.
                try:
                    attempt = store.create_attempt(
                        account_id=account["id"],
                        provider="youtube",
                        status="pending",
                        media_name=file.filename or "",
                        media_type=mime,
                        media_url=content_item["media_url"] if content_item else None,
                        title=title.strip(),
                        caption=description,
                        privacy_status=privacy_status,
                        idempotency_key=key,
                        request_fingerprint=request_fingerprint,
                    )
                except sqlite3.IntegrityError:
                    existing = (
                        store.find_attempt_by_key("youtube", account["id"], key) if key else None
                    )
                    if existing and existing["request_fingerprint"] == request_fingerprint:
                        answer = repeated_key(existing)
                        if answer is not None:
                            return answer
                        if not store.reopen_failed_attempt(existing["id"]):
                            raise HTTPException(
                                409, "This upload is already being processed."
                            ) from None
                        attempt = store.get_attempt(existing["id"])
                    else:
                        raise HTTPException(409, "This upload is already being processed.") from None
                except sqlite3.Error:
                    # No durable receipt means no dispatch: this request sent nothing.
                    raise HTTPException(
                        503,
                        "Could not record this upload safely. Nothing was sent for this "
                        "request. Try again.",
                    ) from None
            try:
                claimed = store.claim_attempt(attempt["id"], expected=("pending",))
            except sqlite3.Error:
                # The claim could not be persisted, so this request dispatched nothing.
                raise HTTPException(
                    503,
                    "Could not record this upload safely. Nothing was sent for this "
                    "request. Try again.",
                ) from None
            if not claimed:
                raise HTTPException(409, "This upload is already being processed.")

            try:
                uploaded = await app.state.providers.upload_youtube(
                    account_token(account),
                    file,
                    metadata,
                )
            except ProviderError as error:
                if error.kind in {"rate_limited", "auth"}:
                    # Definite non-effect: no video was created. Keep the receipt
                    # retryable and record how the user may retry.
                    kind, retry_at = retry_metadata(error)
                    store.release_claim(
                        attempt["id"],
                        status="failed",
                        error=safe_error(error),
                        retry_kind=kind,
                        retry_at=retry_at,
                    )
                else:
                    store.update_attempt(
                        attempt["id"],
                        status="unknown" if error.ambiguous else "failed",
                        error=safe_error(error),
                        retry_kind=None if error.ambiguous else "none",
                    )
                raise
            except HTTPException as error:
                # The provider was never reached. An unreadable credential is fixed
                # only by reconnecting, so the receipt must say so rather than offer
                # a resend that would fail identically forever.
                store.update_attempt(
                    attempt["id"],
                    status="failed",
                    error=safe_error(error),
                    retry_kind="reconnect" if isinstance(error, CredentialsUnreadable) else "none",
                )
                raise
            except Exception:
                store.update_attempt(
                    attempt["id"],
                    status="unknown",
                    error="The upload did not complete. Check YouTube Studio before retrying.",
                )
                raise

            history_error = None
            try:
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
            except Exception:  # noqa: BLE001 - the video exists; history loss is not a failure.
                # The video exists upstream. Losing the history row must not turn a
                # confirmed upload into a reported failure.
                history_error = "The video uploaded, but its history entry could not be saved."
            # The receipt is what makes the success survive a reload. If it cannot be
            # saved, the upload still happened, so this must not be reported as a
            # failure (the user would upload again) nor as a plain success (the
            # receipt would be missing): it is an explicit, sanitised 503 whose
            # outcome says the destination may be live.
            #
            # This is the post-dispatch persistence boundary: the durable snapshot
            # (``StorageError``) and the local SQLite file (``sqlite3.Error``: disk
            # full, locked, I/O error) both leave the same truth. A programming
            # error is deliberately not caught here.
            try:
                receipt = store.update_attempt(
                    attempt["id"],
                    status="published",
                    provider_ref=uploaded["id"],
                    provider_url=uploaded["url"],
                    privacy_status=uploaded.get("privacy_status") or privacy_status,
                    claimed_at=None,
                    error=history_error,
                )
            except (StorageError, sqlite3.Error) as error:
                raise StorageError(
                    "YouTube accepted the upload, but Relay could not record it. "
                    "Check YouTube Studio before uploading again.",
                    outcome="unknown",
                ) from error
            # Additive: existing clients keep ``id``/``url``/``privacy_status``.
            return uploaded | {"attempt": store.public_attempt(receipt)}
        finally:
            await file.close()
