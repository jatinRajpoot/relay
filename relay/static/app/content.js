"use strict";

function renderContent() {
  const list = $("#content-list");
  const empty = $("#content-empty");
  const description = $("#content-empty-description");
  list.replaceChildren();
  $("#content-count").textContent = authenticated ? String(contentItems.length) : "—";
  $("#content-nav-count").textContent = authenticated ? String(contentItems.length) : "—";
  renderOverview();
  if (!authenticated) {
    $("#content-empty-action").hidden = false;
    $("#content-empty-title").textContent = "Unlock your workspace to view content.";
    description.hidden = true;
    $("#content-empty-action").textContent = "Unlock workspace";
    empty.hidden = false;
    list.hidden = true;
    $("#content-storage-hint").textContent = "";
    $("#content-storage-hint").hidden = true;
    return;
  }
  if (!contentStorageConfigured) {
    $("#content-empty-title").textContent = "Media storage isn't configured.";
    description.textContent = "Set INSTAGRAM_MEDIA_BUCKET on the service to enable uploads.";
    description.hidden = false;
    $("#content-empty-action").hidden = false;
    $("#content-empty-action").textContent = "Copy setting name";
    empty.hidden = false;
    list.hidden = true;
    $("#content-storage-hint").textContent = "";
    $("#content-storage-hint").hidden = true;
    return;
  }
  $("#content-empty-action").hidden = false;
  $("#content-storage-hint").textContent = maxUploadBytes > 0
    ? `JPEG photos and videos up to ${formatBytes(maxUploadBytes)} each.`
    : "JPEG photos and videos.";
  $("#content-storage-hint").hidden = false;
  if (!contentItems.length) {
    $("#content-empty-title").textContent = "No media yet";
    description.hidden = true;
    $("#content-empty-action").textContent = "Upload media";
    empty.hidden = false;
    list.hidden = true;
    return;
  }
  description.hidden = true;
  empty.hidden = true;
  list.hidden = false;
  for (const item of contentItems) list.append(makeMediaCard(item));
}

function makeMediaCard(item) {
  const card = document.createElement("article");
  card.className = "media-card";
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
    video.controls = true;
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
  const use = document.createElement("button");
  use.type = "button";
  use.className = "button button-secondary button-sm";
  use.textContent = "Use in post";
  use.addEventListener("click", () => {
    selectedContentId = item.id;
    renderComposer();
    scheduleDraftSave();
    setPage("publish");
  });
  details.append(use);
  card.append(preview, details);
  return card;
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

let draftSaveTimer = null;

function scheduleDraftSave() {
  window.clearTimeout(draftSaveTimer);
  $("#draft-status").textContent = "Saving…";
  draftSaveTimer = window.setTimeout(() => saveLocalDraft({ announce: false }), 350);
}

function saveLocalDraft({ announce = true } = {}) {
  window.clearTimeout(draftSaveTimer);
  const savedAt = Date.now();
  const draft = {
    caption: $("#publish-caption").value,
    title: $("#youtube-publish-title").value,
    description: $("#youtube-publish-description").value,
    visibility: $("#youtube-publish-privacy").value,
    madeForKids: $("#youtube-made-for-kids").checked,
    selectedContentId,
    selectedAccountIds: [...selectedAccountIds],
    savedAt,
  };
  try {
    localStorage.setItem(localDraftKey(), JSON.stringify(draft));
    renderJourney();
    $("#draft-status").textContent = `Saved ${new Date(savedAt).toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" })}`;
    if (historyLoaded) renderPostHistory();
    if (announce) notice("Draft saved in this browser.");
    return true;
  } catch {
    $("#draft-status").textContent = "Draft not saved";
    if (announce) notice("Your draft could not be saved in this browser. Check browser storage and try again.", true);
    return false;
  }
}

function clearPublishedDraft(expectedDraft) {
  if (!expectedDraft) return;
  const currentDraft = readLocalDraft();
  if (!currentDraft || JSON.stringify(currentDraft) !== JSON.stringify(expectedDraft)) return;
  try {
    localStorage.removeItem(localDraftKey());
  } catch {
    return;
  }
  $("#draft-status").textContent = "";
  if (historyLoaded) renderPostHistory();
  notice("All destinations published. The saved draft was cleared.", false, {
    label: "Undo",
    run() {
      if (readLocalDraft()) {
        notice("A newer draft is already saved, so it was left unchanged.");
        return;
      }
      try {
        localStorage.setItem(localDraftKey(), JSON.stringify(expectedDraft));
        restoreLocalDraft(expectedDraft);
        if (historyLoaded) renderPostHistory();
        notice("Draft restored.");
      } catch {
        notice("The draft could not be restored in this browser.", true);
      }
    },
  });
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
