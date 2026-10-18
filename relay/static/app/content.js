"use strict";

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
    const duration = document.createElement("span");
    duration.className = "duration-badge";
    duration.textContent = "Video";
    video.addEventListener("loadedmetadata", () => {
      if (Number.isFinite(video.duration)) {
        const seconds = Math.floor(video.duration);
        duration.textContent = `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
      }
    }, { once: true });
    preview.append(duration);
  }
  const details = document.createElement("div");
  details.className = "media-details";
  const filename = document.createElement("strong");
  filename.textContent = item.filename;
  const metadata = document.createElement("small");
  metadata.textContent = formatBytes(item.size);
  details.append(filename, metadata);
  card.append(preview, details);
  return card;
}

function makeOverviewMediaRow(item) {
  const row = document.createElement("button");
  row.type = "button";
  row.className = "overview-media-tile";
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
  row.addEventListener("click", () => setPage("content"));
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

function localDraftKey() {
  return `relay:draft:${activeWorkspaceId || "legacy"}`;
}

function readLocalDraft() {
  try {
    const value = localStorage.getItem(localDraftKey());
    return value ? JSON.parse(value) : null;
  } catch { return null; }
}

function saveLocalDraft() {
  const draft = {
    caption: $("#publish-caption").value,
    title: $("#youtube-publish-title").value,
    description: $("#youtube-publish-description").value,
    visibility: $("#youtube-publish-privacy").value,
    madeForKids: $("#youtube-made-for-kids").checked,
    selectedContentId,
    selectedAccountIds: [...selectedAccountIds],
    savedAt: Date.now(),
  };
  try {
    localStorage.setItem(localDraftKey(), JSON.stringify(draft));
    $("#draft-status").textContent = "Saved";
    if (historyLoaded) renderPostHistory();
    notice("Draft saved in this browser.");
  } catch {
    $("#draft-status").textContent = "Could not save";
    notice("The draft could not be saved in this browser.", true);
  }
}

function restoreLocalDraft(draft = readLocalDraft()) {
  if (!draft) return;
  $("#publish-caption").value = draft.caption || "";
  $("#youtube-publish-title").value = draft.title || "";
  $("#youtube-publish-description").value = draft.description || "";
  $("#youtube-publish-privacy").value = draft.visibility || "private";
  $("#youtube-made-for-kids").checked = draft.madeForKids === true;
  selectedContentId = draft.selectedContentId || null;
  selectedAccountIds = new Set(draft.selectedAccountIds || []);
  activeDetailsTab = "master";
  $("#caption-count").textContent = String($("#publish-caption").value.length);
  renderComposer();
}

function showUnlock() {
  window.location.assign("/login");
}
