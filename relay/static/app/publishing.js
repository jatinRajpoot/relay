"use strict";

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
    form.append("description", $("#youtube-publish-description").value || $("#publish-caption").value);
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
  if (successful) {
    try { localStorage.removeItem(localDraftKey()); } catch { /* The published post is already recorded. */ }
    $("#draft-status").textContent = "";
    if (historyLoaded) renderPostHistory();
  }
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
  activeDetailsTab = "master";
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
  $("#main-content").scrollTop = 0;
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
      post.error = error.message;
      post.checking = false;
      post.updatedAt = Date.now();
      post.nextCheckAt = Date.now() + 60_000;
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
