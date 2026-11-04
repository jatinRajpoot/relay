"""Round-8 corrections: signup policy, and auth work off the event loop.

Two things are proved here that the earlier suites could not:

* signup neither consults nor spends the login-failure budget, so ten failed logins for
  an address that does not exist cannot deny its owner their first registration — while
  the login budget for a *known* account is unchanged;
* on a cloud-backed store, where every ``store.connection()`` is a snapshot round trip,
  the login/signup routes perform no store work on the event loop. A fake blob that
  blocks a worker thread is used as a deterministic gate: if the route touched the store
  on the loop, ``/health`` could not answer until the blob was released.

The tests use an isolated temporary database and a fake blob; nothing touches GCS, a
provider, or the network.
"""

import asyncio
import threading
import time

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from google.api_core.exceptions import NotFound, PreconditionFailed

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


def signup(client, email="alex@example.com", password=PASSWORD):
    return client.post("/auth/signup", headers=ORIGIN, json={
        "email": email, "password": password, "name": "Alex", "workspace_name": "Studio",
    })


def login(client, email="alex@example.com", password=PASSWORD):
    return client.post("/auth/login", headers=ORIGIN, json={
        "email": email, "password": password,
    })


# --------------------------------------------------------------------------------------
# Signup policy: registration is not an authentication attempt
# --------------------------------------------------------------------------------------


def test_ten_failed_logins_do_not_block_a_first_time_signup(app_settings):
    """Anyone who knows an address must not be able to deny its registration.

    Ten failures for an address with no account are recorded against the login budget.
    Signup must ignore that budget, and the new account must be immediately usable: the
    stale failures are cleared by the successful insert, because they said nothing about
    this account's password.
    """
    app = create_app(app_settings)
    with TestClient(app) as client:
        for _ in range(10):
            assert login(client, "newuser@example.com", "wrong password").status_code == 401
        # The login budget for that address is now spent...
        assert login(client, "newuser@example.com").status_code == 429

        created = signup(client, "newuser@example.com")
        assert created.status_code == 201, created.text
        assert created.json() == {"authenticated": True}
        assert login(client, "newuser@example.com").status_code == 200

        with app.state.store.connection() as db:
            assert db.execute("SELECT COUNT(*) FROM auth_attempts").fetchone()[0] == 0


def test_signup_does_not_consult_the_login_budget_at_all(app_settings):
    """Even with the budget spent, signup must never answer 429.

    A 429 on signup would be an oracle for "this address has been attacked" and would
    make registration depend on someone else's failures.
    """
    app = create_app(app_settings)
    with TestClient(app) as client:
        for _ in range(10):
            assert login(client, "newuser@example.com", "wrong password").status_code == 401
        response = signup(client, "newuser@example.com")
        assert response.status_code == 201
        # The session cookie from signup is immediately valid.
        assert client.get("/session").json()["authenticated"] is True


def test_known_account_failure_limit_still_works_and_duplicates_do_not_clear_it(app_settings):
    """The login budget is unchanged for an account that exists.

    A duplicate signup is a 409 and is not an authentication attempt: it must not be
    charged, and it must not clear the budget either (only a *successful* insert does).
    """
    app = create_app(app_settings)
    with TestClient(app) as client:
        assert signup(client).status_code == 201
        client.cookies.clear()
        for _ in range(10):
            assert login(client, password="wrong password").status_code == 401
        assert login(client).status_code == 429

        for _ in range(5):
            assert signup(client).status_code == 409
        # Neither charged nor cleared by the duplicate attempts.
        assert login(client).status_code == 429
        with app.state.store.connection() as db:
            assert db.execute("SELECT COUNT(*) FROM auth_attempts").fetchone()[0] == 10


def test_signup_never_counts_a_duplicate_as_a_failure(app_settings):
    """A duplicate signup says nothing about the password, so it is never charged."""
    app = create_app(app_settings)
    with TestClient(app) as client:
        assert signup(client).status_code == 201
        client.cookies.clear()
        for _ in range(12):
            assert signup(client).status_code == 409
        with app.state.store.connection() as db:
            assert db.execute("SELECT COUNT(*) FROM auth_attempts").fetchone()[0] == 0
        assert login(client).status_code == 200


# --------------------------------------------------------------------------------------
# Blocking cloud store: auth work must not run on the event loop
# --------------------------------------------------------------------------------------


class BlockingBlob:
    """A snapshot blob that can hold every store operation in a worker thread.

    ``block`` makes each remote call wait on ``release``. Because the store serialises
    connections with a process-wide lock, a call that ran on the event loop would hold
    that loop for as long as the blob is blocked — which is exactly what ``/health``
    checks for. ``active``/``peak`` record how many remote calls are in flight at once.
    """

    def __init__(self):
        self.generation = 0
        self.data = b""
        self.block = False
        self.release = threading.Event()
        self.entered = threading.Event()
        self.lock = threading.Lock()
        self.active = 0
        self.peak = 0

    def _call(self):
        with self.lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
        try:
            self.entered.set()
            if self.block:
                self.release.wait(10)
        finally:
            with self.lock:
                self.active -= 1

    def reload(self):
        self._call()
        if not self.generation:
            raise NotFound("missing")

    def download_as_bytes(self, if_generation_match):
        self._call()
        assert if_generation_match == self.generation
        return self.data

    def upload_from_string(self, data, content_type, if_generation_match):
        self._call()
        if if_generation_match != self.generation:
            raise PreconditionFailed("stale")
        self.data = data
        self.generation += 1


@pytest.fixture
def cloud_app(app_settings, monkeypatch):
    """An app whose store reads and writes a fake snapshot blob."""
    from google.cloud import storage

    blob = BlockingBlob()
    monkeypatch.setenv("DATABASE_BUCKET", "test-private-bucket")
    monkeypatch.setattr(storage, "Client", lambda: type("Client", (), {
        "bucket": lambda self, name: type("Bucket", (), {
            "blob": lambda self, name: blob,
        })(),
    })())
    assert Store.initialize_cloud_snapshot(app_settings.database_path, app_settings.encryption_key)
    app = create_app(app_settings)
    return app, blob


def _run_health_while_blocked(app, blob, start_request, hold=0.25):
    """Start a request, hold a worker inside the blob, and sample the event loop.

    The signal is a heartbeat task on the loop: it sleeps 10 ms and records the gap
    between wake-ups. If the route performs store work on the loop, the heartbeat stops
    for as long as the blob is blocked (the regression this guards against). A
    watchdog releases the blob after a bounded time so a regression fails an assertion
    instead of hanging the test.
    """

    async def scenario():
        async with app.router.lifespan_context(app), httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=("10.0.0.5", 1)),
            base_url="http://localhost:8000",
        ) as client:
            gaps = []
            stop = asyncio.Event()

            async def heartbeat():
                last = time.monotonic()
                while not stop.is_set():
                    await asyncio.sleep(0.01)
                    now = time.monotonic()
                    gaps.append(now - last)
                    last = now

            beat = asyncio.create_task(heartbeat())
            blob.entered.clear()
            blob.block = True
            watchdog = threading.Timer(5.0, blob.release.set)
            watchdog.start()
            task = asyncio.create_task(start_request(client))
            assert await asyncio.to_thread(blob.entered.wait, 5), (
                "the worker never reached the store"
            )
            # The worker is provably inside the blob now. Hold it there while the
            # loop is expected to keep running.
            await asyncio.sleep(hold)
            started = time.monotonic()
            health = await client.get("/health")
            elapsed = time.monotonic() - started
            blob.release.set()
            watchdog.cancel()
            response = await asyncio.wait_for(task, 10)
            stop.set()
            await beat
            return health, elapsed, response, max(gaps)

    return asyncio.run(scenario())


def test_login_does_no_store_work_on_the_event_loop(cloud_app):
    """With a blocked snapshot blob, the loop must keep running.

    The route used to call ``identity.throttle`` synchronously before queueing, which on
    a cloud store is a full snapshot download on the loop. The budget check now runs
    inside the bounded worker, so the loop stays free while the worker waits.
    """
    app, blob = cloud_app
    with TestClient(app) as client:
        assert signup(client).status_code == 201
        client.cookies.clear()

    health, elapsed, response, max_gap = _run_health_while_blocked(
        app, blob,
        lambda client: client.post("/auth/login", headers=ORIGIN, json={
            "email": "alex@example.com", "password": PASSWORD,
        }),
    )

    assert health.status_code == 200
    assert elapsed < 0.5, f"/health waited {elapsed:.2f}s for a blocked worker"
    assert max_gap < 0.5, f"the event loop stalled for {max_gap:.2f}s"
    assert response.status_code == 200, response.text


def test_signup_does_no_store_work_on_the_event_loop(cloud_app):
    """Signup's duplicate check, hash and inserts all run in the bounded worker."""
    app, blob = cloud_app

    health, elapsed, response, max_gap = _run_health_while_blocked(
        app, blob,
        lambda client: client.post("/auth/signup", headers=ORIGIN, json={
            "email": "newuser@example.com", "password": PASSWORD,
            "name": "New", "workspace_name": "Studio",
        }),
    )

    assert health.status_code == 200
    assert elapsed < 0.5, f"/health waited {elapsed:.2f}s for a blocked worker"
    assert max_gap < 0.5, f"the event loop stalled for {max_gap:.2f}s"
    assert response.status_code == 201, response.text


def test_a_blocked_worker_holds_its_permit_and_the_queue_refuses(cloud_app):
    """Admission is bounded while the cloud store is slow, not just while hashing.

    With capacity 1 and no waiter room, a second login is refused with a retryable 503
    while the first is stuck inside the blob. The refusal does no store work and is not
    charged to the budget.
    """
    app, blob = cloud_app
    with TestClient(app) as client:
        assert signup(client).status_code == 201
        client.cookies.clear()

    async def scenario():
        async with app.router.lifespan_context(app):
            app.state.auth_gate = AuthWorkGate(capacity=1, wait_seconds=0.2, queue_limit=0)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app, client=("10.0.0.5", 1)),
                base_url="http://localhost:8000",
            ) as client:
                blob.entered.clear()
                blob.block = True
                first = asyncio.create_task(client.post("/auth/login", headers=ORIGIN, json={
                    "email": "alex@example.com", "password": "wrong password",
                }))
                assert await asyncio.to_thread(blob.entered.wait, 5)
                assert app.state.auth_gate.available() == 0

                second = await client.post("/auth/login", headers=ORIGIN, json={
                    "email": "alex@example.com", "password": "wrong password",
                })
                # The loop is still free to serve an unrelated route.
                assert (await client.get("/health")).status_code == 200

                watchdog = threading.Timer(2.0, blob.release.set)
                watchdog.start()
                first_result = await asyncio.wait_for(first, 10)
                watchdog.cancel()
                blob.release.set()
                return second, first_result

    second, first_result = asyncio.run(scenario())

    assert second.status_code == 503
    assert second.headers["retry-after"] == "1"
    assert "busy" in second.json()["detail"].lower()
    assert first_result.status_code == 401
    # Only the admitted attempt did password work, so only it was charged.
    with app.state.store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM auth_attempts").fetchone()[0] == 1


def test_concurrent_auth_work_stays_within_the_gate_bound(cloud_app):
    """A burst of slow cloud-backed logins must not exceed the configured worker bound.

    The fake blob records how many store calls overlap. Every login runs in a worker, so
    the number of workers inside ``login_session`` must never exceed gate capacity, and
    capacity must be fully restored afterwards.
    """
    app, blob = cloud_app
    with TestClient(app) as client:
        assert signup(client).status_code == 201
        client.cookies.clear()

    real_login_session = Identity.login_session
    state = {"active": 0, "peak": 0}
    lock = threading.Lock()

    def counted_login_session(self, *args, **kwargs):
        with lock:
            state["active"] += 1
            state["peak"] = max(state["peak"], state["active"])
        try:
            time.sleep(0.02)
            return real_login_session(self, *args, **kwargs)
        finally:
            with lock:
                state["active"] -= 1

    Identity.login_session = counted_login_session
    try:
        async def scenario():
            async with app.router.lifespan_context(app):
                gate = AuthWorkGate(capacity=2, wait_seconds=0.05, queue_limit=4)
                app.state.auth_gate = gate
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app, client=("10.0.0.5", 1)),
                    base_url="http://localhost:8000",
                ) as client:
                    responses = await asyncio.gather(*[
                        client.post("/auth/login", headers=ORIGIN, json={
                            "email": "alex@example.com", "password": "wrong password",
                        })
                        for _ in range(12)
                    ])
                    for _ in range(200):
                        if gate.available() == 2:
                            break
                        await asyncio.sleep(0.01)
                    return [response.status_code for response in responses], gate.available()

        statuses, free = asyncio.run(scenario())
    finally:
        Identity.login_session = real_login_session

    assert state["peak"] <= 2, f"{state['peak']} auth workers ran at once"
    assert set(statuses) <= {401, 429, 503}, statuses
    assert free == 2, "capacity was not restored"
    # The store's own serialisation is the only reason the blob never overlapped; the
    # assertion above is about auth workers, which the gate alone bounds.
    assert blob.peak <= 1


def test_login_session_mints_the_session_inside_the_worker(app_settings):
    """The session row is created by the same worker call, not back on the loop.

    This is a direct contract test: the returned token is already in the database when
    ``login_session`` returns, and it replaces the previous token in one transaction.
    """
    store = Store(app_settings.database_path, app_settings.encryption_key)
    identity = Identity(store)
    user_id, workspace_id = identity.signup("alex@example.com", PASSWORD, "Alex", "Studio")
    first = identity.new_session(user_id, workspace_id)

    token = identity.login_session("alex@example.com", PASSWORD, "", first)

    assert token != first
    with store.connection() as db:
        rows = db.execute("SELECT token_hash FROM user_sessions").fetchall()
    assert len(rows) == 1, "the previous session must be replaced"
    assert identity.session(token)["id"] == user_id
    assert identity.session(first) is None


def test_signup_session_creates_the_account_and_its_session_together(app_settings):
    """Signup's account rows and first session are one worker call."""
    store = Store(app_settings.database_path, app_settings.encryption_key)
    identity = Identity(store)

    token = identity.signup_session("Alex@Example.com", PASSWORD, "Alex", "Studio")

    current = identity.session(token)
    assert current["email"] == "alex@example.com"
    assert current["workspace"]["name"] == "Studio"
