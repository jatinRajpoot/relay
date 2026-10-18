"use strict";

function renderOverview() {
  const primaryAction = $("#overview-primary-action");
  if (!primaryAction) return;
  primaryAction.querySelector("span").textContent = authenticated ? "Create a post" : "Unlock workspace";
  primaryAction.querySelector("use").setAttribute("href", authenticated ? "#i-plus" : "#i-lock");
  $("#overview-channel-count").textContent = authenticated ? String(accountList.length) : "—";
  $("#overview-media-count").textContent = authenticated ? String(contentItems.length) : "—";
  const publishedCount = $("#overview-history-count");
  if (publishedCount) {
    publishedCount.textContent = authenticated && historyLoaded && !historyError
      ? String(postHistory.length)
      : "—";
  }
  let readiness = "Your workspace awaits";
  let readinessDetail = "Unlock once to see your channels and media";
  if (authenticated && !accountList.length) {
    readiness = "Set up channels";
    readinessDetail = "Connect Instagram or YouTube";
  } else if (authenticated && !contentStorageConfigured) {
    readiness = "Setup needed";
    readinessDetail = "Configure media storage to upload";
  } else if (authenticated && !contentItems.length) {
    readiness = "Add media";
    readinessDetail = "Your channels are ready for content";
  } else if (authenticated) {
    readiness = "Ready to create";
    readinessDetail = "Choose a channel and publish";
  }
  $("#overview-readiness").textContent = readiness;
  $("#overview-readiness-detail").textContent = readinessDetail;

  const channelList = $("#overview-channel-list");
  channelList.replaceChildren();
  if (!authenticated || !accountList.length) {
    const empty = document.createElement("div");
    empty.className = "overview-channel-empty";
    const icon = document.createElement("span");
    icon.className = "channel-mini-icon";
    const symbol = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    symbol.classList.add("icon");
    const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
    use.setAttribute("href", "#i-link");
    symbol.append(use);
    icon.append(symbol);
    const copy = document.createElement("span");
    const title = document.createElement("strong");
    const detail = document.createElement("small");
    title.textContent = authenticated ? "No channels connected" : "Unlock your workspace";
    detail.textContent = authenticated ? "Connect a channel to start publishing." : "Your connected channels will show up here.";
    copy.append(title, detail);
    empty.append(icon, copy);
    channelList.append(empty);
    const action = document.createElement("button");
    action.className = "overview-channel-action";
    action.type = "button";
    action.textContent = authenticated ? "Connect a channel" : "Open channels";
    action.addEventListener("click", () => setPage("connections"));
    channelList.append(action);
  } else {
    for (const account of accountList.slice(0, 3)) {
      const row = document.createElement("div");
      row.className = "overview-channel-row";
      const icon = document.createElement("span");
      icon.className = `channel-mini-icon ${account.provider}`;
      const symbol = document.createElementNS("http://www.w3.org/2000/svg", "svg");
      symbol.classList.add("icon");
      const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
      use.setAttribute("href", `#i-${account.provider}`);
      symbol.append(use);
      icon.append(symbol);
      const copy = document.createElement("span");
      const title = document.createElement("strong");
      const detail = document.createElement("small");
      title.textContent = account.name;
      detail.textContent = names[account.provider];
      copy.append(title, detail);
      const expired = account.expires_at * 1000 <= Date.now();
      const status = document.createElement("span");
      status.className = `overview-channel-status${expired ? " expired" : ""}`;
      status.textContent = expired ? "Reconnect" : "Connected";
      row.append(icon, copy, status);
      channelList.append(row);
    }
    if (accountList.length > 3) {
      const more = document.createElement("small");
      more.className = "overview-channel-more";
      more.textContent = `+ ${accountList.length - 3} more ${accountList.length - 3 === 1 ? "channel" : "channels"}`;
      channelList.append(more);
    }
  }

  const mediaList = $("#overview-media-list");
  const mediaEmpty = $("#overview-media-empty");
  mediaList.replaceChildren();
  if (authenticated && contentStorageConfigured && contentItems.length) {
    mediaEmpty.hidden = true;
    mediaList.hidden = false;
    for (const item of contentItems.slice(0, 4)) mediaList.append(makeOverviewMediaRow(item));
  } else {
    mediaEmpty.hidden = false;
    mediaList.hidden = true;
    if (!authenticated) {
      $("#overview-media-empty-title").textContent = "Unlock to see your recent media.";
      $("#overview-media-empty-description").textContent = "Your saved photos and videos will appear here.";
    } else if (!contentStorageConfigured) {
      $("#overview-media-empty-title").textContent = "Media storage needs setup.";
      $("#overview-media-empty-description").textContent = "Configure media storage on the service to add photos and videos.";
    } else {
      $("#overview-media-empty-title").textContent = "Your media has a home here.";
      $("#overview-media-empty-description").textContent = "Upload a photo or video and it’ll be ready whenever you create a post.";
    }
  }
}

function formattedMetric(value) {
  return new Intl.NumberFormat().format(Number(value) || 0);
}

function renderAnalytics() {
  const locked = $("#analytics-locked");
  if (!locked) return;
  locked.hidden = authenticated;
  $("#analytics-loading").hidden = !authenticated || !analyticsLoading;
  $("#analytics-error").hidden = !authenticated || !analyticsError;
  $("#analytics-error").textContent = analyticsError || "";
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
  const card = document.createElement("article");
  card.className = `analytics-account-card ${account.provider}`;
  const heading = document.createElement("div");
  heading.className = "analytics-account-heading";
  const identity = document.createElement("div");
  identity.className = "analytics-identity";
  const icon = document.createElement("span");
  icon.className = `channel-mini-icon ${account.provider}`;
  const iconSvg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  iconSvg.classList.add("icon");
  const iconUse = document.createElementNS("http://www.w3.org/2000/svg", "use");
  iconUse.setAttribute("href", `#i-${account.provider}`);
  iconSvg.append(iconUse);
  icon.append(iconSvg);
  const title = document.createElement("span");
  title.className = "analytics-account-title";
  const name = document.createElement("strong");
  name.textContent = account.name;
  const subtitle = document.createElement("small");
  subtitle.textContent = `${names[account.provider]} · ${period.start_date} – ${period.end_date}`;
  title.append(name, subtitle);
  identity.append(icon, title);
  const badge = document.createElement("span");
  badge.className = `analytics-platform-badge ${account.provider}`;
  badge.textContent = names[account.provider];
  heading.append(identity, badge);
  card.append(heading);

  if (account.status !== "ready") {
    const unavailable = document.createElement("div");
    unavailable.className = "analytics-unavailable";
    const copy = document.createElement("p");
    copy.textContent = account.message || "Analytics are unavailable for this channel.";
    const action = document.createElement("button");
    action.className = "text-button";
    action.type = "button";
    action.textContent = "Manage channels →";
    action.addEventListener("click", () => setPage("connections"));
    unavailable.append(copy, action);
    card.append(unavailable);
    return card;
  }

  const metrics = account.period || {};
  const metricList = account.provider === "youtube"
    ? [["Views", metrics.views], ["Likes", metrics.likes], ["Comments", metrics.comments], ["Subscribers gained", metrics.subscribers_gained]]
    : [["Posts", metrics.posts], ["Likes", metrics.likes], ["Comments", metrics.comments], ["Avg. interactions", metrics.average_interactions]];
  const metricGrid = document.createElement("div");
  metricGrid.className = "analytics-metrics";
  for (const [label, value] of metricList) {
    const metric = document.createElement("div");
    metric.className = "analytics-metric";
    const metricValue = document.createElement("strong");
    metricValue.textContent = formattedMetric(value);
    const metricLabel = document.createElement("small");
    metricLabel.textContent = label;
    metric.append(metricValue, metricLabel);
    metricGrid.append(metric);
  }
  card.append(metricGrid);

  if (account.provider === "youtube" && account.channel) {
    const channelSummary = document.createElement("p");
    channelSummary.className = "analytics-channel-summary";
    const subscribers = account.channel.subscribers == null
      ? "Subscriber count hidden"
      : `${formattedMetric(account.channel.subscribers)} subscribers`;
    channelSummary.textContent = `${subscribers} · ${formattedMetric(account.channel.views)} lifetime views · ${formattedMetric(account.channel.videos)} videos · ${formattedMetric(metrics.watch_minutes)} watch minutes this period`;
    card.append(channelSummary);
  } else if (account.provider === "instagram") {
    const note = document.createElement("p");
    note.className = "analytics-channel-summary";
    note.textContent = "Likes and comments on posts published during this period.";
    card.append(note);
  }

  const topPosts = account.top_posts || [];
  const postsHeading = document.createElement("div");
  postsHeading.className = "analytics-posts-heading";
  const postsTitle = document.createElement("h3");
  postsTitle.textContent = "Top posts";
  const postsBasis = document.createElement("small");
  postsBasis.textContent = account.provider === "youtube" ? "Views in this period" : "Likes + comments";
  postsHeading.append(postsTitle, postsBasis);
  card.append(postsHeading);
  if (!topPosts.length) {
    const noPosts = document.createElement("p");
    noPosts.className = "analytics-no-posts";
    noPosts.textContent = "No post data for this period yet.";
    card.append(noPosts);
    return card;
  }
  const postList = document.createElement("ol");
  postList.className = "analytics-post-list";
  for (const post of topPosts) {
    const row = document.createElement("li");
    const link = document.createElement("a");
    link.className = "analytics-post-link";
    let safeUrl = null;
    try { safeUrl = post.url ? new URL(post.url, window.location.origin) : null; } catch {}
    const allowedHost = account.provider === "youtube"
      ? ["www.youtube.com", "youtube.com"]
      : ["www.instagram.com", "instagram.com"];
    if (safeUrl?.protocol === "https:" && allowedHost.includes(safeUrl.hostname)) {
      link.href = safeUrl.href;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
    } else {
      link.removeAttribute("href");
    }
    const postTitle = document.createElement("strong");
    postTitle.textContent = post.title || "Untitled post";
    const postDetails = document.createElement("small");
    postDetails.textContent = account.provider === "youtube"
      ? `${formattedMetric(post.views)} views · ${formattedMetric(post.likes)} likes · ${formattedMetric(post.comments)} comments`
      : `${formattedMetric(post.likes)} likes · ${formattedMetric(post.comments)} comments`;
    link.append(postTitle, postDetails);
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

function setPage(page, { recordHistory = true } = {}) {
  if (!["overview", "analytics", "history", "connections", "content", "publish"].includes(page)) return;
  activePage = page;
  document.querySelectorAll("[data-page-view]").forEach((view) => {
    view.hidden = view.dataset.pageView !== page;
  });
  document.querySelectorAll("[data-nav]").forEach((item) => {
    const selected = item.dataset.nav === page;
    if (item.classList.contains("nav-item")) item.classList.toggle("active", selected);
    if (item.classList.contains("rail-create")) item.classList.toggle("active", selected);
    if (item.matches("a")) {
      if (selected && (item.classList.contains("nav-item") || item.classList.contains("nav-more-link") || item.classList.contains("rail-create"))) item.setAttribute("aria-current", "page");
      else item.removeAttribute("aria-current");
    }
  });
  const pageLabels = { overview: "Overview", analytics: "Analytics", history: "History", connections: "Channels", content: "Media", publish: "Create a post" };
  $("#breadcrumb-label").textContent = pageLabels[page];
  document.title = `${pageLabels[page]} · Relay`;
  if (window.location.hash !== `#${page}`) {
    const update = recordHistory ? "pushState" : "replaceState";
    window.history[update]({}, "", `#${page}`);
  }
  $("#main-content").scrollTop = 0;
  if (page === "analytics") void loadAnalytics();
  if (page === "history") void loadPostHistory();
}
