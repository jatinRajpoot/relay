from __future__ import annotations

import sqlite3
import time

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from relay.providers import ProviderError
from relay.schemas import (
    CreateBoardRequest,
    PinterestPost,
    normalize_idempotency_key,
)
from relay.store import StorageError
from relay.store.attempts import (
    fingerprint,
    retry_metadata,
    safe_error,
)

from .context import CredentialsUnreadable, RouteContext

IDEMPOTENCY_HEADER = "Idempotency-Key"
_KEY_REUSED = "This request key was already used for a different pin. Start a new pin."


def _resolve_key(header_value, body_value):
    try:
        header_key = normalize_idempotency_key(header_value)
    except ValueError as error:
        raise HTTPException(422, str(error)) from None
    body_key = normalize_idempotency_key(body_value)
    if header_key and body_key and header_key != body_key:
        raise HTTPException(422, "Send the idempotency key once, as a header or in the body.")
    return header_key or body_key


def register_pinterest_routes(app, context: RouteContext):
    settings = context.settings
    store = context.store
    protected = context.protected
    valid_account = context.valid_account
    account_token = context.account_token

    def duplicate_response(attempt):
        """Answer a repeated key from the durable receipt."""
        if attempt["status"] == "published" and attempt["provider_ref"]:
            return JSONResponse(
                status_code=201,
                content={
                    "id": attempt["provider_ref"],
                    "url": attempt["provider_url"]
                    or f"https://www.pinterest.com/pin/{attempt['provider_ref']}/",
                    "attempt": store.public_attempt(attempt),
                },
            )
        return JSONResponse(
            status_code=202,
            content={
                "attempt": store.public_attempt(attempt),
                "detail": "A previous pin with this key is still being resolved.",
            },
        )

    def retry_gate(attempt):
        if attempt["retry_kind"] == "reconnect":
            raise HTTPException(409, "Reconnect this account before trying again.")
        remaining = max(1, (attempt["retry_at"] or 0) - int(time.time()))
        raise HTTPException(
            429,
            "The provider asked us to wait before retrying. Try again shortly.",
            headers={"Retry-After": str(remaining)},
        )

    def require_retry_open(attempt):
        if store.reconnect_required(attempt):
            raise HTTPException(409, "Reconnect this account before trying again.")
        if not store.retry_is_open(attempt):
            retry_gate(attempt)

    def repeated_key(attempt):
        if attempt["status"] == "failed":
            require_retry_open(attempt)
            return None
        return duplicate_response(attempt)

    @app.get("/pinterest/boards", dependencies=protected, tags=["Pinterest"])
    async def list_boards(account_id: str):
        """List all boards for a connected Pinterest account."""
        account = await valid_account(account_id, "pinterest")
        boards = await app.state.providers.list_pinterest_boards(account_token(account))
        return {"boards": boards}

    @app.post("/pinterest/boards", dependencies=protected, tags=["Pinterest"], status_code=201)
    async def create_board(body: CreateBoardRequest):
        """Create a new Pinterest board."""
        account = await valid_account(body.account_id, "pinterest")
        board = await app.state.providers.create_pinterest_board(
            account_token(account),
            name=body.name,
            description=body.description,
            privacy=body.privacy,
        )
        return {"board": board}

    @app.post("/pinterest/pins", dependencies=protected, tags=["Pinterest"], status_code=201)
    async def create_pin(request: Request, body: PinterestPost):
        """Publish a pin to Pinterest."""
        if settings.local_media_only:
            raise HTTPException(
                409,
                "Local preview mode stores files on this computer. Pinterest publishing is disabled.",
            )
        account = await valid_account(body.account_id, "pinterest")
        token = account_token(account)
        key = _resolve_key(request.headers.get(IDEMPOTENCY_HEADER), body.idempotency_key)

        request_fingerprint = fingerprint(
            {
                "operation": "pinterest.create",
                "account_id": account["id"],
                "board_id": body.board_id,
                "board_name": body.board_name,
                "title": body.title,
                "description": body.description,
                "link": str(body.link) if body.link else None,
                "media_url": str(body.media_url),
            }
        )

        if key:
            existing = store.find_attempt_by_key("pinterest", account["id"], key)
            if existing:
                if existing["request_fingerprint"] != request_fingerprint:
                    raise HTTPException(409, _KEY_REUSED)
                answer = repeated_key(existing)
                if answer is not None:
                    return answer
                if not store.reopen_failed_attempt(existing["id"]):
                    raise HTTPException(409, "This pin is already being processed.")
                attempt = store.get_attempt(existing["id"])
            else:
                attempt = None
        else:
            attempt = None

        if attempt is None:
            try:
                attempt = store.create_attempt(
                    account_id=account["id"],
                    provider="pinterest",
                    status="pending",
                    media_type="IMAGE",
                    media_url=str(body.media_url),
                    title=body.title,
                    caption=body.description,
                    idempotency_key=key,
                    request_fingerprint=request_fingerprint,
                )
            except sqlite3.IntegrityError:
                existing = (
                    store.find_attempt_by_key("pinterest", account["id"], key) if key else None
                )
                if existing and existing["request_fingerprint"] == request_fingerprint:
                    answer = repeated_key(existing)
                    if answer is not None:
                        return answer
                    if not store.reopen_failed_attempt(existing["id"]):
                        raise HTTPException(409, "This pin is already being processed.") from None
                    attempt = store.get_attempt(existing["id"])
                else:
                    raise HTTPException(409, "This pin is already being processed.") from None
            except sqlite3.Error:
                raise HTTPException(
                    503,
                    "Could not record this pin safely. Nothing was sent for this request. Try again.",
                ) from None

        try:
            claimed = store.claim_attempt(attempt["id"], expected=("pending",))
        except sqlite3.Error:
            raise HTTPException(
                503,
                "Could not record this pin safely. Nothing was sent for this request. Try again.",
            ) from None

        if not claimed:
            raise HTTPException(409, "This pin is already being processed.")

        # Determine target board
        board_id = body.board_id
        if not board_id:
            try:
                boards = await app.state.providers.list_pinterest_boards(token)
                target_name = (body.board_name or "Relay Pins").strip().lower()
                matched = next((b for b in boards if b.get("name", "").lower() == target_name), None)
                if matched:
                    board_id = matched["id"]
                elif boards:
                    board_id = boards[0]["id"]
                else:
                    new_board = await app.state.providers.create_pinterest_board(
                        token, name=body.board_name or "Relay Pins"
                    )
                    board_id = new_board["id"]
            except Exception as error:
                store.update_attempt(
                    attempt["id"],
                    status="failed",
                    error=f"Could not resolve a board for this pin: {str(error)}",
                )
                raise HTTPException(422, f"Could not resolve board: {str(error)}") from error

        try:
            pin = await app.state.providers.create_pinterest_pin(
                token,
                board_id=board_id,
                title=body.title,
                description=body.description,
                media_url=str(body.media_url),
                link=str(body.link) if body.link else None,
                alt_text=body.alt_text,
            )
        except ProviderError as error:
            if error.kind in {"rate_limited", "auth"}:
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
                error="The pin creation did not complete. Check Pinterest before retrying.",
            )
            raise

        history_error = None
        try:
            store.save_pinterest_history(
                account["id"],
                pin["id"],
                body.title,
                body.description,
                media_name="Pin Image",
                media_type="image/jpeg",
                media_url=str(body.media_url),
                post_url=pin["url"],
                visibility="public",
            )
        except Exception:
            history_error = "The pin was published, but its history entry could not be saved."

        try:
            receipt = store.update_attempt(
                attempt["id"],
                status="published",
                provider_ref=pin["id"],
                provider_url=pin["url"],
                claimed_at=None,
                error=history_error,
            )
        except (StorageError, sqlite3.Error) as error:
            raise StorageError(
                "Pinterest accepted the pin, but Relay could not record it. Check Pinterest before posting again.",
                outcome="unknown",
            ) from error

        return pin | {"attempt": store.public_attempt(receipt)}

    @app.get("/pinterest/pins/{pin_id}", dependencies=protected, tags=["Pinterest"])
    async def get_pin(account_id: str, pin_id: str):
        """Read pin status from Pinterest."""
        account = await valid_account(account_id, "pinterest")
        return await app.state.providers.get_pinterest_pin(account_token(account), pin_id)
