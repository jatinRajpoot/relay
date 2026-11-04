"use strict";

/* ── Activity ──────────────────────────────────────────────────
   Activity shows what Relay actually recorded:
   - durable publishing receipts (in flight, failed, unverified, published)
   - legacy post history, merged so a confirmed delivery is never listed twice
   - the browser-local draft

   There is no invented failure and no server-side draft. Filters match the
   receipt's real phase; "Unverified" is uncertainty, not failure.

   Pagination is cursor-based from the server's next_cursor. The first page is
   never presented as the whole history: the footer says whether older
   deliveries exist and offers Load more. */

let activityAttempts = [];
let activityCursor = null;
let activityLoaded = false;
let activityLoading = false;
let activityStale = false;

/* Every control a receipt row may offer. `retry` and `retry_publish` are the
   server's own resend instructions; `reconnect` is a credential repair; `wait`
   is a read-only availability check while the provider's cooldown runs. None of
   them is inferred from a status — each comes from the receipt's retry_action. */
const ACTION_KINDS = ["verify", "check", "wait", "edit_copy", "publish", "retry", "retry_publish", "reconnect"];

function historyStatusLabel(status) {
  return {
    published: "Published",
    pending: "Queued",
    submitted: "Sent",
    publishing: "Publishing",
    processing: "Processing",
    ready: "Ready · not live",
    failed: "Not published",
    expired: "Expired",
    unknown: "Verify",
  }[status] || "Unknown";
}

function activityRow(row) {
  // A generic container, not <article>: `role="listitem"` is not allowed on
  // <article>, and the list semantics are what a screen reader needs here. The
  // row stays a child of the existing role="list" container.
  const article = el("div", "history-row");
  article.setAttribute("role", "listitem");

  if (row.source === "draft") {
    const draft = row.draft;
    const preview = el("div", "history-preview");
    preview.append(icon("media"));
    const copy = el("div", "history-copy");
    copy.append(
      el("h2", null, draftTitle(draft)),
      el("div", "history-platform", "Draft · saved in this browser"),
    );
    const meta = el("div", "history-meta");
    const savedAt = new Date(Number(draft.savedAt) || Date.now());
    meta.append(
      el("span", "history-status draft", "Draft"),
      el("span", "history-when", savedAt.toLocaleDateString(undefined, { month: "short", day: "numeric" })),
    );
    const open = el("button", "button button-ghost button-sm", "Resume");
    open.type = "button";
    open.addEventListener("click", () => { restoreLocalDraft(draft); setPage("publish"); });
    meta.append(open);
    article.append(preview, copy, meta);
    return article;
  }

  const provider = row.provider;
  const preview = el("div", `history-preview ${provider}`);
  const youtubeThumbnail = provider === "youtube" && /^[A-Za-z0-9_-]{1,64}$/.test(row.provider_ref || "")
    ? `https://i.ytimg.com/vi/${row.provider_ref}/hqdefault.jpg`
    : null;
  const imageUrl = youtubeThumbnail ||
    (row.media_type === "IMAGE" || row.media_type?.startsWith("image/") ? row.media_url : null);
  if (imageUrl?.startsWith("https://")) {
    const image = document.createElement("img");
    image.src = imageUrl;
    image.alt = "";
    image.loading = "lazy";
    // A media URL can expire or be unreachable. Fall back to the platform mark
    // rather than showing a broken-image icon.
    image.addEventListener("error", () => {
      image.remove();
      if (!preview.childElementCount) preview.append(icon(provider === "youtube" ? "youtube" : "instagram"));
    }, { once: true });
    preview.append(image);
  } else {
    preview.append(icon(provider === "youtube" ? "youtube" : "instagram"));
  }

  const copy = el("div", "history-copy");
  const platform = el("div", `history-platform ${provider}`);
  platform.append(icon(provider === "youtube" ? "youtube" : "instagram"));
  platform.append(el("strong", null, names[provider] || "Channel"),
    el("span", null, row.account_name || ""));
  copy.append(el("h2", null, activityTitle(row)), platform);
  if (row.media_name) copy.append(el("small", "history-filename", row.media_name));

  const action = row.action || receiptAction(row);
  // Exactly one explanatory line. A failed receipt explains why it cannot be
  // retried (including when a cooldown ends); an unverified one explains what is
  // unknown. The raw server error is the fallback, never a second copy of the
  // same sentence.
  const explanation = action.note || row.error || null;
  if (explanation) copy.append(el("small", "history-note", explanation));

  const meta = el("div", "history-meta");
  const when = row.timestamp ? new Date(row.timestamp) : null;
  const time = el("time", null, when ? when.toLocaleString(undefined, {
    month: "short", day: "numeric", year: "numeric", hour: "numeric", minute: "2-digit",
  }) : "Time unknown");
  if (when) time.dateTime = when.toISOString();
  meta.append(el("span", `history-status ${row.phase}`, phaseLabel(row.phase)), time);
  if (row.privacy_status) meta.append(el("span", "history-visibility", row.privacy_status));

  if (row.provider_url && safeHttpsUrl(row.provider_url)) {
    const link = el("a", "history-open-link");
    link.href = safeHttpsUrl(row.provider_url);
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    link.setAttribute("aria-label", provider === "youtube" ? "Open video" : "Open post");
    link.append(icon("external"));
    meta.append(link);
  } else if (action.kind === "verify" && action.verificationUrl) {
    const link = el("a", "history-open-link");
    link.href = action.verificationUrl;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    link.setAttribute("aria-label", `Open ${names[provider] || "the provider"} to check this delivery`);
    link.append(icon("external"));
    meta.append(link);
  }
  if (row.source === "receipt" && ACTION_KINDS.includes(action.kind)) {
    // A status re-read is a button; the provider link above is the external
    // check. Their labels say which is which. Every kind the server's
    // retry_action can name gets its control here too, so a failed delivery is
    // actionable from Activity — not only from the composer. A cooldown's
    // control is a read-only availability check, never a bypass.
    const label = action.kind === "verify" ? "Check status" : action.label;
    const button = el("button", "button button-ghost button-sm", label);
    button.type = "button";
    button.addEventListener("click", () => void runActivityAction(row, button));
    meta.append(button);
  }
  article.append(preview, copy, meta);
  return article;
}

async function runActivityAction(row, button) {
  const attempt = activityAttempts.find((item) => item.id === row.id) || row;
  const action = row.action || receiptAction(attempt);
  if (action.kind === "edit_copy") {
    editCopyFor(attempt);
    return;
  }
  if (action.kind === "publish") {
    await confirmRecovered(attempt, button);
  } else if (action.kind === "retry" || action.kind === "retry_publish") {
    await retryRecovered(attempt, button);
  } else if (action.kind === "reconnect") {
    connect(attempt.provider);
  } else {
    // A cooldown's control is a read-only availability check; its label is
    // restored after the read.
    await checkRecovered(attempt, button, action.kind === "wait" ? "Check availability" : "Check status");
  }
  await loadPostHistory({ silent: true });
}

function renderPostHistory() {
  const locked = $("#history-locked");
  if (!locked) return;
  const loading = authenticated && (historyLoading || !historyLoaded);
  const hasError = authenticated && !loading && Boolean(historyError);
  const draft = readLocalDraft();
  const merged = mergeActivity({
    attempts: activityAttempts,
    history: postHistory,
    draft,
    filter: historyFilter,
    submission: activeSubmission,
  });

  locked.hidden = authenticated;
  $("#history-loading").hidden = !loading;
  const error = $("#history-error");
  error.hidden = !hasError;
  error.replaceChildren();
  if (hasError) {
    // If rows are already on screen, say plainly that they are the last known
    // state — a failed refresh is not evidence that anything changed.
    const retained = merged.all.length > 0 || Boolean(merged.draft);
    const message = el("span", null, retained
      ? `${historyError} Showing the last known state; nothing was resent.`
      : historyError);
    const refresh = el("button", "text-button", "Retry now");
    refresh.type = "button";
    refresh.addEventListener("click", () => void loadPostHistory());
    error.append(message, refresh);
  }

  // A stale read is shown as stale, not as an empty list.
  const stale = $("#history-stale");
  const showStale = authenticated && activityStale && !hasError;
  stale.hidden = !showStale;
  stale.textContent = showStale
    ? "Couldn't refresh just now. Showing the last known state; nothing was resent."
    : "";

  const hasRows = merged.rows.length > 0;
  $("#history-empty").hidden = !authenticated || loading || hasError || hasRows;
  const emptyTitle = $("#history-empty-title");
  const emptyCopy = $("#history-empty-copy");
  if (historyFilter === "drafts") {
    emptyTitle.textContent = "No draft saved in this browser";
    emptyCopy.textContent = "Drafts live in this browser only. Start a post and it will be saved here automatically.";
  } else if (historyFilter === "unverified") {
    emptyTitle.textContent = "Nothing waiting to be verified";
    emptyCopy.textContent = "A delivery appears here when Relay could not confirm whether the provider received it.";
  } else if (historyFilter === "failed") {
    emptyTitle.textContent = "No failed deliveries";
    // "Failed" is the server's own verdict that nothing was delivered. It is not
    // always a provider rejection — Relay can also refuse before dispatch — so
    // the copy must not promise that every row here was rejected by the provider.
    emptyCopy.textContent = "A delivery appears here when Relay recorded that it was not delivered, with the reason it recorded.";
  } else if (historyFilter === "in_progress") {
    emptyTitle.textContent = "Nothing in progress";
    // This filter also holds a container that finished and is waiting for an
    // explicit confirmation, so the copy names both states it can contain.
    emptyCopy.textContent = "Sends that are still processing, and posts waiting for you to confirm them, appear here with their current state.";
  } else if (historyFilter === "published") {
    emptyTitle.textContent = "Nothing published yet";
    emptyCopy.textContent = "Posts you publish through Relay appear here once the provider confirms them.";
  } else {
    emptyTitle.textContent = "Nothing here yet";
    emptyCopy.textContent = "Create a post to see it here. Drafts are stored in this browser; deliveries are recorded on the server.";
  }

  const list = $("#history-list");
  list.replaceChildren();
  list.hidden = !hasRows;
  for (const row of merged.rows) list.append(activityRow(row));

  // Filter counts are real; the nav count is the number of recorded deliveries.
  document.querySelectorAll("[data-history-filter]").forEach((button) => {
    const key = button.dataset.historyFilter;
    const count = merged.counts[key] ?? 0;
    button.setAttribute("aria-pressed", String(key === historyFilter));
    button.classList.toggle("active", key === historyFilter);
    let badge = button.querySelector(".filter-count");
    if (!badge) {
      badge = el("span", "filter-count");
      button.append(badge);
    }
    badge.textContent = String(count);
    button.setAttribute("aria-label", `${FILTER_LABELS[key] || key}, ${count} item${count === 1 ? "" : "s"}`);
  });

  setText("#history-nav-count", authenticated && historyLoaded && !historyError
    ? String(merged.all.length) : "—");

  const pageStatus = $("#history-page-status");
  if (historyFilter === "drafts") {
    pageStatus.textContent = merged.draft ? "1 draft in this browser." : "No draft in this browser.";
  } else {
    const deliveries = merged.all.length;
    const hasDraft = historyFilter === "all" && Boolean(merged.draft);
    const base = pageLabel({ shown: deliveries, hasMore: Boolean(activityCursor) });
    pageStatus.textContent = hasDraft ? `${base} Plus 1 draft in this browser.` : base;
  }
  const more = $("#history-load-more");
  more.hidden = historyFilter === "drafts" ||
    !canLoadMore({ nextCursor: activityCursor, loading: activityLoading, loaded: activityLoaded });
  more.disabled = activityLoading;
  more.textContent = activityLoading ? "Loading…" : "Load more";
}

/* ── Loading ─────────────────────────────────────────────────── */

async function loadPostHistory({ silent = false } = {}) {
  if (!authenticated) {
    historyRequestId += 1;
    postHistory = [];
    historyLoading = false;
    historyLoaded = false;
    historyError = null;
    activityAttempts = [];
    activityCursor = null;
    activityLoaded = false;
    activityStale = false;
    renderPostHistory();
    return;
  }
  const requestId = ++historyRequestId;
  if (!silent) historyLoading = true;
  historyError = null;
  renderPostHistory();
  try {
    const [posts, attempts] = await Promise.allSettled([
      api("/posts"),
      api(`/publishing/attempts?${new URLSearchParams({ status: "all", limit: "50" })}`),
    ]);
    if (requestId !== historyRequestId || !authenticated) return;
    let failed = false;
    if (posts.status === "fulfilled") {
      postHistory = Array.isArray(posts.value.posts) ? posts.value.posts : [];
    } else {
      failed = true;
      historyError = posts.reason?.message || "Activity couldn't be loaded.";
    }
    if (attempts.status === "fulfilled") {
      activityAttempts = Array.isArray(attempts.value.attempts) ? attempts.value.attempts : [];
      activityCursor = typeof attempts.value.next_cursor === "string" ? attempts.value.next_cursor : null;
      activityLoaded = true;
    } else {
      // Receipts are the newer source. If they fail, keep known state and say so.
      failed = true;
      activityLoaded = activityLoaded || Boolean(activityAttempts.length);
      if (!historyError) historyError = attempts.reason?.message || "Deliveries couldn't be loaded.";
    }
    activityStale = failed && (postHistory.length > 0 || activityAttempts.length > 0);
    if (!failed) historyError = null;
    historyLoaded = true;
  } catch (error) {
    if (requestId !== historyRequestId || !authenticated) return;
    historyError = error.message;
    activityStale = postHistory.length > 0 || activityAttempts.length > 0;
    historyLoaded = true;
  } finally {
    if (requestId === historyRequestId) {
      historyLoading = false;
      renderPostHistory();
      renderOverview();
    }
  }
}

async function loadMoreAttempts() {
  if (!canLoadMore({ nextCursor: activityCursor, loading: activityLoading, loaded: activityLoaded })) return;
  activityLoading = true;
  renderPostHistory();
  try {
    const result = await api(`/publishing/attempts?${new URLSearchParams({
      status: "all", limit: "50", cursor: activityCursor,
    })}`);
    const seen = new Set(activityAttempts.map((attempt) => attempt.id));
    const incoming = Array.isArray(result.attempts) ? result.attempts : [];
    activityAttempts = [...activityAttempts, ...incoming.filter((attempt) => !seen.has(attempt.id))];
    activityCursor = typeof result.next_cursor === "string" ? result.next_cursor : null;
    activityStale = false;
  } catch (error) {
    // Keep what is on screen; say the read failed rather than showing an empty list.
    activityStale = true;
    notice(`${error.message} Older deliveries couldn't be loaded.`, true);
  } finally {
    activityLoading = false;
    renderPostHistory();
  }
}
