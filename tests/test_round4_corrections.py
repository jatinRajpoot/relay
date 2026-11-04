"""Round 4 focused regressions: auth throttle isolation, workspace repair, OAuth verifier.

Each test builds a state the shipped suite does not: a shared proxy peer value, a
database restored without workspaces, a state row whose PKCE verifier cannot be
decrypted, and a saturated password-work gate. They assert observable HTTP/DB
behaviour, never internal structure beyond what the public API exposes.
"""

import asyncio
import gc
import shutil
import subprocess
import sys
import threading
import time
import weakref
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi import HTTPException
from fastapi.testclient import TestClient

from relay.app import create_app
from relay.config import Settings
from relay.identity import Identity
from relay.store import Store, digest

KEY = "test-service-key-with-at-least-32-characters"
HEADERS = {"X-API-Key": KEY}
ORIGIN = {"Origin": "http://localhost:8000"}
PASSWORD = "a long test password"
ROOT = Path(__file__).resolve().parent.parent


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


def signup(client, email="alex@example.com", workspace_name="Studio"):
    response = client.post("/auth/signup", headers=ORIGIN, json={
        "email": email, "password": PASSWORD, "name": "Alex", "workspace_name": workspace_name,
    })
    assert response.status_code == 201, response.text
    return client.get("/session").json()


def login(client, email="alex@example.com", password=PASSWORD):
    return client.post("/auth/login", headers=ORIGIN, json={"email": email, "password": password})


def drop_workspaces(app):
    """Model a database restored without its workspaces (users and sessions survive)."""
    with app.state.store.connection() as db:
        db.execute("PRAGMA foreign_keys=OFF")
        db.execute("DELETE FROM workspaces")


# --------------------------------------------------------------------------------------
# Throttle: a shared peer must not let unrelated failures lock out a legitimate account
# --------------------------------------------------------------------------------------


def test_shared_peer_failures_do_not_lock_out_an_unrelated_account(app_settings):
    """The deployed service sees one proxy peer for every request.

    Thirty failures against thirty *different* emails from that single peer used to fill
    an address-only bucket and 429 every user on the service. They must not.
    """
    app = create_app(app_settings)
    with TestClient(app) as client:
        signup(client)
        client.cookies.clear()
        for index in range(30):
            response = login(client, f"nobody{index}@example.com", "wrong password")
            assert response.status_code == 401, response.text
        # The victim's correct password still works from that same shared peer.
        assert login(client).status_code == 200


def test_targeted_email_failures_still_throttle_that_email(app_settings):
    """Removing the address bucket must not weaken a targeted guessing attack."""
    app = create_app(app_settings)
    with TestClient(app) as client:
        signup(client)
        client.cookies.clear()
        for _ in range(10):
            assert login(client, password="wrong password").status_code == 401
        # The per-email budget is spent: even the correct password is now refused.
        assert login(client).status_code == 429
        # And the refusal says nothing about whether the account exists.
        detail = login(client).json()["detail"]
        assert "alex" not in detail.lower()
        assert "exist" not in detail.lower()


def test_email_cap_survives_address_rotation(app_settings):
    """Rotating peer addresses must not multiply the budget for one email."""
    app = create_app(app_settings)
    with TestClient(app) as client:
        signup(client)
        client.cookies.clear()

    async def scenario():
        async with app.router.lifespan_context(app):
            statuses = []
            for index in range(10):
                # A genuinely different transport peer each time, so only the
                # email-wide bucket can stop the attack.
                transport = httpx.ASGITransport(app=app, client=(f"198.51.100.{index}", 1))
                async with httpx.AsyncClient(
                    transport=transport, base_url="http://localhost:8000"
                ) as client:
                    response = await client.post("/auth/login", headers=ORIGIN, json={
                        "email": "alex@example.com", "password": "wrong password",
                    })
                    statuses.append(response.status_code)
            # The email-wide cap is now spent, from a peer that never failed before.
            transport = httpx.ASGITransport(app=app, client=("203.0.113.250", 1))
            async with httpx.AsyncClient(
                transport=transport, base_url="http://localhost:8000"
            ) as client:
                final = await client.post("/auth/login", headers=ORIGIN, json={
                    "email": "alex@example.com", "password": PASSWORD,
                })
            return statuses, final

    statuses, final = asyncio.run(scenario())

    assert statuses == [401] * 10
    assert final.status_code == 429


def test_successful_logins_never_consume_the_budget(app_settings):
    app = create_app(app_settings)
    with TestClient(app) as client:
        signup(client)
        for _ in range(15):
            assert login(client).status_code == 200


def test_duplicate_signup_cannot_lock_out_a_known_account(app_settings):
    """Ten cheap signup posts must not spend the victim's login budget.

    A 409 means "this address is registered" — it says nothing about the password, so
    charging it to the throttle would be a lockout primitive: anyone who knows an
    address could deny its owner sign-in for fifteen minutes.
    """
    app = create_app(app_settings)
    with TestClient(app) as client:
        signup(client)
        client.cookies.clear()
        for _ in range(12):
            assert client.post("/auth/signup", headers=ORIGIN, json={
                "email": "alex@example.com", "password": PASSWORD, "name": "Alex",
            }).status_code == 409
        # The owner can still sign in; no budget was spent.
        assert login(client).status_code == 200
        with app.state.store.connection() as db:
            assert db.execute("SELECT COUNT(*) FROM auth_attempts").fetchone()[0] == 0


def test_email_throttle_bucket_is_case_and_space_insensitive(app_settings):
    """``Alex@Example.com`` and ``alex@example.com`` must share one budget.

    If the bucket key used the raw input, an attacker could reset the counter by
    varying case, and a user who typed their address differently could never match the
    stored row.
    """
    app = create_app(app_settings)
    with TestClient(app) as client:
        signup(client, "Alex@Example.com")
        client.cookies.clear()
        for variant in ("alex@example.com", "ALEX@EXAMPLE.COM", " Alex@Example.com "):
            assert login(client, email=variant, password="wrong password").status_code == 401
        # Nine more attempts in mixed case exhaust the single shared bucket.
        for index in range(7):
            email = "AlEx@ExAmPlE.cOm" if index % 2 else "alex@example.com"
            assert login(client, email=email, password="wrong password").status_code == 401
        assert login(client, email="ALEX@example.com").status_code == 429


def test_spoofed_forwarded_for_cannot_choose_the_throttle_identity(app_settings):
    """The app keys on the transport peer, never on a client-supplied header.

    Trusting ``X-Forwarded-For`` here would let an attacker mint a fresh bucket per
    request (or frame an arbitrary victim address). The header is ignored entirely.
    """
    app = create_app(app_settings)
    with TestClient(app) as client:
        signup(client)
        client.cookies.clear()
        for index in range(10):
            client.headers["X-Forwarded-For"] = f"198.51.100.{index}"
            assert login(client, password="wrong password").status_code == 401
        # Had the header been trusted, each request would have had its own empty bucket.
        client.headers["X-Forwarded-For"] = "203.0.113.99"
        assert login(client).status_code == 429
        # A spoofed header naming a fresh address does not unlock it either.
        client.headers["X-Forwarded-For"] = "203.0.113.100"
        assert login(client).status_code == 429


# --------------------------------------------------------------------------------------
# Bounded password work: responsiveness and a retryable overload response
# --------------------------------------------------------------------------------------


def test_saturated_gate_refuses_quickly_and_keeps_the_app_responsive(app_settings):
    """A slow verifier must not queue unbounded work or stall unrelated requests."""
    app = create_app(app_settings)
    gate_cls = type(app.state.auth_gate)

    async def scenario():
        async with app.router.lifespan_context(app):
            app.state.auth_gate = gate_cls(capacity=1, wait_seconds=0.05)
            real_login = Identity.login
            entered = threading.Event()
            release = threading.Event()

            def blocking_login(self, email, password, address=""):
                # Stands in for the ~0.6s scrypt call, but controllable. The real login is
                # still used so the response shape and throttle accounting are genuine.
                # A threading.Event is required: this runs in a worker thread.
                entered.set()
                release.wait(5)
                return real_login(self, email, password, address)

            Identity.login = blocking_login
            try:
                transport = httpx.ASGITransport(app=app, client=("10.0.0.5", 1))
                async with httpx.AsyncClient(
                    transport=transport, base_url="http://localhost:8000"
                ) as client:
                    first = asyncio.create_task(client.post(
                        "/auth/login", headers=ORIGIN,
                        json={"email": "a@example.com", "password": "x"},
                    ))
                    assert await asyncio.to_thread(entered.wait, 5)
                    started = time.monotonic()
                    second = await client.post(
                        "/auth/login", headers=ORIGIN,
                        json={"email": "b@example.com", "password": "x"},
                    )
                    elapsed = time.monotonic() - started
                    # The event loop is still serving other routes while the worker is busy.
                    health_started = time.monotonic()
                    health = await client.get("/health")
                    health_elapsed = time.monotonic() - health_started
                    release.set()
                    first_result = await asyncio.wait_for(first, 5)
                    return second, elapsed, health, health_elapsed, first_result
            finally:
                Identity.login = real_login
                release.set()

    second, elapsed, health, health_elapsed, first_result = asyncio.run(scenario())

    assert second.status_code == 503
    assert second.headers["retry-after"] == "1"
    assert "busy" in second.json()["detail"].lower()
    # Refused promptly rather than queued behind the blocked worker.
    assert elapsed < 1.0
    assert health.status_code == 200
    assert health_elapsed < 1.0
    # The admitted request still completed normally once released.
    assert first_result.status_code == 401


def test_the_shipped_gate_defaults_are_actually_bounded(app_settings):
    """The tests above inject a gate; this pins the values production will really use.

    Without this, raising the default capacity or wait to something effectively
    unbounded would leave every other gate test passing.
    """
    app = create_app(app_settings)
    gate = app.state.auth_gate

    assert 1 <= gate.capacity <= 4, "password work must stay narrowly bounded"
    assert gate.wait_seconds <= 1.0, "overload must be refused promptly, not queued"


def test_cancelled_request_keeps_its_slot_until_the_worker_finishes(app_settings):
    """The core admission bug: cancelling must not free capacity while work continues.

    A request that hangs up mid-hash used to release the semaphore as its coroutine
    unwound, while ``asyncio.to_thread`` kept hashing. Repeating that would run unbounded
    scrypt work with only ``capacity`` slots nominally held. The slot must stay taken
    until the worker itself completes, and be released exactly once.
    """
    app = create_app(app_settings)
    gate_cls = type(app.state.auth_gate)

    async def scenario():
        async with app.router.lifespan_context(app):
            gate = gate_cls(capacity=1, wait_seconds=0.05)
            app.state.auth_gate = gate
            entered = threading.Event()
            release = threading.Event()

            def blocking_login(self, email, password, address=""):
                entered.set()
                release.wait(10)
                return ("user", "workspace")

            real_login = Identity.login
            Identity.login = blocking_login
            try:
                transport = httpx.ASGITransport(app=app, client=("10.0.0.5", 1))
                async with httpx.AsyncClient(
                    transport=transport, base_url="http://localhost:8000"
                ) as client:
                    first = asyncio.create_task(client.post(
                        "/auth/login", headers=ORIGIN,
                        json={"email": "a@example.com", "password": "x"},
                    ))
                    assert await asyncio.to_thread(entered.wait, 5)
                    # The client gives up: the request coroutine is cancelled.
                    first.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await first
                    # The worker is still hashing, so the single slot must still be held.
                    assert gate.available() == 0
                    refused = await client.post(
                        "/auth/login", headers=ORIGIN,
                        json={"email": "b@example.com", "password": "x"},
                    )
                    assert refused.status_code == 503
                    # Finish the abandoned worker and let the loop settle its callback.
                    release.set()
                    for _ in range(200):
                        await asyncio.sleep(0.01)
                        if gate.available() == 1:
                            break
                    # Capacity is restored exactly once: one more request can now run.
                    assert gate.available() == 1
                    return refused
            finally:
                Identity.login = real_login
                release.set()

    refused = asyncio.run(scenario())
    assert refused.headers["retry-after"] == "1"


def test_cancelled_worker_releases_exactly_one_slot(app_settings):
    """Repeated cancellation must not leak permits (which would over-admit) or hold them."""
    app = create_app(app_settings)
    gate_cls = type(app.state.auth_gate)

    async def scenario():
        async with app.router.lifespan_context(app):
            gate = gate_cls(capacity=1, wait_seconds=0.05)
            app.state.auth_gate = gate
            entered = threading.Event()
            release = threading.Event()
            calls = []

            def blocking_login(self, email, password, address=""):
                calls.append(email)
                entered.set()
                release.wait(10)
                return ("user", "workspace")

            real_login = Identity.login
            Identity.login = blocking_login
            try:
                transport = httpx.ASGITransport(app=app, client=("10.0.0.5", 1))
                async with httpx.AsyncClient(
                    transport=transport, base_url="http://localhost:8000"
                ) as client:
                    first = asyncio.create_task(client.post(
                        "/auth/login", headers=ORIGIN,
                        json={"email": "a@example.com", "password": "x"},
                    ))
                    assert await asyncio.to_thread(entered.wait, 5)
                    first.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await first
                    release.set()
                    for _ in range(200):
                        await asyncio.sleep(0.01)
                        if gate.available() == 1:
                            break
                    assert gate.available() == 1
                    # Exactly one worker ran: cancellation did not start a second hash.
                    assert calls == ["a@example.com"]
            finally:
                Identity.login = real_login
                release.set()

    asyncio.run(scenario())


def test_worker_failure_after_cancellation_does_not_leak_or_warn(app_settings):
    """A worker raising after its caller is gone must still release the slot."""
    app = create_app(app_settings)
    gate_cls = type(app.state.auth_gate)

    async def scenario():
        async with app.router.lifespan_context(app):
            gate = gate_cls(capacity=1, wait_seconds=0.05)
            app.state.auth_gate = gate
            entered = threading.Event()
            release = threading.Event()

            def failing_login(self, email, password, address=""):
                entered.set()
                release.wait(10)
                raise HTTPException(401, "Email or password is incorrect.")

            real_login = Identity.login
            Identity.login = failing_login
            try:
                transport = httpx.ASGITransport(app=app, client=("10.0.0.5", 1))
                async with httpx.AsyncClient(
                    transport=transport, base_url="http://localhost:8000"
                ) as client:
                    first = asyncio.create_task(client.post(
                        "/auth/login", headers=ORIGIN,
                        json={"email": "a@example.com", "password": "x"},
                    ))
                    assert await asyncio.to_thread(entered.wait, 5)
                    first.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await first
                    release.set()
                    for _ in range(200):
                        await asyncio.sleep(0.01)
                        if gate.available() == 1:
                            break
                    assert gate.available() == 1
            finally:
                Identity.login = real_login
                release.set()

    asyncio.run(scenario())


def test_gate_run_returns_the_value_and_reraises_errors():
    """Direct contract: values pass through, exceptions keep their type, slots return."""
    from relay.identity import AuthWorkGate

    async def scenario():
        gate = AuthWorkGate(capacity=1, wait_seconds=0.5)
        assert await gate.run(lambda: 42) == 42
        assert await gate.run(lambda a, b=0: a + b, 2, b=3) == 5

        def boom():
            raise ValueError("worker failed")

        with pytest.raises(ValueError, match="worker failed"):
            await gate.run(boom)
        # Both slots came back, so the gate is still usable.
        assert await gate.run(lambda: "ok") == "ok"
        assert gate.available() == 1

    asyncio.run(scenario())


def test_gate_never_over_admits_or_leaks_under_a_timeout_storm():
    """Fifty timed-out waiters must not shrink capacity, and must not over-admit.

    A semaphore-based gate can lose a permit when ``wait_for`` cancels the acquire in
    the same tick the permit is handed over. That failure is silent: capacity drops by
    one for the life of the process. The explicit counter must survive the storm.
    """
    from relay.identity import AuthWorkGate

    async def scenario():
        gate = AuthWorkGate(capacity=1, wait_seconds=0.02)
        release = threading.Event()
        state = {"active": 0, "peak": 0}
        lock = threading.Lock()

        def hold():
            with lock:
                state["active"] += 1
                state["peak"] = max(state["peak"], state["active"])
            release.wait(10)
            with lock:
                state["active"] -= 1
            return "held"

        blocker = asyncio.create_task(gate.run(hold))
        await asyncio.sleep(0.05)
        refusals = await asyncio.gather(
            *[gate.run(lambda: "x") for _ in range(50)], return_exceptions=True
        )
        assert all(isinstance(r, HTTPException) for r in refusals)
        assert all(r.status_code == 503 for r in refusals)
        release.set()
        assert await blocker == "held"
        for _ in range(300):
            await asyncio.sleep(0.01)
            if gate.available() == 1:
                break
        assert gate.available() == 1, "a permit leaked under the timeout storm"
        assert state["peak"] == 1, "the gate over-admitted"

    asyncio.run(scenario())


def test_gate_does_not_keep_event_loops_alive():
    """The per-loop cache must be weak, or every test/reload loop is retained forever.

    ``asyncio.Semaphore`` stores a strong reference to its loop, so caching one per loop
    in a weak-keyed map leaks the key too. The gate holds only plain counters.
    """
    from relay.identity import AuthWorkGate

    gate = AuthWorkGate(capacity=1, wait_seconds=0.1)
    refs = []
    for _ in range(3):
        async def main():
            await gate.run(lambda: 1)
            return asyncio.get_running_loop()

        loop = asyncio.run(main())
        refs.append(weakref.ref(loop))
        del loop
    gc.collect()

    assert [ref() for ref in refs] == [None, None, None], "the gate retained an event loop"
    assert len(gate._gates) == 0, "per-loop entries were not released"


def test_gate_rejects_a_degenerate_configuration():
    """Capacity 0 would deadlock every sign-in; a zero wait would refuse every request."""
    from relay.identity import AuthWorkGate

    with pytest.raises(ValueError):
        AuthWorkGate(capacity=0)
    with pytest.raises(ValueError):
        AuthWorkGate(wait_seconds=0)
    with pytest.raises(ValueError):
        AuthWorkGate(queue_limit=-1)


def test_gate_bounds_the_waiter_queue_by_size_not_only_by_time():
    """A short wait does not bound how many requests pile up inside it.

    Without a size limit, a flood grows the queue (and its timers) as fast as clients
    arrive, even though each entry would eventually time out. Past the limit the gate
    must refuse at the door instead of accepting more waiters.
    """
    from relay.identity import AuthWorkGate

    async def scenario():
        gate = AuthWorkGate(capacity=1, wait_seconds=5, queue_limit=2)
        release = threading.Event()
        holder = asyncio.create_task(gate.run(lambda: release.wait(10)))
        await asyncio.sleep(0.05)
        queued = [asyncio.create_task(gate.run(lambda: "queued")) for _ in range(2)]
        await asyncio.sleep(0.05)
        assert len(gate._gate().waiters) == 2

        with pytest.raises(HTTPException) as refusal:
            await gate.run(lambda: "overflow")
        assert refusal.value.status_code == 503
        assert refusal.value.headers["Retry-After"] == "1"
        # The overflow was refused, not queued.
        assert len(gate._gate().waiters) == 2

        release.set()
        await holder
        assert await asyncio.gather(*queued) == ["queued", "queued"]
        for _ in range(200):
            await asyncio.sleep(0.01)
            if gate.available() == 1:
                break
        assert gate.available() == 1
        assert not gate._gate().waiters

    asyncio.run(scenario())


def test_shipped_gate_queue_is_bounded(app_settings):
    """Pin the production queue bound, so it cannot drift to effectively unlimited."""
    from relay.identity import AUTH_WORK_QUEUE_LIMIT

    app = create_app(app_settings)
    gate = app.state.auth_gate

    assert 0 < AUTH_WORK_QUEUE_LIMIT <= 64
    assert gate.queue_limit == AUTH_WORK_QUEUE_LIMIT


def test_cancelled_login_still_charges_the_throttle(app_settings):
    """Cancellation must not be a way to guess passwords for free.

    Failure accounting used to live in the HTTP response path. A client that sent a
    wrong password and hung up before the response was never charged, so it could keep
    guessing without ever spending the per-email budget. The charge now happens inside
    the worker, before the result is delivered (or not delivered) to anyone.

    ``password_hash`` is replaced with a controllable stand-in so the test can cancel
    while the worker is provably mid-hash, instead of racing a real scrypt call.
    """
    import relay.identity as identity_module

    app = create_app(app_settings)
    gate_cls = type(app.state.auth_gate)
    with TestClient(app) as setup:
        signup(setup)

    async def scenario():
        async with app.router.lifespan_context(app):
            app.state.auth_gate = gate_cls(capacity=1, wait_seconds=5)
            entered = threading.Event()
            release = threading.Event()

            def blocking_hash(password, salt=None):
                # Wrong hash, so every attempt is a credential rejection. The wait makes
                # the cancellation point deterministic.
                entered.set()
                release.wait(10)
                return f"{salt or 'salt'}:{'0' * 64}"

            real_hash = identity_module.password_hash
            identity_module.password_hash = blocking_hash
            try:
                transport = httpx.ASGITransport(app=app, client=("10.0.0.5", 1))
                async with httpx.AsyncClient(
                    transport=transport, base_url="http://localhost:8000"
                ) as client:
                    for expected in range(1, 11):
                        entered.clear()
                        release.clear()
                        attempt = asyncio.create_task(client.post(
                            "/auth/login", headers=ORIGIN,
                            json={"email": "alex@example.com", "password": "wrong password"},
                        ))
                        # The worker is inside the hash, and the client gives up.
                        assert await asyncio.to_thread(entered.wait, 5)
                        attempt.cancel()
                        with pytest.raises(asyncio.CancelledError):
                            await attempt
                        # The abandoned worker still finishes and still charges.
                        release.set()
                        for _ in range(200):
                            await asyncio.sleep(0.01)
                            with app.state.store.connection() as db:
                                count = db.execute(
                                    "SELECT COUNT(*) FROM auth_attempts"
                                ).fetchone()[0]
                            if count >= expected:
                                break
                        assert count == expected, f"attempt {expected} was not charged"
                    # The budget is spent by abandoned attempts alone.
                    final = await client.post("/auth/login", headers=ORIGIN, json={
                        "email": "alex@example.com", "password": PASSWORD,
                    })
                    return final
            finally:
                identity_module.password_hash = real_hash
                release.set()

    final = asyncio.run(scenario())
    assert final.status_code == 429, final.text
    with app.state.store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM auth_attempts").fetchone()[0] == 10


def test_overload_refusal_does_not_consume_the_throttle_budget(app_settings):
    """A 503 did no password work, so charging it would let a flood exhaust a victim."""
    app = create_app(app_settings)
    gate_cls = type(app.state.auth_gate)
    with TestClient(app) as setup:
        # A real account, so every refusal below is the only thing that could be charged.
        signup(setup)

    async def scenario():
        async with app.router.lifespan_context(app):
            app.state.auth_gate = gate_cls(capacity=1, wait_seconds=0.01)
            real_login = Identity.login
            entered = threading.Event()
            release = threading.Event()

            def blocking_login(self, email, password, address=""):
                entered.set()
                release.wait(5)
                return real_login(self, email, password, address)

            Identity.login = blocking_login
            try:
                transport = httpx.ASGITransport(app=app, client=("10.0.0.5", 1))
                async with httpx.AsyncClient(
                    transport=transport, base_url="http://localhost:8000"
                ) as client:
                    # The admitted request is a correct login: it must not be charged.
                    first = asyncio.create_task(client.post(
                        "/auth/login", headers=ORIGIN,
                        json={"email": "alex@example.com", "password": PASSWORD},
                    ))
                    assert await asyncio.to_thread(entered.wait, 5)
                    refusals = [
                        (await client.post("/auth/login", headers=ORIGIN, json={
                            "email": "alex@example.com", "password": "wrong password",
                        })).status_code
                        for _ in range(12)
                    ]
                    release.set()
                    admitted = await asyncio.wait_for(first, 5)
                    return refusals, admitted
            finally:
                Identity.login = real_login
                release.set()

    refusals, admitted = asyncio.run(scenario())

    assert set(refusals) == {503}
    assert admitted.status_code == 200
    with app.state.store.connection() as db:
        recorded = db.execute("SELECT COUNT(*) FROM auth_attempts").fetchone()[0]
    # Twelve refusals did no work, so none of them were charged as failures.
    assert recorded == 0


# --------------------------------------------------------------------------------------
# Missing-workspace login repair
# --------------------------------------------------------------------------------------


def test_login_repairs_an_account_with_no_workspace(app_settings):
    """The stale-workspace recovery path had no exit: sign-in itself raised 500."""
    app = create_app(app_settings)
    with TestClient(app) as client:
        signup(client)
        drop_workspaces(app)
        # The shell reports an unauthenticated session rather than 404ing.
        assert client.get("/session").json()["authenticated"] is False
        client.cookies.clear()

        response = login(client)

        assert response.status_code == 200, response.text
        state = client.get("/session").json()
        assert state["authenticated"] is True
        assert state["workspace"]["name"] == "My workspace"
        assert [row["id"] for row in state["workspaces"]] == [state["workspace"]["id"]]
        # The repaired workspace is fully usable, not merely present.
        assert client.get("/accounts").status_code == 200
        assert client.get("/posts").status_code == 200
        assert client.get("/content").status_code == 200


def test_wrong_password_does_not_create_a_workspace(app_settings):
    """Repair happens only after the credential is proven correct."""
    app = create_app(app_settings)
    with TestClient(app) as client:
        signup(client)
        drop_workspaces(app)
        client.cookies.clear()

        assert login(client, password="wrong password").status_code == 401

        with app.state.store.connection() as db:
            assert db.execute("SELECT COUNT(*) FROM workspaces").fetchone()[0] == 0


def test_login_repair_never_attaches_another_users_workspace(app_settings):
    """An account with no workspace gets its own, never somebody else's."""
    app = create_app(app_settings)
    with TestClient(app) as alice, TestClient(app) as bob:
        alice_state = signup(alice)
        bob_state = signup(bob, "bob@example.com")
        # Remove only Alice's workspace; Bob's must remain untouched and unclaimed.
        with app.state.store.connection() as db:
            db.execute("PRAGMA foreign_keys=OFF")
            db.execute(
                "DELETE FROM workspaces WHERE id=?", (alice_state["workspace"]["id"],)
            )
        alice.cookies.clear()

        assert login(alice).status_code == 200

        repaired = alice.get("/session").json()["workspace"]["id"]
        assert repaired != alice_state["workspace"]["id"]
        assert repaired != bob_state["workspace"]["id"]
        # Alice sees only her repaired workspace, never Bob's.
        assert [row["id"] for row in alice.get("/workspaces").json()["workspaces"]] == [repaired]
        assert bob.get("/accounts").status_code == 200
        assert [row["id"] for row in bob.get("/workspaces").json()["workspaces"]] == [
            bob_state["workspace"]["id"]
        ]


def test_concurrent_login_repair_creates_exactly_one_workspace(app_settings):
    """Two correct logins racing on a workspace-less account must not duplicate it."""
    app = create_app(app_settings)
    with TestClient(app) as client:
        signup(client)
        drop_workspaces(app)
        client.cookies.clear()

    signer = Identity(app.state.store)

    def attempt(_):
        try:
            return signer.login("alex@example.com", PASSWORD)
        except HTTPException as error:  # surfaced by the assertion below
            return error

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(attempt, range(6)))

    assert all(isinstance(result, tuple) for result in results), results
    workspace_ids = {result[1] for result in results}
    assert len(workspace_ids) == 1
    with app.state.store.connection() as db:
        rows = db.execute("SELECT id FROM workspaces").fetchall()
    assert len(rows) == 1
    assert rows[0][0] in workspace_ids


def test_repaired_account_can_use_an_owned_resource_end_to_end(app_settings):
    """After repair the session must authorize a real write, not just a read."""
    app = create_app(app_settings)
    with TestClient(app) as client:
        signup(client)
        drop_workspaces(app)
        client.cookies.clear()
        assert login(client).status_code == 200

        created = client.post("/workspaces", headers=ORIGIN, json={"name": "Second"})
        assert created.status_code == 201, created.text
        second = created.json()["id"]
        assert client.post(f"/workspaces/{second}/select", headers=ORIGIN).status_code == 200
        assert client.get("/accounts").status_code == 200
        # Both workspaces belong to the repaired account and the selected pin is honoured.
        listed = {row["id"] for row in client.get("/workspaces").json()["workspaces"]}
        assert len(listed) == 2
        assert second in listed
        assert client.get("/session").json()["workspace"]["id"] == second


# --------------------------------------------------------------------------------------
# Unreadable OAuth PKCE verifier
# --------------------------------------------------------------------------------------


def start_flow(client):
    response = client.post("/oauth/youtube/authorize", headers=HEADERS)
    assert response.status_code == 200, response.text
    return parse_qs(urlsplit(response.json()["authorization_url"]).query)["state"][0]


def corrupt_verifier(app):
    with app.state.store.connection() as db:
        db.execute("UPDATE oauth_states SET verifier='encrypted-under-another-key'")


def test_unreadable_verifier_redirects_an_html_caller_to_expired(app_settings):
    app = create_app(app_settings, httpx.MockTransport(lambda request: httpx.Response(500)))
    with TestClient(app) as client:
        state = start_flow(client)
        corrupt_verifier(app)

        response = client.get(
            "/oauth/youtube/callback", params={"state": state, "code": "x"},
            headers={"accept": "text/html"}, follow_redirects=False,
        )

        assert response.status_code == 303
        assert response.headers["location"] == "/oauth/complete?provider=youtube&result=expired"
        assert "encrypted-under-another-key" not in response.text
        assert "another-key" not in response.text


def test_unreadable_verifier_is_a_400_for_a_json_caller(app_settings):
    app = create_app(app_settings, httpx.MockTransport(lambda request: httpx.Response(500)))
    with TestClient(app) as client:
        state = start_flow(client)
        corrupt_verifier(app)

        response = client.get(
            "/oauth/youtube/callback", params={"state": state, "code": "x"},
            headers={"accept": "application/json"},
        )

        assert response.status_code == 400
        assert "another-key" not in response.text
        assert "Invalid, expired, or reused" in response.json()["detail"]


def test_unreadable_verifier_still_consumes_the_state(app_settings):
    """The row is deleted by the same statement that reads it, so a replay cannot win."""
    app = create_app(app_settings, httpx.MockTransport(lambda request: httpx.Response(500)))
    with TestClient(app) as client:
        state = start_flow(client)
        corrupt_verifier(app)
        assert client.get(
            "/oauth/youtube/callback", params={"state": state, "code": "x"},
            headers={"accept": "application/json"},
        ).status_code == 400

        with app.state.store.connection() as db:
            assert db.execute("SELECT COUNT(*) FROM oauth_states").fetchone()[0] == 0
        # A replay is refused identically, and no account was created.
        assert client.get(
            "/oauth/youtube/callback", params={"state": state, "code": "x"},
            headers={"accept": "application/json"},
        ).status_code == 400
        assert app.state.store.list_accounts() == []


def test_unreadable_verifier_still_requires_the_browser_cookie(app_settings):
    """Recovery must not weaken the browser binding: another browser gets nothing."""
    app = create_app(app_settings, httpx.MockTransport(lambda request: httpx.Response(500)))
    with TestClient(app) as client:
        state = start_flow(client)
        corrupt_verifier(app)
        cookie_name = f"relay_oauth_{digest(state)[:16]}"
        assert client.cookies.get(cookie_name)
        client.cookies.clear()

        response = client.get(
            "/oauth/youtube/callback", params={"state": state, "code": "x"},
            headers={"accept": "text/html"}, follow_redirects=False,
        )

        assert response.status_code == 303
        assert "result=expired" in response.headers["location"]
        # The wrong browser did not consume the state.
        with app.state.store.connection() as db:
            assert db.execute("SELECT COUNT(*) FROM oauth_states").fetchone()[0] == 1


def test_unreadable_verifier_is_not_reachable_without_a_valid_state(app_settings):
    """A garbage state must not be distinguishable from an unreadable one."""
    app = create_app(app_settings, httpx.MockTransport(lambda request: httpx.Response(500)))
    with TestClient(app) as client:
        response = client.get(
            "/oauth/youtube/callback", params={"state": "never-issued", "code": "x"},
            headers={"accept": "application/json"},
        )
        assert response.status_code == 400


def test_consume_state_reports_an_unreadable_verifier_as_absent(app_settings):
    store = Store(app_settings.database_path, app_settings.encryption_key)
    state = store.create_state("youtube", "browser", "pkce-secret")
    with store.connection() as db:
        db.execute("UPDATE oauth_states SET verifier='not-a-fernet-token'")

    assert store.consume_state(state, "youtube", "browser") is None
    # Consumed all the same: single use is preserved.
    assert store.consume_state(state, "youtube", "browser") is None


def test_consume_state_returns_the_verifier_when_it_is_readable(app_settings):
    store = Store(app_settings.database_path, app_settings.encryption_key)
    state = store.create_state("youtube", "browser", "pkce-secret")

    saved = store.consume_state(state, "youtube", "browser")

    assert saved == {"verifier": "pkce-secret", "return_to_ui": False, "popup": False}
    assert store.consume_state(state, "youtube", "browser") is None


# --------------------------------------------------------------------------------------
# CI contract: discovery must cover current and future suites; deploy must be gated.
# --------------------------------------------------------------------------------------

CHECKS = ROOT / ".github/workflows/checks.yml"
DEPLOY = ROOT / ".github/workflows/deploy-cloud-run.yml"


def workflow_text(path):
    return path.read_text(encoding="utf-8")


def job_block(text, job):
    """Return the indented body of a top-level job, without a YAML dependency."""
    lines = text.splitlines()
    start = next(
        index for index, line in enumerate(lines) if line.rstrip() == f"  {job}:"
    )
    body = []
    for line in lines[start + 1:]:
        if line.strip() and not line.startswith("    "):
            break
        body.append(line)
    return "\n".join(body)


def shell_script(text, step_name):
    """Extract a ``run: |`` block from a workflow step, dedented to column zero."""
    lines = text.splitlines()
    start = next(
        index for index, line in enumerate(lines) if line.strip() == f"- name: {step_name}"
    )
    run_at = next(
        index for index in range(start, len(lines)) if lines[index].strip() == "run: |"
    )
    indent = len(lines[run_at]) - len(lines[run_at].lstrip()) + 2
    block = []
    for line in lines[run_at + 1:]:
        if line.strip() and (len(line) - len(line.lstrip())) < indent:
            break
        block.append(line[indent:] if line.strip() else "")
    return "\n".join(block).strip()


BASH_PROBE = (
    "set -euo pipefail\n"
    "mapfile -t rows < <(printf 'a\\nb\\n')\n"
    "[ \"${#rows[@]}\" -eq 2 ]\n"
    "[ \"$(find . -maxdepth 0 -type d)\" = \".\" ]\n"
    "printf 'ok'\n"
)


def _probe_bash(candidate):
    """Return ``candidate`` only if it is really a shell that can run the script.

    Existence is not capability: on Windows ``bash.EXE`` in WindowsApps is a WSL
    launcher stub that exits non-zero and prints nothing for a real script. The probe
    exercises the features the discovery script depends on (``mapfile``, process
    substitution, ``find``) and checks the output, so a stub can never be selected.
    """
    if not candidate or not Path(candidate).is_file():
        return None
    try:
        result = subprocess.run(
            [candidate, "-c", BASH_PROBE], capture_output=True, text=True,
            timeout=15, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return candidate if result.returncode == 0 and result.stdout == "ok" else None


def bash():
    """A bash that can actually run the discovery script.

    Candidates are probed rather than assumed. On Linux CI a working bash is part of
    the contract, so a missing one fails the test instead of skipping it; only platforms
    where bash is genuinely optional may skip, and they say why.
    """
    candidates = [
        shutil.which("bash"),
        r"C:\Program Files\Git\bin\bash.exe",
        r"C:\Program Files\Git\usr\bin\bash.exe",
        "/bin/bash",
        "/usr/bin/bash",
    ]
    seen = []
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.append(candidate)
        working = _probe_bash(candidate)
        if working:
            return working
    if sys.platform.startswith("linux"):
        pytest.fail(f"no functional bash found; probed {seen}")
    pytest.skip(f"no functional bash found (probed {seen}); shell-specific CI tests skipped")


def test_the_bash_probe_rejects_a_binary_that_cannot_run_the_script():
    """A binary that exists but is not a working shell must not be selected.

    ``sys.executable`` is a real executable that cannot run POSIX shell syntax, which
    is the same failure mode as the WindowsApps WSL stub. The selected bash must pass
    the probe and be the one returned.
    """
    assert _probe_bash(sys.executable) is None
    selected = bash()
    assert _probe_bash(selected) == selected


def node():
    """The Node binary, or an explicit skip when it is genuinely absent.

    The Python job on CI runs on an image that ships Node, and the discovery test below
    needs it to prove a helper-importing suite actually executes. If a developer's
    machine has no Node, the test says so rather than silently passing.
    """
    executable = shutil.which("node")
    if executable is None:  # pragma: no cover - CI images ship Node
        pytest.skip("node is required to execute the CI discovery script")
    return executable


def test_node_job_runs_every_mjs_file_including_a_helper_import_suite(tmp_path):
    """The discovery script must run every ``.mjs``, whatever imports what.

    A suite may import ``test`` directly or re-export it from a shared helper. A
    content grep for ``node:test`` would silently skip the helper-importing suite, so
    this builds that exact shape and runs the real script with real Node, then asserts
    the helper-importing suite's own marker appears in the output. The expected set is
    not derived from the script; it is what the fixture contains.
    """
    script = shell_script(workflow_text(CHECKS), "Run Node test suites")
    assert "node --test" in script
    node()  # Skip explicitly if Node is not installed; the script needs it to run.

    workdir = tmp_path / "repo"
    (workdir / "tests" / "support").mkdir(parents=True)
    (workdir / "tests" / "with space").mkdir(parents=True)
    direct = 'import test from "node:test";\ntest("direct", () => {});\n'
    for name in ("test_alpha.mjs", "test_beta.mjs", "test_future_suite.mjs", "polling.mjs"):
        (workdir / "tests" / name).write_text(direct, encoding="utf-8")
    # A helper that re-exports `test`: running it as a file is harmless (zero tests).
    (workdir / "tests" / "support" / "harness.mjs").write_text(
        'import test from "node:test";\nexport { test };\n', encoding="utf-8"
    )
    # A suite that imports `test` only through that helper. A content grep cannot see
    # the `node:test` literal here, so this is the suite that used to disappear.
    (workdir / "tests" / "test_via_helper.mjs").write_text(
        'import { test } from "./support/harness.mjs";\n'
        'test("runs through a shared harness", () => { console.log("HELPER_SUITE_RAN"); });\n',
        encoding="utf-8",
    )
    # A path with spaces must survive discovery and argument passing.
    (workdir / "tests" / "with space" / "test spaced suite.mjs").write_text(
        'import test from "node:test";\n'
        'test("spaced path", () => { console.log("SPACED_SUITE_RAN"); });\n',
        encoding="utf-8",
    )

    result = subprocess.run(
        [bash(), "-c", script], cwd=workdir, capture_output=True, text=True, check=False,
        timeout=120,
    )

    assert result.returncode == 0, result.stderr
    assert "HELPER_SUITE_RAN" in result.stdout, "the helper-import suite did not run"
    assert "SPACED_SUITE_RAN" in result.stdout, "a suite in a path with spaces did not run"
    # Every fixture file was handed to Node, and nothing else was invented.
    listed = [
        line.strip() for line in result.stdout.splitlines()
        if line.strip().startswith("tests/") or line.strip().startswith("tests\\")
    ]
    expected = {
        str(path.relative_to(workdir)).replace("\\", "/")
        for path in (workdir / "tests").rglob("*.mjs")
    }
    assert {line.replace("\\", "/") for line in listed} == expected


def test_node_job_fails_loudly_when_no_suite_matches(tmp_path):
    """A silently empty run (bare ``node --test`` exits 0) must not pass CI."""
    script = shell_script(workflow_text(CHECKS), "Run Node test suites")
    workdir = tmp_path / "repo"
    (workdir / "tests").mkdir(parents=True)

    result = subprocess.run(
        [bash(), "-c", script], cwd=workdir, capture_output=True, text=True, check=False,
        timeout=60,
    )

    assert result.returncode != 0
    assert "No tests/*.mjs files found." in result.stderr


def test_the_repository_suites_are_all_discovered():
    """Every ``.mjs`` under tests/ must be handed to Node by CI.

    The expected set is the repository's actual files, not a re-implementation of the
    discovery rule: any helper or suite in any subdirectory is included, so a new
    suite cannot be silently skipped because of its name or its import style.
    """
    script = shell_script(workflow_text(CHECKS), "Run Node test suites")
    probe = script.replace('node --test "${suites[@]}"', 'printf "%s\\n" "${suites[@]}"')
    result = subprocess.run(
        [bash(), "-c", probe], cwd=ROOT, capture_output=True, text=True, check=False,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    discovered = {
        line.strip().replace("\\", "/")
        for line in result.stdout.splitlines() if line.strip()
    }

    suites = {
        path.relative_to(ROOT).as_posix()
        for path in (ROOT / "tests").rglob("*.mjs")
    }
    assert suites, "no Node suites found in the repository"
    assert discovered == suites, f"CI would not run: {sorted(suites - discovered)}"


def test_deploy_requires_the_full_check_suite_for_the_same_commit():
    text = workflow_text(DEPLOY)
    deploy = job_block(text, "deploy")
    checks = job_block(text, "checks")

    # The deploy job depends on the checks job, inside the same workflow run: no
    # assumption about branch protection, and the same commit is checked and deployed.
    assert "needs: checks" in deploy
    # The checks job is the reusable workflow, not a re-implementation that could drift.
    assert "uses: ./.github/workflows/checks.yml" in checks
    # The deploy job alone holds the OIDC token; the check phase cannot reach it.
    assert "id-token: write" in deploy
    assert "id-token: write" not in checks
    assert "secrets:" not in checks
    # A manual run executes the same jobs, so it is gated too.
    assert "workflow_dispatch:" in text


def test_checks_workflow_is_reusable_and_self_contained():
    text = workflow_text(CHECKS)

    assert "workflow_call:" in text
    # Pull requests and manual runs execute the checks directly.
    assert "pull_request:" in text
    assert "workflow_dispatch:" in text
    # No credentials of any kind in the check phase.
    assert "id-token" not in text
    assert "secrets." not in text
    # Python lint and tests are part of the reusable contract.
    assert "ruff check" in text
    assert "pytest" in text


def top_level_block(text, key):
    """Return the indented body of a top-level mapping key, comments excluded."""
    lines = text.splitlines()
    start = next(
        index for index, line in enumerate(lines) if line.rstrip() == f"{key}:"
    )
    body = []
    for line in lines[start + 1:]:
        if line.strip() and not line.startswith((" ", "\t")):
            break
        if line.lstrip().startswith("#"):
            continue
        body.append(line)
    return "\n".join(body)


def trigger_keys(text):
    """The trigger names declared under ``on:``, without a YAML dependency."""
    return {
        line.strip().rstrip(":")
        for line in top_level_block(text, "on").splitlines()
        if line.strip() and not line.strip().startswith("-")
    }


def test_main_pushes_are_checked_once_through_the_deploy_gate():
    """A main push must not run the same checks twice against the same SHA.

    The deploy workflow calls this file, so a standalone main-push trigger here would
    duplicate every check run on main. Main stays gated because the deploy job needs this
    one.
    """
    checks = workflow_text(CHECKS)
    deploy = workflow_text(DEPLOY)

    assert "push" not in trigger_keys(checks), (
        "main pushes are already checked by the deploy gate"
    )
    assert {"pull_request", "workflow_call", "workflow_dispatch"} == trigger_keys(checks)
    assert "uses: ./.github/workflows/checks.yml" in deploy
    assert "needs: checks" in job_block(deploy, "deploy")


def test_checks_concurrency_cannot_cancel_a_deploy_gate():
    """Cancelling a called checks run would fail a deploy whose checks never ran.

    Cancellation is confined to pull requests; a ``workflow_call`` run (the deploy gate)
    or a manual run is never cancelled by a newer run.
    """
    checks = workflow_text(CHECKS)

    assert "cancel-in-progress: ${{ github.event_name == 'pull_request' }}" in checks
    # Only contexts that are certainly available on every trigger are used.
    assert "inputs." not in checks.split("jobs:")[0]


def test_the_checks_call_uses_only_keywords_github_allows():
    """A job that calls a reusable workflow may not set runs-on/steps/timeout-minutes."""
    checks_job = job_block(workflow_text(DEPLOY), "checks")

    for forbidden in ("runs-on:", "steps:", "timeout-minutes:", "environment:"):
        assert forbidden not in checks_job
    assert "uses: ./.github/workflows/checks.yml" in checks_job
    assert "permissions:" in checks_job


def test_workflows_have_no_recursive_or_duplicate_deploy_trigger():
    deploy = workflow_text(DEPLOY)

    # Triggered by main pushes and manual dispatch only: no schedule, no workflow_run,
    # and no call back into itself.
    assert "workflow_run:" not in deploy
    assert "schedule:" not in deploy
    assert deploy.count("needs:") == 1
    # Exactly one deploy job, so a single push cannot deploy twice.
    assert deploy.count("runs-on: ubuntu-latest") == 1
