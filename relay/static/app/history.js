"use strict";

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
  const draft = readLocalDraft();
  const filteredPosts = historyFilter === "published"
    ? postHistory.filter((post) => (post.status || "published") === "published")
    : historyFilter === "failed"
      ? postHistory.filter((post) => ["failed", "expired", "unknown"].includes(post.status))
      : historyFilter === "drafts" ? [] : postHistory;
  const hasVisiblePosts = historyFilter === "drafts" ? Boolean(draft) : filteredPosts.length > 0 || (historyFilter === "all" && Boolean(draft));
  const hasPosts = authenticated && !loading && !hasError && hasVisiblePosts;
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
  const emptyTitle = $("#history-empty h3");
  const emptyCopy = $("#history-empty p");
  if (historyFilter === "drafts") {
    emptyTitle.textContent = "No saved drafts";
    emptyCopy.textContent = "Save a draft from Create a post to keep it here.";
  } else if (historyFilter === "failed") {
    emptyTitle.textContent = "No failed posts";
    emptyCopy.textContent = "Failed posts will appear here.";
  } else if (historyFilter === "published") {
    emptyTitle.textContent = "Nothing published yet";
    emptyCopy.textContent = "Published posts will appear here.";
  } else {
    emptyTitle.textContent = "Nothing here yet";
    emptyCopy.textContent = "Posts and saved drafts will appear here.";
  }
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
  if ((historyFilter === "drafts" || historyFilter === "all") && draft) {
    const row = document.createElement("article");
    row.className = "history-row history-draft-row";
    row.setAttribute("role", "listitem");
    const preview = document.createElement("div");
    preview.className = "history-preview";
    preview.append(icon("media"));
    const copy = document.createElement("div");
    copy.className = "history-copy";
    const title = document.createElement("h2");
    title.textContent = draft.title || draft.caption || "Untitled draft";
    const platform = document.createElement("div");
    platform.className = "history-platform";
    const label = document.createElement("span");
    label.textContent = "Saved in this browser";
    platform.append(label);
    copy.append(title, platform);
    const meta = document.createElement("div");
    meta.className = "history-meta";
    const status = document.createElement("span");
    status.className = "history-status ready";
    status.textContent = "Draft";
    const date = document.createElement("time");
    date.textContent = new Date(draft.savedAt || Date.now()).toLocaleDateString(undefined, { month: "short", day: "numeric" });
    meta.append(status, date);
    row.append(preview, copy, meta);
    row.tabIndex = 0;
    row.setAttribute("role", "button");
    row.setAttribute("aria-label", "Edit saved draft");
    row.addEventListener("click", () => { restoreLocalDraft(draft); setPage("publish"); });
    row.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") { event.preventDefault(); restoreLocalDraft(draft); setPage("publish"); }
    });
    list.append(row);
  }
  for (const post of filteredPosts) {
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
