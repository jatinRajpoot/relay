"use strict";
import { OAuthPopup } from "/static/oauth-popup.mjs";

const $ = (selector) => document.querySelector(selector);
const names = { instagram: "Instagram", youtube: "YouTube" };
let authenticated = false;
let accountList = [];
let contentItems = [];
let activeWorkspaceId = null;
let disconnecting = null;
let instagramMediaConfigured = false;
let contentStorageConfigured = false;
let maxUploadBytes = 0;
let activeInstagramPosts = [];
let youtubeUploadResults = [];
let instagramStatusTimer = null;
let instagramStatusCheckRunning = false;
let analyticsData = null;
let analyticsLoading = false;
let analyticsError = null;
let analyticsRequestId = 0;
let postHistory = [];
let historyLoading = false;
let historyLoaded = false;
let historyError = null;
let historyRequestId = 0;
let analyticsDays = 30;
let selectedContentId = null;
let selectedAccountIds = new Set();
let activeDetailsTab = "instagram";
let showMediaPicker = false;
let publishing = false;
let activePage = "overview";
const providerConfigured = { instagram: true, youtube: true };
const providerIssues = { instagram: null, youtube: null };

function resetConnectButtons() {
  document.querySelectorAll("[data-connect]").forEach((button) => {
    const provider = button.dataset.connect;
    button.disabled = !providerConfigured[provider];
    button.querySelector("span").textContent = providerConfigured[provider]
      ? `Connect ${names[provider]}` : "App credentials needed";
  });
}

const oauthPopup = new OAuthPopup(window, async ({provider, result}) => {
  resetConnectButtons();
  try { await loadAccounts(); } catch (error) { notice(error.message, true); return; }
  if (activePage === "analytics") await loadAnalytics();
  if (result === "connected") notice(`${names[provider]} connected. Your account is ready.`);
  else {
    const messages = {
      cancelled: "Sign-in was cancelled. You can connect again whenever you're ready.",
      expired: "That sign-in request expired. Click Connect to start again.",
      connection: "We couldn't finish connecting. Check the exact callback URL and permissions in the Setup guide.",
    };
    notice(messages[result], true);
  }
}, async () => {
  resetConnectButtons();
  try { await loadAccounts(); } catch (error) { notice(error.message, true); return; }
  if (activePage === "analytics") await loadAnalytics();
  notice("Sign-in window closed. If the provider reported an invalid redirect URI, register the callback URL in the Setup guide. Otherwise, refresh the list after completing sign-in.");
});

function httpError(response, data) {
  const detail = typeof data?.detail === "string" ? data.detail.trim() : "";
  const error = new Error(
    detail || `Relay returned an unexpected response (HTTP ${response.status}). Please try again.`
  );
  error.status = response.status;
  return error;
}

async function api(path, options = {}) {
  let response;
  try {
    const headers = new Headers(options.headers);
    if (activeWorkspaceId) headers.set("X-Workspace-ID", activeWorkspaceId);
    response = await fetch(path, { credentials: "same-origin", ...options, headers });
  } catch {
    throw new Error("Couldn't reach Relay. Check that the local service is running.");
  }
  if (response.status === 204) return null;
  // Read the body defensively. A framework or proxy error page is not JSON, and a
  // parse failure must not replace the real HTTP status with a JavaScript message.
  const data = await response.json().catch(() => null);
  if (!response.ok) {
    if (response.status === 401) {
      authenticated = false;
      renderSession();
      window.location.replace("/login");
    }
    throw httpError(response, data);
  }
  if (data === null) throw new Error("Relay returned an unexpected response. Please try again.");
  return data;
}

function icon(name) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("class", "icon");
  const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
  use.setAttribute("href", `#i-${name}`);
  svg.append(use);
  return svg;
}

function dismissToast(toast) {
  if (toast.dataset.leaving) return;
  toast.dataset.leaving = "true";
  toast.classList.add("leaving");
  window.setTimeout(() => toast.remove(), 200);
}

function notice(message, error = false, action = null) {
  const stack = $("#toasts");
  if (!stack) return;
  const toast = document.createElement("div");
  toast.className = `toast${error ? " error" : ""}`;
  toast.append(icon(error ? "alert" : "check"));
  toast.querySelector(".icon").classList.add("toast-icon");
  const text = document.createElement("span");
  text.className = "toast-text";
  text.textContent = message;
  toast.append(text);
  if (action) {
    const run = document.createElement("button");
    run.type = "button";
    run.className = "toast-action";
    run.textContent = action.label;
    run.addEventListener("click", () => {
      action.run();
      dismissToast(toast);
    });
    toast.append(run);
  }
  const close = document.createElement("button");
  close.type = "button";
  close.className = "toast-close";
  close.setAttribute("aria-label", "Dismiss notification");
  close.textContent = "×";
  close.addEventListener("click", () => dismissToast(toast));
  toast.append(close);
  stack.append(toast);
  while (stack.children.length > 3) stack.firstElementChild.remove();
  const timer = window.setTimeout(() => dismissToast(toast), error ? 9000 : 5500);
  toast.addEventListener("mouseenter", () => window.clearTimeout(timer), { once: true });
}

function renderSession() {
  const button = $("#session-button");
  button.classList.toggle("unlocked", authenticated);
  button.querySelector("span").textContent = authenticated ? "Log out" : "Log in";
  button.querySelector("use").setAttribute("href", authenticated ? "#i-unlock" : "#i-lock");
  $("#empty-connect").textContent = authenticated ? "Connect a channel" : "Unlock workspace";
  $("#empty-description").textContent = authenticated
    ? "Connect Instagram or YouTube to start publishing."
    : "Unlock your workspace to view or connect an account.";
  if (!authenticated) {
    if (instagramStatusTimer !== null) window.clearTimeout(instagramStatusTimer);
    instagramStatusTimer = null;
    accountList = [];
    contentItems = [];
    selectedAccountIds.clear();
    selectedContentId = null;
    activeInstagramPosts = [];
    youtubeUploadResults = [];
    analyticsData = null;
    analyticsLoading = false;
    analyticsError = null;
    analyticsRequestId += 1;
    postHistory = [];
    historyLoading = false;
    historyLoaded = false;
    historyError = null;
    historyRequestId += 1;
    publishing = false;
    $("#accounts-list").replaceChildren();
    $("#accounts-list").hidden = true;
    $("#empty-state").hidden = false;
    $("#publish-results-section").hidden = true;
    $("#account-count").textContent = "—";
    $("#nav-count").textContent = "—";
    for (const provider of Object.keys(names)) {
      $(`#${provider}-state`).textContent = providerIssues[provider] ? "Setup needed" : "Ready to connect";
      $(`#${provider}-state`).classList.remove("connected");
    }
  }
  renderContent();
  renderComposer();
  renderWorkspaceFlow();
  renderAnalytics();
  renderPostHistory();
}

function renderWorkspaceFlow() {
  const hasChannel = authenticated && accountList.length > 0;
  const hasMedia = authenticated && contentItems.length > 0;
  let status = "Unlock to begin";
  let description = "Unlock your workspace, then connect a channel to get started.";
  if (authenticated && !hasChannel) {
    status = "Connect your first channel";
    description = "Start by connecting Instagram or YouTube. You can add more channels later.";
  } else if (hasChannel && !contentStorageConfigured) {
    status = "Media storage needs setup";
    description = "Your channels are connected. Configure media storage to add content and publish.";
  } else if (hasChannel && !hasMedia) {
    status = "Add your first media";
    description = "Your channels are connected. Upload a photo or video to continue.";
  } else if (hasChannel && hasMedia) {
    status = "Ready to publish";
    description = "Your workspace is ready. Create a post whenever you are.";
  }
  $("#workflow-status").textContent = status;
  $("#workflow-description").textContent = description;
  const states = {
    channels: !authenticated ? "upcoming" : hasChannel ? "complete" : "current",
    media: hasChannel ? (hasMedia ? "complete" : "current") : "upcoming",
    publish: hasMedia ? "current" : "upcoming",
  };
  $("#workflow-card").hidden = hasChannel && hasMedia;
  document.querySelectorAll("[data-flow-step]").forEach((step, index) => {
    const state = states[step.dataset.flowStep];
    step.dataset.flowState = state;
    step.querySelector(".flow-number").textContent = state === "complete" ? "✓" : String(index + 1);
    if (state === "current") step.setAttribute("aria-current", "step");
    else step.removeAttribute("aria-current");
  });
  renderOverview();
}

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
    for (const item of contentItems.slice(0, 3)) mediaList.append(makeOverviewMediaRow(item));
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
    if (item.matches("a")) {
      if (selected && (item.classList.contains("nav-item") || item.classList.contains("nav-more-link"))) item.setAttribute("aria-current", "page");
      else item.removeAttribute("aria-current");
    }
  });
  const pageLabels = { overview: "Home", analytics: "Analytics", history: "Post history", connections: "Channels", content: "Media library", publish: "Create a post" };
  $("#breadcrumb-label").textContent = pageLabels[page];
  document.title = `${pageLabels[page]} · Relay`;
  if (window.location.hash !== `#${page}`) {
    const update = recordHistory ? "pushState" : "replaceState";
    window.history[update]({}, "", `#${page}`);
  }
  window.scrollTo({ top: 0, behavior: "smooth" });
  if (page === "analytics") void loadAnalytics();
  if (page === "history") void loadPostHistory();
}

function profileInitials(name) {
  return String(name || "?").trim().split(/\s+/).slice(0, 2).map((part) => part[0]).join("").toUpperCase() || "?";
}

function makeProfileAvatar(account, provider, size = "") {
  const avatar = document.createElement("span");
  avatar.className = `profile-avatar ${provider} ${size}`.trim();
  const initials = document.createElement("span");
  initials.className = "profile-avatar-initials";
  initials.textContent = profileInitials(account.name);
  avatar.append(initials);
  if (account.avatar_url) {
    const image = document.createElement("img");
    image.src = account.avatar_url;
    image.alt = `${account.name} profile photo`;
    image.loading = "lazy";
    image.referrerPolicy = "no-referrer";
    image.addEventListener("error", () => image.remove(), { once: true });
    avatar.append(image);
  }
  const badge = document.createElement("span");
  badge.className = "profile-platform-badge";
  const icon = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
  use.setAttribute("href", `#i-${provider}`);
  icon.append(use);
  badge.append(icon);
  avatar.append(badge);
  return avatar;
}

function canUseMedia(item, provider) {
  return provider === "instagram" ? item.instagram_compatible : item.youtube_compatible;
}

function mediaKind(item) {
  return item.content_type.startsWith("image/") ? "Photo" : "Video";
}

function renderComposerMedia() {
  const grid = $("#composer-media-grid");
  const empty = $("#composer-media-empty");
  const attachment = $("#composer-selected-attachment");
  grid.replaceChildren();
  attachment.replaceChildren();
  const emptyTitle = $("#composer-media-empty-title");
  const emptyCopy = $("#composer-media-empty-copy");
  const emptyAction = $("#composer-media-action");
  const item = contentItems.find((content) => content.id === selectedContentId);
  if (!authenticated) {
    emptyTitle.textContent = "Unlock your workspace to see your media";
    emptyCopy.textContent = "Your saved photos and videos will show here as visual previews.";
    emptyAction.textContent = "Unlock workspace";
    empty.hidden = false;
    attachment.hidden = true;
    grid.hidden = true;
    return;
  }
  if (!contentStorageConfigured) {
    emptyTitle.textContent = "Media storage needs setup";
    emptyCopy.textContent = "Media storage needs a one-time setup before uploads can be saved.";
    emptyAction.hidden = true;
    empty.hidden = false;
    attachment.hidden = true;
    grid.hidden = true;
    return;
  }
  emptyAction.hidden = false;
  emptyAction.textContent = "Open media library";
  if (item) {
    const preview = document.createElement("span");
    preview.className = "attachment-thumbnail";
    const node = document.createElement(item.content_type.startsWith("image/") ? "img" : "video");
    node.src = item.media_url;
    node.alt = "";
    if (node instanceof HTMLVideoElement) {
      node.muted = true;
      node.playsInline = true;
      node.preload = "metadata";
    }
    preview.append(node);
    const copy = document.createElement("span");
    copy.className = "attachment-copy";
    const name = document.createElement("strong");
    name.textContent = item.filename;
    const metadata = document.createElement("small");
    metadata.textContent = mediaKind(item) + " · " + formatBytes(item.size);
    copy.append(name, metadata);
    const change = document.createElement("button");
    change.className = "button button-secondary button-sm";
    change.type = "button";
    change.textContent = showMediaPicker ? "Done" : "Change";
    change.addEventListener("click", () => {
      showMediaPicker = !showMediaPicker;
      renderComposerMedia();
    });
    attachment.append(preview, copy, change);
    attachment.hidden = false;
    empty.hidden = true;
    grid.hidden = !showMediaPicker;
  } else {
    attachment.hidden = true;
    showMediaPicker = false;
    if (!contentItems.length) {
      emptyTitle.textContent = "Your library is ready for a first upload";
      emptyCopy.textContent = "Upload a photo or video above. It will appear here as a preview.";
      empty.hidden = false;
      grid.hidden = true;
      return;
    }
    empty.hidden = true;
    grid.hidden = false;
  }
  for (const item of contentItems) {
    const option = document.createElement("button");
    option.type = "button";
    option.className = "composer-media-option";
    option.setAttribute("aria-pressed", String(item.id === selectedContentId));
    option.disabled = publishing;
    if (item.id === selectedContentId) option.classList.add("selected");
    const preview = document.createElement("span");
    preview.className = "composer-media-preview";
    if (item.content_type.startsWith("image/")) {
      const image = document.createElement("img");
      image.src = item.media_url;
      image.alt = "";
      image.loading = "lazy";
      preview.append(image);
    } else {
      const video = document.createElement("video");
      video.src = item.media_url;
      video.muted = true;
      video.loop = true;
      video.autoplay = true;
      video.playsInline = true;
      video.preload = "metadata";
      preview.append(video);
      const play = document.createElement("span");
      play.className = "media-play-marker";
      play.textContent = "▶";
      preview.append(play);
    }
    const details = document.createElement("span");
    details.className = "composer-media-copy";
    const filename = document.createElement("strong");
    filename.textContent = item.filename;
    const metadata = document.createElement("small");
    metadata.textContent = `${mediaKind(item)} · ${formatBytes(item.size)}`;
    const platforms = document.createElement("span");
    platforms.className = "composer-media-platforms";
    if (item.instagram_compatible) platforms.append(makeMediaTag("Instagram", "instagram"));
    if (item.youtube_compatible) platforms.append(makeMediaTag("YouTube", "youtube"));
    details.append(filename, metadata, platforms);
    const selected = document.createElement("span");
    selected.className = "media-selected-mark";
    selected.textContent = "✓";
    option.append(preview, details, selected);
    option.addEventListener("click", () => selectComposerMedia(item));
    grid.append(option);
  }
}

function selectComposerMedia(item) {
  if (publishing) return;
  showMediaPicker = false;
  if (selectedContentId === item.id) {
    selectedContentId = null;
  } else {
    const blocked = accountList.filter((account) => selectedAccountIds.has(account.id) && !canUseMedia(item, account.provider));
    blocked.forEach((account) => selectedAccountIds.delete(account.id));
    selectedContentId = item.id;
    if (blocked.length) {
      const providers = [...new Set(blocked.map((account) => names[account.provider]))].join(" and ");
      notice(`Media changed. ${providers} ${blocked.length === 1 ? "selection was" : "selections were"} removed because this ${mediaKind(item).toLowerCase()} can't be sent there.`);
    }
  }
  renderComposer();
}

function renderComposerDestinations() {
  const container = $("#composer-destinations");
  container.replaceChildren();
  const media = contentItems.find((item) => item.id === selectedContentId);
  for (const account of accountList) {
    const selected = selectedAccountIds.has(account.id);
    const compatible = !media || canUseMedia(media, account.provider);
    const configured = account.provider === "instagram" ? instagramMediaConfigured : contentStorageConfigured;
    const choice = document.createElement("label");
    choice.className = "profile-pill" + (selected ? " selected" : "") + (!compatible ? " incompatible" : "");
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.checked = selected;
    checkbox.disabled = publishing || !authenticated || !configured || !compatible;
    checkbox.setAttribute("aria-label", (selected ? "Remove " : "Add ") + account.name + " on " + names[account.provider]);
    checkbox.addEventListener("change", () => {
      if (checkbox.checked) selectedAccountIds.add(account.id);
      else selectedAccountIds.delete(account.id);
      renderComposer();
    });
    const name = document.createElement("span");
    name.className = "profile-pill-name";
    name.textContent = account.name;
    const platform = document.createElement("span");
    platform.className = "profile-pill-platform " + account.provider;
    platform.append(icon(account.provider));
    const check = document.createElement("span");
    check.className = "profile-pill-check";
    check.textContent = "✓";
    choice.append(checkbox, makeProfileAvatar(account, account.provider), name, platform, check);
    container.append(choice);
  }
  if (!accountList.length) {
    const empty = document.createElement("span");
    empty.className = "profile-pills-empty";
    empty.textContent = authenticated ? "No channels connected yet." : "Unlock your workspace to see connected channels.";
    container.append(empty);
  }
  const selected = accountList.filter((account) => selectedAccountIds.has(account.id));
  $("#selected-account-count").textContent = selected.length + " selected";
}

function renderComposerDetails() {
  $("#instagram-details").hidden = activeDetailsTab !== "instagram";
  $("#youtube-details").hidden = activeDetailsTab !== "youtube";
  document.querySelectorAll("[data-details-tab]").forEach((tab) => {
    const active = tab.dataset.detailsTab === activeDetailsTab;
    tab.classList.toggle("active", active);
    tab.setAttribute("aria-selected", String(active));
    tab.tabIndex = active ? 0 : -1;
  });
  $("#publish-caption").disabled = publishing;
  $("#youtube-publish-title").disabled = publishing;
  $("#youtube-publish-title").required = false;
  $("#youtube-publish-description").disabled = publishing;
  $("#youtube-publish-privacy").disabled = publishing;
  $("#youtube-made-for-kids").disabled = publishing;
}

function renderComposerPreview() {
  const item = contentItems.find((content) => content.id === selectedContentId);
  const media = $("#composer-preview-media");
  media.replaceChildren();
  if (item) {
    media.classList.add("has-media");
    const node = document.createElement(item.content_type.startsWith("image/") ? "img" : "video");
    node.src = item.media_url;
    node.alt = item.content_type.startsWith("image/") ? item.filename : "Selected video preview";
    if (node instanceof HTMLVideoElement) {
      node.muted = true;
      node.controls = true;
      node.playsInline = true;
      node.preload = "metadata";
    }
    media.append(node);
    $("#preview-file-name").textContent = item.filename;
    $("#preview-file-meta").textContent = `${mediaKind(item)} · ${formatBytes(item.size)}`;
  } else {
    media.classList.remove("has-media");
    const placeholder = document.createElement("span");
    placeholder.innerHTML = '<svg class="icon"><use href="#i-media"/></svg><small>Your selected media<br>will appear here</small>';
    media.append(placeholder);
    $("#preview-file-name").textContent = "No media selected";
    $("#preview-file-meta").textContent = "Choose a preview to begin";
  }
  const selected = accountList.filter((account) => selectedAccountIds.has(account.id));
  const targets = $("#composer-preview-destinations");
  targets.replaceChildren();
  $("#preview-target-count").textContent = selected.length ? `${selected.length} selected` : "No profiles yet";
  if (selected.length) {
    for (const account of selected) {
      const chip = document.createElement("span");
      chip.className = "preview-destination-chip";
      chip.append(makeProfileAvatar(account, account.provider, "tiny"));
      const name = document.createElement("span");
      name.textContent = account.name;
      chip.append(name);
      targets.append(chip);
    }
  } else {
    const placeholder = document.createElement("span");
    placeholder.className = "preview-placeholder";
    placeholder.textContent = "Your selected profiles will show here";
    targets.append(placeholder);
  }
  const copy = $("#composer-preview-copy");
  if (copy) {
    const instagramSelected = selected.some((account) => account.provider === "instagram");
    const youtubeSelected = selected.some((account) => account.provider === "youtube");
    if (youtubeSelected && $("#youtube-publish-title").value.trim()) copy.textContent = $("#youtube-publish-title").value.trim();
    else if (instagramSelected && $("#publish-caption").value.trim()) copy.textContent = $("#publish-caption").value.trim();
    else copy.textContent = "Your post text preview will appear here.";
  }
}

function renderComposer() {
  selectedAccountIds = new Set([...selectedAccountIds].filter((id) => accountList.some((account) => account.id === id)));
  if (selectedContentId && !contentItems.some((item) => item.id === selectedContentId)) selectedContentId = null;
  renderComposerMedia();
  renderComposerDestinations();
  renderComposerWorkflow();
  renderComposerDetails();
  renderComposerPreview();
  syncPublishButtons();
}

function renderComposerWorkflow() {
  const item = contentItems.find((content) => content.id === selectedContentId);
  const selected = accountList.filter((account) => selectedAccountIds.has(account.id));
  $("#composer-media-hint").textContent = item
    ? mediaKind(item) + " selected."
    : !authenticated ? "Unlock your workspace to choose saved media."
      : !contentStorageConfigured ? "Media storage needs setup before uploads can be saved."
        : "Choose one item from your library.";
  $("#composer-channel-hint").textContent = selected.length
    ? selected.length + " " + (selected.length === 1 ? "channel" : "channels") + " selected."
    : "Choose at least one account.";
}

function syncPublishButtons() {
  const selected = accountList.filter((account) => selectedAccountIds.has(account.id));
  const hasResults = activeInstagramPosts.length > 0 || youtubeUploadResults.length > 0;
  const item = contentItems.find((content) => content.id === selectedContentId);
  const hasInstagram = selected.some((account) => account.provider === "instagram");
  const hasYoutube = selected.some((account) => account.provider === "youtube");
  const title = $("#youtube-publish-title").value.trim();
  const ready = authenticated && contentStorageConfigured && Boolean(item) && selected.length > 0 &&
    (!hasInstagram || instagramMediaConfigured) && (!hasYoutube || item.youtube_compatible) &&
    (!hasInstagram || item.instagram_compatible) && (!hasYoutube || title.length > 0) && !publishing && !hasResults;
  const button = $("#publish-submit");
  button.disabled = !ready;
  button.querySelector("span").textContent = publishing
    ? "Publishing…"
    : hasResults
      ? "Delivery status"
      : "Publish to " + selected.length + (selected.length === 1 ? " channel" : " channels");
  $("#session-button").disabled = publishing;
  $("#workspace-select").disabled = publishing;
  $("#new-workspace").disabled = publishing;
  $("#content-file").disabled = !authenticated || !contentStorageConfigured || publishing;
  $("#content-upload-submit").disabled = !authenticated || !contentStorageConfigured || publishing;
  $("#publish-file").disabled = !authenticated || !contentStorageConfigured || publishing;
  $("#publish-upload-trigger").disabled = !authenticated || !contentStorageConfigured || publishing;
}



function renderContent() {
  const list = $("#content-list");
  const empty = $("#content-empty");
  list.replaceChildren();
  $("#content-count").textContent = authenticated ? String(contentItems.length) : "—";
  $("#content-nav-count").textContent = authenticated ? String(contentItems.length) : "—";
  renderOverview();
  if (!authenticated) {
    $("#content-empty-action").hidden = false;
    $("#content-empty-title").textContent = "Unlock your workspace to view content.";
    $("#content-empty-description").textContent = "Your saved photos and videos are available after you unlock.";
    $("#content-empty-action").textContent = "Unlock workspace";
    empty.hidden = false;
    list.hidden = true;
    $("#content-storage-hint").textContent = "Unlock your workspace before uploading content.";
    return;
  }
  if (!contentStorageConfigured) {
    $("#content-empty-title").textContent = "Media storage isn't configured.";
    $("#content-empty-description").textContent = "Set INSTAGRAM_MEDIA_BUCKET on the service to enable content uploads.";
    $("#content-empty-action").hidden = true;
    empty.hidden = false;
    list.hidden = true;
    $("#content-storage-hint").textContent = "Set INSTAGRAM_MEDIA_BUCKET to enable uploads.";
    return;
  }
  $("#content-empty-action").hidden = false;
  $("#content-storage-hint").textContent = `JPEG photos and videos up to ${formatBytes(maxUploadBytes)} each.`;
  if (!contentItems.length) {
    $("#content-empty-title").textContent = "Your media will appear here.";
    $("#content-empty-description").textContent = "Upload a photo or video to get started.";
    $("#content-empty-action").textContent = "Upload media";
    empty.hidden = false;
    list.hidden = true;
    return;
  }
  empty.hidden = true;
  list.hidden = false;
  for (const item of contentItems) list.append(makeMediaCard(item));
}

function makeMediaCard(item, { compact = false } = {}) {
  const card = document.createElement("article");
  card.className = compact ? "media-card overview-media-card" : "media-card";
  const preview = document.createElement("div");
  preview.className = "media-preview";
  if (item.content_type.startsWith("image/")) {
    const image = document.createElement("img");
    image.src = item.media_url;
    image.alt = item.filename;
    image.loading = "lazy";
    preview.append(image);
  } else {
    const video = document.createElement("video");
    video.src = item.media_url;
    video.controls = !compact;
    video.preload = "metadata";
    video.playsInline = true;
    preview.append(video);
  }
  const details = document.createElement("div");
  details.className = "media-details";
  const filename = document.createElement("strong");
  filename.textContent = item.filename;
  const metadata = document.createElement("small");
  metadata.textContent = `${item.content_type} · ${formatBytes(item.size)}`;
  const tags = document.createElement("div");
  tags.className = "media-tags";
  if (item.instagram_compatible) tags.append(makeMediaTag("Instagram", "instagram"));
  if (item.youtube_compatible) tags.append(makeMediaTag("YouTube", "youtube"));
  details.append(filename, metadata, tags);
  card.append(preview, details);
  return card;
}

function makeOverviewMediaRow(item) {
  const row = document.createElement("div");
  row.className = "overview-media-row";
  const preview = document.createElement("span");
  preview.className = "overview-media-thumb";
  const node = document.createElement(item.content_type.startsWith("image/") ? "img" : "video");
  node.src = item.media_url;
  node.alt = "";
  if (node instanceof HTMLVideoElement) {
    node.muted = true;
    node.preload = "metadata";
    node.playsInline = true;
  }
  preview.append(node);
  const copy = document.createElement("span");
  copy.className = "overview-media-copy";
  const title = document.createElement("strong");
  title.textContent = item.filename;
  const meta = document.createElement("small");
  meta.textContent = mediaKind(item) + " · " + formatBytes(item.size);
  copy.append(title, meta);
  row.append(preview, copy);
  return row;
}

function makeMediaTag(label, platform) {
  const tag = document.createElement("span");
  tag.className = `media-tag ${platform}`;
  tag.textContent = label;
  return tag;
}

function formatBytes(bytes) {
  if (bytes < 1024 * 1024) return `${Math.max(1, Math.round(bytes / 1024))} KB`;
  if (bytes >= 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024 * 1024)).toFixed(1)} GB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function showUnlock() {
  window.location.assign("/login");
}

function renderAccounts() {
  const list = $("#accounts-list");
  list.replaceChildren();
  $("#account-count").textContent = String(accountList.length);
  $("#nav-count").textContent = String(accountList.length);
  $("#empty-state").hidden = accountList.length > 0;
  list.hidden = accountList.length === 0;
  $("#empty-title").textContent = "No channels connected yet";
  $("#empty-description").textContent = authenticated
    ? "Connect Instagram or YouTube to start publishing."
    : "Unlock your workspace to view or connect an account.";
  $("#empty-connect").textContent = authenticated ? "Connect a channel" : "Unlock workspace";
  for (const provider of Object.keys(names)) {
    const count = accountList.filter((account) => account.provider === provider).length;
    const badge = $(`#${provider}-state`);
    badge.textContent = count ? `${count} connected` : (providerIssues[provider] ? "Setup needed" : "Ready to connect");
    badge.classList.toggle("connected", count > 0);
  }
  for (const account of accountList) {
    const row = $("#account-template").content.firstElementChild.cloneNode(true);
    const provider = account.provider;
    const expired = account.expires_at * 1000 <= Date.now();
    row.querySelector(".account-platform").classList.add(provider);
    row.querySelector("use").setAttribute("href", `#i-${provider}`);
    row.querySelector(".account-name").textContent = account.name;
    row.querySelector(".account-platform-name").textContent = names[provider];
    const status = row.querySelector(".account-status");
    status.textContent = expired ? "Reconnect needed" : "Connected";
    status.classList.toggle("expired", expired);
    status.title = `Authorization expires ${new Date(account.expires_at * 1000).toLocaleString()}`;
    const reconnect = row.querySelector(".reconnect-account");
    reconnect.setAttribute("aria-label", `Reconnect ${account.name}`);
    reconnect.addEventListener("click", () => connect(provider));
    const disconnect = row.querySelector(".disconnect-account");
    disconnect.setAttribute("aria-label", `Disconnect ${account.name}`);
    disconnect.addEventListener("click", () => {
      disconnecting = account;
      $("#disconnect-description").textContent = `${account.name} · ${names[provider]}`;
      $("#disconnect-dialog").showModal();
    });
    list.append(row);
  }
  renderComposer();
  renderWorkspaceFlow();
}

async function loadAccounts() {
  if (!authenticated) return;
  const result = await api("/accounts?include_avatars=true");
  accountList = result.accounts;
  renderAccounts();
}

async function loadContent() {
  if (!authenticated) {
    contentItems = [];
    renderContent();
    renderComposer();
    return;
  }
  const result = await api("/content");
  contentItems = result.items;
  renderContent();
  renderComposer();
  renderWorkspaceFlow();
}

function renderOverviewPosts() {
  const container = $("#overview-posts");
  if (!container) return;
  container.replaceChildren();
  const note = (text) => {
    const paragraph = document.createElement("p");
    paragraph.className = "mini-empty";
    paragraph.textContent = text;
    return paragraph;
  };
  if (!authenticated) { container.append(note("Unlock to see your publishing activity.")); return; }
  if (historyError) { container.append(note(historyError)); return; }
  if (!historyLoaded || historyLoading) { container.append(note("Loading recent activity…")); return; }
  if (!postHistory.length) { container.append(note("Nothing published yet.")); return; }
  for (const post of postHistory.slice(0, 3)) {
    const row = document.createElement("button");
    row.type = "button";
    row.className = "mini-post";
    const badge = document.createElement("span");
    badge.className = `mini-post-dot ${post.provider}`;
    badge.append(icon(post.provider === "youtube" ? "youtube" : "instagram"));
    const copy = document.createElement("span");
    copy.className = "mini-post-copy";
    const title = document.createElement("strong");
    title.textContent = post.title || post.caption || post.media_name ||
      (post.provider === "youtube" ? "YouTube video" : "Instagram post");
    const meta = document.createElement("small");
    const when = new Date(Number(post.published_at) * 1000)
      .toLocaleDateString(undefined, { month: "short", day: "numeric" });
    meta.textContent = `${names[post.provider]} · ${when}`;
    copy.append(title, meta);
    row.append(badge, copy);
    row.addEventListener("click", () => setPage("history"));
    container.append(row);
  }
}

function historyStatusLabel(status) {
  const labels = {
    published: "Published",
    publishing: "Publishing",
    processing: "Processing",
    ready: "Ready",
    failed: "Failed",
    expired: "Expired",
    unknown: "Unknown",
  };
  return labels[status] || status;
}

function renderPostHistory() {
  const locked = $("#history-locked");
  if (!locked) return;
  const loading = authenticated && (historyLoading || !historyLoaded);
  const hasError = authenticated && !loading && Boolean(historyError);
  const hasPosts = authenticated && !loading && !hasError && postHistory.length > 0;
  locked.hidden = authenticated;
  $("#history-loading").hidden = !loading;
  const error = $("#history-error");
  error.hidden = !hasError;
  error.replaceChildren();
  if (hasError) {
    const message = document.createElement("span");
    message.textContent = historyError;
    const refresh = document.createElement("button");
    refresh.className = "text-button";
    refresh.type = "button";
    refresh.textContent = "Try again";
    refresh.addEventListener("click", () => void loadPostHistory());
    error.append(message, refresh);
  }
  $("#history-empty").hidden = !authenticated || loading || hasError || hasPosts;
  const list = $("#history-list");
  list.replaceChildren();
  list.hidden = !hasPosts;
  $("#history-nav-count").textContent = authenticated && historyLoaded && !historyError
    ? String(postHistory.length)
    : "—";
  const homeCount = $("#overview-history-count");
  if (homeCount) {
    homeCount.textContent = authenticated && historyLoaded && !historyError
      ? String(postHistory.length)
      : "—";
  }
  renderOverviewPosts();
  for (const post of postHistory) {
    const row = document.createElement("article");
    row.className = "history-row";
    row.setAttribute("role", "listitem");

    const preview = document.createElement("div");
    preview.className = `history-preview ${post.provider}`;
    const youtubeThumbnail = post.provider === "youtube" && /^[A-Za-z0-9_-]{1,64}$/.test(post.platform_post_id)
      ? `https://i.ytimg.com/vi/${post.platform_post_id}/hqdefault.jpg`
      : null;
    const imageUrl = youtubeThumbnail ||
      (post.media_type === "IMAGE" || post.media_type?.startsWith("image/")
        ? post.media_url
        : null);
    if (imageUrl?.startsWith("https://")) {
      const image = document.createElement("img");
      image.src = imageUrl;
      image.alt = "";
      preview.append(image);
    } else {
      const icon = document.createElementNS("http://www.w3.org/2000/svg", "svg");
      icon.classList.add("icon");
      const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
      use.setAttribute("href", post.provider === "youtube" ? "#i-youtube" : "#i-instagram");
      icon.append(use);
      preview.append(icon);
    }

    const copy = document.createElement("div");
    copy.className = "history-copy";
    const platform = document.createElement("div");
    platform.className = `history-platform ${post.provider}`;
    const platformIcon = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    platformIcon.classList.add("icon");
    const platformUse = document.createElementNS("http://www.w3.org/2000/svg", "use");
    platformUse.setAttribute("href", post.provider === "youtube" ? "#i-youtube" : "#i-instagram");
    platformIcon.append(platformUse);
    const platformName = document.createElement("strong");
    platformName.textContent = names[post.provider] || "Social channel";
    const account = document.createElement("span");
    account.textContent = post.account_name;
    platform.append(platformIcon, platformName, account);

    const title = document.createElement("h2");
    title.textContent = post.title || post.caption || (post.provider === "youtube" ? "YouTube video" : "Instagram post");
    copy.append(title, platform);
    if (post.media_name) {
      const filename = document.createElement("small");
      filename.className = "history-filename";
      filename.textContent = post.media_name;
      copy.append(filename);
    }

    const meta = document.createElement("div");
    meta.className = "history-meta";
    const date = document.createElement("time");
    const publishedAt = new Date(Number(post.published_at) * 1000);
    date.dateTime = publishedAt.toISOString();
    date.textContent = publishedAt.toLocaleString(undefined, {
      month: "short", day: "numeric", year: "numeric", hour: "numeric", minute: "2-digit",
    });
    const status = post.status || "published";
    const statusBadge = document.createElement("span");
    statusBadge.className = `history-status ${status}`;
    statusBadge.textContent = historyStatusLabel(status);
    meta.append(statusBadge, date);
    if (post.visibility) {
      const visibility = document.createElement("span");
      visibility.className = "history-visibility";
      visibility.textContent = post.visibility;
      meta.append(visibility);
    }
    if (post.post_url?.startsWith("https://")) {
      const link = document.createElement("a");
      link.className = "history-open-link";
      link.href = post.post_url;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      link.setAttribute("aria-label", "Open published post");
      link.append(icon("external"));
      meta.append(link);
    }
    row.append(preview, copy, meta);
    list.append(row);
  }
}

async function loadPostHistory() {
  if (!authenticated) {
    historyRequestId += 1;
    postHistory = [];
    historyLoading = false;
    historyLoaded = false;
    historyError = null;
    renderPostHistory();
    return;
  }
  const requestId = ++historyRequestId;
  historyLoading = true;
  historyError = null;
  renderPostHistory();
  try {
    const result = await api("/posts");
    if (requestId !== historyRequestId || !authenticated) return;
    postHistory = result.posts;
    historyLoaded = true;
  } catch (error) {
    if (requestId !== historyRequestId || !authenticated) return;
    historyError = error.message;
    historyLoaded = authenticated;
  } finally {
    if (requestId === historyRequestId) {
      historyLoading = false;
      renderPostHistory();
    }
  }
}

async function connect(provider, popupAlreadyOpen = false) {
  if (providerIssues[provider]) {
    notice(providerIssues[provider], true);
    $("#setup-dialog").showModal();
    return;
  }
  if (!providerConfigured[provider]) {
    notice(`${names[provider]} setup is incomplete. Open the setup guide to see what's missing.`, true);
    return;
  }
  if (!authenticated) { showUnlock(provider); return; }
  const buttons = document.querySelectorAll("[data-connect]");
  const button = document.querySelector(`[data-connect="${provider}"]`);
  const label = button.querySelector("span");
  try {
    // Opening before the first await preserves the browser's user-activation permission.
    if (!popupAlreadyOpen && !oauthPopup.open(provider)) return;
    buttons.forEach((button) => { button.disabled = true; });
    label.textContent = `Sign in to ${names[provider]}…`;
    const result = await api(`/oauth/${provider}/authorize?ui=true&popup=true`, { method: "POST" });
    oauthPopup.navigate(result.authorization_url);
    notice(`Finish connecting ${names[provider]} in the sign-in window. This workspace will stay open.`, false, {
      label: "Cancel sign-in",
      run() {
        oauthPopup.close();
        resetConnectButtons();
        notice("Sign-in cancelled. Your existing connections haven't changed.");
      },
    });
  } catch (error) {
    oauthPopup.close();
    notice(error.message, true);
    if (!authenticated) showUnlock(provider);
    resetConnectButtons();
  }
}

document.querySelectorAll("[data-connect]").forEach((button) => {
  button.addEventListener("click", () => connect(button.dataset.connect));
});
document.querySelectorAll("[data-nav]").forEach((item) => {
  item.addEventListener("click", (event) => {
    event.preventDefault();
    setPage(item.dataset.nav);
  });
});
document.querySelectorAll("[data-details-tab]").forEach((tab) => {
  tab.addEventListener("click", () => {
    activeDetailsTab = tab.dataset.detailsTab;
    renderComposerDetails();
    renderComposerPreview();
  });
});
$("#composer-media-action").addEventListener("click", () => {
  if (!authenticated) { showUnlock(); return; }
  setPage("content");
});
function openContentUpload() {
  if (!authenticated) { showUnlock(); return; }
  if (!contentStorageConfigured) {
    notice("Media storage needs setup before uploads can be saved.", true);
    return;
  }
  $("#content-upload-status").textContent = "";
  $("#upload-dialog").showModal();
}
$("#content-upload-trigger").addEventListener("click", openContentUpload);
document.querySelectorAll("[data-details-tab]").forEach((tab) => {
  tab.addEventListener("keydown", (event) => {
    const tabs = [...document.querySelectorAll("[data-details-tab]")];
    const current = tabs.indexOf(tab);
    const next = event.key === "ArrowRight" ? (current + 1) % tabs.length
      : event.key === "ArrowLeft" ? (current - 1 + tabs.length) % tabs.length
        : event.key === "Home" ? 0
          : event.key === "End" ? tabs.length - 1
            : current;
    if (next === current) return;
    event.preventDefault();
    tabs[next].click();
    tabs[next].focus();
  });
});
$("#publish-upload-trigger").addEventListener("click", () => {
  if (!authenticated) { showUnlock(); return; }
  if (!contentStorageConfigured) {
    notice("Media storage needs setup before uploads can be saved.", true);
    return;
  }
  $("#publish-file").click();
});
$("#publish-file").addEventListener("change", async (event) => {
  const file = event.target.files[0];
  if (!file) return;
  if (!authenticated) { showUnlock(); return; }
  if (!contentStorageConfigured) {
    notice("Media storage needs setup before uploads can be saved.", true);
    return;
  }
  const button = $("#publish-upload-trigger");
  const status = $("#publish-upload-status");
  $("#publish-upload-name").textContent = file.name;
  status.hidden = false;
  status.textContent = "Adding your file to the media library…";
  button.disabled = true;
  try {
    const form = new FormData();
    form.append("file", file);
    const item = await api("/content", { method: "POST", body: form });
    await loadContent();
    selectedContentId = item.id;
    renderComposer();
    status.textContent = "Added to your library.";
    $("#publish-file").value = "";
    $("#publish-upload-name").textContent = "No file selected";
  } catch (error) {
    status.textContent = error.message;
    notice(error.message, true);
  } finally {
    button.disabled = false;
    syncPublishButtons();
  }
});
$("#refresh-history").addEventListener("click", () => {
  if (!authenticated) { showUnlock(); return; }
  void loadPostHistory();
});
$("#history-unlock").addEventListener("click", () => showUnlock());
$("#overview-primary-action").addEventListener("click", () => {
  if (!authenticated) { showUnlock(); return; }
  setPage("publish");
});
window.addEventListener("popstate", () => {
  const page = window.location.hash.slice(1);
  setPage(["overview", "analytics", "history", "connections", "content", "publish"].includes(page) ? page : "overview", { recordHistory: false });
});
$("#analytics-range").addEventListener("change", (event) => {
  analyticsDays = Number(event.target.value);
  void loadAnalytics();
});
$("#refresh-analytics").addEventListener("click", () => { void loadAnalytics(); });
$("#analytics-unlock").addEventListener("click", () => showUnlock());
$("#empty-connect").addEventListener("click", () => {
  if (!authenticated) { showUnlock(); return; }
  $("#add-channel").scrollIntoView({ behavior: "smooth", block: "start" });
});
$("#content-file").addEventListener("change", (event) => {
  $("#content-file-name").textContent = event.target.files[0]?.name || "No file selected";
});
document.querySelectorAll("[data-setup]").forEach((button) => {
  button.addEventListener("click", () => {
    $("#setup-dialog").showModal();
  });
});
document.querySelectorAll("[data-close]").forEach((button) => {
  button.addEventListener("click", () => document.getElementById(button.dataset.close).close());
});
document.querySelectorAll("[data-copy]").forEach((button) => {
  button.addEventListener("click", async () => {
    const input = document.getElementById(button.dataset.copy);
    try {
      await navigator.clipboard.writeText(input.value);
      const label = button.textContent;
      button.textContent = "Copied";
      window.setTimeout(() => { button.textContent = label; }, 1800);
    } catch {
      input.focus();
      input.select();
      notice("Select and copy the callback address from the setup guide.");
    }
  });
});
$("#session-button").addEventListener("click", () => lockWorkspace());

$("#workspace-select").addEventListener("change", async (event) => {
  const select = event.target;
  select.disabled = true;
  try {
    await api(`/workspaces/${encodeURIComponent(select.value)}/select`, { method: "POST" });
    window.location.assign("/app");
  } catch (error) {
    select.value = activeWorkspaceId;
    notice(error.message, true);
    select.disabled = false;
  }
});
$("#new-workspace").addEventListener("click", () => {
  $("#workspace-error").hidden = true;
  $("#workspace-dialog").showModal();
  $("#new-workspace-name").focus();
});
$("#workspace-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = $("#workspace-submit");
  button.disabled = true;
  $("#workspace-error").hidden = true;
  try {
    const workspace = await api("/workspaces", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: $("#new-workspace-name").value.trim() }),
    });
    await api(`/workspaces/${workspace.id}/select`, { method: "POST" });
    window.location.assign("/app");
  } catch (error) {
    $("#workspace-error").textContent = error.message;
    $("#workspace-error").hidden = false;
  } finally { button.disabled = false; }
});

$("#reload-accounts").addEventListener("click", async () => {
  if (!authenticated) { showUnlock(); return; }
  const button = $("#reload-accounts");
  button.disabled = true;
  try { await loadAccounts(); notice("Your connections are up to date."); }
  catch (error) { notice(error.message, true); }
  finally { button.disabled = false; }
});

$("#content-empty-action").addEventListener("click", () => {
  openContentUpload();
});

$("#content-upload-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!authenticated) { showUnlock(); return; }
  const file = $("#content-file").files[0];
  if (!file) { notice("Choose a photo or video to upload.", true); return; }
  const button = $("#content-upload-submit");
  button.disabled = true;
  button.querySelector("span").textContent = "Uploading…";
  $("#content-upload-status").textContent = "Saving your media to the content library…";
  try {
    const form = new FormData();
    form.append("file", file);
    await api("/content", { method: "POST", body: form });
    $("#content-file").value = "";
    $("#content-file-name").textContent = "No file selected";
    await loadContent();
    $("#content-upload-status").textContent = "Content uploaded and ready to publish.";
    $("#upload-dialog").close();
    notice("Content added to your library.");
  } catch (error) {
    $("#content-upload-status").textContent = error.message;
    notice(error.message, true);
  } finally {
    button.querySelector("span").textContent = "Upload media";
    syncPublishButtons();
  }
});

$("#reload-content").addEventListener("click", async () => {
  if (!authenticated) { showUnlock(); return; }
  const button = $("#reload-content");
  button.disabled = true;
  try { await loadContent(); notice("Your content library is up to date."); }
  catch (error) { notice(error.message, true); }
  finally { button.disabled = false; }
});

$("#publish-caption").addEventListener("input", (event) => {
  $("#caption-count").textContent = String(event.target.value.length);
  renderComposerPreview();
});
$("#youtube-publish-title").addEventListener("input", () => {
  syncPublishButtons();
  renderComposerPreview();
});
$("#youtube-publish-description").addEventListener("input", syncPublishButtons);
$("#youtube-publish-privacy").addEventListener("change", syncPublishButtons);
$("#youtube-made-for-kids").addEventListener("change", syncPublishButtons);

$("#confirm-disconnect").addEventListener("click", async () => {
  if (!disconnecting) return;
  const button = $("#confirm-disconnect");
  button.disabled = true;
  try {
    await api(`/accounts/${encodeURIComponent(disconnecting.id)}`, { method: "DELETE" });
    $("#disconnect-dialog").close();
    await loadAccounts();
    notice("Account disconnected from Relay.");
    disconnecting = null;
  } catch (error) {
    $("#disconnect-dialog").close();
    notice(error.message, true);
  } finally { button.disabled = false; }
});

function formatStatusTime(value) {
  if (!value) return null;
  return new Date(value).toLocaleTimeString([], { hour: "numeric", minute: "2-digit", second: "2-digit" });
}

function statusText(status, post) {
  const publishedTime = formatStatusTime(post.updatedAt);
  const labels = {
    processing: "Not published yet · Instagram is checking this media.",
    ready: "Not live yet · Instagram is ready for you to publish.",
    publishing: "Sending the publish request · waiting for Instagram to confirm.",
    published: publishedTime ? `Published on Instagram · confirmed at ${publishedTime}.` : "Published on Instagram · confirmed live.",
    failed: "Not published · Instagram returned an error for this destination.",
    expired: "Not published · the Instagram media container expired.",
    unknown: "Not confirmed · Instagram may have received it. Check status before retrying.",
  };
  return labels[status] || status || "Waiting for Instagram status.";
}

function renderInstagramStatusSummary() {
  const summary = $("#publish-status-summary");
  const posts = activeInstagramPosts;
  summary.hidden = posts.length === 0;
  if (!posts.length) {
    summary.textContent = "";
    return;
  }
  const count = (status) => posts.filter((post) => post.status === status).length;
  const published = count("published");
  const processing = count("processing");
  const ready = count("ready");
  const publishing = count("publishing");
  const unknown = count("unknown");
  const failed = count("failed") + count("expired");
  const messages = [];
  if (published) messages.push("Instagram confirmed " + published + " of " + posts.length + " destination" + (posts.length === 1 ? "" : "s") + " published.");
  if (processing) messages.push(processing + " Instagram post" + (processing === 1 ? " is" : "s are") + " still processing and not published yet. Status refreshes automatically every minute.");
  if (ready) messages.push(ready + " Instagram post" + (ready === 1 ? " is" : "s are") + " ready to publish, but not live yet. Choose “Publish ready posts” to publish.");
  if (publishing) messages.push(publishing + " publish request" + (publishing === 1 ? " is" : "s are") + " waiting for Instagram confirmation.");
  if (unknown) messages.push(unknown + " status" + (unknown === 1 ? " is" : "es are") + " unclear. Check Instagram before retrying.");
  if (failed) messages.push(failed + " destination" + (failed === 1 ? " needs" : "s need") + " attention.");
  summary.textContent = messages.join(" ");
  summary.className = "status-summary" + (processing || publishing || unknown ? " pending" : failed ? " attention" : published ? " published" : " attention");
}

function renderPublishResults() {
  const section = $("#publish-results-section");
  const list = $("#publish-results");
  list.replaceChildren();
  const results = [...activeInstagramPosts, ...youtubeUploadResults];
  section.hidden = !publishing && results.length === 0;
  const checkablePosts = activeInstagramPosts.filter((post) => ["processing", "ready", "unknown", "publishing"].includes(post.status));
  const checkDue = checkablePosts.some((post) => !post.nextCheckAt || post.nextCheckAt <= Date.now());
  const ready = activeInstagramPosts.some((post) => post.status === "ready");
  const checkButton = $("#instagram-check-all");
  checkButton.hidden = !checkablePosts.length;
  checkButton.disabled = instagramStatusCheckRunning || !checkDue;
  checkButton.textContent = checkDue ? "Check status now" : "Checking again automatically";
  $("#instagram-publish-ready").hidden = !ready;
  renderInstagramStatusSummary();
  for (const result of activeInstagramPosts) {
    const row = document.createElement("article");
    row.className = `publish-result-row ${result.status === "failed" ? "failed" : result.status}${result.checking ? " checking" : ""}`;
    row.append(makeProfileAvatar(result.account, "instagram", "result-avatar"));
    const copy = document.createElement("span");
    copy.className = "publish-result-copy";
    const name = document.createElement("strong");
    name.textContent = result.account.name;
    const description = document.createElement("small");
    copy.append(name, description);
    description.textContent = statusText(result.status, result);
    if (result.updatedAt && result.status !== "published") {
      const updated = document.createElement("small");
      updated.className = "publish-result-updated";
      updated.textContent = "Status updated " + formatStatusTime(result.updatedAt);
      copy.append(updated);
    }
    if (result.error) {
      const error = document.createElement("small");
      error.className = "publish-result-error";
      error.textContent = result.error;
      copy.append(error);
    }
    const badge = document.createElement("span");
    badge.className = "result-status-badge";
    const badges = { processing: "Checking", ready: "Ready · not live", publishing: "Publishing", published: "Published", failed: "Failed", expired: "Expired", unknown: "Verify status" };
    badge.textContent = result.checking ? "Checking…" : badges[result.status] || "Pending";
    row.append(copy, badge);
    list.append(row);
  }
  for (const result of youtubeUploadResults) {
    const row = document.createElement("article");
    row.className = `publish-result-row ${result.error ? "failed" : "published"}`;
    row.append(makeProfileAvatar(result.account, "youtube", "result-avatar"));
    const copy = document.createElement("span");
    copy.className = "publish-result-copy";
    const name = document.createElement("strong");
    name.textContent = result.account.name;
    const description = document.createElement("small");
    description.textContent = result.error || `Uploaded · ${result.privacyStatus || "private"}`;
    copy.append(name, description);
    if (result.url) {
      const link = document.createElement("a");
      link.className = "result-open-link";
      link.href = result.url;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      link.textContent = "Open video ↗";
      row.append(copy, link);
    } else {
      row.append(copy);
      const badge = document.createElement("span");
      badge.className = "result-status-badge";
      badge.textContent = "Failed";
      row.append(badge);
    }
    list.append(row);
  }
}

function setComposerProgress(title, message) {
  $("#publish-results-title").textContent = title;
  $("#publish-results-title").nextElementSibling.textContent = message;
}

$("#publish-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!authenticated) { showUnlock(); return; }
  const item = contentItems.find((content) => content.id === selectedContentId);
  const selected = accountList.filter((account) => selectedAccountIds.has(account.id));
  const instagramAccounts = selected.filter((account) => account.provider === "instagram");
  const youtubeAccounts = selected.filter((account) => account.provider === "youtube");
  if (!item || !selected.length) { notice("Choose a media preview and at least one destination profile first.", true); return; }
  if (youtubeAccounts.length && !$("#youtube-publish-title").value.trim()) { notice("YouTube needs a video title before upload.", true); return; }

  activeInstagramPosts = [];
  youtubeUploadResults = [];
  publishing = true;
  renderComposer();
  renderPublishResults();
  const mediaType = item.content_type === "image/jpeg" ? "IMAGE" : "REELS";
  for (const account of instagramAccounts) {
    setComposerProgress("Preparing Instagram", `Sending ${item.filename} to ${account.name}…`);
    try {
      const post = await api("/instagram/posts", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ account_id: account.id, media_type: mediaType, media_url: item.media_url, caption: $("#publish-caption").value }),
      });
      const status = post.status || "processing";
      activeInstagramPosts.push({
        account,
        id: post.id,
        status,
        updatedAt: Date.now(),
        nextCheckAt: ["processing", "unknown"].includes(status) ? Date.now() + 60_000 : null,
      });
    } catch (error) {
      activeInstagramPosts.push({ account, status: "failed", error: error.message });
    }
    renderPublishResults();
  }
  scheduleInstagramStatusCheck();
  for (const account of youtubeAccounts) {
    setComposerProgress("Uploading to YouTube", `Uploading ${item.filename} to ${account.name}. This can take a while…`);
    const form = new FormData();
    form.append("account_id", account.id);
    form.append("title", $("#youtube-publish-title").value.trim());
    form.append("description", $("#youtube-publish-description").value);
    form.append("privacy_status", $("#youtube-publish-privacy").value);
    form.append("made_for_kids", String($("#youtube-made-for-kids").checked));
    form.append("content_id", item.id);
    try {
      const uploaded = await api("/youtube/videos", { method: "POST", body: form });
      youtubeUploadResults.push({ account, url: uploaded.url, privacyStatus: uploaded.privacy_status });
    } catch (error) {
      youtubeUploadResults.push({ account, error: error.message });
    }
    renderPublishResults();
  }
  publishing = false;
  renderComposer();
  renderPublishResults();
  if (youtubeUploadResults.some((result) => result.url)) await loadPostHistory();
  const successful = activeInstagramPosts.filter((post) => post.id).length + youtubeUploadResults.filter((result) => result.url).length;
  const failures = activeInstagramPosts.filter((post) => post.status === "failed").length + youtubeUploadResults.filter((result) => result.error).length;
  if (failures) setComposerProgress("Some destinations need attention", `${successful} of ${selected.length} destinations accepted the post. Review the status for each destination below.`);
  else if (instagramAccounts.length) setComposerProgress("Instagram is checking your media", "Not published yet. This area refreshes automatically about once a minute; when ready, choose Publish ready posts.");
  else setComposerProgress("YouTube upload complete", "Your upload is complete. Open the video below to view it.");
  scheduleInstagramStatusCheck();
});

$("#publish-new-post").addEventListener("click", () => {
  if (instagramStatusTimer !== null) window.clearTimeout(instagramStatusTimer);
  instagramStatusTimer = null;
  selectedContentId = null;
  selectedAccountIds.clear();
  showMediaPicker = false;
  activeDetailsTab = "instagram";
  activeInstagramPosts = [];
  youtubeUploadResults = [];
  $("#publish-caption").value = "";
  $("#caption-count").textContent = "0";
  $("#youtube-publish-title").value = "";
  $("#youtube-publish-description").value = "";
  $("#youtube-publish-privacy").value = "private";
  $("#youtube-made-for-kids").checked = false;
  renderPublishResults();
  renderComposer();
  window.scrollTo({ top: 0, behavior: "smooth" });
});

function scheduleInstagramStatusCheck() {
  if (instagramStatusTimer !== null) window.clearTimeout(instagramStatusTimer);
  instagramStatusTimer = null;
  const pending = activeInstagramPosts.filter((post) => post.id && ["processing", "unknown", "publishing"].includes(post.status));
  if (!authenticated || !pending.length) return;
  const dueAt = Math.min(...pending.map((post) => post.nextCheckAt || Date.now() + 60_000));
  instagramStatusTimer = window.setTimeout(() => {
    instagramStatusTimer = null;
    void checkInstagramStatuses();
  }, Math.max(0, dueAt - Date.now()));
}

async function checkInstagramStatuses() {
  if (instagramStatusCheckRunning) return;
  const posts = activeInstagramPosts.filter((post) => post.id && ["processing", "unknown", "publishing"].includes(post.status) && (!post.nextCheckAt || post.nextCheckAt <= Date.now()));
  if (!posts.length) {
    scheduleInstagramStatusCheck();
    renderPublishResults();
    return;
  }
  instagramStatusCheckRunning = true;
  renderPublishResults();
  let newlyPublished = false;
  for (const post of posts) {
    post.checking = true;
    post.error = null;
    renderPublishResults();
    try {
      const updated = await api(`/instagram/posts/${encodeURIComponent(post.id)}`);
      newlyPublished ||= post.status !== "published" && updated.status === "published";
      post.status = updated.status;
      post.updatedAt = Date.now();
      post.error = null;
      post.checking = false;
      post.nextCheckAt = ["processing", "unknown", "publishing"].includes(post.status) ? Date.now() + 60_000 : null;
    } catch (error) {
      post.checking = false;
      post.updatedAt = Date.now();
      if (error.status === 404) {
        // The channel was disconnected, so this job can never resolve.
        post.status = "failed";
        post.error = "This post is no longer available. Its channel may have been disconnected.";
        post.nextCheckAt = null;
      } else {
        post.error = error.message;
        post.nextCheckAt = Date.now() + 60_000;
      }
    }
    renderPublishResults();
  }
  instagramStatusCheckRunning = false;
  if (newlyPublished) await loadPostHistory();
  if (activeInstagramPosts.some((post) => post.status === "ready")) setComposerProgress("Instagram is ready to publish", "This post is not live yet. Choose Publish ready posts when you want to make it live.");
  if (activeInstagramPosts.length && activeInstagramPosts.every((post) => ["published", "failed", "expired"].includes(post.status))) setComposerProgress("Instagram status confirmed", "Each destination now shows its final publishing result below.");
  renderPublishResults();
  scheduleInstagramStatusCheck();
}

$("#instagram-check-all").addEventListener("click", () => void checkInstagramStatuses());

$("#instagram-publish-ready").addEventListener("click", async () => {
  const button = $("#instagram-publish-ready");
  button.disabled = true;
  publishing = true;
  renderComposer();
  for (const post of activeInstagramPosts.filter((item) => item.id && item.status === "ready")) {
    post.status = "publishing";
    post.updatedAt = Date.now();
    post.error = null;
    renderPublishResults();
    try {
      const updated = await api(`/instagram/posts/${encodeURIComponent(post.id)}/publish`, { method: "POST" });
      post.status = updated.status;
      post.updatedAt = Date.now();
      post.error = null;
    } catch (error) {
      post.status = "unknown";
      post.error = error.message;
      post.updatedAt = Date.now();
      post.nextCheckAt = Date.now() + 60_000;
    }
    renderPublishResults();
  }
  publishing = false;
  renderComposer();
  renderPublishResults();
  button.disabled = false;
  if (activeInstagramPosts.every((post) => post.status === "published" || post.status === "failed" || post.status === "expired")) {
    setComposerProgress("Instagram publishing complete", "Your final post statuses are shown below.");
  }
  if (activeInstagramPosts.some((post) => post.status === "published")) await loadPostHistory();
  scheduleInstagramStatusCheck();
});
$("#palette-button").addEventListener("click", () => openPalette());
$("#palette-trigger").addEventListener("click", () => openPalette());

/* ── Command palette ───────────────────────────────────────── */
const paletteCommands = [
  { label: "Home", hint: "Overview", icon: "home", group: "Go to", run: () => setPage("overview") },
  { label: "Create a post", hint: "Compose", icon: "plus", group: "Go to", run: () => setPage("publish") },
  { label: "Media library", hint: "Upload and reuse", icon: "media", group: "Go to", run: () => setPage("content") },
  { label: "Channels", hint: "Connect accounts", icon: "link", group: "Go to", run: () => setPage("connections") },
  { label: "Analytics", hint: "Performance", icon: "chart", group: "Go to", run: () => setPage("analytics") },
  { label: "Post history", hint: "Recent posts and statuses", icon: "history", group: "Go to", run: () => setPage("history") },
  { label: "Log in", hint: "Open your account", icon: "lock", group: "Actions", run: () => showUnlock() },
  { label: "Log out", hint: "Sign out of this browser", icon: "lock", group: "Actions", run: () => lockWorkspace() },
  { label: "Connect Instagram", hint: "OAuth popup", icon: "instagram", group: "Actions", run: () => connect("instagram") },
  { label: "Connect YouTube", hint: "OAuth popup", icon: "youtube", group: "Actions", run: () => connect("youtube") },
  { label: "Setup guide", hint: "Callback URLs", icon: "book", group: "Actions", run: () => $("#setup-dialog").showModal() },
  { label: "API reference", hint: "Opens /docs", icon: "external", group: "Actions", run: () => window.open("/docs", "_blank", "noopener") },
];

let paletteMatches = [];
let paletteIndex = 0;

function openPalette() {
  const dialog = $("#palette-dialog");
  if (dialog.open) { dialog.close(); return; }
  $("#palette-input").value = "";
  renderPalette("");
  dialog.showModal();
  $("#palette-input").focus();
}

function renderPalette(query) {
  const list = $("#palette-list");
  const needle = query.trim().toLowerCase();
  paletteMatches = paletteCommands.filter((command) =>
    !needle || `${command.label} ${command.hint} ${command.group}`.toLowerCase().includes(needle));
  paletteIndex = 0;
  list.replaceChildren();
  if (!paletteMatches.length) {
    const empty = document.createElement("p");
    empty.className = "palette-empty";
    empty.textContent = "No matching commands.";
    list.append(empty);
    return;
  }
  let group = null;
  paletteMatches.forEach((command, index) => {
    if (command.group !== group) {
      group = command.group;
      const heading = document.createElement("p");
      heading.className = "palette-group";
      heading.textContent = group;
      list.append(heading);
    }
    const item = document.createElement("button");
    item.type = "button";
    item.className = "palette-item";
    item.setAttribute("role", "option");
    item.setAttribute("aria-selected", String(index === paletteIndex));
    item.append(icon(command.icon));
    const label = document.createElement("span");
    label.textContent = command.label;
    item.append(label);
    const hint = document.createElement("small");
    hint.textContent = command.hint;
    item.append(hint);
    item.addEventListener("click", () => runPalette(index));
    item.addEventListener("mousemove", () => {
      paletteIndex = index;
      [...list.querySelectorAll(".palette-item")].forEach((node, i) =>
        node.setAttribute("aria-selected", String(i === index)));
    });
    list.append(item);
  });
}

function runPalette(index) {
  const command = paletteMatches[index];
  $("#palette-dialog").close();
  if (command) window.setTimeout(() => command.run(), 0);
}

function movePalette(delta) {
  if (!paletteMatches.length) return;
  paletteIndex = (paletteIndex + delta + paletteMatches.length) % paletteMatches.length;
  const items = [...$("#palette-list").querySelectorAll(".palette-item")];
  items.forEach((node, i) => node.setAttribute("aria-selected", String(i === paletteIndex)));
  items[paletteIndex]?.scrollIntoView({ block: "nearest" });
}

$("#palette-input").addEventListener("input", (event) => renderPalette(event.target.value));
$("#palette-input").addEventListener("keydown", (event) => {
  if (event.key === "ArrowDown") { event.preventDefault(); movePalette(1); }
  else if (event.key === "ArrowUp") { event.preventDefault(); movePalette(-1); }
  else if (event.key === "Enter") { event.preventDefault(); runPalette(paletteIndex); }
});
$("#palette-dialog").addEventListener("click", (event) => {
  if (event.target === $("#palette-dialog")) $("#palette-dialog").close();
});

/* ── Keyboard shortcuts ────────────────────────────────────── */
const pageKeys = { h: "overview", n: "publish", l: "content", c: "connections", a: "analytics", y: "history" };
let pendingGoto = false;

async function lockWorkspace() {
  if (!authenticated) { showUnlock(); return; }
  try {
    await api("/session", { method: "DELETE" });
    authenticated = false;
    renderSession();
    window.location.replace("/");
  } catch (error) { notice(error.message, true); }
}

function isTyping(event) {
  const node = event.target;
  return node instanceof HTMLElement &&
    (node.tagName === "INPUT" || node.tagName === "TEXTAREA" || node.tagName === "SELECT" || node.isContentEditable);
}

document.addEventListener("keydown", (event) => {
  if (event.defaultPrevented) return;
  const meta = event.metaKey || event.ctrlKey;
  if (meta && event.key.toLowerCase() === "k") {
    event.preventDefault();
    openPalette();
    return;
  }
  if (isTyping(event) || meta || event.altKey) return;
  if ($("#palette-dialog").open) return;
  const openDialog = document.querySelector("dialog[open]");
  if (openDialog) return;

  if (event.key === "n") { event.preventDefault(); setPage("publish"); return; }
  if (event.key === "g") { pendingGoto = true; window.setTimeout(() => { pendingGoto = false; }, 1200); return; }
  if (pendingGoto && pageKeys[event.key]) {
    event.preventDefault();
    pendingGoto = false;
    setPage(pageKeys[event.key]);
  }
});

async function initialize() {
  try {
    const initialPage = window.location.hash.slice(1);
    setPage(["overview", "analytics", "history", "connections", "content", "publish"].includes(initialPage) ? initialPage : "overview", { recordHistory: false });
    const state = await api("/session");
    authenticated = state.authenticated;
    if (!authenticated) { window.location.replace("/login"); return; }
    activeWorkspaceId = state.workspace?.id || null;
    $("#workspace-tools").hidden = !state.user;
    $("#account-label").textContent = state.user?.name || "Legacy workspace";
    $("#account-label").title = state.user?.email || "Service key access";
    for (const workspace of state.workspaces || []) {
      const option = document.createElement("option");
      option.value = workspace.id;
      option.textContent = workspace.name;
      $("#workspace-select").append(option);
    }
    if (activeWorkspaceId) $("#workspace-select").value = activeWorkspaceId;
    instagramMediaConfigured = state.publishing?.instagram_media_configured === true;
    contentStorageConfigured = state.publishing?.content_storage_configured === true;
    maxUploadBytes = state.publishing?.max_upload_bytes || 0;
    renderSession();
    for (const [provider, configuration] of Object.entries(state.providers)) {
      providerConfigured[provider] = configuration.configured;
      providerIssues[provider] = configuration.setup_issue;
      $(`#${provider}-callback`).value = configuration.redirect_uri;
      if (configuration.setup_issue) {
        $(`#${provider}-state`).textContent = "Setup needed";
        $(`#${provider}-state`).classList.add("needs-setup");
        if (provider === "instagram") {
          $("#instagram-setup-warning").textContent = configuration.setup_issue;
          $("#instagram-setup-warning").hidden = false;
          const copyButton = document.querySelector('[data-copy="instagram-callback"]');
          copyButton.disabled = true;
          copyButton.textContent = "Configure HTTPS before registering this callback";
        }
      }
      if (!configuration.configured) {
        const button = document.querySelector(`[data-connect="${provider}"]`);
        button.disabled = true;
        button.querySelector("span").textContent = "App credentials needed";
        $(`#${provider}-state`).textContent = "Setup needed";
      }
    }
    await loadAccounts();
    await loadContent();
    if (authenticated) await loadPostHistory();
    if (activePage === "analytics") await loadAnalytics();
    const params = new URLSearchParams(window.location.search);
    const connected = params.get("connected");
    const error = params.get("oauth_error");
    if (Object.hasOwn(names, connected)) notice(`${names[connected]} connected. Welcome back!`);
    else if (error) {
      const messages = {
        cancelled: "Sign-in was cancelled. Your existing connections haven't changed.",
        expired: "That sign-in link expired or belongs to another browser. Click Connect to start again.",
      };
      notice(messages[error] || "We couldn't finish connecting. Check the setup guide and try again.", true);
    }
    if (connected || error) window.history.replaceState({}, "", "/");
  } catch (error) { notice(error.message, true); }
}
initialize();
