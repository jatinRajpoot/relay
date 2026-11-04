"use strict";

/* ── Home ────────────────────────────────────────────────────
   Home answers three questions: is there work to resume, what did Relay
   deliver, and which channels can it send to. It never shows a dashboard of
   zeros or metrics that don't exist. */

function draftSummary(draft) {
  const content = draftContent(draft);
  const parts = [];
  const item = contentItems.find((contentItem) => contentItem.id === content.media);
  if (item) parts.push(item.filename);
  if (content.title || content.caption || content.description) parts.push("copy added");
  const accounts = (draft.selectedAccountIds || [])
    .map((id) => accountList.find((account) => account.id === id))
    .filter(Boolean);
  if (accounts.length) parts.push(`${accounts.length} channel${accounts.length === 1 ? "" : "s"}`);
  if (content.unresolved) parts.push("unfinished delivery");
  return parts.join(" · ");
}

function renderHomeDraft() {
  const card = $("#home-draft-card");
  if (!card) return;
  const draft = readLocalDraft();
  const show = authenticated && draftHasContent(draft);
  card.hidden = !show;
  const resume = $("#home-resume-draft");
  resume.hidden = !show;
  if (!show) return;
  setText("#home-draft-detail", draftSummary(draft));
  resume.textContent = "Continue draft";
  const thumb = $("#home-draft-thumb");
  const item = contentItems.find((contentItem) => contentItem.id === draftContent(draft).media);
  if (item?.content_type.startsWith("image/")) {
    thumb.onerror = () => { thumb.hidden = true; };
    thumb.src = item.media_url;
    thumb.hidden = false;
  } else {
    thumb.removeAttribute("src");
    thumb.hidden = true;
  }
}

/* The Home primary action must agree with the rail: both open the composer,
   whether the composer is empty or holding a restored draft. */
function renderHomePrimaryAction() {
  const button = $("#overview-primary-action");
  if (!button) return;
  const hasDraft = authenticated && draftHasContent(readLocalDraft());
  const actionArea = button.closest(".intro-actions");
  if (actionArea) actionArea.hidden = hasDraft;
  const label = button.querySelector("span");
  if (!authenticated) {
    label.textContent = "Sign in to create";
  } else if (hasDraft) {
    label.textContent = "Continue draft";
  } else {
    label.textContent = "Create a post";
  }
  button.querySelector("svg use")?.setAttribute("href", hasDraft ? "#i-arrow" : "#i-plus");
  button.dataset.intent = hasDraft ? "resume" : "create";
}

function renderOverviewPosts() {
  const container = $("#overview-posts");
  if (!container) return;
  container.replaceChildren();
  const note = (text) => el("p", "mini-empty", text);

  if (!authenticated) { container.append(note("Sign in to see what Relay delivered.")); return; }
  if (historyError) {
    container.append(note(`${historyError} Your posts are unchanged.`));
    const retry = el("button", "text-button", "Retry now");
    retry.type = "button";
    retry.addEventListener("click", () => void loadPostHistory());
    container.append(retry);
    return;
  }
  if (!historyLoaded || historyLoading) { container.append(note("Loading recent activity…")); return; }
  const merged = mergeActivity({
    attempts: activityAttempts, history: postHistory, draft: null, filter: "all",
    submission: activeSubmission,
  });
  if (!merged.all.length) {
    container.append(note("Nothing sent from this workspace yet. Your first post will appear here with its delivery outcome."));
    return;
  }
  for (const row of merged.all.slice(0, 3)) {
    const button = el("button", "mini-post");
    button.type = "button";
    const badge = el("span", `mini-post-dot ${row.provider}`);
    badge.append(icon(row.provider === "youtube" ? "youtube" : "instagram"));
    const copy = el("span", "mini-post-copy");
    const when = row.timestamp
      ? new Date(row.timestamp).toLocaleDateString(undefined, { month: "short", day: "numeric" })
      : "Time unknown";
    copy.append(
      el("strong", null, `${phaseLabel(row.phase)} · ${activityTitle(row)}`),
      el("small", null, `${names[row.provider]} · ${when}`),
    );
    button.append(badge, copy);
    button.addEventListener("click", () => setPage("history"));
    container.append(button);
  }
}

function renderOverview() {
  const primaryAction = $("#overview-primary-action");
  if (!primaryAction) return;

  renderHomeDraft();
  renderHomePrimaryAction();
  renderOverviewPosts();

  const subtitle = $("#overview-subtitle");
  const draft = readLocalDraft();
  if (!authenticated) {
    subtitle.textContent = "Sign in to publish from this workspace.";
  } else if (!accountList.length) {
    subtitle.textContent = "Connect a channel, then send your first post.";
  } else {
    subtitle.textContent = "Recent posts and channels for this workspace.";
  }

  const channelList = $("#overview-channel-list");
  channelList.replaceChildren();
  if (!authenticated || !accountList.length) {
    const empty = el("div", "overview-channel-empty");
    const iconWrap = el("span", "channel-mini-icon");
    iconWrap.append(icon("link"));
    const copy = el("span");
    copy.append(el("strong", null, authenticated ? "No channels connected" : "Sign in to connect channels"));
    empty.append(iconWrap, copy);
    channelList.append(empty);
    const action = el("button", "overview-channel-action", authenticated ? "Connect a channel" : "Sign in");
    action.type = "button";
    action.addEventListener("click", () => authenticated ? setPage("connections") : showUnlock());
    channelList.append(action);
    return;
  }
  for (const account of accountList.slice(0, 4)) {
    const row = el("div", "overview-channel-row");
    const iconWrap = el("span", `channel-mini-icon ${account.provider}`);
    iconWrap.append(icon(account.provider));
    const copy = el("span");
    copy.append(el("strong", null, account.name), el("small", null, names[account.provider]));
    // Home uses the same readiness predicate as Channels and the composer, so a
    // channel that cannot send is never presented here as "Connected".
    const readiness = channelStatus(account, "home");
    const status = el("span", `overview-channel-status${readiness.reconnect ? " expired" : ""}`, readiness.label);
    if (readiness.unreadable) {
      status.title = "Relay can't read this channel's saved authorization. Reconnect it to send again.";
    } else if (readiness.expired && !readiness.reconnect) {
      status.title = "Relay refreshes this channel's access automatically before sending.";
    }
    row.append(iconWrap, copy, status);
    channelList.append(row);
  }
  if (accountList.length > 4) {
    channelList.append(el("small", "overview-channel-more", `+ ${accountList.length - 4} more`));
  }
}

/* ── Insights ──────────────────────────────────────────────── */

function formattedMetric(value) {
  return new Intl.NumberFormat().format(Number(value) || 0);
}

function renderAnalytics() {
  const locked = $("#analytics-locked");
  if (!locked) return;
  locked.hidden = authenticated;
  $("#analytics-loading").hidden = !authenticated || !analyticsLoading;
  const error = $("#analytics-error");
  error.hidden = !authenticated || !analyticsError;
  error.replaceChildren();
  if (analyticsError) {
    const message = el("span", null, `${analyticsError} Your channel data is unchanged.`);
    const retry = el("button", "button button-ghost button-sm", "Retry now");
    retry.type = "button";
    retry.addEventListener("click", () => void loadAnalytics());
    error.append(message, retry);
  }
  const hasResult = Boolean(analyticsData);
  const hasAccounts = Boolean(analyticsData?.accounts?.length);
  $("#analytics-empty").hidden = !authenticated || analyticsLoading || Boolean(analyticsError) || !hasResult || hasAccounts;
  const list = $("#analytics-list");
  list.hidden = !authenticated || analyticsLoading || Boolean(analyticsError) || !hasAccounts;
  list.replaceChildren();
  if (!list.hidden) {
    for (const account of analyticsData.accounts) list.append(makeAnalyticsCard(account, analyticsData.period));
  }
  $("#analytics-range").value = String(analyticsDays);
}

function makeAnalyticsCard(account, period) {
  const card = el("article", `analytics-account-card ${account.provider}`);
  const heading = el("div", "analytics-account-heading");
  const identity = el("div", "analytics-identity");
  const iconWrap = el("span", `channel-mini-icon ${account.provider}`);
  iconWrap.append(icon(account.provider));
  const title = el("span", "analytics-account-title");
  title.append(
    el("strong", null, account.name),
    el("small", null, `${names[account.provider]} · ${period.start_date} – ${period.end_date}`),
  );
  identity.append(iconWrap, title);
  heading.append(identity, el("span", `analytics-platform-badge ${account.provider}`, names[account.provider]));
  card.append(heading);

  if (account.status !== "ready") {
    const unavailable = el("div", "analytics-unavailable");
    unavailable.append(el("p", null, account.message || "Analytics are unavailable for this channel."));
    const action = el("button", "text-button", "Manage channels →");
    action.type = "button";
    action.addEventListener("click", () => setPage("connections"));
    unavailable.append(action);
    card.append(unavailable);
    return card;
  }

  const metrics = account.period || {};
  const metricList = account.provider === "youtube"
    ? [["Views", metrics.views], ["Likes", metrics.likes], ["Comments", metrics.comments], ["Subscribers gained", metrics.subscribers_gained]]
    : [["Posts", metrics.posts], ["Likes", metrics.likes], ["Comments", metrics.comments], ["Avg. interactions", metrics.average_interactions]];
  const metricGrid = el("div", "analytics-metrics");
  for (const [label, value] of metricList) {
    const metric = el("div", "analytics-metric");
    metric.append(el("strong", null, formattedMetric(value)), el("small", null, label));
    metricGrid.append(metric);
  }
  card.append(metricGrid);

  if (account.provider === "youtube" && account.channel) {
    const subscribers = account.channel.subscribers == null
      ? "Subscriber count hidden"
      : `${formattedMetric(account.channel.subscribers)} subscribers`;
    card.append(el("p", "analytics-channel-summary",
      `${subscribers} · ${formattedMetric(account.channel.views)} lifetime views · ${formattedMetric(account.channel.videos)} videos · ${formattedMetric(metrics.watch_minutes)} watch minutes this period`));
  } else if (account.provider === "instagram") {
    card.append(el("p", "analytics-channel-summary", "Likes and comments on posts published during this period."));
  }

  const topPosts = account.top_posts || [];
  const postsHeading = el("div", "analytics-posts-heading");
  postsHeading.append(
    el("h3", null, "Top posts"),
    el("small", null, account.provider === "youtube" ? "Views in this period" : "Likes + comments"),
  );
  card.append(postsHeading);
  if (!topPosts.length) {
    card.append(el("p", "analytics-no-posts", "No post data for this period yet."));
    return card;
  }
  const postList = el("ol", "analytics-post-list");
  for (const post of topPosts) {
    const row = el("li");
    const link = el("a", "analytics-post-link");
    let safeUrl = null;
    try { safeUrl = post.url ? new URL(post.url, window.location.origin) : null; } catch { /* invalid URL */ }
    const allowedHost = account.provider === "youtube"
      ? ["www.youtube.com", "youtube.com"]
      : ["www.instagram.com", "instagram.com"];
    if (safeUrl?.protocol === "https:" && allowedHost.includes(safeUrl.hostname)) {
      link.href = safeUrl.href;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
    }
    link.append(
      el("strong", null, post.title || "Untitled post"),
      el("small", null, account.provider === "youtube"
        ? `${formattedMetric(post.views)} views · ${formattedMetric(post.likes)} likes · ${formattedMetric(post.comments)} comments`
        : `${formattedMetric(post.likes)} likes · ${formattedMetric(post.comments)} comments`),
    );
    row.append(link);
    postList.append(row);
  }
  card.append(postList);
  return card;
}

async function loadAnalytics() {
  if (!authenticated) {
    analyticsData = null;
    analyticsError = null;
    analyticsLoading = false;
    renderAnalytics();
    return;
  }
  const requestId = ++analyticsRequestId;
  analyticsLoading = true;
  analyticsError = null;
  renderAnalytics();
  try {
    const result = await api(`/analytics?days=${encodeURIComponent(analyticsDays)}`);
    if (requestId === analyticsRequestId) analyticsData = result;
  } catch (error) {
    if (requestId === analyticsRequestId) analyticsError = error.message;
  } finally {
    if (requestId === analyticsRequestId) {
      analyticsLoading = false;
      renderAnalytics();
    }
  }
}

/* ── Routing ───────────────────────────────────────────────── */

const PAGE_LABELS = {
  overview: "Home",
  analytics: "Insights",
  history: "Activity",
  connections: "Channels",
  content: "Library",
  publish: "Create a post",
};
const PAGES = Object.keys(PAGE_LABELS);

/* The skip link must never navigate the router. It moves focus to the current
   page's main region and leaves the hash — and therefore the current page —
   exactly as it was. */
function focusMainContent() {
  const main = $("#main-content");
  if (!main) return;
  if (!main.hasAttribute("tabindex")) main.setAttribute("tabindex", "-1");
  main.focus({ preventScroll: false });
  main.scrollIntoView({ block: "start", behavior: "instant" });
}

function initSkipLink() {
  const link = document.querySelector(".skip-link");
  if (!link) return;
  link.addEventListener("click", (event) => {
    event.preventDefault();
    focusMainContent();
  });
}

/* An unknown hash is not a page. It is replaced with the current page so a
   stray fragment cannot silently change the view or break the back button. */
function routeFromHash() {
  const page = window.location.hash.slice(1);
  return PAGES.includes(page) ? page : null;
}

function setPage(page, { recordHistory = true } = {}) {
  if (!PAGES.includes(page)) return;
  const updatePage = () => {
    if (activePage === "publish" && page !== "publish" && !publishing) {
      saveLocalDraft({ announce: false });
    }
    activePage = page;
    document.body.dataset.page = page;
    document.querySelectorAll("[data-page-view]").forEach((view) => {
      view.hidden = view.dataset.pageView !== page;
    });
    document.querySelectorAll("[data-nav]").forEach((item) => {
      const selected = item.dataset.nav === page;
      if (item.classList.contains("nav-item")) item.classList.toggle("active", selected);
      if (item.classList.contains("rail-create")) item.classList.toggle("active", selected);
      if (item.matches("a")) {
        if (selected) item.setAttribute("aria-current", "page");
        else item.removeAttribute("aria-current");
      }
    });
    setText("#breadcrumb-label", PAGE_LABELS[page]);
    document.title = `${PAGE_LABELS[page]} · Relay`;
    if (page === "publish") renderComposer();
    if (window.location.hash !== `#${page}`) {
      const update = recordHistory ? "pushState" : "replaceState";
      window.history[update]({}, "", `#${page}`);
    }
    window.scrollTo({ top: 0, behavior: "instant" });
    // Move focus for screen-reader route orientation without painting a ring.
    const heading = document.querySelector(`[data-page-view="${page}"] h1`);
    heading?.setAttribute("tabindex", "-1");
    heading?.focus({ preventScroll: true });
    if (page === "overview") renderOverview();
    if (page === "analytics") void loadAnalytics();
    if (page === "history") void loadPostHistory();
    if (page === "publish") void loadRecoveredAttempts();
  };

  if (activePage !== page && document.startViewTransition &&
      !window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
    document.startViewTransition(updatePage);
  } else {
    updatePage();
  }
}
