"use strict";

/* ── Delivery results ──────────────────────────────────────────
   The UI mirrors server state. It never upgrades an outcome on its own:
   - Instagram statuses come from GET /instagram/posts/{id}
   - YouTube status comes from GET /publishing/attempts/{id} (a receipt read;
     the uploaded bytes are gone, so it can only confirm or leave it unknown)
   - A publish failure re-reads the authoritative receipt; if that read fails,
     the destination stays uncertain and is never resent.
   - Unknown destinations are never re-sent automatically.

   Two separate lists are rendered on purpose:
   - `activeInstagramPosts` / `youtubeUploadResults` are the destinations of the
     submission on screen.
   - `recoveredAttempts` are earlier sends recovered from the server. They are
     never grouped into "this post": the backend is per destination and has no
     batch aggregate, so a workspace-wide list is not one submission. */

let activePublishDraft = null;

/* One logical submission: persisted with the draft before the first request. */
let activeSubmission = null;

/* Workspace receipts recovered from GET /publishing/attempts. */
let recoveredAttempts = [];
let recoveredNextCursor = null;
let recoveredLoaded = false;
let recoveredError = null;
let recoveredLoading = false;
let recoveredRequestId = 0;

function formatStatusTime(value) {
  if (!value) return null;
  const date = value instanceof Date ? value : new Date(value);
  if (Number.isNaN(date.getTime())) return null;
  return date.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
}

function statusText(status, result) {
  const at = formatStatusTime(result.updatedAt);
  const labels = {
    pending: "Relay recorded this send but has not dispatched it yet.",
    submitted: "Sent to the provider. Waiting for it to answer.",
    processing: "Instagram is still processing this media. It is not live yet.",
    ready: "Instagram processed the media. It is not live until you confirm.",
    publishing: "Sending the publish request. Waiting for Instagram to confirm.",
    published: at ? `Published and confirmed at ${at}.` : "Published and confirmed.",
    // A failed receipt means the server knows this destination was not
    // delivered. It does not always mean the provider refused it: Relay can
    // also refuse before dispatch (an unreadable credential, for example), so
    // the row states the fact and shows the server's own reason beside it.
    failed: "Not published. Relay recorded this destination as not delivered.",
    expired: "Not published. The Instagram media container expired.",
    // Relay refused the request before dispatch: nothing reached the provider,
    // so this is not a rejection by it.
    not_sent: "Not sent. Relay didn't dispatch this, so nothing was published.",
    unknown: "Not confirmed. The provider may have received it — check before doing anything else.",
  };
  return labels[status] || "Waiting for a status.";
}

function statusBadge(status, checking) {
  if (checking) return "Checking…";
  return {
    pending: "Queued",
    submitted: "Sent",
    processing: "Processing",
    ready: "Ready · not live",
    publishing: "Publishing",
    published: "Published",
    failed: "Failed",
    expired: "Expired",
    not_sent: "Not sent",
    unknown: "Verify",
  }[status] || "Pending";
}

/* A result row. `actions` is a list of {label, kind, href, run}. Every action
   here is safe: none of them re-sends an unresolved destination. */
function resultRow({
  account, provider, status, description, url, error, checking, updatedAt,
  verifyLink, actions = [], recovered = false, timeLabel = null,
}) {
  const row = el("article", `publish-result-row ${status || ""}${checking ? " checking" : ""}`);
  row.append(makeProfileAvatar(account, provider, "result-avatar"));
  const copy = el("span", "publish-result-copy");
  copy.append(el("strong", null, account.name));
  const platform = el("small", "publish-result-platform", names[provider] || provider);
  copy.append(platform);
  if (description) copy.append(el("small", null, description));
  if (error) copy.append(el("small", "publish-result-error", error));
  if (updatedAt && status !== "published") {
    const checked = formatStatusTime(updatedAt);
    if (checked) copy.append(el("small", "publish-result-updated", `${timeLabel || (recovered ? "Updated" : "Checked")} ${checked}`));
  }
  row.append(copy);

  const actionWrap = el("span", "result-actions");
  if (url) {
    const link = el("a", "result-open-link", provider === "youtube" ? "Open video" : "Open post");
    link.href = url;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    actionWrap.append(link);
  } else if (verifyLink) {
    // The provider link opens the provider; it does not claim a status. Its
    // label is distinct from the "Check status" button that re-reads Relay.
    const link = el("a", "result-open-link", "Check provider");
    link.href = verifyLink;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    link.setAttribute("aria-label", `Open ${names[provider] || "the provider"} to check this delivery`);
    actionWrap.append(link);
  }
  for (const action of actions) {
    const button = el("button", `button button-ghost button-sm result-action ${action.kind || ""}`, action.label);
    button.type = "button";
    if (action.disabled) {
      button.disabled = true;
      button.setAttribute("aria-disabled", "true");
    }
    button.addEventListener("click", () => { if (!button.disabled) action.run(button); });
    actionWrap.append(button);
  }
  if (actionWrap.childElementCount) row.append(actionWrap);
  row.append(el("span", `result-status-badge ${status || ""}`, statusBadge(status, checking)));
  return row;
}

/* ── The submission on screen ──────────────────────────────── */

function currentResults() {
  return [...activeInstagramPosts, ...youtubeUploadResults];
}

/* The controls a finished destination may offer.
   The decision itself is pure and shared (failedDestinationControls in
   recovery.js), so "This submission" and "Earlier deliveries" derive the same
   action from the same receipt. Only the DOM wiring lives here. */
function failedDestinationActions(result, receipt) {
  const resolved = failedDestinationControls(result, receipt, activeSubmission, {
    receiptsState: receiptLookupState(),
  });
  const actions = resolved.controls.map((control) => {
    if (control.kind === "retry") {
      return { ...control, run: (button) => retryRecovered(receipt, button) };
    }
    if (control.kind === "reconnect") {
      const provider = result.account ? result.account.provider : null;
      return { ...control, run: () => connect(provider) };
    }
    if (control.kind === "wait") {
      // Read-only: re-reads the receipt, never dispatches. The label is restored
      // to "Check availability" after the read.
      return { ...control, run: (button) => checkRecovered(receipt, button, control.label) };
    }
    if (control.kind === "check") {
      return { ...control, run: () => recheckDestination(result) };
    }
    if (control.kind === "pending") return { ...control, run: () => {} };
    return { ...control, run: () => editCopyFor(result) };
  });
  return { note: resolved.note, actions };
}

/* What the receipt list can currently prove. `loading` covers the first read,
   every refresh, and the window while a send is still resolving its own
   receipts. `failed` is a read that did not complete. Only `loaded` may be
   treated as "the server has no receipt for this destination". */
function receiptLookupState() {
  if (recoveredLoading || receiptsSyncing) return "loading";
  if (recoveredError) return "failed";
  if (!recoveredLoaded) return "loading";
  return "loaded";
}

/* True from the moment a send starts resolving until its receipts have been
   read. A row rendered in that window must not fall back to "Edit a copy" just
   because the list has not caught up yet. */
let receiptsSyncing = false;

function renderDeliverySection() {
  const section = $("#publish-results-section");
  if (!section) return;
  const results = currentResults();
  const show = publishing || results.length > 0;
  section.hidden = !show;
  if (!show) {
    $("#publish-results").replaceChildren();
    $("#publish-status-summary").hidden = true;
    return;
  }

  const checkable = results.filter((result) =>
    ["processing", "submitted", "pending", "unknown"].includes(result.status) &&
    (result.id || result.attemptId));
  const checkButton = $("#instagram-check-all");
  checkButton.hidden = !checkable.length;
  checkButton.disabled = instagramStatusCheckRunning || !checkable.length;
  checkButton.textContent = instagramStatusCheckRunning ? "Checking…" : "Check now";

  const ready = activeInstagramPosts.some((post) => post.status === "ready" && post.id);
  const publishReady = $("#instagram-publish-ready");
  publishReady.hidden = !ready;
  publishReady.disabled = publishing;

  const newPost = $("#publish-new-post");
  newPost.hidden = !results.length;
  // A copy is a *new* submission with new keys; the earlier receipt is left
  // untouched, so it is safe even while that send is still in flight. The two
  // states where a copy is the wrong next step are kept blocked, with a reason:
  // a ready container needs confirming, and a publish already being sent must
  // not be duplicated.
  const holding = results.filter((result) =>
    ["ready", "publishing", "pending"].includes(result.status));
  newPost.disabled = publishing || holding.length > 0;
  // This header control starts deliberate new work with whatever is in the
  // composer. It is not "Edit a copy" of a failed row: a cooldown or a
  // reconnect applies to that row's recovery, and calling this a copy would
  // imply the earlier attempt is being resumed when it is not.
  newPost.textContent = deliveryOutcome(results).confirmed === results.length ? "Create next post" : "New post";
  newPost.title = holding.length
    ? holding.some((result) => result.status === "ready")
      ? "Confirm this post on Instagram first, or wait for it to settle."
      : "This send is still being recorded. Wait for it to settle, then start another."
    : "Starts a new post with what is in the composer. The earlier delivery is unchanged.";

  const outcome = deliveryOutcome(results);
  const summary = $("#publish-status-summary");
  summary.hidden = false;
  summary.textContent = `${outcome.headline}. ${outcome.detail}.`;
  summary.className = "status-summary" + (
    outcome.unknown ? " attention" :
    outcome.failed ? " attention" :
    outcome.notSent ? " attention" :
    outcome.ready ? " attention" :
    outcome.pending ? " pending" :
    outcome.confirmed === outcome.total ? " published" : "");

  // Say why "Edit a copy" is unavailable rather than leaving a dead control.
  const holdingNote = holding.length
    ? holding.some((result) => result.status === "ready")
      ? "Confirm this post on Instagram, or wait for it to settle, before starting another."
      : "This send is still being recorded. Wait for it to settle, or check its status, before starting another."
    : "";
  setText("#composer-progress-detail", holdingNote || pollingNote());

  const list = $("#publish-results");
  list.replaceChildren();
  for (const post of activeInstagramPosts) {
    const actions = [];
    const receipt = receiptForResult(post, recoveredAttempts, activeSubmission);
    let description = statusText(post.status, post);
    let explanation = post.error;
    if (post.status === "ready" && post.id) {
      actions.push({ label: "Confirm on Instagram", kind: "publish", run: () => publishReadyPost(post) });
    }
    if (post.status === "failed" || post.status === "expired") {
      const resolved = failedDestinationActions(post, receipt);
      actions.push(...resolved.actions);
      if (resolved.note) {
        description = resolved.note;
        // The action's explanation replaces the raw provider error rather than
        // appearing beside it: two lines saying the same thing is noise.
        explanation = null;
      }
    } else if (post.status === "not_sent") {
      // Relay refused this before dispatching, so nothing was delivered. Only a
      // refusal about the request itself (a session, an origin, a storage write
      // known not to have happened) is safe to repeat unchanged; a rejected
      // field or a wait would fail the same way, and after a reload the reason is
      // no longer known, so those offer a new, editable attempt instead — but
      // only once the receipt list has actually been read. Before that, a new
      // attempt could bypass a cooldown the server is about to name.
      if (post.resend === true) {
        actions.push({ label: "Send again", kind: "retry", run: () => publishPost() });
      } else if (receiptLookupState() !== "loaded") {
        actions.push({ label: "Check status", kind: "check", run: () => recheckDestination(post) });
      } else {
        actions.push({ label: "Edit a copy", kind: "edit-copy", run: () => editCopyFor(post) });
      }
    }
    if (["unknown", "processing", "submitted", "pending"].includes(post.status) && (post.id || post.attemptId)) {
      actions.push({ label: "Check status", kind: "check", run: () => checkDeliveryStatuses({ manual: true }) });
    } else if (post.status === "unknown") {
      // The response was lost before an id came back. Re-read the receipt list
      // and adopt only a receipt that began with this submission.
      actions.push({ label: "Check status", kind: "check", run: () => recheckDestination(post) });
    }
    list.append(resultRow({
      account: post.account,
      provider: "instagram",
      status: post.status,
      description,
      error: explanation,
      checking: post.checking,
      updatedAt: post.updatedAt,
      timeLabel: post.restored ? "Last updated" : null,
      url: post.url,
      verifyLink: post.status === "unknown" ? safeHttpsUrl(post.verificationUrl) : null,
      actions,
    }));
  }
  for (const result of youtubeUploadResults) {
    const actions = [];
    const receipt = receiptForResult(result, recoveredAttempts, activeSubmission);
    let description = statusText(result.status, result);
    let explanation = result.error;
    if (result.status === "failed" || result.status === "expired") {
      const resolved = failedDestinationActions(result, receipt);
      actions.push(...resolved.actions);
      if (resolved.note) {
        description = resolved.note;
        explanation = null;
      }
    } else if (result.status === "not_sent") {
      // See the Instagram branch: only a pre-dispatch refusal about the request
      // itself is safe to repeat unchanged.
      actions.push(result.resend === true
        ? { label: "Send again", kind: "retry", run: () => publishPost() }
        : { label: "Edit a copy", kind: "edit-copy", run: () => editCopyFor(result) });
    }
    if (["unknown", "submitted", "pending"].includes(result.status) && result.attemptId) {
      actions.push({ label: "Check status", kind: "check", run: () => checkDeliveryStatuses({ manual: true }) });
    } else if (result.status === "unknown") {
      actions.push({ label: "Check status", kind: "check", run: () => recheckDestination(result) });
    }
    list.append(resultRow({
      account: result.account,
      provider: "youtube",
      status: result.status,
      description,
      error: explanation,
      updatedAt: result.updatedAt,
      timeLabel: result.restored ? "Last updated" : null,
      url: result.url,
      verifyLink: result.status === "unknown" ? safeHttpsUrl(result.verificationUrl) : null,
      actions,
    }));
  }
}

/* A single, honest note about automatic checking. */
function pollingNote() {
  const tracked = currentResults().filter((result) =>
    ["processing", "submitted", "pending", "unknown"].includes(result.status));
  if (!tracked.length) return "Results by channel.";
  const stopped = tracked.filter((result) => result.pollingStopped);
  if (stopped.length === tracked.length) {
    return "Automatic checking has stopped. Use Check now to ask the provider again.";
  }
  if (document.hidden) return "Automatic checking pauses while this tab is in the background.";
  return "Checking automatically for a few minutes. You can also check now.";
}

/* ── Recovered receipts ─────────────────────────────────────── */

function renderRecoveredSection() {
  const section = $("#recovered-section");
  if (!section) return;
  const { other } = matchReceipts(recoveredAttempts, activeSubmission);
  const visible = other
    .filter((attempt) => OPEN_RECEIPT_STATUSES.includes(normalizeStatus(attempt.status)));
  const show = authenticated && (visible.length > 0 || recoveredError || recoveredLoading);
  section.hidden = !show;
  if (!show) {
    $("#recovered-results").replaceChildren();
    $("#recovered-unassociated").hidden = true;
    return;
  }

  const status = $("#recovered-status");
  status.hidden = !recoveredError && !recoveredLoading;
  status.textContent = recoveredError
    ? `${recoveredError} Showing the last known state; nothing was resent.`
    : recoveredLoading ? "Refreshing earlier deliveries…" : "";

  // A receipt on an account this submission sent to, with no exact reference or
  // echoed key, cannot be proven to be this post. It stays in this list — never
  // in "This submission" — and the copy says so instead of implying an outcome.
  // Only receipts created at or after this send are flagged: older ones are
  // plainly earlier work, and a timestamp is never used to attach anything.
  const since = Number(activeSubmission && activeSubmission.createdAt) || 0;
  const unassociated = visible.filter((attempt) => {
    const entry = activeSubmission && activeSubmission.destinations &&
      activeSubmission.destinations[attempt.account_id];
    if (!entry || !["sent", "unknown", "failed"].includes(entry.state)) return false;
    const created = secondsToMs(attempt.created_at) || 0;
    return since > 0 && created >= since;
  });
  const noteNode = $("#recovered-unassociated");
  noteNode.hidden = !unassociated.length;
  noteNode.textContent = unassociatedNote(unassociated.length);

  const refresh = $("#recovered-refresh");
  refresh.disabled = recoveredLoading;
  refresh.textContent = recoveredLoading ? "Refreshing…" : "Refresh";

  const list = $("#recovered-results");
  list.replaceChildren();
  for (const attempt of visible) {
    const entry = receiptToEntry(attempt);
    const account = accountList.find((candidate) => candidate.id === entry.accountId) || {
      id: entry.accountId,
      name: entry.accountName || "Removed channel",
      provider: entry.provider,
    };
    const action = receiptAction(attempt, { publishKey: storedPublishKey(attempt) });
    const actions = [];
    if (action.kind === "check") {
      actions.push({ label: "Check status", kind: "check", run: (button) => checkRecovered(attempt, button) });
    } else if (action.kind === "verify") {
      // An unknown outcome gets both: re-read the receipt (safe, never resends)
      // and open the provider. The link does not claim a status.
      actions.push({ label: "Check status", kind: "check", run: (button) => checkRecovered(attempt, button) });
    } else if (action.kind === "wait") {
      // A provider-requested wait is honoured. Checking re-reads the receipt and
      // never dispatches; once the deadline passes the server's own recipe
      // replaces this row's action without a page reload.
      actions.push({ label: "Check availability", kind: "wait", run: (button) => checkRecovered(attempt, button, "Check availability") });
    } else if (action.kind === "publish") {
      actions.push({ label: "Confirm on Instagram", kind: "publish", run: (button) => confirmRecovered(attempt, button) });
    } else if (action.kind === "edit_copy") {
      actions.push({ label: "Edit a copy", kind: "edit-copy", run: () => editCopyFor(attempt) });
    } else if (action.kind === "reconnect") {
      actions.push({ label: "Reconnect channel", kind: "reconnect", run: () => connect(entry.provider) });
    } else if (action.kind === "retry") {
      actions.push({ label: "Try again", kind: "retry", run: (button) => retryRecovered(attempt, button) });
    } else if (action.kind === "retry_publish") {
      actions.push({ label: "Try again", kind: "retry", run: (button) => retryRecovered(attempt, button) });
    }
    const row = resultRow({
      account,
      provider: entry.provider,
      status: entry.status,
      description: action.note || statusText(entry.status, entry),
      // One explanation, not two. When the action already explains the state
      // (a cooldown's end time, why a resend is unavailable), the raw server
      // error would only repeat it in different words.
      error: action.note ? null : entry.error,
      updatedAt: entry.updatedAt,
      url: entry.url,
      verifyLink: action.verificationUrl || null,
      actions,
      recovered: true,
    });
    if (unassociated.includes(attempt)) row.classList.add("unassociated");
    list.append(row);
  }

  const more = $("#recovered-more");
  more.hidden = !canLoadMore({ nextCursor: recoveredNextCursor, loading: recoveredLoading, loaded: recoveredLoaded });
  more.disabled = recoveredLoading;
}

async function loadRecoveredAttempts({ append = false } = {}) {
  if (!authenticated) {
    recoveredAttempts = [];
    recoveredNextCursor = null;
    recoveredLoaded = false;
    recoveredError = null;
    renderRecoveredSection();
    return;
  }
  const requestId = ++recoveredRequestId;
  recoveredLoading = true;
  recoveredError = null;
  renderRecoveredSection();
  try {
    const query = new URLSearchParams({ status: "active", limit: "50" });
    if (append && recoveredNextCursor) query.set("cursor", recoveredNextCursor);
    const result = await api(`/publishing/attempts?${query}`);
    if (requestId !== recoveredRequestId) return;
    const incoming = Array.isArray(result.attempts) ? result.attempts : [];
    if (append) {
      const seen = new Set(recoveredAttempts.map((attempt) => attempt.id));
      recoveredAttempts = [...recoveredAttempts, ...incoming.filter((attempt) => !seen.has(attempt.id))];
    } else {
      recoveredAttempts = incoming;
    }
    recoveredNextCursor = typeof result.next_cursor === "string" ? result.next_cursor : null;
    recoveredLoaded = true;
    syncSubmissionFromReceipts();
  } catch (error) {
    if (requestId !== recoveredRequestId) return;
    // Keep the last known state. A read failure is not evidence about delivery.
    recoveredError = error.message;
    recoveredLoaded = recoveredLoaded || Boolean(recoveredAttempts.length);
  } finally {
    if (requestId === recoveredRequestId) {
      recoveredLoading = false;
      renderRecoveredSection();
      renderComposerActions();
      // The rows in "This submission" borrow their action from the same receipt
      // list. Re-render them here so both lists settle from one completed state
      // instead of the submission row keeping a fallback chosen while the read
      // was still in flight.
      renderDeliverySection();
    }
  }
}

/* Rebuild the on-screen rows for a submission restored from the browser draft.
   This uses only the persisted snapshot: it never invents an outcome. A
   destination whose request was in flight when the page went away is shown as
   unconfirmed, with Check status as the only action. Receipts, when they load,
   replace these rows with the server's verdict. */
function hydrateResultsFromSubmission() {
  if (!activeSubmission || !activeSubmission.destinations) return;
  for (const [accountId, entry] of Object.entries(activeSubmission.destinations)) {
    if (!entry || entry.state === "idle") continue;
    const account = accountList.find((candidate) => candidate.id === accountId);
    if (!account) continue;
    const list = account.provider === "youtube" ? youtubeUploadResults : activeInstagramPosts;
    if (list.some((result) => result.account.id === accountId)) continue;
    const status = entry.state === "sent" ? "unknown" : entry.state;
    list.push({
      account,
      id: entry.postId || null,
      attemptId: entry.attemptId || null,
      status,
      url: null,
      verificationUrl: null,
      error: entry.state === "sent"
        ? "Relay sent this before the page reloaded. Check its status before doing anything else."
        : null,
      updatedAt: activeSubmission.createdAt || Date.now(),
      checking: false,
      restored: true,
      // Only a refusal that was safe to repeat is remembered across a reload;
      // anything else falls back to a deliberate new attempt.
      resend: entry.resend === true,
      attempts: 0,
      firstCheckedAt: null,
      nextCheckAt: null,
    });
  }
}

/* Mirror a receipt into the submission record so a reload reuses the same
   references and never re-sends a resolved destination. */
function syncSubmissionFromReceipts() {
  if (!activeSubmission) return;
  const { current } = matchReceipts(recoveredAttempts, activeSubmission);
  if (!current.length) return;
  let next = activeSubmission;
  for (const attempt of current) {
    const entry = next.destinations[attempt.account_id];
    if (!entry) continue;
    const status = normalizeStatus(attempt.status);
    next = recordDestination(next, {
      accountId: attempt.account_id,
      provider: attempt.provider,
      payload: entry.payloads[destinationAction(attempt.provider)],
      state: receiptState(status),
      attemptId: attempt.id,
      postId: attempt.post_id || null,
    });
  }
  activeSubmission = next;
  updateSubmissionEntries(current);
}

function receiptState(status) {
  if (status === "published") return "published";
  if (status === "failed") return "failed";
  if (status === "expired") return "expired";
  if (status === "ready") return "ready";
  if (status === "unknown") return "unknown";
  return "sent";
}

/* Replace the on-screen entries for receipts that belong to this submission. */
function updateSubmissionEntries(attempts) {
  for (const attempt of attempts) {
    const entry = receiptToEntry(attempt);
    const account = accountList.find((candidate) => candidate.id === entry.accountId);
    if (!account) continue;
    const list = entry.provider === "youtube" ? youtubeUploadResults : activeInstagramPosts;
    const existing = list.find((result) => result.account.id === entry.accountId);
    const merged = {
      account,
      // The exact receipt travels with the row so its action never depends on a
      // later list read.
      receipt: attempt,
      id: entry.postId,
      attemptId: entry.attemptId,
      status: entry.status,
      url: entry.url,
      verificationUrl: entry.verificationUrl,
      error: entry.error,
      updatedAt: entry.updatedAt,
      checking: false,
      attempts: existing?.attempts || 0,
      firstCheckedAt: existing?.firstCheckedAt || null,
      nextCheckAt: existing?.nextCheckAt || null,
    };
    if (existing) Object.assign(existing, merged);
    else list.push(merged);
  }
}

/* Re-read receipts for a destination whose response was lost. It adopts only a
   receipt that can belong to this submission (see matchReceipts); if none can,
   the destination stays unconfirmed. It never sends anything. */
async function recheckDestination(result) {
  const accountId = result.account.id;
  await loadRecoveredAttempts();
  await adoptReceiptFor(accountId);
  renderDeliverySection();
  const entry = activeSubmission && activeSubmission.destinations[accountId];
  if (entry && entry.state !== "unknown" && entry.state !== "sent") {
    notice("Relay found a receipt for this send. Its status is shown above.");
  } else {
    notice("Relay still has no receipt for this send. It is unconfirmed; nothing was resent.", true);
  }
}

/* ── Checking (never re-sends) ─────────────────────────────── */

/* Re-read one receipt. This is the only control a cooldown offers, and it is
   deliberately read-only: it never dispatches a create, an upload, or a publish.
   `label` restores the control's own wording ("Check availability" for a wait). */
async function checkRecovered(attempt, button, label = "Check status") {
  if (button) { button.disabled = true; button.textContent = "Checking…"; }
  try {
    if (attempt.provider === "instagram" && attempt.post_id) {
      await api(`/instagram/posts/${encodeURIComponent(attempt.post_id)}`);
    } else {
      await api(`/publishing/attempts/${encodeURIComponent(attempt.id)}`);
    }
    await loadRecoveredAttempts();
    if (historyLoaded) await loadPostHistory({ silent: true });
    // A cooldown that has not elapsed stays a cooldown. Say that plainly rather
    // than implying the check changed anything.
    const refreshed = recoveredAttempts.find((row) => row.id === attempt.id) || attempt;
    const action = receiptAction(refreshed, { publishKey: storedPublishKey(refreshed) });
    if (action.kind === "wait") {
      notice(`${action.note} Nothing was sent.`, false);
    } else if (action.kind === "retry" || action.kind === "retry_publish") {
      notice("The wait is over. Relay can send this again now — use Try again when you're ready. Nothing has been sent yet.");
    } else {
      notice("Status refreshed. Nothing was resent.");
    }
  } catch (error) {
    // A failed read says nothing about what the provider did.
    notice(`${error.message} The delivery is still unconfirmed. Nothing was resent.`, true);
  } finally {
    if (button && document.contains(button)) {
      button.disabled = false;
      button.textContent = label;
    }
  }
}

/* Confirm a ready Instagram container. This is the only action that can make
   an Instagram post live, and it is always explicit. */
async function confirmRecovered(attempt, button) {
  if (publishing) return;
  if (!attempt.post_id) return;
  if (button) { button.disabled = true; button.textContent = "Confirming…"; }
  publishing = true;
  renderComposerActions();
  try {
    const key = publishKeyFor(attempt);
    await api(`/instagram/posts/${encodeURIComponent(attempt.post_id)}/publish`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "Idempotency-Key": key },
      body: JSON.stringify({ idempotency_key: key }),
    });
    await loadRecoveredAttempts();
    if (historyLoaded) await loadPostHistory({ silent: true });
    notice("Instagram confirmed the post is live.");
  } catch (error) {
    // Re-read the authoritative receipt rather than guessing. A failed read
    // leaves the destination unresolved and offers no resend.
    await loadRecoveredAttempts();
    const receipt = recoveredAttempts.find((row) => row.id === attempt.id);
    if (receipt && normalizeStatus(receipt.status) === "published") {
      notice("Instagram confirmed the post is live.");
    } else {
      notice(`${error.message} The outcome is unconfirmed — check before trying again.`, true);
    }
  } finally {
    publishing = false;
    renderComposerActions();
    renderRecoveredSection();
  }
}

/* The confirmation key for a destination. See publishKeyForReceipt in
   recovery.js: a locally stored key is preferred, and a server-echoed
   ``publish_key`` is an exact correlation when the draft is gone. */
function storedPublishKey(attempt) {
  return publishKeyForReceipt(attempt, activeSubmission);
}

/* Retry a definitively failed destination.

   The backend re-arms this exact receipt when the same key is sent again with
   the same payload, so the request reuses the key the server named — never a
   freshly minted one. The server stays authoritative: a 429 cooldown or a 409
   (reconnect / already processing) is surfaced as-is, never looped. */
async function retryRecovered(attempt, button) {
  if (publishing) return;
  const action = receiptAction(attempt, { publishKey: storedPublishKey(attempt) });
  if (action.kind !== "retry" && action.kind !== "retry_publish") return;
  if (button) { button.disabled = true; button.textContent = "Trying again…"; }
  publishing = true;
  renderComposerActions();
  let response = null;
  try {
    if (action.kind === "retry_publish") {
      response = await api(action.endpoint, {
        method: "POST",
        headers: { "Content-Type": "application/json", "Idempotency-Key": action.publishKey },
        body: JSON.stringify({ idempotency_key: action.publishKey }),
      });
    } else {
      response = await api(action.endpoint, {
        method: "POST",
        headers: { "Content-Type": "application/json", "Idempotency-Key": action.requestKey },
        body: JSON.stringify({
          account_id: attempt.account_id,
          media_type: attempt.media_type,
          media_url: attempt.media_url,
          caption: attempt.caption || "",
          idempotency_key: action.requestKey,
        }),
      });
    }
    await loadRecoveredAttempts();
    if (historyLoaded) await loadPostHistory({ silent: true });
    // Report the outcome the server actually returned. A resolved receipt leaves
    // the active list, so "look above" would point at a row that is no longer
    // there; the response is the authoritative statement about this retry.
    const settled = recoveredAttempts.find((row) => row.id === attempt.id);
    const settledState = settled
      ? normalizeStatus(settled.status)
      : normalizeStatus(response && (response.status || (response.attempt && response.attempt.status)));
    if (settledState === "published") {
      notice("The retry was confirmed. This delivery is now published.");
    } else if (settledState === "processing" || settledState === "ready" || settledState === "submitted") {
      notice("The retry reached the provider. Its status is shown above.");
    } else if (settledState === "unknown") {
      notice("Relay could not confirm the retry. Check the provider before trying again.", true);
    } else {
      notice("Relay sent this attempt again. Its status is shown above.");
    }
  } catch (error) {
    // Re-read the receipt so the row shows the server's verdict, not a guess.
    await loadRecoveredAttempts();
    const receipt = recoveredAttempts.find((row) => row.id === attempt.id);
    const state = receipt ? normalizeStatus(receipt.status) : null;
    if (state === "published") {
      notice("The retry was confirmed. This delivery is now published.");
    } else if (state === "processing" || state === "ready" || state === "submitted") {
      notice("The retry reached the provider. Its status is shown above.");
    } else if (error.status === 429) {
      notice(`${error.message} Relay will not resend until the provider's wait is over.`, true);
    } else if (error.status === 409) {
      notice(`${error.message} Nothing was resent.`, true);
    } else {
      notice(`${error.message} The outcome is unconfirmed — check before trying again.`, true);
    }
  } finally {
    publishing = false;
    renderComposerActions();
    renderDeliverySection();
    renderRecoveredSection();
  }
}

function publishKeyFor(attempt) {
  const entry = activeSubmission && activeSubmission.destinations[attempt.account_id];
  if (entry && entry.keys && entry.keys.publish) return entry.keys.publish;
  return destinationKey({
    userId: activeUserId,
    workspaceId: activeWorkspaceId,
    submissionId: attempt.post_id || attempt.id,
    accountId: attempt.account_id,
    action: "publish",
    payload: { post_id: attempt.post_id, provider_ref: attempt.provider_ref },
  });
}

/* ── Publishing a new submission ───────────────────────────── */

function resetDeliveryState() {
  stopStatusPolling();
  activeInstagramPosts = [];
  youtubeUploadResults = [];
  publishing = false;
}

function draftSnapshot() {
  return {
    caption: $("#publish-caption").value,
    title: $("#youtube-publish-title").value,
    description: $("#youtube-publish-description").value,
    visibility: $("#youtube-publish-privacy").value,
    madeForKids: $("#youtube-made-for-kids").checked,
    selectedContentId,
    selectedAccountIds: [...selectedAccountIds],
    submission: activeSubmission ? cloneSubmission(activeSubmission) : null,
    savedAt: Date.now(),
  };
}

/* The semantic payload for a destination. The same values produce the same
   idempotency key; any edit produces a different one. */
function instagramPayload(account, item, caption) {
  return {
    account_id: account.id,
    media_type: item.content_type === "image/jpeg" ? "IMAGE" : "REELS",
    media_url: item.media_url,
    caption,
  };
}

function youtubePayload(account, item, fields) {
  return {
    account_id: account.id,
    title: fields.title,
    description: fields.description,
    privacy_status: fields.privacy,
    made_for_kids: fields.madeForKids,
    content_id: item.id,
    filename: item.filename,
    size: item.size,
    content_type: item.content_type,
  };
}

/* Decide, per destination, whether this Send may proceed. A destination that
   is already resolved or uncertain is skipped and explained; it is never
   silently resent with a fresh key. */
function planSubmission({ accounts, item, fields }) {
  const plans = [];
  for (const account of accounts) {
    const payload = account.provider === "youtube"
      ? youtubePayload(account, item, fields)
      : instagramPayload(account, item, fields.caption);
    const plan = planDestination(activeSubmission, {
      accountId: account.id,
      provider: account.provider,
      payload,
    });
    plans.push({ account, payload, plan });
  }
  return plans;
}

async function publishPost() {
  if (publishing) return;
  if (!authenticated) { showUnlock(); return; }
  const item = selectedMedia();
  const accounts = selectedAccounts();
  if (!item || !accounts.length) {
    notice("Choose media and at least one channel first.", true);
    return;
  }
  if (accounts.some((account) => account.provider === "youtube") && !$("#youtube-publish-title").value.trim()) {
    notice("YouTube needs a title before it can upload.", true);
    $("#youtube-publish-title").focus();
    return;
  }

  const fields = {
    caption: $("#publish-caption").value,
    title: $("#youtube-publish-title").value.trim(),
    description: $("#youtube-publish-description").value || $("#publish-caption").value,
    privacy: $("#youtube-publish-privacy").value,
    madeForKids: $("#youtube-made-for-kids").checked,
  };

  // A new submission is created only when there is no persisted one, or when
  // the persisted one has no destination left to send. Otherwise the existing
  // intent (and its keys) is reused, which is what makes a reload harmless.
  if (!activeSubmission || !Object.keys(activeSubmission.destinations || {}).length) {
    activeSubmission = createSubmission({
      userId: activeUserId,
      workspaceId: activeWorkspaceId,
      destinations: accounts.map((account) => ({
        accountId: account.id,
        provider: account.provider,
        payload: account.provider === "youtube"
          ? youtubePayload(account, item, fields)
          : instagramPayload(account, item, fields.caption),
      })),
    });
  }

  const plans = planSubmission({ accounts, item, fields });
  const sendable = plans.filter(({ plan }) => plan.ok);
  const blocked = plans.filter(({ plan }) => !plan.ok);
  if (!sendable.length) {
    const reasons = [...new Set(blocked.map(({ plan }) => plan.reason))];
    const message = reasons.includes("unresolved")
      ? "These destinations are still unconfirmed. Check their status instead of sending again."
      : reasons.includes("published")
        ? "These destinations were already published. Start a new post to send something else."
        : "These destinations need a new attempt. Use Edit a copy.";
    notice(message, true);
    return;
  }

  saveLocalDraft({ announce: false });
  activePublishDraft = draftSnapshot();
  publishing = true;
  // Until this send's own receipts have been read, a row must not fall back to
  // "Edit a copy": the list has not caught up yet, and the server may be about
  // to name a cooldown or a reconnect for the destination.
  receiptsSyncing = true;
  renderComposer();

  // Instagram: create the container, then poll. Publishing is a separate,
  // explicit confirmation, so nothing goes live from this call.
  for (const { account, payload, plan } of sendable.filter(({ account }) => account.provider === "instagram")) {
    let entry = activeInstagramPosts.find((post) => post.account.id === account.id);
    if (!entry) {
      entry = { account, id: null, attemptId: null, status: "publishing", updatedAt: Date.now(), attempts: 0, firstCheckedAt: null, nextCheckAt: null };
      activeInstagramPosts.push(entry);
    }
    entry.status = "publishing";
    entry.error = null;
    activeSubmission = recordDestination(activeSubmission, {
      accountId: account.id,
      provider: "instagram",
      payload,
      state: "sent",
    });
    saveLocalDraft({ announce: false });
    renderDeliverySection();
    try {
      const post = await api("/instagram/posts", {
        method: "POST",
        headers: { "Content-Type": "application/json", "Idempotency-Key": plan.key },
        body: JSON.stringify({ ...payload, idempotency_key: plan.key }),
      });
      if (!post || !post.id) {
        // A 202 means the receipt is still resolving. Never treat it as success,
        // but keep the receipt reference so Check status can read it.
        entry.status = "unknown";
        entry.attemptId = post?.attempt?.id || null;
        entry.updatedAt = Date.now();
        entry.error = "Relay is still resolving this send. Check its status before doing anything else.";
      } else {
        entry.id = post.id;
        entry.attemptId = post.attempt?.id || null;
        entry.status = post.status || "processing";
        entry.updatedAt = Date.now();
        entry.firstCheckedAt = Date.now();
        entry.nextCheckAt = Date.now() + POLLING.baseDelayMs;
        activeSubmission = recordDestination(activeSubmission, {
          accountId: account.id,
          provider: "instagram",
          payload,
          state: receiptState(entry.status),
          postId: post.id,
          attemptId: post.attempt?.id || null,
        });
      }
    } catch (error) {
      entry.updatedAt = Date.now();
      entry.error = error.message;
      // Classify by what the failure actually proves. A refusal from Relay (an
      // ended session, a rejected field, a storage write known not to have been
      // applied) never dispatched anything, so it is not a provider verdict; an
      // ambiguous failure may have been delivered and stays unknown. The receipt
      // read below replaces this guess with the server's own state when one exists.
      const state = sendFailureState(error);
      entry.status = state.state;
      entry.resend = state.resend;
      activeSubmission = recordDestination(activeSubmission, {
        accountId: account.id,
        provider: "instagram",
        payload,
        state: state.state,
        resend: state.resend,
      });
      await adoptReceiptFor(account.id);
    }
    saveLocalDraft({ announce: false });
    renderDeliverySection();
  }

  // YouTube: one upload per channel. A 202 is an in-flight duplicate, not a
  // success; the receipt is read instead of assuming anything.
  for (const { account, payload, plan } of sendable.filter(({ account }) => account.provider === "youtube")) {
    let entry = youtubeUploadResults.find((result) => result.account.id === account.id);
    if (!entry) {
      entry = { account, url: null, attemptId: null, status: "submitted", error: null, updatedAt: Date.now() };
      youtubeUploadResults.push(entry);
    }
    entry.status = "submitted";
    entry.attemptId = null;
    entry.error = null;
    activeSubmission = recordDestination(activeSubmission, {
      accountId: account.id,
      provider: "youtube",
      payload,
      state: "sent",
    });
    saveLocalDraft({ announce: false });
    renderDeliverySection();
    const form = new FormData();
    form.append("account_id", account.id);
    form.append("title", fields.title);
    form.append("description", fields.description);
    form.append("privacy_status", fields.privacy);
    form.append("made_for_kids", String(fields.madeForKids));
    form.append("content_id", item.id);
    form.append("idempotency_key", plan.key);
    try {
      const uploaded = await api("/youtube/videos", {
        method: "POST",
        headers: { "Idempotency-Key": plan.key },
        body: form,
      });
      if (uploaded && uploaded.url) {
        entry.url = uploaded.url;
        entry.status = "published";
        entry.privacyStatus = uploaded.privacy_status;
        entry.updatedAt = Date.now();
        activeSubmission = recordDestination(activeSubmission, {
          accountId: account.id,
          provider: "youtube",
          payload,
          state: "published",
          attemptId: uploaded.attempt?.id || null,
        });
      } else {
        // 202: the key is bound to an upload that is still resolving. Keep the
        // receipt reference so Check status can read it; never call it success.
        entry.status = "unknown";
        entry.attemptId = uploaded?.attempt?.id || null;
        entry.updatedAt = Date.now();
        entry.error = "Relay is still resolving this upload. Check YouTube Studio before doing anything else.";
        activeSubmission = recordDestination(activeSubmission, {
          accountId: account.id,
          provider: "youtube",
          payload,
          state: "unknown",
          attemptId: uploaded?.attempt?.id || null,
        });
      }
    } catch (error) {
      entry.updatedAt = Date.now();
      entry.error = error.message;
      // Same classification as the Instagram path: Relay's own refusal is not a
      // provider verdict, and an ambiguous failure is never called failed.
      const state = sendFailureState(error);
      entry.status = state.state;
      entry.resend = state.resend;
      activeSubmission = recordDestination(activeSubmission, {
        accountId: account.id,
        provider: "youtube",
        payload,
        state: state.state,
        resend: state.resend,
      });
      await adoptReceiptFor(account.id);
    }
    saveLocalDraft({ announce: false });
    renderDeliverySection();
  }

  publishing = false;
  saveLocalDraft({ announce: false });
  renderComposer();
  renderDeliverySection();
  // Read the authoritative receipts, then re-render both lists from that one
  // completed state. `loadRecoveredAttempts` re-renders the delivery section in
  // its own finally block; clearing the flag here would let a row fall back to
  // a copy before the read lands, so it is cleared after the read resolves.
  try {
    await loadRecoveredAttempts();
  } finally {
    receiptsSyncing = false;
    renderDeliverySection();
  }
  if (youtubeUploadResults.some((result) => result.status === "published")) await loadPostHistory({ silent: true });
  if (currentResults().some((result) => ["processing", "submitted", "unknown"].includes(result.status))) {
    scheduleStatusPolling();
  }
  maybeClearDraft();
}

/* After a failure, read the authoritative receipt so the UI shows the server's
   verdict rather than a local guess. A failed read changes nothing.

   Only a receipt that plausibly belongs to THIS submission is adopted: one whose
   id/post id is already stored, or — when the response was lost — one created
   after this submission started for an account that has no reference yet. An
   older, unrelated receipt is never attached to the current post. */
async function adoptReceiptFor(accountId) {
  if (!activeSubmission) return;
  try {
    const result = await api(`/publishing/attempts?status=active&limit=50`);
    const attempts = Array.isArray(result.attempts) ? result.attempts : [];
    const { current } = matchReceipts(attempts, activeSubmission);
    const match = current.find((attempt) => attempt.account_id === accountId);
    if (!match) return;
    const entry = receiptToEntry(match);
    const instagram = activeInstagramPosts.find((post) => post.account.id === accountId);
    const youtube = youtubeUploadResults.find((post) => post.account.id === accountId);
    const target = instagram || youtube;
    if (target) {
      // Keep the receipt itself on the row. The action for a failed destination
      // is derived from this exact receipt, so the row no longer depends on the
      // list read having finished.
      target.receipt = match;
      target.attemptId = entry.attemptId;
      target.status = entry.status;
      target.error = entry.error;
      target.url = entry.url || target.url;
      target.verificationUrl = entry.verificationUrl;
      target.updatedAt = entry.updatedAt;
    }
    if (activeSubmission.destinations[accountId]) {
      activeSubmission = recordDestination(activeSubmission, {
        accountId,
        provider: match.provider,
        payload: activeSubmission.destinations[accountId].payloads[destinationAction(match.provider)],
        state: receiptState(entry.status),
        attemptId: entry.attemptId,
        postId: entry.postId,
      });
      saveLocalDraft({ announce: false });
    }
  } catch {
    // Keep the existing uncertainty. Never invent a terminal state.
  }
}

function maybeClearDraft() {
  const results = currentResults();
  if (!results.length) return;
  const outcome = deliveryOutcome(results);
  if (outcome.confirmed === outcome.total) {
    clearPublishedDraft(activePublishDraft);
    activeSubmission = null;
  }
}

/* ── Status polling (bounded, visibility-aware) ────────────── */

function stopStatusPolling() {
  if (instagramStatusTimer !== null) window.clearTimeout(instagramStatusTimer);
  instagramStatusTimer = null;
}

function pollableResults() {
  return currentResults().filter((result) =>
    (result.id || result.attemptId) &&
    ["processing", "submitted", "unknown", "publishing"].includes(result.status));
}

function scheduleStatusPolling() {
  stopStatusPolling();
  if (!authenticated) return;
  const tracked = pollableResults();
  if (!tracked.length) return;
  const now = Date.now();
  const decisions = tracked.map((result) => nextPollDecision(result, now, { hidden: document.hidden }));
  const due = decisions.filter((decision) => decision.poll);
  const waits = decisions.filter((decision) => decision.reason === "waiting").map((decision) => decision.delayMs);
  const delay = due.length ? 0 : waits.length ? Math.min(...waits) : null;
  if (delay === null) {
    tracked.forEach((result) => { result.pollingStopped = true; });
    renderDeliverySection();
    return;
  }
  instagramStatusTimer = window.setTimeout(() => {
    instagramStatusTimer = null;
    void checkDeliveryStatuses();
  }, Math.max(250, delay));
}

async function checkDeliveryStatuses({ manual = false } = {}) {
  if (instagramStatusCheckRunning) return;
  const now = Date.now();
  const tracked = pollableResults();
  if (!tracked.length) { renderDeliverySection(); return; }

  const due = manual ? tracked : tracked.filter((result) => nextPollDecision(result, now, { hidden: document.hidden }).poll);
  if (!due.length) { scheduleStatusPolling(); return; }

  instagramStatusCheckRunning = true;
  renderDeliverySection();
  let newlyPublished = false;
  let readFailure = false;

  for (const result of due) {
    result.checking = true;
    renderDeliverySection();
    try {
      if (result.account.provider === "instagram" && result.id) {
        const updated = await api(`/instagram/posts/${encodeURIComponent(result.id)}`);
        newlyPublished ||= result.status !== "published" && updated.status === "published";
        result.status = updated.status;
        result.error = null;
      } else if (result.attemptId) {
        // YouTube: a receipt read. It cannot recover bytes, so it only confirms
        // or leaves the upload unconfirmed.
        const updated = await api(`/publishing/attempts/${encodeURIComponent(result.attemptId)}`);
        const status = normalizeStatus(updated.status);
        newlyPublished ||= result.status !== "published" && status === "published";
        result.status = status;
        result.error = updated.error || null;
        result.url = safeHttpsUrl(updated.provider_url) || result.url;
        result.verificationUrl = safeHttpsUrl(updated.verification_url);
      } else {
        result.checking = false;
        continue;
      }
      result.checking = false;
      result.updatedAt = Date.now();
      Object.assign(result, afterCheck(result, Date.now()));
      if (activeSubmission && activeSubmission.destinations[result.account.id]) {
        activeSubmission = recordDestination(activeSubmission, {
          accountId: result.account.id,
          provider: result.account.provider,
          payload: activeSubmission.destinations[result.account.id].payloads[destinationAction(result.account.provider)],
          state: receiptState(result.status),
        });
      }
    } catch (error) {
      // A failed read does not change what the provider may have done.
      result.checking = false;
      result.error = `Status check failed: ${error.message}`;
      result.updatedAt = Date.now();
      readFailure = true;
      Object.assign(result, afterCheck(result, Date.now(), { failed: true }));
    }
    renderDeliverySection();
  }

  instagramStatusCheckRunning = false;
  saveLocalDraft({ announce: false });
  if (newlyPublished) await loadPostHistory({ silent: true });
  maybeClearDraft();
  renderDeliverySection();
  if (readFailure) {
    setText("#composer-progress-detail", "Status verification is unavailable right now. Nothing was resent; try Check now when the connection is back.");
  }
  await loadRecoveredAttempts();
  scheduleStatusPolling();
}

/* ── Re-reading a job after a failed publish ───────────────── */

async function refreshPostFromServer(post) {
  if (!post.id) return;
  try {
    const updated = await api(`/instagram/posts/${encodeURIComponent(post.id)}`);
    post.status = updated.status;
    post.error = null;
    post.updatedAt = Date.now();
  } catch {
    // Keep the existing uncertainty; do not invent a terminal state.
  }
  renderDeliverySection();
}

/* ── Explicit Instagram confirmation for this submission ───── */

async function publishReadyPost(post) {
  if (publishing || !post.id) return;
  publishing = true;
  post.status = "publishing";
  post.updatedAt = Date.now();
  post.error = null;
  renderComposer();
  renderDeliverySection();
  try {
    const key = publishKeyFor({ account_id: post.account.id, post_id: post.id, id: post.attemptId });
    const updated = await api(`/instagram/posts/${encodeURIComponent(post.id)}/publish`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "Idempotency-Key": key },
      body: JSON.stringify({ idempotency_key: key }),
    });
    post.status = updated.status;
    post.updatedAt = Date.now();
    post.error = null;
    Object.assign(post, afterCheck(post, Date.now()));
    if (activeSubmission && activeSubmission.destinations[post.account.id]) {
      activeSubmission = recordDestination(activeSubmission, {
        accountId: post.account.id,
        provider: "instagram",
        payload: activeSubmission.destinations[post.account.id].payloads.create,
        state: receiptState(post.status),
      });
    }
  } catch (error) {
    post.error = error.message;
    post.updatedAt = Date.now();
    await refreshPostFromServer(post);
    if (post.status === "ready" || post.status === "publishing") post.status = "unknown";
    Object.assign(post, afterCheck(post, Date.now(), { failed: true }));
  } finally {
    publishing = false;
    saveLocalDraft({ announce: false });
    renderComposer();
    renderDeliverySection();
    if (post.status === "published") await loadPostHistory({ silent: true });
    await loadRecoveredAttempts();
    maybeClearDraft();
    scheduleStatusPolling();
  }
}

/* ── Edit a copy ──────────────────────────────────────────────
   The safe alternative to an unsupported resend. It starts a NEW submission
   (new keys) with only the destinations that are not confirmed, so a published
   destination can never be sent twice.

   A copy REPLACES the composer's content with the receipt's own content. It is
   never merged into whatever happened to be on screen: blending one post's
   photo with another post's caption produces a post neither person wrote. When
   the composer already holds unsent work, the person chooses Replace or Keep
   before anything changes. */

/* The receipt (or exact persisted payload) a copy is built from. A recovered
   receipt is authoritative; the submission's own payload is used only for the
   same account when no receipt is loaded. */
function copySourceForResult(result) {
  if (!result) return null;
  // A recovered receipt passed straight from the list is itself the receipt.
  const isAttempt = !result.account && Boolean(result.account_id) && Boolean(result.provider);
  const accountId = result.account ? result.account.id : result.account_id || null;
  const provider = result.account ? result.account.provider : result.provider || null;
  const receipt = isAttempt
    ? result
    : (result.receipt || receiptForResult(result, recoveredAttempts, activeSubmission));
  return copySourceFor({ receipt, submission: activeSubmission, accountId, provider });
}

/* Does the composer currently hold work that a copy would overwrite? Only text
   and media count: a channel selection alone is not content. */
function composerHasUnsentWork() {
  return Boolean(
    $("#publish-caption").value.trim() ||
    $("#youtube-publish-title").value.trim() ||
    $("#youtube-publish-description").value.trim() ||
    selectedContentId,
  );
}

function applyCopyPlan(plan, { previousDraft, previousSubmission, openAccountIds }) {
  resetDeliveryState();
  activeSubmission = null;
  // The copy's destinations are the unresolved ones from the earlier send, plus
  // the channel the copy came from. Published destinations are never included.
  selectedAccountIds = new Set(openAccountIds);
  if (!selectedAccountIds.size && plan.accountId) selectedAccountIds.add(plan.accountId);
  selectedContentId = plan.media.item ? plan.media.item.id : null;
  // Replacement, not merge: every field the receipt owns is set, including to
  // empty. Nothing from an unrelated draft survives.
  $("#publish-caption").value = plan.caption || "";
  $("#youtube-publish-title").value = plan.title || "";
  $("#youtube-publish-description").value = plan.description || "";
  if (plan.visibility) $("#youtube-publish-privacy").value = plan.visibility;
  saveLocalDraft({ announce: false });
  renderComposer();
  setPage("publish");
  notice(copyNotice(plan), false, {
    label: "Undo",
    run() {
      activeSubmission = previousSubmission;
      if (previousDraft) restoreLocalDraft(previousDraft);
      saveLocalDraft({ announce: false });
      renderComposer();
    },
  });
  window.scrollTo({ top: 0, behavior: "instant" });
}

function editCopyFor(result) {
  const source = copySourceForResult(result);
  if (!source) {
    notice("Relay can't find this delivery's content. Nothing was changed.", true);
    return;
  }
  saveLocalDraft({ announce: false });
  const previousDraft = readLocalDraft();
  const previousSubmission = activeSubmission ? cloneSubmission(activeSubmission) : null;
  const sourceAccountId = source.account_id || (result && result.account ? result.account.id : null);
  // A copy of a row in "This submission" carries that submission's unresolved
  // destinations, so a partial retry stays one deliberate send. A copy of an
  // earlier delivery selects only that delivery's channel: it must not drag in
  // whatever happens to be open in the current composer.
  const fromCurrentSubmission = Boolean(
    result && result.account && currentResults().some((entry) => entry.account.id === result.account.id),
  );
  const openAccountIds = fromCurrentSubmission
    ? copyDestinations(currentResults(), null)
    : new Set(sourceAccountId ? [sourceAccountId] : []);
  const plan = copyPlan(source, contentItems);
  plan.accountId = sourceAccountId;
  plan.visibility = source.privacy_status || null;
  const context = { plan, previousDraft, previousSubmission, openAccountIds };

  if (composerHasUnsentWork()) {
    // Two unsent posts must not be blended. Ask, and change nothing until the
    // person chooses.
    askCopyChoice({
      ...context,
      currentText: $("#publish-caption").value.trim() || $("#youtube-publish-title").value.trim(),
      copiedText: plan.caption || plan.title || plan.description,
    });
    return;
  }
  applyCopyPlan(plan, context);
}

/* An explicit choice before a copy replaces unsent work. The dialog is built in
   JS so this file owns it end to end, and Cancel leaves the composer untouched. */
let pendingCopyChoice = null;

function copyChoiceDialog() {
  let dialog = document.getElementById("copy-choice-dialog");
  if (dialog) return dialog;
  dialog = document.createElement("dialog");
  dialog.id = "copy-choice-dialog";
  dialog.className = "dialog";
  dialog.setAttribute("aria-labelledby", "copy-choice-title");

  const top = el("div", "dialog-top");
  const heading = el("h2", null, "Replace your current post?");
  heading.id = "copy-choice-title";
  const close = el("button", "icon-button icon-button-sm", "×");
  close.type = "button";
  close.setAttribute("aria-label", "Cancel and keep your current post");
  close.addEventListener("click", () => resolveCopyChoice(false));
  top.append(heading, close);

  const intro = el("p", null,
    "Your composer already has unsent work. A copy of the earlier delivery replaces it rather than mixing the two.");
  // The label carries its own separator so the comparison still reads correctly
  // if the stylesheet has no rule for this dialog: the two texts must never run
  // together into one sentence.
  const current = el("p", "copy-choice-compare");
  current.append(el("strong", null, "Your current post: "), el("span", null, ""));
  current.lastElementChild.id = "copy-choice-current";
  const incoming = el("p", "copy-choice-compare");
  incoming.append(el("strong", null, "The earlier delivery: "), el("span", null, ""));
  incoming.lastElementChild.id = "copy-choice-incoming";
  const note = el("p", "field-meta");
  note.id = "copy-choice-note";

  const actions = el("div", "dialog-actions");
  const keep = el("button", "button button-ghost", "Keep my post");
  keep.type = "button";
  keep.addEventListener("click", () => resolveCopyChoice(false));
  const replace = el("button", "button button-primary", "Replace with the copy");
  replace.type = "button";
  replace.addEventListener("click", () => resolveCopyChoice(true));
  actions.append(keep, replace);

  dialog.append(top, intro, current, incoming, note, actions);
  dialog.addEventListener("cancel", (event) => {
    // Escape is a cancel, never a silent replace.
    event.preventDefault();
    resolveCopyChoice(false);
  });
  document.body.append(dialog);
  return dialog;
}

function askCopyChoice({ plan, previousDraft, previousSubmission, openAccountIds, currentText, copiedText }) {
  const dialog = copyChoiceDialog();
  pendingCopyChoice = { plan, previousDraft, previousSubmission, openAccountIds };
  const shorten = (text) => (text.length > 90 ? `${text.slice(0, 90).trimEnd()}…` : text);
  setText("#copy-choice-current", currentText ? `“${shorten(currentText)}”` : "Your current unsent post");
  setText("#copy-choice-incoming", copiedText ? `“${shorten(copiedText)}”` : "The earlier delivery's content");
  setText("#copy-choice-note", plan.complete
    ? "Replacing discards your current text and media. The earlier delivery is unchanged either way."
    : copyNotice(plan));
  openDialog(dialog, document.activeElement);
}

function resolveCopyChoice(replace) {
  const pending = pendingCopyChoice;
  pendingCopyChoice = null;
  const dialog = document.getElementById("copy-choice-dialog");
  if (dialog && dialog.open) dialog.close();
  if (!pending) return;
  if (!replace) {
    notice("Kept your current post. Nothing was copied.");
    return;
  }
  applyCopyPlan(pending.plan, pending);
}

/* ── Wiring ─────────────────────────────────────────────────── */

$("#publish-form").addEventListener("submit", (event) => {
  event.preventDefault();
  void publishPost();
});

$("#publish-new-post").addEventListener("click", () => {
  if (publishing || $("#publish-new-post").disabled) return;
  const outcome = deliveryOutcome(currentResults());
  if (outcome.confirmed === outcome.total) {
    // Everything landed: start clean.
    saveLocalDraft({ announce: false });
    resetDeliveryState();
    activeSubmission = null;
    selectedContentId = null;
    selectedAccountIds.clear();
    $("#publish-caption").value = "";
    $("#youtube-publish-title").value = "";
    $("#youtube-publish-description").value = "";
    $("#youtube-publish-privacy").value = "private";
    $("#youtube-made-for-kids").checked = false;
    saveLocalDraft({ announce: false });
    renderComposer();
    notice("Ready for a new post.");
    window.scrollTo({ top: 0, behavior: "instant" });
    return;
  }
  // Unresolved work stays on the server: clearing this submission moves its
  // receipts to "Earlier deliveries still open", where they remain actionable.
  // The composer keeps the person's text, so this is deliberate new work rather
  // than a silent resend — and it never claims to honour a channel-wide wait.
  saveLocalDraft({ announce: false });
  const previousDraft = readLocalDraft();
  resetDeliveryState();
  activeSubmission = null;
  saveLocalDraft({ announce: false });
  renderComposer();
  void loadRecoveredAttempts();
  notice("Started a new post. The earlier delivery is unchanged and still listed below.", false, {
    label: "Undo",
    run() {
      if (previousDraft) restoreLocalDraft(previousDraft);
      else renderComposer();
    },
  });
  window.scrollTo({ top: 0, behavior: "instant" });
});

$("#instagram-check-all").addEventListener("click", () => void checkDeliveryStatuses({ manual: true }));

$("#instagram-publish-ready").addEventListener("click", async () => {
  const ready = activeInstagramPosts.filter((post) => post.id && post.status === "ready");
  for (const post of ready) await publishReadyPost(post);
});

$("#recovered-refresh").addEventListener("click", () => void loadRecoveredAttempts());
$("#recovered-more").addEventListener("click", () => void loadRecoveredAttempts({ append: true }));

/* Pause polling while the tab is hidden; resume when it returns. */
document.addEventListener("visibilitychange", () => {
  if (document.hidden) {
    stopStatusPolling();
  } else if (pollableResults().length) {
    scheduleStatusPolling();
  }
  renderDeliverySection();
});

$("#palette-button").addEventListener("click", () => openPalette());
$("#palette-trigger").addEventListener("click", () => openPalette());
