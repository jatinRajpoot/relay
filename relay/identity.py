"""Account credentials, revocable browser sessions, and workspace ownership."""

import asyncio
import collections
import contextvars
import hashlib
import secrets
import sqlite3
import time
import weakref

from fastapi import HTTPException

from relay.store import digest

SESSION_SECONDS = 8 * 3600
WINDOW_SECONDS = 15 * 60
# Failed attempts allowed per normalized email per window. Unchanged from the previous
# release: the round's job was to stop one peer's failures locking out *other* accounts,
# not to loosen the per-account limit.
EMAIL_LIMIT = 10
DEFAULT_WORKSPACE_NAME = "My workspace"
# Concurrent password hashes allowed per event loop, how many requests may wait for a
# permit, and how long one may wait before being refused. Read when a gate is constructed
# so tests can lower them.
AUTH_WORK_LIMIT = 2
AUTH_WORK_QUEUE_LIMIT = 16
AUTH_WORK_WAIT_SECONDS = 0.25


class _LoopGate:
    """One event loop's admission state: free permits and the queue waiting for one.

    It deliberately holds no reference to its loop. The gate is cached in a weak-keyed
    map, and a value that referenced its own key would keep that entry alive for the
    life of the process. The only loop references here are inside pending waiter
    futures, which are removed as soon as they are resolved or cancelled.
    """

    __slots__ = ("free", "waiters")

    def __init__(self, capacity):
        self.free = capacity
        self.waiters = collections.deque()


class AuthWorkGate:
    """Bound how much password hashing may run at once.

    scrypt is deliberately expensive. Running it on the event loop would stall every
    other request; running it without a bound would let an unauthenticated flood queue
    arbitrary work. Callers take a permit before hashing and run the hash in a worker
    thread. When every permit is busy the request waits briefly and is then refused with
    a retryable 503 rather than joining an unbounded queue.

    **Admission is tied to worker completion, not to the request lifetime.** A plain
    ``async with gate.admit(): await asyncio.to_thread(hash)`` releases the permit when
    the request coroutine unwinds, which happens immediately on client disconnect or
    cancellation — while the thread keeps hashing. Repeatedly cancelling requests would
    then let an attacker run unbounded scrypt work with only ``capacity`` permits
    "held". So the permit is released by the worker's own completion callback, exactly
    once, and a cancelled request simply stops waiting for an answer it cannot deliver.

    The permit bookkeeping is an explicit counter rather than ``asyncio.Semaphore``:
    a semaphore caches a strong reference to its loop, and ``wait_for`` can cancel the
    acquire at the instant a permit is handed over, losing it. Both would be silent —
    the first leaks a gate per loop, the second shrinks capacity permanently. Here a
    permit is either in ``free``, in the hands of exactly one waiter, or held by exactly
    one worker, and every path that abandons a request returns it first.

    The waiter queue is bounded too. A short wait alone does not bound how many requests
    pile up inside it, so ``queue_limit`` refuses at the door once it is full.
    """

    def __init__(
        self,
        capacity=AUTH_WORK_LIMIT,
        wait_seconds=AUTH_WORK_WAIT_SECONDS,
        queue_limit=AUTH_WORK_QUEUE_LIMIT,
    ):
        if capacity < 1:
            raise ValueError("AuthWorkGate capacity must be at least 1.")
        if not wait_seconds > 0:
            raise ValueError("AuthWorkGate wait_seconds must be positive.")
        if queue_limit < 0:
            raise ValueError("AuthWorkGate queue_limit must not be negative.")
        self.capacity = capacity
        self.wait_seconds = wait_seconds
        self.queue_limit = queue_limit
        self._gates = weakref.WeakKeyDictionary()

    def _gate(self):
        loop = asyncio.get_running_loop()
        gate = self._gates.get(loop)
        if gate is None:
            gate = self._gates[loop] = _LoopGate(self.capacity)
        return gate

    @staticmethod
    def _release(gate):
        """Return one permit, handing it straight to the oldest live waiter if any."""
        while gate.waiters:
            waiter = gate.waiters.popleft()
            if not waiter.done():
                # Transferred, not added to ``free``: the permit never becomes visible
                # to a second claimant.
                waiter.set_result(None)
                return
        gate.free += 1

    async def _admit(self, gate):
        """Take one permit, or raise the retryable refusal."""
        if gate.free > 0:
            gate.free -= 1
            return
        if len(gate.waiters) >= self.queue_limit:
            # The wait is already short, but a flood could still grow the queue faster
            # than it drains. Refusing at the door keeps memory and timer count bounded
            # by configuration rather than by how many clients arrive at once.
            self._refuse()
        waiter = asyncio.get_running_loop().create_future()
        gate.waiters.append(waiter)
        try:
            async with asyncio.timeout(self.wait_seconds):
                await waiter
        except BaseException as error:
            if waiter.done() and not waiter.cancelled():
                # The permit was handed over in the same instant this request gave up
                # (or was cancelled). Pass it on; dropping it would shrink capacity for
                # the life of the process.
                self._release(gate)
            if isinstance(error, TimeoutError):
                self._refuse()
            raise
        finally:
            try:
                gate.waiters.remove(waiter)
            except ValueError:
                pass

    @staticmethod
    def _refuse():
        # Deliberately says nothing about the account being attempted.
        raise HTTPException(
            503,
            "Sign-in is busy right now. Try again in a moment.",
            headers={"Retry-After": "1"},
        )

    def available(self):
        """Free permits on the current loop; ``0`` means every permit is doing work.

        Observability for operators and tests. Reading it never perturbs admission.
        """
        return self._gate().free

    async def run(self, function, /, *args, **kwargs):
        """Run ``function(*args)`` in a worker thread while holding one permit.

        The permit is taken before the thread starts and returned by the thread's
        completion, never by this coroutine. If the caller is cancelled, the thread is
        left to finish (it cannot be interrupted safely) and the permit comes back when
        it does — so cancellation can never inflate the bound.
        """
        loop = asyncio.get_running_loop()
        gate = self._gate()
        await self._admit(gate)

        # No await point separates the admission above from the hand-off below, so a
        # cancellation cannot slip in and leave a permit with no owner.
        #
        # The future carries a plain ``(result, error)`` pair rather than an exception:
        # a request cancelled after the worker finished would otherwise leave an
        # unretrieved exception behind. The caller re-raises on its own side.
        future = loop.create_future()
        context = contextvars.copy_context()

        def settle(outcome):
            # Runs on the loop, which owns the future. The permit is returned here and
            # nowhere else, so it is returned exactly once even if nobody is waiting.
            self._release(gate)
            if not future.done():
                future.set_result(outcome)

        def complete(outcome):
            # Runs on the worker thread, which owns neither the loop nor the permit.
            try:
                loop.call_soon_threadsafe(settle, outcome)
            except RuntimeError:
                # The loop closed while this thread was still working. There is no one
                # left to deliver to and its gate is unreachable, so nothing to release.
                pass

        def work():
            try:
                outcome = (context.run(function, *args, **kwargs), None)
            except BaseException as error:  # noqa: BLE001 - re-raised on the caller's side
                outcome = (None, error)
            complete(outcome)

        try:
            loop.run_in_executor(None, work)
        except BaseException:
            # The executor refused the job (shutting down), so no worker owns the permit.
            self._release(gate)
            raise
        result, error = await future
        if error is not None:
            raise error
        return result


def password_hash(password, salt=None):
    salt = salt or secrets.token_hex(16)
    derived = hashlib.scrypt(password.encode(), salt=salt.encode(), n=16384, r=8, p=5)
    return f"{salt}:{derived.hex()}"


class Identity:
    def __init__(self, store):
        self.store = store
        self.dummy_hash = password_hash(secrets.token_urlsafe(32))
        with store.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY, email TEXT NOT NULL UNIQUE,
                    name TEXT NOT NULL, password_hash TEXT NOT NULL, created_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS workspaces (
                    id TEXT PRIMARY KEY, owner_id TEXT NOT NULL REFERENCES users(id),
                    name TEXT NOT NULL, created_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS user_sessions (
                    token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id),
                    workspace_id TEXT NOT NULL REFERENCES workspaces(id), expires_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS auth_attempts (
                    bucket TEXT NOT NULL, created_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS auth_attempt_bucket ON auth_attempts(bucket, created_at);
            """)

    @staticmethod
    def _email(value):
        """Normalize an email the same way the request schema does.

        The throttle bucket, the account lookup and the stored row must agree on one
        spelling, or ``Alex@Example.com`` would spend a different budget than
        ``alex@example.com`` and could never match the stored account. Routes already
        normalize; doing it here as well keeps every caller — including direct ones and
        future ones — on the same bucket.
        """
        return value.strip().lower()

    @staticmethod
    def _email_bucket(email):
        """The one bucket a failure for this normalized email counts against."""
        return digest(f"email:{Identity._email(email)}")

    @staticmethod
    def _buckets(address, email):
        """Return ``(bucket, limit)`` pairs that a failure for this attempt counts against.

        Keyed on the normalized email only, deliberately.

        The previous release also keyed a bucket on the raw peer address. That is wrong
        in this deployment shape: the container is reached through the platform front
        end, uvicorn only honours ``X-Forwarded-For`` from ``FORWARDED_ALLOW_IPS``
        (default ``127.0.0.1``), and that is not the platform proxy — so
        ``request.client.host`` is one constant value for every request. A peer bucket
        is then a single global bucket: thirty failures against thirty different emails
        locked every user out of the service.

        Trusting the header instead is not an option either (a client could choose its
        own bucket, or frame an arbitrary address), and a peer bucket that is loose
        enough to be safe under a shared peer is loose enough to never bind.

        So the effective control is per account: ten failed attempts per normalized
        email per window, which is exactly the strictness the previous release had.
        ``address`` is accepted for call-site symmetry but does not key the bucket.

        What this deliberately does not do: stop a distributed flood spread across many
        distinct emails. No application-level counter can; that needs an edge rate limit
        or WAF. What bounds the *cost* of such a flood here is the work gate, which caps
        concurrent scrypt hashes and refuses the rest with a retryable 503.
        """
        return ((Identity._email_bucket(email), EMAIL_LIMIT),)

    def throttle(self, address, email):
        """Refuse a new attempt once this email has failed too often recently.

        Only failed attempts are recorded (see ``record_failure``), so signing in
        successfully never consumes the budget. This is called by ``login`` inside the
        bounded worker, immediately before the hash. It is deliberately *not* called by
        the routes: a pre-gate check was a synchronous store round trip on the event
        loop, and it was redundant with this one. Signup does not call it at all — a
        registration is not an authentication attempt against an existing account, so it
        must not consult or spend the login-failure budget (see ``signup_session``).
        """
        now = int(time.time())
        with self.store.connection() as db:
            db.execute("DELETE FROM auth_attempts WHERE created_at<?", (now - WINDOW_SECONDS,))
            for bucket, limit in self._buckets(address, email):
                count = db.execute(
                    "SELECT COUNT(*) FROM auth_attempts WHERE bucket=?", (bucket,)
                ).fetchone()[0]
                if count >= limit:
                    raise HTTPException(429, "Too many attempts. Try again in 15 minutes.")

    def record_failure(self, address, email):
        """Count one failed attempt against its bucket(s)."""
        now = int(time.time())
        with self.store.connection() as db:
            db.executemany(
                "INSERT INTO auth_attempts(bucket, created_at) VALUES (?, ?)",
                [(bucket, now) for bucket, _ in self._buckets(address, email)],
            )

    def signup(self, email, password, name, workspace_name):
        """Create an account and its first workspace.

        This deliberately neither consults nor spends the login-failure budget. A
        registration is not an authentication attempt against an existing account: it
        proves nothing about a password, and charging it would let anyone deny a known
        address its first sign-in for fifteen minutes. The duplicate check below is the
        signup policy, and the bounded work gate — not a counter — bounds its cost. The
        caller runs this whole method in a worker, because every ``store.connection()``
        is a snapshot round trip on a cloud-backed store.
        """
        email = self._email(email)
        user_id, workspace_id = secrets.token_hex(16), secrets.token_hex(16)
        encoded = password_hash(password)
        try:
            with self.store.connection() as db:
                db.execute(
                    "INSERT INTO users VALUES (?, ?, ?, ?, ?)",
                    (user_id, email, name, encoded, int(time.time())),
                )
                db.execute(
                    "INSERT INTO workspaces VALUES (?, ?, ?, ?)",
                    (workspace_id, user_id, workspace_name, int(time.time())),
                )
                # Failures recorded before this address had an account say nothing about
                # this account's password, and would otherwise lock its new owner out of
                # sign-in for the rest of the window. Only a successful insert reaches
                # this line, so a duplicate signup (409, rolled back) can never clear the
                # budget of an existing account.
                db.execute(
                    "DELETE FROM auth_attempts WHERE bucket=?", (self._email_bucket(email),)
                )
        except sqlite3.IntegrityError:
            # A duplicate address is *not* recorded as a failed attempt: it says nothing
            # about the password, and charging it would let anyone lock a known address
            # out of sign-in with ten cheap signup posts.
            raise HTTPException(409, "Unable to create this account. Try logging in instead.") from None
        return user_id, workspace_id

    def login(self, email, password, address=""):
        """Verify a credential and return ``(user_id, workspace_id)``.

        Failure accounting happens **here**, inside the same worker call as the hash,
        not in the HTTP response path. A caller that disconnects or is cancelled after
        the hash has already been computed would otherwise skip the charge entirely and
        could guess passwords without ever consuming the per-email budget. Recording
        before raising makes the count independent of whether anyone is still listening.

        ``address`` is accepted for call-site symmetry with ``throttle``; it does not
        key the bucket (see ``_buckets``).
        """
        email = self._email(email)
        # Re-check the budget here, not only in the route (the route no longer checks at
        # all). A burst of concurrent attempts can all read the same count before any of
        # them records a failure, so the check and the charge are separate transactions
        # with the hash between them. With gate capacity 2 the overshoot is bounded by
        # the number of attempts that can be inside this method at once, which is why
        # the budget is a strong deterrent rather than a hard cap. Serialising the
        # expensive hash under the process-wide store lock to force atomicity would
        # stall every other request, so the bounded overshoot is the deliberate
        # trade-off.
        self.throttle(address, email)
        # Read the row, then release the store lock before hashing. scrypt costs ~0.6s and
        # the connection guard is a process-wide RLock: hashing inside it would block every
        # other request for that whole time, including the loop's own unrelated routes when
        # a cloud snapshot read is waiting on the lock.
        with self.store.connection() as db:
            user = db.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
            encoded = user["password_hash"] if user else self.dummy_hash
        valid = secrets.compare_digest(password_hash(password, encoded.split(":")[0]), encoded)
        if not user or not valid:
            self.record_failure(address, email)
            raise HTTPException(401, "Email or password is incorrect.")
        with self.store.connection() as db:
            # The row is re-read because the lock was released for the hash. If the account
            # vanished in between, repair must not insert a workspace for a missing owner
            # (the foreign key would turn that into a 500).
            if db.execute("SELECT 1 FROM users WHERE id=?", (user["id"],)).fetchone() is None:
                raise HTTPException(401, "Email or password is incorrect.")
            # Only after the password is proven correct, so a wrong password can never
            # create a workspace for an account it did not authenticate.
            workspace = self._workspace_for_login(db, user["id"])
        return user["id"], workspace["id"]

    @staticmethod
    def _workspace_for_login(db, user_id):
        """Return an owned workspace, repairing an account that has none.

        A partial restore (users and sessions present, workspaces missing) used to make
        sign-in raise 500 and left the account with no way back in. Minting one default
        workspace here keeps the session that follows from ever pointing at a missing
        row: the repair is committed before ``new_session`` runs, so the session can
        only ever reference a workspace that already exists. If the process dies in
        between, the account simply has a workspace and no session, and the next sign-in
        reuses it.

        The insert is a single ``INSERT ... SELECT ... WHERE NOT EXISTS`` so two
        concurrent correct logins cannot both create a repair workspace: SQLite holds
        the write lock for the whole statement, and the second one sees the row the
        first committed.
        """
        workspace = db.execute(
            "SELECT id, name FROM workspaces WHERE owner_id=? ORDER BY created_at, id LIMIT 1",
            (user_id,),
        ).fetchone()
        if workspace is not None:
            return workspace
        db.execute(
            """INSERT INTO workspaces(id, owner_id, name, created_at)
               SELECT ?, ?, ?, ?
               WHERE NOT EXISTS (SELECT 1 FROM workspaces WHERE owner_id=?)""",
            (
                secrets.token_hex(16),
                user_id,
                DEFAULT_WORKSPACE_NAME,
                int(time.time()),
                user_id,
            ),
        )
        return db.execute(
            "SELECT id, name FROM workspaces WHERE owner_id=? ORDER BY created_at, id LIMIT 1",
            (user_id,),
        ).fetchone()

    def new_session(self, user_id, workspace_id, previous_token=""):
        token = secrets.token_urlsafe(32)
        with self.store.connection() as db:
            db.execute("DELETE FROM user_sessions WHERE expires_at<=? OR token_hash=?",
                       (int(time.time()), digest(previous_token)))
            db.execute("INSERT INTO user_sessions VALUES (?, ?, ?, ?)",
                       (digest(token), user_id, workspace_id, int(time.time()) + SESSION_SECONDS))
        return token

    def signup_session(self, email, password, name, workspace_name, previous_token=""):
        """Create the account and its first session as one worker-thread operation.

        The route calls this through the bounded gate. Every store access here is a
        snapshot round trip on a cloud-backed store, so none of it may run on the event
        loop. ``signup`` owns the duplicate policy; this method only adds the session
        that the response's cookie will carry.

        If the caller is cancelled after the account is created, the account simply
        exists without a delivered session — the next sign-in or a retry (which gets the
        ordinary duplicate 409) recovers, and no half-built account is visible.
        """
        user_id, workspace_id = self.signup(email, password, name, workspace_name)
        return self.new_session(user_id, workspace_id, previous_token)

    def login_session(self, email, password, address="", previous_token=""):
        """Verify the credential and mint the replacement session in one worker call.

        The session insert is another store round trip, so it belongs in the worker with
        the credential check rather than back on the event loop. A cancelled caller may
        leave a freshly minted session row behind; its token was never delivered to
        anyone and it expires with every other session, so it is not a credential leak.
        """
        user_id, workspace_id = self.login(email, password, address)
        return self.new_session(user_id, workspace_id, previous_token)

    def session(self, token, workspace_id=None, strict=True):
        """Resolve a browser session, optionally pinned to one of the user's workspaces.

        With ``strict`` (the default, used by API routes) an unknown or foreign pin raises
        404. Bootstrap callers pass ``strict=False``: a pin left over from a deleted
        workspace then falls back to the session's own workspace and finally to the user's
        oldest workspace. Every candidate is filtered by ``owner_id``, so the fallback can
        never resolve to another user's workspace.
        """
        if not token:
            return None
        with self.store.connection() as db:
            row = db.execute(
                """SELECT u.id, u.email, u.name, s.workspace_id FROM user_sessions s
                   JOIN users u ON u.id=s.user_id WHERE s.token_hash=? AND s.expires_at>?""",
                (digest(token), int(time.time())),
            ).fetchone()
            if row is None:
                return None
            result = dict(row)
            pinned = workspace_id or result["workspace_id"]
            workspace = db.execute(
                "SELECT id, name FROM workspaces WHERE id=? AND owner_id=?",
                (pinned, result["id"]),
            ).fetchone()
            if workspace is None and not strict:
                # Bootstrap only. A pin naming another user's workspace is tampering and
                # still fails; a pin naming a workspace that no longer exists at all is
                # merely stale, so fall back to the session's own workspace and then to
                # the user's oldest. Every candidate is owner-filtered.
                if db.execute("SELECT 1 FROM workspaces WHERE id=?", (pinned,)).fetchone():
                    raise HTTPException(404, "Workspace not found.")
                workspace = db.execute(
                    "SELECT id, name FROM workspaces WHERE id=? AND owner_id=?",
                    (result["workspace_id"], result["id"]),
                ).fetchone() or db.execute(
                    "SELECT id, name FROM workspaces WHERE owner_id=? "
                    "ORDER BY created_at, id LIMIT 1",
                    (result["id"],),
                ).fetchone()
            if workspace is None:
                raise HTTPException(404, "Workspace not found.")
            result["workspace_id"] = workspace["id"]
            result["workspace"] = dict(workspace)
            return result

    def workspaces(self, user_id):
        with self.store.connection() as db:
            return [dict(row) for row in db.execute(
                "SELECT id, name FROM workspaces WHERE owner_id=? ORDER BY created_at, id",
                (user_id,),
            )]

    def create_workspace(self, user_id, name):
        workspace_id = secrets.token_hex(16)
        with self.store.connection() as db:
            count = db.execute("SELECT COUNT(*) FROM workspaces WHERE owner_id=?",
                               (user_id,)).fetchone()[0]
            if count >= 50:
                raise HTTPException(409, "This account has reached its limit of 50 workspaces.")
            db.execute("INSERT INTO workspaces VALUES (?, ?, ?, ?)",
                       (workspace_id, user_id, name, int(time.time())))
        return {"id": workspace_id, "name": name}

    def select_workspace(self, token, workspace_id):
        # This also checks ownership before changing the browser's selected workspace.
        self.session(token, workspace_id)
        with self.store.connection() as db:
            db.execute("UPDATE user_sessions SET workspace_id=? WHERE token_hash=?",
                       (workspace_id, digest(token)))

    def logout(self, token):
        with self.store.connection() as db:
            db.execute("DELETE FROM user_sessions WHERE token_hash=?", (digest(token),))
