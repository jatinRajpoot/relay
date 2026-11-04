"use strict";

/* ── Publishing recovery ────────────────────────────────────────
   Pure decisions for durable receipts, idempotent submissions, and the
   Activity merge. No DOM here, so Node tests exercise the same code the
   browser runs.

   The server owns the outcome. Everything below either mirrors a receipt or
   derives a fact that is provable from the absence of one. Nothing here
   upgrades an uncertain send to a success, and nothing offers a resend for a
   destination that may already have been delivered. */

const RECEIPT_STATUSES = [
  "pending", "submitted", "processing", "ready",
  "published", "failed", "expired", "unknown",
];

/* Statuses that still need a person: work in flight plus outcomes awaiting a
   decision. ``expired`` is included because it needs an edit-a-copy decision,
   even though the server's ``active`` filter treats it as history. */
const OPEN_RECEIPT_STATUSES = [
  "pending", "submitted", "processing", "ready", "failed", "unknown", "expired",
];

/* The server's next_action vocabulary. Anything else is ignored rather than
   guessed at, so a newer backend cannot make the client offer an unsafe action. */
const SAFE_NEXT_ACTIONS = [
  "wait", "check", "publish", "none", "retry", "retry_later", "reconnect", "verify", "edit_copy",
];

const ACTIVITY_FILTERS = ["all", "published", "in_progress", "failed", "unverified", "drafts"];

const FILTER_LABELS = {
  all: "All",
  published: "Published",
  in_progress: "In progress",
  failed: "Failed",
  unverified: "Unverified",
  drafts: "Drafts",
};

/* ── Receipt projection ─────────────────────────────────────── */

function normalizeStatus(status) {
  return RECEIPT_STATUSES.includes(status) ? status : "unknown";
}

/* The phase a person cares about. An unrecognised status is never success. */
function receiptPhase(status) {
  switch (normalizeStatus(status)) {
    case "published": return "published";
    case "failed": return "failed";
    case "expired": return "expired";
    case "unknown": return "unverified";
    case "ready": return "action_needed";
    case "pending":
    case "submitted":
    case "processing": return "in_progress";
    default: return "unverified";
  }
}

function phaseLabel(phase) {
  return {
    published: "Published",
    failed: "Not published",
    expired: "Expired",
    unverified: "Verify",
    action_needed: "Ready · not live",
    in_progress: "In progress",
  }[phase] || "Unknown";
}

/* A receipt's identity for deduplication: the account and provider matter, so
   the same platform id on two channels is two deliveries. */
function receiptIdentity(attempt) {
  if (!attempt) return "";
  const reference = attempt.provider_ref || attempt.post_id || attempt.id || "";
  return `${attempt.provider || "?"}:${attempt.account_id || "?"}:${reference}`;
}

function historyIdentity(post) {
  return `${post.provider || "?"}:${post.account_id || "?"}:${post.platform_post_id || post.id || ""}`;
}

function secondsToMs(value) {
  const number = Number(value);
  return Number.isFinite(number) && number > 0 ? number * 1000 : null;
}

/* When the server says a retry must wait, say until when. A vague "wait" is not
   actionable; the receipt's own retry_at is. */
function retryWaitLabel(attempt) {
  const retry = attempt && attempt.retry_action;
  const at = retry && typeof retry === "object" ? Number(retry.retry_at) : NaN;
  if (!Number.isFinite(at) || at <= 0) return null;
  const when = new Date(at * 1000);
  if (Number.isNaN(when.getTime())) return null;
  return `Try again after ${when.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}`;
}

/* Whether a provider-requested wait has actually elapsed. The server recomputes
   the recipe on every read, so this is used only to phrase the read-only
   "Check availability" result — never to infer a resend endpoint. */
function waitStatus(attempt, now = Date.now()) {
  const retry = attempt && attempt.retry_action;
  const at = retry && typeof retry === "object" ? Number(retry.retry_at) : NaN;
  if (!Number.isFinite(at) || at <= 0) return { available: true, until: null, remainingMs: 0 };
  const until = at * 1000;
  const remainingMs = Math.max(0, until - now);
  return { available: remainingMs === 0, until, remainingMs };
}

/* The one control a receipt should offer.

   A failed destination is retried only through the backend's own projection:
   ``retry_action`` names the exact endpoint and which key to reuse. The client
   never infers a resend from ``status === "failed"``.

   A provider-requested wait is its own state, not a prompt to start a new post:
   the row says when the wait ends and offers a read-only "Check availability"
   that re-reads the receipt. Once the deadline has passed the server's own
   projection changes, so the same read turns into the server's real recipe —
   without a page reload, an inferred endpoint, or an automatic send. Edit a copy
   is deliberately not offered from a wait row: it would produce a ready new
   send that ignores the wait the sentence just described.

   ``publishKey`` is the confirmation key this browser generated. The server
   never echoes it, so a publish retry is offered only when the caller still
   holds it; otherwise the destination falls back to a new attempt. */
function receiptAction(attempt, { publishKey = null } = {}) {
  const status = normalizeStatus(attempt && attempt.status);
  if (status === "published") return { kind: "open", label: "Open post", safe: true };
  if (status === "ready") return { kind: "publish", label: "Confirm on Instagram", safe: true };
  if (status === "processing" || status === "submitted" || status === "pending") {
    return { kind: "check", label: "Check status", safe: true };
  }
  if (status === "unknown") {
    return { kind: "verify", label: "Check provider", safe: true, verificationUrl: safeHttpsUrl(attempt.verification_url) };
  }
  if (status === "expired") {
    return { kind: "edit_copy", label: "Edit a copy", safe: true };
  }
  if (status === "failed") {
    const retry = attempt && attempt.retry_action;
    const kind = retry && typeof retry === "object" ? retry.kind : null;
    // A credential Relay could not read needs reconnecting, whatever the
    // projection says: repeating the request would be refused identically.
    if (reconnectRequired(attempt)) return { kind: "reconnect", label: "Reconnect channel", safe: true };
    if (kind === "reconnect") return { kind: "reconnect", label: "Reconnect channel", safe: true };
    if (kind === "resend") {
      const keyKind = retry.key;
      const requestKey = attempt.request_key;
      const endpoint = retry.endpoint;
      if (keyKind === "request" && isValidIdempotencyKey(requestKey) && endpoint === "/instagram/posts") {
        return { kind: "retry", label: "Try again", safe: true, endpoint, requestKey };
      }
      if (keyKind === "request" && isValidIdempotencyKey(requestKey) && endpoint === "/youtube/videos") {
        // YouTube bytes are not resumable from a receipt, so this client cannot
        // replay the upload it no longer holds. A new attempt is the honest path.
        return {
          kind: "edit_copy",
          label: "Edit a copy",
          safe: true,
          note: "This upload has to be sent again from the start. Edit a copy to choose the file and retry.",
        };
      }
      if (keyKind === "publish" && isValidIdempotencyKey(publishKey) && endpoint &&
          endpoint.startsWith("/instagram/posts/") && endpoint.endsWith("/publish")) {
        return { kind: "retry_publish", label: "Try again", safe: true, endpoint, publishKey };
      }
      return {
        kind: "edit_copy",
        label: "Edit a copy",
        safe: true,
        note: "This browser no longer holds the key for this exact send, so Relay can't repeat it safely. Edit a copy to start a new attempt.",
      };
    }
    if (kind === "wait") {
      // The wait is a real constraint on this destination. The only honest
      // control is a read-only re-read: it shows the same wait until the
      // deadline, then the server's own next recipe.
      return {
        kind: "wait",
        label: "Check availability",
        safe: true,
        retryAt: Number(retry.retry_at) || null,
        note: retryWaitLabel(attempt)
          ? `The provider asked Relay to wait before another send. ${retryWaitLabel(attempt)}.`
          : "The provider asked Relay to wait before another send.",
      };
    }
    return {
      kind: "edit_copy",
      label: "Edit a copy",
      safe: true,
      note: "Sending the same post again is not supported for this destination. Edit a copy to start a new attempt.",
    };
  }
  return { kind: "check", label: "Check status", safe: true };
}

/* What a failed send request actually proves.

   A refusal from Relay itself — an ended session, a wrong origin, a rejected
   field, a missing account — is decided before anything is dispatched, so it is
   not a provider verdict and must never be shown as "the provider rejected this
   destination". Only a provider refusal, or a receipt that says so, may be
   called ``failed``. Everything ambiguous stays ``unknown``: it may have been
   delivered, so it is never resent.

   ``not_sent`` is the client's own state for "Relay did not dispatch this". It
   is not part of the server's receipt vocabulary. ``resend`` says whether the
   same request may be repeated unchanged: true only when the refusal was about
   the request's credentials or origin, or a storage write known not to have
   happened. A rejected field, a wrong file, or a wait would fail identically,
   so those offer a new, editable attempt instead. */
const NOT_SENT_STATUSES = [400, 401, 403, 404, 413, 415, 422];

function sendFailureState(error) {
  const status = (error && Number(error.status)) || 0;
  const outcome = error && error.outcome;
  // A storage failure states its own outcome. "not_applied" means the write is
  // known not to have happened; "unknown" may have applied and stays uncertain.
  if (outcome === "not_applied") return { state: "not_sent", resend: true };
  if (outcome === "unknown") return { state: "unknown", resend: false };
  // 429 is Relay's own wait gate: nothing was dispatched, but sending again now
  // would only be refused again, so no immediate resend is offered.
  if (status === 429) return { state: "not_sent", resend: false };
  if (NOT_SENT_STATUSES.includes(status)) {
    // A session or origin refusal is repaired outside the request, so the same
    // send is safe to repeat. A rejected field or file is not.
    return { state: "not_sent", resend: status === 401 || status === 403 };
  }
  // 409 (already processing / key reused), every 5xx, and transport failures are
  // ambiguous: a receipt may exist and the provider may have acted.
  return { state: "unknown", resend: false };
}

/* Relay's own pre-dispatch refusals that a reconnect clears.

   The server currently reports an unreadable stored credential as a normal
   failed receipt whose ``retry_action`` says "resend", but the endpoint will
   answer that resend with the same 409 forever. Until the projection names
   ``reconnect`` for this case, the receipt's own error text is the only signal
   available; matching it is conservative because the alternative — repeating a
   request that can never succeed — is strictly worse. */
const RECONNECT_REQUIRED = /reconnect (this|the) account|stored credentials can't be read|token expired|authorization expired/i;

function reconnectRequired(attempt) {
  const text = attempt && typeof attempt.error === "string" ? attempt.error : "";
  return RECONNECT_REQUIRED.test(text);
}

/* The controls a failed destination row shows, as plain data.

   This is the single decision both the composer's "This submission" rows and
   the recovered rows use, so the two can never disagree about the same receipt.
   `receiptsState` is what the caller knows about the authoritative list:
   ``loading`` and ``failed`` prove nothing about this destination, so the row
   stays uncertain and offers a read-only check rather than a copy that could
   bypass a cooldown or a reconnect the server is about to name. */
function failedDestinationControls(result, receipt, submission = null, { receiptsState = "loaded" } = {}) {
  const resolved = failedDestinationAction(result, receipt, submission, { receiptsState });
  if (resolved.kind === "retry") {
    return { note: resolved.note, controls: [{ label: "Try again", kind: "retry" }] };
  }
  if (resolved.kind === "reconnect") {
    return {
      note: resolved.note || (receipt && reconnectRequired(receipt)
        ? "Relay can't read this channel's saved authorization. Reconnect it before sending again."
        : null),
      controls: [{ label: "Reconnect channel", kind: "reconnect" }],
    };
  }
  if (resolved.kind === "wait") {
    // The wait is honoured: a read-only re-read shows the same deadline until it
    // passes, then the server's own recipe. Nothing is dispatched by checking.
    return { note: resolved.note, controls: [{ label: "Check availability", kind: "wait" }] };
  }
  if (resolved.kind === "unresolved") {
    return {
      note: resolved.note,
      controls: resolved.pending
        ? [{ label: "Checking…", kind: "pending", disabled: true }]
        : [{ label: "Check status", kind: "check" }],
    };
  }
  return { note: resolved.note, controls: [{ label: "Edit a copy", kind: "edit-copy" }] };
}

/* The note above receipts that cannot be proven to belong to this send.

   The safety property is unchanged; the words are the publisher's, not the
   ledger's. It names what happened to their work (listed separately, nothing
   resent) without internal vocabulary like "exact reference" or "what the
   server last recorded". */
function unassociatedNote(count) {
  if (!count) return "";
  return count === 1
    ? "This delivery was sent from this workspace around the same time. Relay can't tell whether it belongs to this post, so it is listed separately and nothing was resent."
    : "These deliveries were sent from this workspace around the same time. Relay can't tell whether they belong to this post, so they are listed separately and nothing was resent.";
}

/* The exact receipt behind an on-screen destination, when one is loaded.

   Matching is exact only, in two steps:
   - the attempt id this browser recorded for that destination, or
   - the request key this browser generated for the same account and provider,
     which the server echoes back.

   It is never by account, provider, or time. A destination sent from another
   tab, or an older receipt on the same channel, can therefore never lend its
   ``retry_action`` to this row. The key step matters when the receipt read that
   would have stored an attempt id failed: the row can still find its own
   receipt once the list loads, instead of falling back to a new attempt. */
function receiptForResult(result, attempts = [], submission = null) {
  // A receipt this row already read from the server is the most exact reference
  // there is, and it is available even before the list read finishes.
  if (result && result.receipt) return result.receipt;
  const accountId = result && result.account && result.account.id;
  if (!accountId) return null;
  const attemptId = result && result.attemptId;
  if (attemptId) {
    const exact = attempts.find((attempt) =>
      attempt.id === attemptId && attempt.account_id === accountId);
    if (exact) return exact;
  }
  const entry = submission && submission.destinations ? submission.destinations[accountId] : null;
  const keys = new Set(Object.values((entry && entry.keys) || {}).filter(isValidIdempotencyKey));
  if (!keys.size) return null;
  return attempts.find((attempt) =>
    attempt.account_id === accountId && keys.has(attempt.request_key)) || null;
}

/* The confirmation key for a destination, if one can be proven.

   The browser's own stored key is preferred. When the local draft is gone, the
   server may echo the same client-generated key back (``publish_key``); that is
   an exact correlation, not a guess, so it restores the ability to repeat the
   *same* confirmation. Anything else returns null and the destination falls
   back to a deliberate new attempt. */
function publishKeyForReceipt(attempt, submission = null) {
  const entry = submission && attempt && submission.destinations
    ? submission.destinations[attempt.account_id]
    : null;
  const local = entry && entry.keys && entry.keys.publish;
  if (isValidIdempotencyKey(local)) return local;
  const echoed = attempt && attempt.publish_key;
  return isValidIdempotencyKey(echoed) ? echoed : null;
}

/* The controls a finished destination may offer.

   A failed destination is retried only when its own receipt says how: the
   server's ``retry_action`` names the endpoint and which key to reuse, and a
   cooldown or a missing credential is honoured rather than overridden.

   ``receiptsState`` is what the caller knows about the authoritative receipt
   list. While that list is loading — or when the read failed — the absence of a
   receipt proves nothing, so the row stays uncertain and offers a read-only
   check instead of a "new attempt" that would silently bypass the server's own
   recipe. Only a completed, successful read may fall back to a deliberate copy. */
function failedDestinationAction(result, receipt, submission = null, { receiptsState = "loaded" } = {}) {
  if (!receipt) {
    if (receiptsState === "loading") {
      return { kind: "unresolved", note: "Checking this delivery's record…", pending: true };
    }
    if (receiptsState === "failed") {
      return {
        kind: "unresolved",
        note: "Relay couldn't read this delivery's record just now. The outcome is still unconfirmed; nothing was resent.",
      };
    }
    return { kind: "edit_copy", note: null };
  }
  const action = receiptAction(receipt, { publishKey: publishKeyForReceipt(receipt, submission) });
  if (action.kind === "retry" || action.kind === "retry_publish") {
    return { kind: "retry", note: action.note || null, action };
  }
  if (action.kind === "reconnect") return { kind: "reconnect", note: action.note || null, action };
  if (action.kind === "wait") return { kind: "wait", note: action.note || null, action };
  return { kind: "edit_copy", note: action.note || null, action };
}

/* A receipt rendered as a composer result entry. Times are milliseconds. */
function receiptToEntry(attempt) {
  return {
    provider: attempt.provider,
    accountId: attempt.account_id,
    accountName: attempt.account_name || null,
    attemptId: attempt.id || null,
    postId: attempt.post_id || null,
    status: normalizeStatus(attempt.status),
    nextAction: attempt.next_action || null,
    url: safeHttpsUrl(attempt.provider_url),
    verificationUrl: safeHttpsUrl(attempt.verification_url),
    error: attempt.error || null,
    title: attempt.title || null,
    caption: attempt.caption || "",
    mediaName: attempt.media_name || null,
    mediaType: attempt.media_type || null,
    mediaUrl: safeHttpsUrl(attempt.media_url),
    privacyStatus: attempt.privacy_status || null,
    createdAt: secondsToMs(attempt.created_at),
    updatedAt: secondsToMs(attempt.updated_at),
    adopted: Boolean(attempt.adopted),
  };
}

/* Only https links are ever rendered as an outbound action. */
function safeHttpsUrl(value) {
  if (typeof value !== "string" || !value) return null;
  try {
    const url = new URL(value, window.location.origin);
    return url.protocol === "https:" ? url.href : null;
  } catch {
    return null;
  }
}

/* ── Activity merge ─────────────────────────────────────────── */

/* Merge receipts with legacy history and the browser draft.
   - A post that has both a receipt and a history row is one delivery, keyed by
     account + provider + platform reference (never by platform id alone).
   - History only enriches a receipt; it never creates a duplicate success.
   - No row is invented: a delivery appears only if the server recorded it. */
function mergeActivity({ attempts = [], history = [], draft = null, filter = "all", submission = null } = {}) {
  const byIdentity = new Map();

  for (const attempt of attempts) {
    if (!attempt || !attempt.provider) continue;
    const identity = receiptIdentity(attempt);
    const previous = byIdentity.get(identity);
    if (previous && Number(previous.updated_at || 0) >= Number(attempt.updated_at || 0)) continue;
    byIdentity.set(identity, { ...attempt, source: "receipt" });
  }

  for (const post of history) {
    if (!post || !post.provider) continue;
    const identity = historyIdentity(post);
    const existing = byIdentity.get(identity);
    if (existing) {
      // Enrich only. The receipt's status stays authoritative.
      if (!existing.provider_url && post.post_url) existing.provider_url = post.post_url;
      if (!existing.title && post.title) existing.title = post.title;
      if (!existing.media_name && post.media_name) existing.media_name = post.media_name;
      existing.history = true;
      continue;
    }
    byIdentity.set(identity, {
      id: `history:${post.id}`,
      source: "history",
      provider: post.provider,
      account_id: post.account_id,
      account_name: post.account_name || null,
      status: "published",
      provider_ref: post.platform_post_id,
      provider_url: post.post_url || null,
      title: post.title || null,
      caption: post.caption || "",
      media_name: post.media_name || null,
      media_type: post.media_type || null,
      media_url: post.media_url || null,
      privacy_status: post.visibility || null,
      created_at: post.published_at,
      updated_at: post.published_at,
      history: true,
    });
  }

  const rows = [...byIdentity.values()].map((row) => {
    const phase = receiptPhase(row.status);
    return {
      ...row,
      phase,
      identity: receiptIdentity(row),
      timestamp: secondsToMs(row.updated_at) || secondsToMs(row.created_at) || 0,
      // A publish retry can only be offered when a confirmation key is provable:
      // the browser's own, or the exact one the server echoed back.
      action: receiptAction(row, { publishKey: publishKeyForReceipt(row, submission) }),
    };
  }).sort((left, right) => right.timestamp - left.timestamp);

  const draftRow = draft && draftContent(draft).hasContent
    ? {
      id: "local-draft",
      source: "draft",
      phase: "draft",
      timestamp: Number(draft.savedAt) || 0,
      draft,
    }
    : null;

  const counts = { all: rows.length + (draftRow ? 1 : 0) };
  for (const key of ACTIVITY_FILTERS) {
    if (key === "all") continue;
    counts[key] = key === "drafts"
      ? (draftRow ? 1 : 0)
      : rows.filter((row) => matchesFilter(row, key)).length;
  }

  let visible;
  if (filter === "drafts") visible = draftRow ? [draftRow] : [];
  else if (filter === "all") visible = draftRow ? [draftRow, ...rows] : rows;
  else visible = rows.filter((row) => matchesFilter(row, filter));

  return { rows: visible, all: rows, draft: draftRow, counts };
}

function matchesFilter(row, filter) {
  if (filter === "published") return row.phase === "published";
  if (filter === "in_progress") return row.phase === "in_progress" || row.phase === "action_needed";
  if (filter === "failed") return row.phase === "failed" || row.phase === "expired";
  if (filter === "unverified") return row.phase === "unverified";
  return false;
}

function activityTitle(row) {
  if (!row) return "";
  if (row.source === "draft") return draftTitle(row.draft);
  return row.title || row.caption || row.media_name ||
    (row.provider === "youtube" ? "YouTube video" : "Instagram post");
}

/* ── Draft truth ────────────────────────────────────────────── */

/* ── Edit a copy: what a receipt can actually restore ───────── */

/* Resolve a receipt's media against the workspace library.

   Only a provable reference is accepted, in order:
   - the exact library item id the submission recorded (YouTube sends carry it),
   - the exact ``media_url`` the receipt recorded, or
   - the exact filename, but only when exactly one library item has that name.

   A filename alone is not proof when two files share it, so an ambiguous name is
   reported as unresolved instead of silently choosing one. Nothing is ever
   guessed from the current composer. */
function resolveReceiptMedia(receipt, items = []) {
  const contentId = receipt && receipt.content_id ? String(receipt.content_id) : null;
  if (contentId) {
    const exact = items.find((item) => item.id === contentId);
    if (exact) return { item: exact, matchedBy: "id" };
  }
  const mediaUrl = receipt && receipt.media_url ? String(receipt.media_url) : null;
  if (mediaUrl) {
    const exact = items.find((item) => item.media_url === mediaUrl);
    if (exact) return { item: exact, matchedBy: "url" };
  }
  const name = receipt && receipt.media_name ? String(receipt.media_name).trim() : "";
  if (!name) return { item: null, matchedBy: null, reason: "no-reference" };
  const sameName = items.filter((item) => item.filename === name);
  if (sameName.length === 1) return { item: sameName[0], matchedBy: "filename" };
  if (sameName.length > 1) return { item: null, matchedBy: null, reason: "ambiguous", candidates: sameName };
  return { item: null, matchedBy: null, reason: "missing", filename: name };
}

/* The content a copy may restore, from the authoritative receipt when one is
   loaded, otherwise from the submission's own persisted payload.

   The submission payload is exact: it is the request this browser sent. It is
   used only for this destination, never for a different account, and it never
   borrows text from whatever is currently in the composer. */
function copySourceFor({ receipt = null, submission = null, accountId = null, provider = null } = {}) {
  if (receipt) return { ...receipt, provider: receipt.provider || provider || null };
  const entry = submission && accountId && submission.destinations
    ? submission.destinations[accountId]
    : null;
  if (!entry) return null;
  const payloads = entry.payloads || {};
  if (entry.provider === "youtube") {
    const payload = payloads.upload || {};
    return {
      provider: "youtube",
      account_id: accountId,
      title: payload.title || "",
      caption: payload.description || "",
      media_name: payload.filename || null,
      media_type: payload.content_type || null,
      content_id: payload.content_id || null,
    };
  }
  const payload = payloads.create || {};
  return {
    provider: "instagram",
    account_id: accountId,
    caption: payload.caption || "",
    media_url: payload.media_url || null,
    media_type: payload.media_type || null,
  };
}

/* What Edit a copy will put in the composer, and what it cannot.

   ``replace`` is always true: a copy is the receipt's content, not a merge with
   whatever happened to be on screen. Fields the receipt does not record are
   left empty rather than filled from an unrelated draft.

   The receipt's own vocabulary is provider-shaped: a YouTube receipt stores its
   description in ``caption``, so it is mapped back to the description field and
   never leaks into the Instagram caption. */
function copyPlan(receipt, items = []) {
  const provider = receipt && (receipt.provider || (receipt.account && receipt.account.provider)) || null;
  const media = resolveReceiptMedia(receipt, items);
  const text = typeof receipt?.caption === "string" ? receipt.caption : "";
  const title = typeof receipt?.title === "string" ? receipt.title : "";
  const youtube = provider === "youtube";
  return {
    provider,
    replace: true,
    caption: youtube ? "" : text,
    title: youtube ? title : "",
    description: youtube ? text : "",
    media,
    // A copy is complete only when its media is resolved. Text-only restores are
    // still useful, but the interface must say the file still needs choosing.
    complete: Boolean(media.item),
  };
}

/* The sentence shown after a copy. It names exactly what was carried over and
   what the person still has to do — never a blanket "copied". */
function copyNotice(plan) {
  if (!plan) return "Nothing to copy.";
  const copied = [];
  if (plan.media.item) copied.push("media");
  if (plan.title) copied.push("title");
  if (plan.caption) copied.push("caption");
  if (plan.description) copied.push("description");
  if (plan.media.item) {
    return "Copied for a new attempt. This sends a new post — the earlier one is unchanged.";
  }
  if (plan.media.reason === "ambiguous") {
    const names = [...new Set((plan.media.candidates || []).map((item) => item.filename).filter(Boolean))];
    const name = names[0] ? `“${names[0]}”` : "the file";
    const count = (plan.media.candidates || []).length;
    const choose = count > 1
      ? `${count} files are named ${name}. Choose the right one in your library to continue.`
      : `Choose ${name} in your library to continue.`;
    return copied.length ? `${capitalized(copied.join(" and "))} copied. ${choose}` : choose;
  }
  if (plan.media.reason === "missing" && plan.media.filename) {
    const choose = `Choose “${plan.media.filename}” from your library, or upload it again, to continue.`;
    return copied.length ? `${capitalized(copied.join(" and "))} copied. ${choose}` : choose;
  }
  const choose = "Choose the file this post used to continue.";
  return copied.length ? `${capitalized(copied.join(" and "))} copied. ${choose}` : choose;
}

function capitalized(text) {
  return text ? text.charAt(0).toUpperCase() + text.slice(1) : text;
}

/* Which destinations a copy may select.

   Only the ones that are not confirmed. A published destination is never
   included, so a copy can never send it twice. An unresolved destination is
   included so its row stays visible, but the send planner still refuses it. */
function copyDestinations(results, fallbackAccountId = null) {
  const open = new Set(
    (results || [])
      .filter((entry) => entry && entry.status !== "published" && entry.account)
      .map((entry) => entry.account.id),
  );
  if (!open.size && fallbackAccountId) open.add(fallbackAccountId);
  return open;
}
/* One definition of "this draft is worth keeping", used by the save path, the
   Home card, Activity, and the import offers. Text or media is content; a
   channel selection alone is not, so it never becomes a phantom "Untitled
   draft". An unresolved submission is content because it is recoverable work. */
function draftContent(draft) {
  if (!draft || typeof draft !== "object") return { hasContent: false, reason: "none" };
  const caption = String(draft.caption || "").trim();
  const title = String(draft.title || "").trim();
  const description = String(draft.description || "").trim();
  const media = draft.selectedContentId || null;
  const unresolved = submissionHasUnresolved(draft.submission);
  const hasContent = Boolean(caption || title || description || media || unresolved);
  return { hasContent, caption, title, description, media, unresolved };
}

function draftTitle(draft) {
  const content = draftContent(draft);
  const text = content.title || content.caption || content.description;
  if (text) return text.length > 70 ? `${text.slice(0, 70).trimEnd()}…` : text;
  const name = draft && draft.mediaName;
  if (name) return name;
  if (content.unresolved) return "Unfinished delivery";
  return "Draft";
}

/* ── Submission intent and idempotency ──────────────────────── */

/* Stable serialization so the same semantic payload always hashes the same. */
function stableStringify(value) {
  if (value === undefined) return "null";
  if (value === null || typeof value !== "object") return JSON.stringify(value);
  if (Array.isArray(value)) return `[${value.map(stableStringify).join(",")}]`;
  const keys = Object.keys(value).sort();
  return `{${keys.map((key) => `${JSON.stringify(key)}:${stableStringify(value[key])}`).join(",")}}`;
}

/* FNV-1a, base36. Only used to make compact, stable tokens — not security. */
function shortHash(text) {
  let hash = 0x811c9dc5;
  const source = String(text);
  for (let index = 0; index < source.length; index += 1) {
    hash ^= source.charCodeAt(index);
    hash = Math.imul(hash, 0x01000193) >>> 0;
  }
  return hash.toString(36).padStart(7, "0");
}

function destinationAction(provider) {
  return provider === "youtube" ? "upload" : "create";
}

function token(value, length) {
  return String(value || "").replace(/[^A-Za-z0-9]/g, "").slice(0, length) || "0";
}

/* Per destination, per action, per payload. The key is derived, not random, so
   a double click, a network retry, or a reload after a lost response reuses the
   same key. A changed payload necessarily produces a different key. */
function destinationKey({ userId, workspaceId, submissionId, accountId, action, payload }) {
  const scope = shortHash(`${userId || ""}\u0000${workspaceId || ""}`);
  const submission = token(submissionId, 16);
  const account = token(accountId, 16);
  const payloadHash = shortHash(stableStringify(payload));
  return `relay.${scope}.${submission}.${account}.${action}.${payloadHash}`;
}

function isValidIdempotencyKey(key) {
  return typeof key === "string" && /^[A-Za-z0-9._:-]{8,128}$/.test(key);
}

function randomSubmissionId() {
  try {
    if (typeof crypto !== "undefined" && crypto.randomUUID) return crypto.randomUUID();
  } catch { /* fall through */ }
  return `sub-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}

function cloneSubmission(submission) {
  return JSON.parse(JSON.stringify(submission));
}

/* A logical submission: one deliberate Send, with one key per destination and
   action. Created and persisted before the first network call. */
function createSubmission({ userId, workspaceId, destinations = [], now = Date.now(), id = null }) {
  const submission = {
    id: id || randomSubmissionId(),
    userId: userId || null,
    workspaceId: workspaceId || null,
    createdAt: now,
    destinations: {},
  };
  for (const destination of destinations) {
    const action = destinationAction(destination.provider);
    submission.destinations[destination.accountId] = {
      provider: destination.provider,
      payloads: { [action]: destination.payload },
      keys: {
        [action]: destinationKey({
          userId: submission.userId,
          workspaceId: submission.workspaceId,
          submissionId: submission.id,
          accountId: destination.accountId,
          action,
          payload: destination.payload,
        }),
      },
      state: "idle",
      attemptId: null,
      postId: null,
    };
  }
  return submission;
}

function recordDestination(submission, { accountId, provider, payload, state, attemptId, postId, resend }) {
  const next = cloneSubmission(submission);
  const action = destinationAction(provider);
  const previous = next.destinations[accountId] || {};
  next.destinations[accountId] = {
    provider,
    payloads: { ...(previous.payloads || {}), [action]: payload },
    keys: {
      ...(previous.keys || {}),
      [action]: destinationKey({
        userId: next.userId,
        workspaceId: next.workspaceId,
        submissionId: next.id,
        accountId,
        action,
        payload,
      }),
    },
    state: state || previous.state || "idle",
    attemptId: attemptId !== undefined ? attemptId : (previous.attemptId || null),
    postId: postId !== undefined ? postId : (previous.postId || null),
  };
  // Why a pre-dispatch refusal happened is only known in this window. It is kept
  // so a reload can still offer "Send again" for a session or origin refusal —
  // but only when it was actually safe to repeat; otherwise the entry carries
  // nothing and the reloaded row falls back to a deliberate new attempt.
  if (resend === true) next.destinations[accountId].resend = true;
  return next;
}

/* Decide whether this destination may be sent now.
   - published: never again
   - sent/unknown/processing/ready: unresolved; status actions only
   - failed/expired: a new attempt is required (Edit a copy)
   - idle: allowed, with a key derived from the current payload */
function planDestination(submission, { accountId, provider, payload }) {
  const action = destinationAction(provider);
  if (!submission) return { ok: false, reason: "no-submission", action };
  const key = destinationKey({
    userId: submission.userId,
    workspaceId: submission.workspaceId,
    submissionId: submission.id,
    accountId,
    action,
    payload,
  });
  const entry = submission.destinations[accountId];
  if (!entry) return { ok: true, key, action, state: "idle", changed: true, fresh: true };
  const changed = stableStringify(entry.payloads && entry.payloads[action]) !== stableStringify(payload);
  const state = entry.state || "idle";
  if (state === "published") return { ok: false, reason: "published", key, action, state, changed };
  if (state === "sent" || state === "unknown" || state === "processing" || state === "ready") {
    return { ok: false, reason: "unresolved", key, action, state, changed };
  }
  if (state === "failed" || state === "expired") {
    return { ok: false, reason: "failed", key, action, state, changed };
  }
  return { ok: true, key, action, state, changed };
}

function submissionHasUnresolved(submission) {
  if (!submission || !submission.destinations) return false;
  return Object.values(submission.destinations).some((entry) =>
    ["sent", "unknown", "processing", "ready", "failed", "expired", "not_sent"].includes(entry.state));
}

function submissionComplete(submission) {
  if (!submission || !submission.destinations) return false;
  const entries = Object.values(submission.destinations);
  return entries.length > 0 && entries.every((entry) => entry.state === "published");
}

/* ── Matching receipts to the current submission ────────────── */

/* Receipts that belong to the submission on screen, versus earlier deliveries.

   Matching is exact only:
   - a receipt id or post id already stored for a destination, or
   - the server-echoed request key (the Idempotency-Key this client sent) for
     the same provider and account.

   There is deliberately no time-based fallback. A receipt created after this
   submission started can belong to another tab, another device, or an
   unrelated send on the same account, so adopting it would show a status for
   work this person did not do. An uncorrelated receipt stays in `other`
   (recovered workspace activity) and the destination stays unresolved. */
function matchReceipts(attempts = [], submission = null) {
  if (!submission) return { current: [], other: [...attempts] };
  const ids = new Set();
  const postIds = new Set();
  const keysByAccount = new Map();
  for (const [accountId, entry] of Object.entries(submission.destinations || {})) {
    if (!entry) continue;
    if (entry.attemptId) ids.add(entry.attemptId);
    if (entry.postId) postIds.add(entry.postId);
    const keys = new Set(Object.values(entry.keys || {}).filter(isValidIdempotencyKey));
    if (keys.size) keysByAccount.set(accountId, keys);
  }
  const current = [];
  const other = [];
  const seen = new Set();
  for (const attempt of attempts) {
    if (!attempt) continue;
    if (ids.has(attempt.id) || (attempt.post_id && postIds.has(attempt.post_id))) {
      if (!seen.has(attempt.id)) {
        seen.add(attempt.id);
        current.push(attempt);
      }
      continue;
    }
    const key = typeof attempt.request_key === "string" ? attempt.request_key : null;
    const keys = attempt.account_id ? keysByAccount.get(attempt.account_id) : null;
    const entry = attempt.account_id ? submission.destinations[attempt.account_id] : null;
    if (key && keys && keys.has(key) && entry && (!entry.provider || entry.provider === attempt.provider)) {
      if (!seen.has(attempt.id)) {
        seen.add(attempt.id);
        // `adopted` means "correlated by key, not by a stored reference".
        current.push({ ...attempt, adopted: true });
      }
      continue;
    }
    other.push(attempt);
  }
  return { current, other };
}

/* ── Pagination ─────────────────────────────────────────────── */

function canLoadMore({ nextCursor, loading, loaded } = {}) {
  return Boolean(loaded && nextCursor && !loading);
}

function pageLabel({ shown, hasMore } = {}) {
  const count = Number(shown) || 0;
  if (!count) return "No deliveries yet";
  return hasMore
    ? `Showing ${count} most recent. Load more for older deliveries.`
    : `Showing all ${count} deliver${count === 1 ? "y" : "ies"}.`;
}

/* ── Draft import eligibility ───────────────────────────────── */

/* A legacy draft may be offered only for the exact workspace the authenticated
   bootstrap proved this user owns. No other key is ever read. */
function legacyDraftImport({ legacyKey, ownedKey, legacyDraft, ownedDraft, dismissed }) {
  if (!legacyKey || !ownedKey) return { offer: false, reason: "unowned" };
  if (dismissed) return { offer: false, reason: "dismissed" };
  if (!draftContent(legacyDraft).hasContent) return { offer: false, reason: "empty" };
  if (draftContent(ownedDraft).hasContent) return { offer: false, reason: "owned-draft-newer" };
  return { offer: true, reason: "recoverable" };
}

/* The /try caption is separate from the legacy draft. It is adopted silently
   only when there is no draft of the user's own; otherwise the person chooses. */
function previewDraftDecision({ previewDraft, ownedDraft }) {
  if (!draftContent(previewDraft).hasContent) return { action: "discard", reason: "empty" };
  const owned = draftContent(ownedDraft);
  if (!owned.hasContent) return { action: "adopt", reason: "no-owned-draft" };
  if (String(ownedDraft.caption || "").trim() === String(previewDraft.caption || "").trim()) {
    return { action: "discard", reason: "same-caption" };
  }
  return { action: "conflict", reason: "different-caption" };
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    RECEIPT_STATUSES,
    OPEN_RECEIPT_STATUSES,
    SAFE_NEXT_ACTIONS,
    ACTIVITY_FILTERS,
    FILTER_LABELS,
    normalizeStatus,
    sendFailureState,
    receiptPhase,
    phaseLabel,
    receiptIdentity,
    historyIdentity,
    receiptAction,
    reconnectRequired,
    receiptForResult,
    publishKeyForReceipt,
    failedDestinationAction,
    failedDestinationControls,
    unassociatedNote,
    waitStatus,
    resolveReceiptMedia,
    copySourceFor,
    copyPlan,
    copyNotice,
    copyDestinations,
    receiptToEntry,
    safeHttpsUrl,
    mergeActivity,
    matchesFilter,
    activityTitle,
    draftContent,
    draftTitle,
    stableStringify,
    shortHash,
    destinationAction,
    destinationKey,
    isValidIdempotencyKey,
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
  };
}
