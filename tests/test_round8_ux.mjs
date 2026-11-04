import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";
import test from "node:test";

/* Round-8 UX corrections, exercised against the same pure modules the browser
   loads. No DOM shim is needed: every decision below is a plain function.

   Covered:
   - one channel-readiness predicate for Home, Channels, and the composer
   - one delivery-action decision for "This submission" and "Earlier deliveries"
   - a cooldown that is honoured rather than bypassed
   - Edit a copy as replacement, with exact media resolution
   - plain-language copy for uncorrelated receipts
   - list semantics for Activity rows (source-level, since it is a DOM choice) */

function load(file, sandbox = {}) {
  const context = vm.createContext({
    window: { location: { origin: "https://relay.example" } },
    URL,
    module: { exports: {} },
    ...sandbox,
  });
  context.globalThis = context;
  vm.runInContext(readFileSync(new URL(`../relay/static/app/${file}`, import.meta.url), "utf8"), context);
  return context.module.exports;
}

const flow = load("flow-state.js");
const recovery = load("recovery.js");

const NOW = 1_700_000_000_000;
const past = Math.floor(NOW / 1000) - 3600;
const future = Math.floor(NOW / 1000) + 3600;

function account(overrides = {}) {
  return {
    id: "acct-1",
    provider: "instagram",
    name: "Field Notes Archive",
    expires_at: future,
    has_refresh_token: false,
    credentials_readable: true,
    ...overrides,
  };
}

/* ── 1. Channel readiness: one predicate, three surfaces ───── */

test("a healthy channel is ready on Home, Channels, and the composer", () => {
  const healthy = account();
  const readiness = flow.channelReadiness(healthy, NOW);
  assert.equal(readiness.ready, true);
  assert.equal(readiness.reconnect, false);
  assert.equal(flow.channelStatus(healthy, "home", NOW).label, "Connected");
  assert.equal(flow.channelStatus(healthy, "channels", NOW).label, "Connected");
});

test("an unreadable credential is never Connected, whatever the expiry says", () => {
  const unreadable = account({ credentials_readable: false, expires_at: future });
  const readiness = flow.channelReadiness(unreadable, NOW);
  assert.equal(readiness.ready, false);
  assert.equal(readiness.reason, "unreadable");
  // The exact defect from the review: Home used to say Connected here.
  assert.equal(flow.channelStatus(unreadable, "home", NOW).label, "Reconnect");
  assert.equal(flow.channelStatus(unreadable, "channels", NOW).label, "Reconnect needed");
  assert.equal(flow.channelStatus(unreadable, "home", NOW).tone, "expired");
});

test("an expired Instagram token needs reconnecting on every surface", () => {
  const expired = account({ provider: "instagram", expires_at: past });
  const readiness = flow.channelReadiness(expired, NOW);
  assert.equal(readiness.reconnect, true);
  assert.equal(readiness.reason, "expired");
  assert.equal(flow.channelStatus(expired, "home", NOW).label, "Reconnect");
  assert.equal(flow.channelStatus(expired, "channels", NOW).label, "Reconnect needed");
});

test("an expired YouTube token with a refresh token stays usable", () => {
  // The publish path refreshes YouTube before dispatch, so expiry alone is not
  // a reconnect signal. Forcing a reconnect here would be a false instruction.
  const refreshable = account({ provider: "youtube", expires_at: past, has_refresh_token: true });
  const readiness = flow.channelReadiness(refreshable, NOW);
  assert.equal(readiness.ready, true);
  assert.equal(readiness.reconnect, false);
  assert.equal(readiness.reason, "refreshable");
  assert.equal(flow.channelStatus(refreshable, "home", NOW).label, "Connected");
  assert.equal(flow.channelStatus(refreshable, "channels", NOW).label, "Connected");
});

test("an expired YouTube token without a refresh token is dead", () => {
  const dead = account({ provider: "youtube", expires_at: past, has_refresh_token: false });
  assert.equal(flow.channelReadiness(dead, NOW).reconnect, true);
  assert.equal(flow.channelStatus(dead, "channels", NOW).label, "Reconnect needed");
});

test("an older backend without the readiness fields is not read as broken", () => {
  // `credentials_readable` absent is not `false`; `has_refresh_token` absent is
  // not a missing refresh token.
  const legacy = { id: "a", provider: "youtube", name: "Old", expires_at: past };
  const readiness = flow.channelReadiness(legacy, NOW);
  assert.equal(readiness.unreadable, false);
  assert.equal(readiness.reconnect, false);
  const legacyInstagram = { id: "b", provider: "instagram", name: "Old IG", expires_at: future };
  assert.equal(flow.channelReadiness(legacyInstagram, NOW).reconnect, false);
});

test("Home, Channels, and the composer agree for all four states", () => {
  const cases = [
    account(),
    account({ credentials_readable: false }),
    account({ provider: "instagram", expires_at: past }),
    account({ provider: "youtube", expires_at: past, has_refresh_token: true }),
  ];
  for (const candidate of cases) {
    const home = flow.channelStatus(candidate, "home", NOW);
    const channels = flow.channelStatus(candidate, "channels", NOW);
    const composer = flow.channelReadiness(candidate, NOW);
    // The verdict is one value; only the wording differs by surface.
    assert.equal(home.ready, composer.ready);
    assert.equal(channels.ready, composer.ready);
    assert.equal(home.reconnect, channels.reconnect);
    assert.equal(home.label === "Connected", composer.ready);
    assert.equal(channels.label === "Connected", composer.ready);
  }
});

/* ── 2. One delivery-action decision for both lists ────────── */

const IG = { id: "acct-1", provider: "instagram", name: "Field Notes Archive" };

function attempt(overrides = {}) {
  return {
    id: "attempt-1",
    provider: "instagram",
    account_id: "acct-1",
    status: "failed",
    created_at: 1_700_000_000,
    updated_at: 1_700_000_000,
    ...overrides,
  };
}

test("a failed row and its recovered twin derive the same control", () => {
  const receipt = attempt({
    status: "failed",
    error: "Provider authorization expired or was revoked. Reconnect account.",
    retry_action: { kind: "reconnect", endpoint: null, key: "none", retry_at: null },
  });
  const result = { account: IG, attemptId: "attempt-1", status: "failed" };
  const fromRow = recovery.failedDestinationControls(result, receipt, null);
  const fromList = recovery.receiptAction(receipt);
  assert.equal(fromRow.controls[0].kind, "reconnect");
  assert.equal(fromList.kind, "reconnect");
  // The exact round-7 defect: the row said "Edit a copy" while the list said
  // "Reconnect channel" for the same receipt.
  assert.notEqual(fromRow.controls[0].label, "Edit a copy");
});

test("a 401 failure before the receipt list loads stays uncertain, not a copy", () => {
  const result = { account: IG, attemptId: null, status: "failed", error: "Your session ended." };
  // Loading: the absence of a receipt proves nothing.
  const loading = recovery.failedDestinationControls(result, null, null, { receiptsState: "loading" });
  assert.equal(loading.controls[0].kind, "pending");
  assert.equal(loading.controls[0].disabled, true);
  assert.match(loading.note, /Checking/);
  // A failed read is uncertainty too, never a silent new attempt.
  const failed = recovery.failedDestinationControls(result, null, null, { receiptsState: "failed" });
  assert.equal(failed.controls[0].kind, "check");
  assert.match(failed.note, /still unconfirmed|nothing was resent/i);
  // Only a completed read may fall back to a deliberate copy.
  const loaded = recovery.failedDestinationControls(result, null, null, { receiptsState: "loaded" });
  assert.equal(loaded.controls[0].kind, "edit-copy");
});

test("a receipt stored on the row wins before the list finishes loading", () => {
  const receipt = attempt({
    status: "failed",
    retry_action: { kind: "reconnect", endpoint: null, key: "none", retry_at: null },
  });
  const result = { account: IG, attemptId: null, status: "failed", receipt };
  // No attemptId yet and the list is empty, but the row's own receipt is exact.
  assert.equal(recovery.receiptForResult(result, [], null), receipt);
  const controls = recovery.failedDestinationControls(result, receipt, null, { receiptsState: "loading" });
  assert.equal(controls.controls[0].kind, "reconnect");
});

test("a request key correlates a row whose receipt id was never stored", () => {
  const key = "relay.abc1234.sub1.acct1.create.payload1";
  const submission = {
    id: "sub-1", userId: "u", workspaceId: "w", createdAt: NOW,
    destinations: {
      "acct-1": { provider: "instagram", payloads: {}, keys: { create: key }, state: "sent" },
    },
  };
  const receipt = attempt({ id: "late", request_key: key });
  const result = { account: IG, attemptId: null, status: "failed" };
  assert.equal(recovery.receiptForResult(result, [receipt], submission), receipt);
  // A receipt for another account with the same key never matches.
  const other = attempt({ id: "other", account_id: "acct-2", request_key: key });
  assert.equal(recovery.receiptForResult(result, [other], submission), null);
});

/* ── 3. Cooldown is honoured, not bypassed ─────────────────── */

test("a cooldown row offers a read-only check, never a copy", () => {
  const receipt = attempt({
    status: "failed",
    retry_action: { kind: "wait", endpoint: null, key: "none", retry_at: 1_700_000_900 },
  });
  const result = { account: IG, attemptId: "attempt-1", status: "failed" };
  const controls = recovery.failedDestinationControls(result, receipt, null);
  assert.equal(controls.controls.length, 1);
  assert.equal(controls.controls[0].kind, "wait");
  assert.equal(controls.controls[0].label, "Check availability");
  assert.notEqual(controls.controls[0].kind, "edit-copy");
  assert.match(controls.note, /wait/i);
  assert.match(controls.note, /Try again after \d/);
});

test("the cooldown deadline is computable before and after it passes", () => {
  const receipt = attempt({
    status: "failed",
    retry_action: { kind: "wait", endpoint: null, key: "none", retry_at: 1_700_000_900 },
  });
  const before = recovery.waitStatus(receipt, 1_700_000_000_000);
  assert.equal(before.available, false);
  assert.equal(before.remainingMs, 900_000);
  const after = recovery.waitStatus(receipt, 1_700_000_900_000);
  assert.equal(after.available, true);
  assert.equal(after.remainingMs, 0);
  // A receipt with no deadline is not a wait at all.
  assert.equal(recovery.waitStatus(attempt({ retry_action: null }), NOW).available, true);
});

test("after the deadline the server's own recipe replaces the wait", () => {
  // The server recomputes retry_action on every read: once retry_at has passed
  // it reports a resend, and the same row turns into Try again. No inferred
  // endpoint, no automatic send.
  const expiredWait = attempt({
    status: "failed",
    request_key: "relay.abc1234.sub1.acct1.create.payload1",
    retry_action: { kind: "resend", endpoint: "/instagram/posts", key: "request", retry_at: null },
  });
  assert.equal(recovery.receiptAction(expiredWait).kind, "retry");
  const stillWaiting = attempt({
    status: "failed",
    request_key: "relay.abc1234.sub1.acct1.create.payload1",
    retry_action: { kind: "wait", endpoint: null, key: "none", retry_at: 1_700_000_900 },
  });
  assert.equal(recovery.receiptAction(stillWaiting).kind, "wait");
});

/* ── 4. Edit a copy is replacement, not merge ──────────────── */

const library = [
  { id: "c1", filename: "harbour.mp4", media_url: "https://storage.example/harbour.mp4", content_type: "video/mp4" },
  { id: "c2", filename: "still.jpg", media_url: "https://storage.example/still.jpg", content_type: "image/jpeg" },
  { id: "c3", filename: "duplicate.mp4", media_url: "https://storage.example/dup-a.mp4", content_type: "video/mp4" },
  { id: "c4", filename: "duplicate.mp4", media_url: "https://storage.example/dup-b.mp4", content_type: "video/mp4" },
];

test("a YouTube copy restores title and description into their own fields", () => {
  const receipt = attempt({
    provider: "youtube",
    title: "Harbour at dawn",
    caption: "A quiet morning on the water.",
    media_name: "harbour.mp4",
  });
  const plan = recovery.copyPlan(receipt, library);
  assert.equal(plan.replace, true);
  assert.equal(plan.title, "Harbour at dawn");
  assert.equal(plan.description, "A quiet morning on the water.");
  // The receipt's description never leaks into the Instagram caption.
  assert.equal(plan.caption, "");
  assert.equal(plan.media.item.id, "c1");
  assert.equal(plan.media.matchedBy, "filename");
  assert.equal(plan.complete, true);
});

test("an Instagram copy restores its caption and exact media URL", () => {
  const receipt = attempt({ caption: "The tide came in.", media_url: "https://storage.example/still.jpg" });
  const plan = recovery.copyPlan(receipt, library);
  assert.equal(plan.caption, "The tide came in.");
  assert.equal(plan.title, "");
  assert.equal(plan.media.item.id, "c2");
  assert.equal(plan.media.matchedBy, "url");
});

test("a copy never fills a missing field from the current draft", () => {
  // The receipt records no media and no description. The plan must not invent
  // either from whatever happens to be in the composer.
  const receipt = attempt({ provider: "youtube", title: "Only a title", media_name: null });
  const plan = recovery.copyPlan(receipt, library);
  assert.equal(plan.media.item, null);
  assert.equal(plan.description, "");
  assert.equal(plan.complete, false);
});

test("an exact content id resolves even when the filename is ambiguous", () => {
  const receipt = attempt({
    provider: "youtube",
    title: "Two files, one name",
    media_name: "duplicate.mp4",
    content_id: "c4",
  });
  const plan = recovery.copyPlan(receipt, library);
  assert.equal(plan.media.item.id, "c4");
  assert.equal(plan.media.matchedBy, "id");
  assert.equal(plan.complete, true);
});

test("an ambiguous filename is never silently chosen", () => {
  const receipt = attempt({
    provider: "youtube",
    title: "Two files, one name",
    media_name: "duplicate.mp4",
  });
  const plan = recovery.copyPlan(receipt, library);
  assert.equal(plan.media.item, null);
  assert.equal(plan.media.reason, "ambiguous");
  assert.equal(plan.media.candidates.length, 2);
  assert.equal(plan.complete, false);
  // The notice names the file and asks for a choice instead of promising a copy.
  const notice = recovery.copyNotice(plan);
  assert.match(notice, /Title copied/);
  assert.match(notice, /duplicate\.mp4/);
  assert.match(notice, /Choose/);
  assert.doesNotMatch(notice, /Copied for a new attempt/);
});

test("a missing library file says exactly what to do", () => {
  const receipt = attempt({ provider: "youtube", title: "Gone", media_name: "deleted.mp4" });
  const plan = recovery.copyPlan(receipt, library);
  assert.equal(plan.media.item, null);
  assert.equal(plan.media.reason, "missing");
  const notice = recovery.copyNotice(plan);
  assert.match(notice, /Title copied/);
  assert.match(notice, /deleted\.mp4/);
  assert.match(notice, /upload it again|Choose/);
});

test("a complete copy says so plainly; an empty one promises nothing", () => {
  const complete = recovery.copyPlan(attempt({ media_url: "https://storage.example/still.jpg", caption: "Hi" }), library);
  assert.match(recovery.copyNotice(complete), /Copied for a new attempt/);
  const empty = recovery.copyPlan(attempt({}), library);
  assert.equal(empty.complete, false);
  assert.doesNotMatch(recovery.copyNotice(empty), /Copied for a new attempt/);
});

test("a copy selects only unresolved destinations, never a published one", () => {
  const results = [
    { account: { id: "acct-1", provider: "instagram" }, status: "published" },
    { account: { id: "acct-2", provider: "youtube" }, status: "failed" },
    { account: { id: "acct-3", provider: "instagram" }, status: "unknown" },
  ];
  const selected = recovery.copyDestinations(results, null);
  assert.equal(selected.has("acct-1"), false, "a published destination is never re-sent");
  assert.equal(selected.has("acct-2"), true);
  assert.equal(selected.has("acct-3"), true);
  // No open destination at all: the row's own channel is the only fallback.
  const fallback = recovery.copyDestinations(
    [{ account: { id: "acct-1", provider: "instagram" }, status: "published" }],
    "acct-1",
  );
  assert.deepEqual([...fallback], ["acct-1"]);
});

test("a copy built from the persisted payload is exact and account-scoped", () => {
  const submission = {
    id: "sub-1", userId: "u", workspaceId: "w", createdAt: NOW,
    destinations: {
      "acct-1": {
        provider: "youtube",
        payloads: {
          upload: {
            title: "Harbour at dawn",
            description: "A quiet morning.",
            content_id: "c1",
            filename: "harbour.mp4",
            content_type: "video/mp4",
          },
        },
        keys: {},
        state: "failed",
      },
    },
  };
  const source = recovery.copySourceFor({ submission, accountId: "acct-1" });
  assert.equal(source.title, "Harbour at dawn");
  assert.equal(source.content_id, "c1");
  const plan = recovery.copyPlan(source, library);
  assert.equal(plan.media.item.id, "c1");
  assert.equal(plan.complete, true);
  // A different account has no payload, so no copy is invented for it.
  assert.equal(recovery.copySourceFor({ submission, accountId: "acct-9" }), null);
  // A loaded receipt is authoritative over the persisted payload.
  const receipt = attempt({ provider: "youtube", title: "Server title", media_name: "harbour.mp4" });
  const fromReceipt = recovery.copySourceFor({ receipt, submission, accountId: "acct-1" });
  assert.equal(fromReceipt.title, "Server title");
});

/* ── 5. Uncorrelated-receipt banner is plain language ──────── */

test("the uncorrelated note is short, plain, and keeps the safety facts", () => {
  const one = recovery.unassociatedNote(1);
  const many = recovery.unassociatedNote(3);
  assert.equal(recovery.unassociatedNote(0), "");
  for (const text of [one, many]) {
    assert.match(text, /listed separately/);
    assert.match(text, /nothing was resent/);
    assert.match(text, /can't tell whether/);
    // No ledger vocabulary in the product surface.
    assert.doesNotMatch(text, /exact reference|server last recorded|idempotenc|ledger|correlat/i);
    // Short enough to read at a glance.
    assert.ok(text.length < 220, `note is too long: ${text.length}`);
  }
  assert.match(one, /^This delivery/);
  assert.match(many, /^These deliveries/);
});

/* ── 6. Activity list semantics ────────────────────────────── */

test("Activity rows are generic containers with listitem semantics", () => {
  // The row is a <div role="listitem"> inside the existing role="list" host.
  // <article role="listitem"> is what axe flagged: the role is not allowed on
  // <article>, and dropping the role would leave the list without items.
  const source = readFileSync(new URL("../relay/static/app/history.js", import.meta.url), "utf8");
  assert.match(source, /el\("div", "history-row"\)/);
  assert.match(source, /setAttribute\("role", "listitem"\)/);
  assert.doesNotMatch(source, /el\("article", "history-row"\)/);
  // The draft row and the delivery row share this one builder, so both get it.
  assert.equal((source.match(/function activityRow/g) || []).length, 1);
});
