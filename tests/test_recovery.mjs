import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";
import test from "node:test";

/* Recovery, drafts, and idempotency decisions, exercised against the same
   recovery.js the browser loads. The only shim is `window`, which safeHttpsUrl
   reads for a base URL. */

const sandbox = {
  window: { location: { origin: "https://relay.example" } },
  // The browser has URL globally; the vm sandbox needs it for safeHttpsUrl.
  URL,
  module: { exports: {} },
};
sandbox.globalThis = sandbox;
vm.createContext(sandbox);
vm.runInContext(readFileSync(new URL("../relay/static/app/recovery.js", import.meta.url), "utf8"), sandbox);
const recovery = sandbox.module.exports;

const {
  normalizeStatus,
  sendFailureState,
  receiptPhase,
  receiptAction,
  reconnectRequired,
  receiptForResult,
  publishKeyForReceipt,
  failedDestinationAction,
  receiptIdentity,
  mergeActivity,
  matchesFilter,
  draftContent,
  destinationKey,
  createSubmission,
  recordDestination,
  planDestination,
  submissionHasUnresolved,
  submissionComplete,
  matchReceipts,
  canLoadMore,
  pageLabel,
  legacyDraftImport,
  previewDraftDecision,
  stableStringify,
  isValidIdempotencyKey,
  safeHttpsUrl,
} = recovery;

const account = "acct-1";
const otherAccount = "acct-2";

function attempt(overrides = {}) {
  return {
    id: "attempt-1",
    provider: "instagram",
    account_id: account,
    status: "processing",
    created_at: 1_700_000_000,
    updated_at: 1_700_000_000,
    ...overrides,
  };
}

function submissionWith(destinations, extra = {}) {
  return {
    id: "sub-1",
    userId: "user-1",
    workspaceId: "ws-1",
    createdAt: 1_700_000_000_000,
    destinations,
    ...extra,
  };
}

/* ── Status projection ─────────────────────────────────────── */

test("an unrecognised receipt status is uncertainty, never success", () => {
  assert.equal(normalizeStatus("published"), "published");
  assert.equal(normalizeStatus("something-new"), "unknown");
  assert.equal(normalizeStatus(undefined), "unknown");
  assert.equal(receiptPhase("something-new"), "unverified");
  assert.equal(receiptPhase("ready"), "action_needed");
});

test("a ready container is not in progress and a failed one is not a verify", () => {
  assert.equal(receiptPhase("ready"), "action_needed");
  assert.notEqual(receiptPhase("ready"), "in_progress");
  assert.equal(receiptPhase("failed"), "failed");
  assert.equal(receiptPhase("unknown"), "unverified");
});

test("a failed receipt is retried only through the server's retry_action", () => {
  const key = "relay.abc1234.sub1.acct1.create.payload1";
  // No retry_action at all: nothing may be inferred from the failed status.
  for (const next of ["retry", "retry_later", "none", undefined]) {
    const action = receiptAction(attempt({ status: "failed", next_action: next }));
    assert.equal(action.kind, "edit_copy", `next_action=${next} without retry_action must not offer a resend`);
  }
  // The server names the exact endpoint and the key to reuse.
  const retry = receiptAction(attempt({
    status: "failed",
    next_action: "retry",
    request_key: key,
    retry_action: { kind: "resend", endpoint: "/instagram/posts", key: "request", retry_at: null },
  }));
  assert.equal(retry.kind, "retry");
  assert.equal(retry.requestKey, key);
  assert.equal(retry.endpoint, "/instagram/posts");
  // A resend that names a key this client does not hold is not offered.
  assert.equal(receiptAction(attempt({
    status: "failed",
    retry_action: { kind: "resend", endpoint: "/instagram/posts", key: "request", retry_at: null },
  })).kind, "edit_copy");
  // A publish retry needs the confirmation key this browser generated.
  const publishRetryAction = {
    kind: "resend", endpoint: "/instagram/posts/post-1/publish", key: "publish", retry_at: null,
  };
  const publishKey = "relay.abc1234.sub1.acct1.publish.payload1";
  assert.equal(receiptAction(attempt({ status: "failed", retry_action: publishRetryAction })).kind, "edit_copy");
  const publishRetry = receiptAction(
    attempt({ status: "failed", retry_action: publishRetryAction }), { publishKey },
  );
  assert.equal(publishRetry.kind, "retry_publish");
  assert.equal(publishRetry.endpoint, "/instagram/posts/post-1/publish");
  // YouTube bytes cannot be replayed from a receipt.
  assert.equal(receiptAction(attempt({
    status: "failed", provider: "youtube", request_key: key,
    retry_action: { kind: "resend", endpoint: "/youtube/videos", key: "request", retry_at: null },
  })).kind, "edit_copy");
  // A cooldown or a credential problem is never a resend, even with a key.
  // A cooldown is its own read-only state: the row says when the wait ends and
  // offers a check that never dispatches, instead of a copy that would bypass it.
  for (const kind of ["wait", "reconnect"]) {
    const action = receiptAction(attempt({
      status: "failed", request_key: key,
      retry_action: { kind, endpoint: null, key: "none", retry_at: 1_700_000_900 },
    }));
    assert.equal(action.kind, kind === "reconnect" ? "reconnect" : "wait");
  }
  // A job that may already be live is a new attempt, not a resend.
  assert.equal(receiptAction(attempt({
    status: "failed", request_key: key,
    retry_action: { kind: "new_attempt", endpoint: null, key: "none", retry_at: null },
  })).kind, "edit_copy");
});

/* ── Classifying a failed send request ─────────────────────── */

test("a refusal from Relay is not a provider verdict and is never called failed", () => {
  // Relay rejected the request before dispatch: a session, an origin, a field.
  for (const status of [400, 401, 403, 404, 413, 415, 422]) {
    const state = sendFailureState({ status });
    assert.equal(state.state, "not_sent", `${status} was not dispatched`);
  }
  // Repeating a session or origin refusal unchanged is safe once it is repaired.
  for (const status of [401, 403]) {
    assert.equal(sendFailureState({ status }).resend, true);
  }
  // A rejected field or file would be refused identically, so it needs an edit.
  for (const status of [400, 404, 413, 415, 422]) {
    assert.equal(sendFailureState({ status }).resend, false, `${status} needs an edit`);
  }
  // A wait gate also dispatched nothing, but repeating it now would be refused.
  assert.deepEqual({ ...sendFailureState({ status: 429 }) }, { state: "not_sent", resend: false });
  // Ambiguous: a receipt may exist and the provider may have acted.
  for (const status of [409, 500, 502, 503, 504, 0]) {
    const state = sendFailureState({ status });
    assert.equal(state.state, "unknown", `${status} must stay uncertain`);
    assert.equal(state.resend, false);
  }
});

test("a storage outcome decides uncertainty, never the status code alone", () => {
  assert.deepEqual({ ...sendFailureState({ status: 503, outcome: "not_applied" }) },
    { state: "not_sent", resend: true });
  assert.deepEqual({ ...sendFailureState({ status: 503, outcome: "unknown" }) },
    { state: "unknown", resend: false });
  // A generic 503 with no stated outcome is not a safe retry.
  assert.equal(sendFailureState({ status: 503 }).state, "unknown");
  // An unrecognised outcome is ignored rather than trusted.
  assert.equal(sendFailureState({ status: 503, outcome: "maybe" }).state, "unknown");
});

test("an unreadable credential offers reconnect, never a resend that must fail", () => {
  const unreadable = attempt({
    status: "failed",
    error: "Stored credentials can't be read. Reconnect this account.",
    request_key: "relay.abc1234.sub1.acct1.create.payload1",
    // The current projection still calls this a resend; the client must not.
    retry_action: { kind: "resend", endpoint: "/instagram/posts", key: "request", retry_at: null },
  });
  assert.equal(reconnectRequired(unreadable), true);
  assert.equal(receiptAction(unreadable).kind, "reconnect");
  assert.equal(failedDestinationAction(
    { account: { id: account, provider: "instagram" }, attemptId: "attempt-1", status: "failed" },
    unreadable,
    null,
  ).kind, "reconnect");
  // An ordinary provider refusal is still a resend.
  const refused = attempt({
    status: "failed",
    error: "Provider request failed (HTTP 400).",
    request_key: "relay.abc1234.sub1.acct1.create.payload1",
    retry_action: { kind: "resend", endpoint: "/instagram/posts", key: "request", retry_at: null },
  });
  assert.equal(reconnectRequired(refused), false);
  assert.equal(receiptAction(refused).kind, "retry");
});

test("an unknown or submitted receipt never offers a resend", () => {
  for (const status of ["unknown", "submitted", "pending", "processing"]) {
    const action = receiptAction(attempt({
      status,
      request_key: "relay.abc1234.sub1.acct1.create.payload1",
      retry_action: { kind: "resend", endpoint: "/instagram/posts", key: "request", retry_at: null },
    }));
    assert.notEqual(action.kind, "retry", `${status} must not be resent`);
    assert.notEqual(action.kind, "retry_publish", `${status} must not be resent`);
  }
});

test("only a ready receipt offers Instagram confirmation", () => {
  assert.equal(receiptAction(attempt({ status: "ready" })).kind, "publish");
  assert.equal(receiptAction(attempt({ status: "processing" })).kind, "check");
  assert.equal(receiptAction(attempt({ status: "unknown" })).kind, "verify");
  assert.equal(receiptAction(attempt({ status: "published" })).kind, "open");
});

/* ── The submission row's own receipt ──────────────────────── */

test("a submission row borrows a retry only from its own stored receipt", () => {
  const result = { account: { id: account, provider: "instagram" }, attemptId: "attempt-1", status: "failed" };
  const own = attempt({ id: "attempt-1", status: "failed", retry_action: {
    kind: "resend", endpoint: "/instagram/posts", key: "request", retry_at: null,
  }, request_key: "relay.abc1234.sub1.acct1.create.payload1" });
  // A newer receipt on the same account is not this row's receipt.
  const newer = attempt({ id: "attempt-2", created_at: 1_700_000_500 });
  assert.equal(receiptForResult(result, [newer]), null);
  assert.equal(receiptForResult(result, [newer, own]).id, "attempt-1");
  // Without a stored attempt id there is no correlation at all.
  assert.equal(receiptForResult({ ...result, attemptId: null }, [own]), null);
  // An attempt id on a different account never matches.
  assert.equal(receiptForResult(result, [{ ...own, account_id: otherAccount }]), null);

  assert.equal(failedDestinationAction(result, null, null).kind, "edit_copy");
  assert.equal(failedDestinationAction(result, own, null).kind, "retry");
  const waiting = attempt({ id: "attempt-1", status: "failed", retry_action: {
    kind: "wait", endpoint: null, key: "none", retry_at: 1_700_000_900,
  } });
  // A cooldown is honoured: the row offers a read-only availability check, not a
  // copy that would create a ready new send while the wait is still running.
  assert.equal(failedDestinationAction(result, waiting, null).kind, "wait");
  // Before the receipt list has been read, absence of a receipt proves nothing.
  assert.equal(failedDestinationAction(result, null, null, { receiptsState: "loading" }).kind, "unresolved");
  assert.equal(failedDestinationAction(result, null, null, { receiptsState: "failed" }).kind, "unresolved");
  assert.equal(failedDestinationAction(result, null, null, { receiptsState: "loaded" }).kind, "edit_copy");
  const reconnect = attempt({ id: "attempt-1", status: "failed", retry_action: {
    kind: "reconnect", endpoint: null, key: "none", retry_at: null,
  } });
  assert.equal(failedDestinationAction(result, reconnect, null).kind, "reconnect");
});

test("a server-echoed publish key restores an exact confirmation retry", () => {
  const publishRetry = {
    kind: "resend", endpoint: "/instagram/posts/post-1/publish", key: "publish", retry_at: null,
  };
  const receipt = attempt({ status: "failed", retry_action: publishRetry, publish_key: "relay.abc1234.sub1.acct1.publish.payload1" });
  // No local draft: the echoed key is exact, so the retry is offered.
  assert.equal(publishKeyForReceipt(receipt, null), "relay.abc1234.sub1.acct1.publish.payload1");
  assert.equal(receiptAction(receipt, { publishKey: publishKeyForReceipt(receipt, null) }).kind, "retry_publish");
  // The browser's own key wins when both exist.
  const submission = submissionWith({
    [account]: { provider: "instagram", payloads: { create: {} }, keys: { publish: "relay.local.publish.key.0001" } },
  });
  assert.equal(publishKeyForReceipt(receipt, submission), "relay.local.publish.key.0001");
  // An invalid echoed value is not a key.
  assert.equal(publishKeyForReceipt({ ...receipt, publish_key: "short" }, null), null);
  assert.equal(publishKeyForReceipt({ ...receipt, publish_key: null }, null), null);
  // A key echoed for another account is not this destination's key.
  const otherSubmission = submissionWith({
    [otherAccount]: { provider: "instagram", payloads: { create: {} }, keys: { publish: "relay.other.publish.key.0001" } },
  });
  assert.equal(publishKeyForReceipt(receipt, otherSubmission), "relay.abc1234.sub1.acct1.publish.payload1");
});

/* ── Activity rows ─────────────────────────────────────────── */

test("Activity offers every retry control the server names", () => {
  const key = "relay.abc1234.sub1.acct1.create.payload1";
  const rows = mergeActivity({
    attempts: [
      attempt({ id: "a-create", status: "failed", request_key: key, retry_action: {
        kind: "resend", endpoint: "/instagram/posts", key: "request", retry_at: null,
      } }),
      attempt({ id: "a-publish", status: "failed", post_id: "post-1", publish_key: "relay.abc1234.sub1.acct1.publish.payload1", retry_action: {
        kind: "resend", endpoint: "/instagram/posts/post-1/publish", key: "publish", retry_at: null,
      } }),
      attempt({ id: "a-reconnect", status: "failed", retry_action: {
        kind: "reconnect", endpoint: null, key: "none", retry_at: null,
      } }),
      attempt({ id: "a-wait", status: "failed", retry_action: {
        kind: "wait", endpoint: null, key: "none", retry_at: 1_700_000_900,
      } }),
      attempt({ id: "a-unknown", status: "unknown" }),
    ],
  });
  const byId = Object.fromEntries(rows.all.map((row) => [row.id, row.action]));
  assert.equal(byId["a-create"].kind, "retry");
  assert.equal(byId["a-publish"].kind, "retry_publish");
  assert.equal(byId["a-reconnect"].kind, "reconnect");
  // A cooldown stays a cooldown: Activity offers the same read-only check the
  // composer does, never a copy that ignores the wait.
  assert.equal(byId["a-wait"].kind, "wait");
  assert.equal(byId["a-unknown"].kind, "verify");
  // A cooldown says when it ends rather than only that it exists.
  assert.match(byId["a-wait"].note, /Try again after \d/);
});

test("Activity never offers a publish retry without a provable key", () => {
  const rows = mergeActivity({
    attempts: [attempt({ id: "a-publish", status: "failed", post_id: "post-1", retry_action: {
      kind: "resend", endpoint: "/instagram/posts/post-1/publish", key: "publish", retry_at: null,
    } })],
  });
  assert.equal(rows.all[0].action.kind, "edit_copy");
  // The submission's own stored key makes it available again.
  const withKey = mergeActivity({
    attempts: [attempt({ id: "a-publish", status: "failed", post_id: "post-1", retry_action: {
      kind: "resend", endpoint: "/instagram/posts/post-1/publish", key: "publish", retry_at: null,
    } })],
    submission: submissionWith({
      [account]: { provider: "instagram", payloads: { create: {} }, keys: { publish: "relay.abc1234.sub1.acct1.publish.payload1" } },
    }),
  });
  assert.equal(withKey.all[0].action.kind, "retry_publish");
});

test("a verification link is only offered when it is https", () => {
  assert.equal(safeHttpsUrl("http://studio.youtube.com/"), null);
  assert.equal(safeHttpsUrl("javascript:alert(1)"), null);
  assert.equal(safeHttpsUrl("https://studio.youtube.com/"), "https://studio.youtube.com/");
  assert.equal(safeHttpsUrl(null), null);
});

/* ── Activity merge ────────────────────────────────────────── */

test("a receipt and its history row are one delivery, not two", () => {
  const receipt = attempt({
    id: "a1", status: "published", provider_ref: "media-1",
    provider_url: "https://www.instagram.com/p/1/",
  });
  const history = {
    id: "h1", provider: "instagram", account_id: account,
    platform_post_id: "media-1", post_url: "https://www.instagram.com/p/1/",
    title: "Same post", published_at: 1_700_000_050,
  };
  const merged = mergeActivity({ attempts: [receipt], history: [history] });
  assert.equal(merged.rows.length, 1);
  assert.equal(merged.rows[0].source, "receipt");
  assert.equal(merged.rows[0].history, true, "history only enriches the receipt");
});

test("the same platform id on two channels stays two deliveries", () => {
  const merged = mergeActivity({
    attempts: [
      attempt({ id: "a1", account_id: account, provider_ref: "same-id", status: "published" }),
      attempt({ id: "a2", account_id: otherAccount, provider_ref: "same-id", status: "published" }),
    ],
  });
  assert.equal(merged.rows.length, 2);
  assert.notEqual(receiptIdentity({ provider: "instagram", account_id: account, provider_ref: "same-id" }),
    receiptIdentity({ provider: "instagram", account_id: otherAccount, provider_ref: "same-id" }));
});

test("history never invents a failure and never creates a row the server did not record", () => {
  const merged = mergeActivity({ attempts: [], history: [] });
  assert.equal(merged.rows.length, 0);
  assert.equal(merged.counts.failed, 0);
  assert.equal(merged.counts.unverified, 0);
});

test("filters match the real phase, and ready is its own phase", () => {
  const merged = mergeActivity({
    attempts: [
      attempt({ id: "a1", status: "processing" }),
      attempt({ id: "a2", account_id: "acct-2", status: "ready" }),
      attempt({ id: "a3", account_id: "acct-3", status: "failed" }),
      attempt({ id: "a4", account_id: "acct-4", status: "unknown" }),
      attempt({ id: "a5", account_id: "acct-5", status: "published" }),
      attempt({ id: "a6", account_id: "acct-6", status: "expired" }),
    ],
  });
  assert.equal(merged.counts.all, 6);
  assert.equal(merged.counts.published, 1);
  assert.equal(merged.counts.in_progress, 2, "processing and ready both need attention");
  assert.equal(merged.counts.failed, 2, "failed and expired are both definite non-deliveries");
  assert.equal(merged.counts.unverified, 1);
  assert.equal(merged.counts.drafts, 0);
});

test("a contentful draft is a row; an empty one is not", () => {
  const withDraft = mergeActivity({ attempts: [], draft: { caption: "Hello", savedAt: 5 } });
  assert.equal(withDraft.rows.length, 1);
  assert.equal(withDraft.rows[0].source, "draft");
  assert.equal(withDraft.counts.drafts, 1);

  const empty = mergeActivity({ attempts: [], draft: { caption: "   ", selectedAccountIds: ["a"] } });
  assert.equal(empty.rows.length, 0, "a channel selection alone is not a draft");
  assert.equal(empty.counts.drafts, 0);
});

test("an unresolved submission is content even without text", () => {
  const draft = {
    caption: "",
    submission: submissionWith({
      [account]: { provider: "instagram", state: "unknown", payloads: {}, keys: {} },
    }),
  };
  assert.equal(draftContent(draft).hasContent, true);
  assert.equal(draftContent(draft).unresolved, true);
});

test("pagination only offers more when the server returned a cursor", () => {
  assert.equal(canLoadMore({ nextCursor: "abc", loading: false, loaded: true }), true);
  assert.equal(canLoadMore({ nextCursor: null, loading: false, loaded: true }), false);
  assert.equal(canLoadMore({ nextCursor: "abc", loading: true, loaded: true }), false);
  assert.equal(canLoadMore({ nextCursor: "abc", loading: false, loaded: false }), false);
  assert.match(pageLabel({ shown: 50, hasMore: true }), /Load more/);
  assert.match(pageLabel({ shown: 7, hasMore: false }), /all 7 deliveries/);
});

/* ── Idempotency ───────────────────────────────────────────── */

test("the same payload produces the same key; an edit produces a different one", () => {
  const base = { userId: "u", workspaceId: "w", submissionId: "s", accountId: "a", action: "create" };
  const first = destinationKey({ ...base, payload: { caption: "One", media: "m" } });
  const again = destinationKey({ ...base, payload: { media: "m", caption: "One" } });
  const edited = destinationKey({ ...base, payload: { caption: "Two", media: "m" } });
  assert.equal(first, again, "key order must not change the key");
  assert.notEqual(first, edited);
  assert.equal(isValidIdempotencyKey(first), true);
});

test("keys are bound to the user, workspace, account, and action", () => {
  const payload = { caption: "One" };
  const base = { userId: "u", workspaceId: "w", submissionId: "s", accountId: "a", action: "create", payload };
  const variants = [
    destinationKey({ ...base, userId: "u2" }),
    destinationKey({ ...base, workspaceId: "w2" }),
    destinationKey({ ...base, accountId: "a2" }),
    destinationKey({ ...base, action: "publish" }),
    destinationKey({ ...base, submissionId: "s2" }),
  ];
  const keys = new Set([destinationKey(base), ...variants]);
  assert.equal(keys.size, 6);
  for (const key of keys) assert.equal(isValidIdempotencyKey(key), true, key);
});

test("stableStringify is insensitive to key order but sensitive to values", () => {
  assert.equal(stableStringify({ b: 1, a: 2 }), stableStringify({ a: 2, b: 1 }));
  assert.notEqual(stableStringify({ a: 2 }), stableStringify({ a: 3 }));
  assert.equal(stableStringify([1, { b: 2, a: 1 }]), stableStringify([1, { a: 1, b: 2 }]));
});

test("a submission records one key per destination and keeps it across updates", () => {
  const payload = { caption: "One" };
  let submission = createSubmission({
    userId: "u", workspaceId: "w", id: "sub-fixed",
    destinations: [{ accountId: account, provider: "instagram", payload }],
  });
  const firstKey = submission.destinations[account].keys.create;
  submission = recordDestination(submission, {
    accountId: account, provider: "instagram", payload, state: "sent", postId: "post-1",
  });
  assert.equal(submission.destinations[account].keys.create, firstKey, "a status update must not rotate the key");
  assert.equal(submission.destinations[account].state, "sent");
  assert.equal(submission.destinations[account].postId, "post-1");
});

test("a not-sent destination survives a reload only when repeating it is safe", () => {
  const payload = { caption: "One" };
  const base = createSubmission({
    userId: "u", workspaceId: "w", id: "sub-fixed",
    destinations: [{ accountId: account, provider: "instagram", payload }],
  });
  const safe = recordDestination(base, {
    accountId: account, provider: "instagram", payload, state: "not_sent", resend: true,
  });
  assert.equal(safe.destinations[account].resend, true, "a session refusal is repeatable after a reload");
  assert.equal(submissionHasUnresolved(safe), true, "it is still open work");
  // An unsafe refusal carries no resend flag, so a reloaded row offers a copy.
  const unsafe = recordDestination(base, {
    accountId: account, provider: "instagram", payload, state: "not_sent", resend: false,
  });
  assert.equal("resend" in unsafe.destinations[account], false);
  // A later status update clears the stale flag rather than keeping it forever.
  const settled = recordDestination(safe, {
    accountId: account, provider: "instagram", payload, state: "processing",
  });
  assert.equal("resend" in settled.destinations[account], false);
});

test("a changed payload on a resolved destination is visible, not silently resent", () => {  const payload = { caption: "One" };
  const submission = createSubmission({
    userId: "u", workspaceId: "w", id: "sub-fixed",
    destinations: [{ accountId: account, provider: "instagram", payload }],
  });
  const sent = recordDestination(submission, {
    accountId: account, provider: "instagram", payload, state: "sent",
  });
  const plan = planDestination(sent, {
    accountId: account, provider: "instagram", payload: { caption: "Edited" },
  });
  assert.equal(plan.ok, false);
  assert.equal(plan.reason, "unresolved");
  assert.equal(plan.changed, true);
});

test("planDestination refuses every resolved state", () => {
  const payload = { caption: "One" };
  for (const state of ["published", "sent", "unknown", "processing", "ready", "failed", "expired"]) {
    const submission = submissionWith({
      [account]: { provider: "instagram", payloads: { create: payload }, keys: {}, state },
    });
    const plan = planDestination(submission, { accountId: account, provider: "instagram", payload });
    assert.equal(plan.ok, false, `state ${state} must not be sendable`);
  }
  const idle = submissionWith({
    [account]: { provider: "instagram", payloads: { create: payload }, keys: {}, state: "idle" },
  });
  assert.equal(planDestination(idle, { accountId: account, provider: "instagram", payload }).ok, true);
});

test("a destination with no stored intent is sendable once, with a fresh key", () => {
  const submission = submissionWith({});
  const plan = planDestination(submission, {
    accountId: account, provider: "instagram", payload: { caption: "One" },
  });
  assert.equal(plan.ok, true);
  assert.equal(plan.state, "idle");
  assert.equal(isValidIdempotencyKey(plan.key), true);
});

test("completion requires every destination to be published", () => {
  const base = { provider: "instagram", payloads: {}, keys: {} };
  assert.equal(submissionComplete(submissionWith({
    [account]: { ...base, state: "published" },
    [otherAccount]: { ...base, state: "published" },
  })), true);
  assert.equal(submissionComplete(submissionWith({
    [account]: { ...base, state: "published" },
    [otherAccount]: { ...base, state: "ready" },
  })), false);
  assert.equal(submissionComplete(submissionWith({})), false);
});

test("an unresolved destination keeps a draft alive", () => {
  assert.equal(submissionHasUnresolved(submissionWith({
    [account]: { provider: "instagram", state: "unknown", payloads: {}, keys: {} },
  })), true);
  assert.equal(submissionHasUnresolved(submissionWith({
    [account]: { provider: "instagram", state: "published", payloads: {}, keys: {} },
  })), false);
});

/* ── Matching receipts to the submission on screen ─────────── */

test("a receipt is matched to the current submission by its stored reference", () => {
  const submission = submissionWith({
    [account]: { provider: "instagram", state: "sent", attemptId: "attempt-1", payloads: {}, keys: {} },
  });
  const { current, other } = matchReceipts([
    attempt({ id: "attempt-1" }),
    attempt({ id: "attempt-old", account_id: otherAccount, created_at: 1_600_000_000 }),
  ], submission);
  assert.deepEqual([...current.map((row) => row.id)], ["attempt-1"]);
  assert.deepEqual([...other.map((row) => row.id)], ["attempt-old"]);
});

test("an uncorrelated receipt is never adopted from timing alone", () => {
  const submission = submissionWith({
    [account]: { provider: "instagram", state: "sent", attemptId: null, payloads: {}, keys: {} },
  });
  const older = attempt({ id: "old", created_at: 1_600_000_000 });
  // Same account, created after this submission began, but no stored reference
  // and no echoed key: this may be another tab's post, so it must not attach.
  const newerSameAccount = attempt({ id: "new", created_at: 1_700_000_010 });
  const { current, other } = matchReceipts([older, newerSameAccount], submission);
  assert.deepEqual([...current.map((row) => row.id)], []);
  assert.deepEqual([...other.map((row) => row.id)].sort(), ["new", "old"]);
});

test("a server-echoed request key correlates a lost-response receipt", () => {
  const key = "relay.abc1234.sub1.acct1.create.payload1";
  const submission = submissionWith({
    [account]: {
      provider: "instagram", state: "sent", attemptId: null, payloads: {},
      keys: { create: key },
    },
  });
  const correlated = attempt({ id: "new", request_key: key, created_at: 1_700_000_010 });
  const unrelated = attempt({ id: "other-key", request_key: "relay.zzz.other", created_at: 1_700_000_020 });
  const { current, other } = matchReceipts([correlated, unrelated], submission);
  assert.deepEqual([...current.map((row) => row.id)], ["new"]);
  assert.equal(current[0].adopted, true);
  assert.deepEqual([...other.map((row) => row.id)], ["other-key"]);
});

test("a key echoed for a different account or provider never correlates", () => {
  const key = "relay.abc1234.sub1.acct1.create.payload1";
  const submission = submissionWith({
    [account]: { provider: "instagram", state: "sent", attemptId: null, payloads: {}, keys: { create: key } },
  });
  const wrongAccount = attempt({ id: "wrong-account", account_id: otherAccount, request_key: key });
  const wrongProvider = attempt({ id: "wrong-provider", provider: "youtube", request_key: key });
  const { current, other } = matchReceipts([wrongAccount, wrongProvider], submission);
  assert.equal(current.length, 0);
  assert.equal(other.length, 2);
});

test("a resolved destination never adopts a newer receipt for its account", () => {
  const submission = submissionWith({
    [account]: { provider: "instagram", state: "published", attemptId: "attempt-1", payloads: {}, keys: {} },
  });
  const { current, other } = matchReceipts([
    attempt({ id: "attempt-1", status: "published" }),
    attempt({ id: "later", created_at: 1_700_000_900, status: "processing" }),
  ], submission);
  assert.deepEqual([...current.map((row) => row.id)], ["attempt-1"]);
  assert.deepEqual([...other.map((row) => row.id)], ["later"]);
});

test("with no submission on screen, every receipt is earlier work", () => {
  const { current, other } = matchReceipts([attempt({ id: "a" }), attempt({ id: "b" })], null);
  assert.equal(current.length, 0);
  assert.equal(other.length, 2);
});

/* ── Draft upgrade and preview conflict ────────────────────── */

test("a legacy draft is offered only for an owned workspace with no newer draft", () => {
  const ownedKey = "relay:draft:u:w";
  const legacyKey = "relay:draft:w";
  const legacyDraft = { caption: "Older work" };

  assert.equal(legacyDraftImport({ legacyKey, ownedKey, legacyDraft, ownedDraft: null }).offer, true);
  assert.equal(legacyDraftImport({ legacyKey, ownedKey, legacyDraft, ownedDraft: null, dismissed: true }).offer, false);
  assert.equal(legacyDraftImport({ legacyKey, ownedKey, legacyDraft, ownedDraft: { caption: "Newer" } }).offer, false);
  assert.equal(legacyDraftImport({ legacyKey, ownedKey, legacyDraft: { caption: " " }, ownedDraft: null }).offer, false);
  assert.equal(legacyDraftImport({ legacyKey: null, ownedKey, legacyDraft, ownedDraft: null }).offer, false,
    "an unowned legacy key is never offered");
});

test("a preview caption is adopted only when there is no draft of the user's own", () => {
  assert.equal(previewDraftDecision({ previewDraft: { caption: "Preview" }, ownedDraft: null }).action, "adopt");
  assert.equal(previewDraftDecision({ previewDraft: { caption: "Preview" }, ownedDraft: { caption: " " } }).action, "adopt");
  assert.equal(previewDraftDecision({ previewDraft: { caption: "Preview" }, ownedDraft: { caption: "Preview" } }).action, "discard");
  assert.equal(previewDraftDecision({ previewDraft: { caption: "Preview" }, ownedDraft: { caption: "Mine" } }).action, "conflict");
  assert.equal(previewDraftDecision({ previewDraft: { caption: "" }, ownedDraft: null }).action, "discard");
});

/* ── Filter helper ─────────────────────────────────────────── */

test("matchesFilter is exhaustive and never matches an unknown filter", () => {
  const phases = ["published", "in_progress", "action_needed", "failed", "expired", "unverified"];
  for (const phase of phases) {
    const matched = ["published", "in_progress", "failed", "unverified"].filter((filter) =>
      matchesFilter({ phase }, filter));
    assert.equal(matched.length, 1, `${phase} must match exactly one filter`);
  }
  assert.equal(matchesFilter({ phase: "published" }, "drafts"), false);
});
