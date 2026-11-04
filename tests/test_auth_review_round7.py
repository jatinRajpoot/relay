"""Independent round-7 review regressions for the auth domain.

These are deliberately *not* the author's scenarios re-run. Each one targets a state the
shipped suite does not build:

* a randomized cancellation storm that samples the gate's accounting while it runs,
  instead of asserting only the post-storm total;
* an account that disappears between the password read and the credential check, which
  the login repair path claims to survive;
* a direct ``Identity`` caller (no route, no pydantic normalization) using a differently
  cased address;
* an Instagram OAuth state, whose verifier is legitimately ``None``, passing through the
  new unreadable-verifier recovery path.

Nothing here touches the network, a real provider, or a real database.
"""

import asyncio
import random
import threading
import time

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi import HTTPException
from fastapi.testclient import TestClient

from relay.app import create_app
from relay.config import Settings
from relay.identity import AuthWorkGate, Identity
from relay.store import Store

KEY = "test-service-key-with-at-least-32-characters"
ORIGIN = {"Origin": "http://localhost:8000"}
PASSWORD = "a long test password"


@pytest.fixture
def app_settings(tmp_path):
    return Settings(
        api_key=KEY,
        encryption_key=Fernet.generate_key().decode(),
        youtube_client_id="test-google-client",
        youtube_client_secret="test-google-secret",
        instagram_app_id="test-ig-client",
        instagram_app_secret="test-ig-secret",
        database_path=tmp_path / "tokens.sqlite3",
    )


def signup(client, email="alex@example.com"):
    response = client.post("/auth/signup", headers=ORIGIN, json={
        "email": email, "password": PASSWORD, "name": "Alex", "workspace_name": "Studio",
    })
    assert response.status_code == 201, response.text


# --------------------------------------------------------------------------------------
# Gate accounting sampled *during* a randomized storm
# --------------------------------------------------------------------------------------


def test_gate_accounting_holds_at_every_sample_not_only_after_the_storm():
    """A permit is in ``free``, in one waiter's hands, or owned by one worker — always.

    The author's storms are fixed shapes (50 timeouts, 60 cancels). This one interleaves
    random cancels of waiters *and* of permit holders with random completions, and reads
    ``available()`` while the storm is still running, so a transient over-admission or a
    momentarily negative counter is caught rather than averaged away.
    """

    async def scenario():
        gate = AuthWorkGate(capacity=2, wait_seconds=0.03, queue_limit=8)
        lock = threading.Lock()
        state = {"active": 0, "peak": 0}
        samples = []
        stop = threading.Event()

        def work(hold):
            with lock:
                state["active"] += 1
                state["peak"] = max(state["peak"], state["active"])
            stop.wait(hold)
            with lock:
                state["active"] -= 1
            return "done"

        async def sample():
            while not done.is_set():
                free = gate.available()
                with lock:
                    active = state["active"]
                samples.append((free, active))
                # ``free`` is read without the worker lock, so the two reads are not
                # atomic and their sum may transiently look too high. What must never
                # happen is the counter leaving its range (a double release) or a
                # negative value; true over-admission is caught by ``peak`` below,
                # which the workers update under the lock.
                assert 0 <= free <= 2, f"free counter left [0, 2]: {free}"
                await asyncio.sleep(0.002)

        done = asyncio.Event()
        sampler = asyncio.create_task(sample())
        rng = random.Random(20261022)
        tasks = []
        try:
            for _ in range(120):
                task = asyncio.create_task(gate.run(work, rng.uniform(0.0, 0.02)))
                tasks.append(task)
                if rng.random() < 0.35:
                    await asyncio.sleep(rng.uniform(0.0, 0.01))
                    rng.choice(tasks).cancel()
                else:
                    await asyncio.sleep(rng.uniform(0.0, 0.004))
            await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            stop.set()
            done.set()
            await sampler
        # Drain any worker that was still holding a permit when the storm ended.
        for _ in range(300):
            if gate.available() == 2:
                break
            await asyncio.sleep(0.01)
        # The gate must still be usable after the storm, with full capacity.
        assert await gate.run(lambda: "ok") == "ok"
        return gate.available(), state, samples

    free, state, samples = asyncio.run(scenario())

    assert free == 2, "capacity was not fully restored"
    assert state["peak"] <= 2, f"the gate ran {state['peak']} workers at once"
    assert samples, "the sampler never observed the storm"


def test_a_refused_request_never_leaves_a_permit_held():
    """A 503 at the door must not decrement anything, and must not consume a waiter slot."""

    async def scenario():
        gate = AuthWorkGate(capacity=1, wait_seconds=0.01, queue_limit=0)
        release = threading.Event()
        holder = asyncio.create_task(gate.run(lambda: release.wait(10)))
        await asyncio.sleep(0.05)
        for _ in range(5):
            with pytest.raises(HTTPException) as refusal:
                await gate.run(lambda: "never")
            assert refusal.value.status_code == 503
            assert refusal.value.headers["Retry-After"] == "1"
        assert gate.available() == 0
        assert len(gate._gate().waiters) == 0
        release.set()
        await holder
        for _ in range(200):
            if gate.available() == 1:
                break
            await asyncio.sleep(0.01)
        assert gate.available() == 1

    asyncio.run(scenario())


# --------------------------------------------------------------------------------------
# The account disappears between the credential read and the verdict
# --------------------------------------------------------------------------------------


def test_login_fails_cleanly_when_the_account_is_deleted_mid_hash(app_settings):
    """The re-read after hashing must turn a vanished account into a 401, not a 500.

    ``login`` releases the store lock for the hash and then re-reads the user before
    repairing a workspace. If the account is gone by then, the repair must not insert a
    workspace for a missing owner (the foreign key would make that a 500), and the
    caller must see the same generic rejection as any other bad credential.
    """
    import relay.identity as identity_module

    app = create_app(app_settings)
    with TestClient(app) as client:
        signup(client)
        client.cookies.clear()
    with app.state.store.connection() as db:
        db.execute("PRAGMA foreign_keys=OFF")
        db.execute("DELETE FROM workspaces")

    real_hash = identity_module.password_hash

    def hash_then_delete(password, salt=None):
        result = real_hash(password, salt)
        if salt is not None:
            # Only the verification call passes the stored salt; signup does not.
            with app.state.store.connection() as db:
                db.execute("PRAGMA foreign_keys=OFF")
                db.execute("DELETE FROM users WHERE email=?", ("alex@example.com",))
        return result

    identity_module.password_hash = hash_then_delete
    try:
        async def scenario():
            async with app.router.lifespan_context(app):
                transport = httpx.ASGITransport(app=app, client=("10.0.0.5", 1))
                async with httpx.AsyncClient(
                    transport=transport, base_url="http://localhost:8000"
                ) as client:
                    return await client.post("/auth/login", headers=ORIGIN, json={
                        "email": "alex@example.com", "password": PASSWORD,
                    })

        response = asyncio.run(scenario())
    finally:
        identity_module.password_hash = real_hash

    assert response.status_code == 401, response.text
    assert response.json()["detail"] == "Email or password is incorrect."
    with app.state.store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
        # No repair workspace was minted for the account that no longer exists.
        assert db.execute("SELECT COUNT(*) FROM workspaces").fetchone()[0] == 0


# --------------------------------------------------------------------------------------
# Direct Identity callers normalize the address themselves
# --------------------------------------------------------------------------------------


def test_direct_identity_login_normalizes_the_address_without_the_schema(app_settings):
    """Routes normalize through pydantic; ``Identity`` must not depend on that.

    A direct caller (a script, a future route, a test) passing a differently cased
    address must reach the same account and spend the same throttle bucket.
    """
    store = Store(app_settings.database_path, app_settings.encryption_key)
    identity = Identity(store)
    user_id, workspace_id = identity.signup("Alex@Example.com", PASSWORD, "Alex", "Studio")

    assert identity.login("alex@example.com", PASSWORD) == (user_id, workspace_id)
    assert identity.login("  ALEX@EXAMPLE.COM  ", PASSWORD) == (user_id, workspace_id)

    # A wrong password under a different spelling still lands in the one bucket.
    with pytest.raises(HTTPException) as rejected:
        identity.login("AlEx@ExAmPlE.cOm", "wrong password")
    assert rejected.value.status_code == 401
    with store.connection() as db:
        buckets = {row[0] for row in db.execute("SELECT bucket FROM auth_attempts")}
    assert len(buckets) == 1


# --------------------------------------------------------------------------------------
# The unreadable-verifier recovery must not swallow the legitimate ``None`` verifier
# --------------------------------------------------------------------------------------


def test_instagram_state_without_a_verifier_is_still_consumable(app_settings):
    """Instagram has no PKCE, so its verifier is NULL — not unreadable.

    The new ``InvalidToken`` recovery returns ``None`` for an unreadable verifier. A
    NULL verifier must take the ordinary path (``decrypt(None) -> None``) and still
    yield a usable state exactly once, or every Instagram connect would silently expire.
    """
    store = Store(app_settings.database_path, app_settings.encryption_key)
    state = store.create_state("instagram", "browser", None)

    saved = store.consume_state(state, "instagram", "browser")

    assert saved is not None
    assert saved["verifier"] is None
    assert saved["return_to_ui"] is False
    assert saved["popup"] is False
    # Still single use.
    assert store.consume_state(state, "instagram", "browser") is None


def test_unreadable_verifier_is_indistinguishable_from_an_unknown_state(app_settings):
    """The recovery path must not create an oracle: same shape, same timing class.

    Both outcomes are ``None`` from the store and the same 400 from the callback. This
    asserts the store-level contract directly, including that the row is gone either way.
    """
    store = Store(app_settings.database_path, app_settings.encryption_key)
    unreadable = store.create_state("youtube", "browser", "pkce-secret")
    with store.connection() as db:
        db.execute("UPDATE oauth_states SET verifier='not-a-fernet-token'")

    started = time.monotonic()
    assert store.consume_state(unreadable, "youtube", "browser") is None
    assert store.consume_state("never-issued", "youtube", "browser") is None
    # Both were cheap: no scrypt, no provider call, no exception handling by the caller.
    assert time.monotonic() - started < 1.0
    with store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM oauth_states").fetchone()[0] == 0
