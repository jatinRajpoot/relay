"use strict";

function formatBytes(bytes) {
  if (!bytes || bytes < 1024) return `${Math.max(0, Math.round((bytes || 0) / 1024))} KB`;
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
  if (bytes >= 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024 * 1024)).toFixed(1)} GB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

/* ── Drafts ────────────────────────────────────────────────────
   A draft is owned by the signed-in user inside one workspace. The key includes
   both ids, so signing in as another person on the same browser cannot surface
   the previous person's text. Drafts are browser-local: there is no server copy
   and no cross-device sync, and the interface never claims otherwise.

   "Has content" is decided once, by draftContent() in recovery.js, so the save
   path, Home, Activity, and the import offers can never disagree. An empty
   composer writes no key at all — that is what used to leave a phantom
   "Untitled draft" behind after every visit. */

function localDraftKey() {
  return draftStorageKey({ userId: activeUserId, workspaceId: activeWorkspaceId });
}

/* The pre-ownership key. It is never read as a draft. It is offered for
   recovery only when the authenticated bootstrap has proved this exact
   workspace belongs to the current user, and only until the person dismisses
   the offer or writes their own draft. */
function legacyKeyForWorkspace() {
  if (!activeUserId || !activeWorkspaceId) return null;
  return legacyDraftKey(activeWorkspaceId);
}

function readJson(key) {
  if (!key) return null;
  try {
    const value = localStorage.getItem(key);
    if (!value) return null;
    const parsed = JSON.parse(value);
    return parsed && typeof parsed === "object" ? parsed : null;
  } catch { return null; }
}

function readLocalDraft() {
  return readJson(localDraftKey());
}

function draftHasContent(draft) {
  return draftContent(draft).hasContent;
}

let draftSaveTimer = null;
let draftSaveFailed = false;

function setDraftStatus(text) {
  setText("#draft-status", text);
}

function scheduleDraftSave() {
  window.clearTimeout(draftSaveTimer);
  setDraftStatus("Saving…");
  draftSaveTimer = window.setTimeout(() => saveLocalDraft({ announce: false }), 350);
}

function composerDraft() {
  return {
    caption: $("#publish-caption").value,
    title: $("#youtube-publish-title").value,
    description: $("#youtube-publish-description").value,
    visibility: $("#youtube-publish-privacy").value,
    madeForKids: $("#youtube-made-for-kids").checked,
    selectedContentId,
    selectedAccountIds: [...selectedAccountIds],
    // The intent record travels with the draft so a reload can resume the same
    // logical submission and reuse its per-destination keys.
    submission: activeSubmission ? cloneSubmission(activeSubmission) : null,
    savedAt: Date.now(),
  };
}

function saveLocalDraft({ announce = true } = {}) {
  window.clearTimeout(draftSaveTimer);
  const key = localDraftKey();
  if (!key) return false;
  const draft = composerDraft();
  try {
    // A submission whose destinations are all confirmed is finished work, not a
    // draft. The same is true once every on-screen result is published: leaving
    // the page must not re-save a published post and make Home offer to
    // "continue" it. Starting the next post resets the results, and saving
    // resumes normally.
    const results = typeof currentResults === "function" ? currentResults() : [];
    const allPublished = results.length > 0 && results.every((result) => result.status === "published");
    const finished = allPublished || (draft.submission && submissionComplete(draft.submission));
    if (!draftHasContent(draft) || finished) {
      // No key for an empty or finished draft. Remove any older one for this owner.
      localStorage.removeItem(key);
      draftSaveFailed = false;
      setDraftStatus("");
      renderHomeDraft();
      renderPostHistory();
      return true;
    }
    localStorage.setItem(key, JSON.stringify(draft));
    draftSaveFailed = false;
    setDraftStatus(`Draft saved in this browser · ${new Date(draft.savedAt).toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" })}`);
    renderHomeDraft();
    if (historyLoaded) renderPostHistory();
    if (announce) notice("Draft saved in this browser.");
    return true;
  } catch {
    draftSaveFailed = true;
    setDraftStatus("Draft not saved — browser storage is unavailable");
    if (announce) notice("Your draft couldn't be saved in this browser. Check browser storage and try again.", true);
    return false;
  }
}

/* Clear only the draft that was actually published, never a newer one.
   Publishing rewrites `savedAt` and `submission` as destinations resolve, so
   comparing whole snapshots would never match. The content fields and the
   submission id are what identify "the draft that was sent". */
function sameDraftContent(left, right) {
  if (!left || !right) return false;
  return left.caption === right.caption &&
    left.title === right.title &&
    left.description === right.description &&
    left.visibility === right.visibility &&
    left.madeForKids === right.madeForKids &&
    left.selectedContentId === right.selectedContentId &&
    JSON.stringify(left.selectedAccountIds || []) === JSON.stringify(right.selectedAccountIds || []) &&
    (left.submission?.id || null) === (right.submission?.id || null);
}

function clearPublishedDraft(expectedDraft) {
  if (!expectedDraft) return;
  const currentDraft = readLocalDraft();
  if (!currentDraft || !sameDraftContent(currentDraft, expectedDraft)) return;
  const key = localDraftKey();
  if (!key) return;
  try {
    localStorage.removeItem(key);
  } catch {
    return;
  }
  setDraftStatus("");
  renderHomeDraft();
  if (historyLoaded) renderPostHistory();
  notice("Every destination published. The saved draft was cleared.", false, {
    label: "Undo",
    run() {
      if (readLocalDraft()) {
        notice("A newer draft is already saved, so it was left unchanged.");
        return;
      }
      try {
        localStorage.setItem(key, JSON.stringify(expectedDraft));
        restoreLocalDraft(expectedDraft);
        renderHomeDraft();
        if (historyLoaded) renderPostHistory();
        notice("Draft restored.");
      } catch {
        notice("The draft couldn't be restored in this browser.", true);
      }
    },
  });
}

function restoreLocalDraft(draft = readLocalDraft()) {
  if (!draft) return false;
  $("#publish-caption").value = draft.caption || "";
  $("#youtube-publish-title").value = draft.title || "";
  $("#youtube-publish-description").value = draft.description || "";
  $("#youtube-publish-privacy").value = draft.visibility || "private";
  $("#youtube-made-for-kids").checked = draft.madeForKids === true;
  selectedContentId = draft.selectedContentId || null;
  selectedAccountIds = new Set(draft.selectedAccountIds || []);
  activeSubmission = draft.submission ? cloneSubmission(draft.submission) : null;
  // A restored submission is real work: show its destinations instead of an
  // empty result list, so a send that was in flight is never invisible.
  hydrateResultsFromSubmission();
  renderComposer();
  return true;
}

/* ── Legacy draft recovery ─────────────────────────────────────
   The old key `relay:draft:<workspace>` is not intrinsically cross-user when
   the workspace id is globally unique and ownership is proven by the
   authenticated bootstrap. So it is offered — for this exact owned workspace
   only — instead of being silently ignored forever. It is never scanned across
   workspaces, never read before authentication, and never erased until the
   person chooses. */

const LEGACY_DISMISS_KEY = "relay:draft-legacy-dismissed";

function legacyDismissedFor(workspaceId) {
  try {
    return localStorage.getItem(`${LEGACY_DISMISS_KEY}:${workspaceId}`) === "1";
  } catch { return false; }
}

function dismissLegacyDraft(workspaceId) {
  try { localStorage.setItem(`${LEGACY_DISMISS_KEY}:${workspaceId}`, "1"); } catch { /* ignore */ }
  $("#draft-import").hidden = true;
}

function legacyDraftOffer() {
  if (!authenticated || !activeUserId || !activeWorkspaceId) return null;
  const legacyKey = legacyKeyForWorkspace();
  const ownedKey = localDraftKey();
  const legacyDraft = readJson(legacyKey);
  const ownedDraft = readJson(ownedKey);
  const decision = legacyDraftImport({
    legacyKey,
    ownedKey,
    legacyDraft,
    ownedDraft,
    dismissed: legacyDismissedFor(activeWorkspaceId),
  });
  return decision.offer ? { legacyKey, legacyDraft, decision } : null;
}

function renderLegacyDraftOffer() {
  const banner = $("#draft-import");
  if (!banner) return;
  const offer = legacyDraftOffer();
  if (!offer) {
    banner.hidden = true;
    return;
  }
  const content = draftContent(offer.legacyDraft);
  const parts = [];
  const text = content.title || content.caption;
  if (text) parts.push(text.length > 90 ? `${text.slice(0, 90).trimEnd()}…` : text);
  const media = contentItems.find((item) => item.id === content.media);
  if (media) parts.push(media.filename);
  const when = offer.legacyDraft.savedAt ? new Date(offer.legacyDraft.savedAt) : null;
  if (when && !Number.isNaN(when.getTime())) {
    parts.push(`saved ${when.toLocaleDateString(undefined, { month: "short", day: "numeric" })}`);
  }
  setText("#draft-import-copy", `${parts.join(" · ")}. It was saved before Relay started keeping drafts per account, so it can't be proven to be yours. Recover it only if you recognise it.`);
  banner.hidden = false;
}

function recoverLegacyDraft() {
  const offer = legacyDraftOffer();
  if (!offer) return;
  try {
    // Write the owned draft first. The legacy key is removed only after that
    // write succeeds, so a storage failure cannot destroy the only copy.
    const draft = {
      caption: String(offer.legacyDraft.caption || "").slice(0, 2200),
      title: String(offer.legacyDraft.title || "").slice(0, 100),
      description: String(offer.legacyDraft.description || "").slice(0, 5000),
      visibility: ["private", "unlisted", "public"].includes(offer.legacyDraft.visibility)
        ? offer.legacyDraft.visibility : "private",
      madeForKids: offer.legacyDraft.madeForKids === true,
      selectedContentId: offer.legacyDraft.selectedContentId || null,
      selectedAccountIds: Array.isArray(offer.legacyDraft.selectedAccountIds)
        ? offer.legacyDraft.selectedAccountIds.slice(0, 20) : [],
      submission: null,
      savedAt: Number(offer.legacyDraft.savedAt) || Date.now(),
    };
    localStorage.setItem(localDraftKey(), JSON.stringify(draft));
    localStorage.removeItem(offer.legacyKey);
  } catch {
    notice("The older draft couldn't be recovered because browser storage is unavailable. It has not been deleted.", true);
    return;
  }
  $("#draft-import").hidden = true;
  restoreLocalDraft();
  notice("Older draft recovered into this workspace.");
}

/* ── /try preview caption ──────────────────────────────────────
   The preview page writes `relay:preview-draft` with no ownership claim. It is
   adopted silently only when the person has no draft of their own. Otherwise
   they are shown both captions and must choose, rather than having either one
   silently discarded. */

const PREVIEW_DRAFT_KEY = "relay:preview-draft";

function readPreviewDraft() {
  const preview = readJson(PREVIEW_DRAFT_KEY);
  if (!preview || !String(preview.caption || "").trim()) return null;
  return preview;
}

function discardPreviewDraft() {
  try { localStorage.removeItem(PREVIEW_DRAFT_KEY); } catch { /* ignore */ }
  $("#draft-conflict").hidden = true;
}

function applyPreviewCaption(preview) {
  const caption = String(preview.caption || "").slice(0, 2200);
  $("#publish-caption").value = caption;
  activeSubmission = null;
  discardPreviewDraft();
  renderComposer();
  saveLocalDraft({ announce: false });
  notice("Preview caption added to this draft.");
}

function renderPreviewConflict() {
  const banner = $("#draft-conflict");
  if (!banner) return;
  if (!authenticated) { banner.hidden = true; return; }
  const preview = readPreviewDraft();
  const owned = readLocalDraft();
  const decision = previewDraftDecision({ previewDraft: preview, ownedDraft: owned });
  if (decision.action === "discard") {
    if (decision.reason === "same-caption") discardPreviewDraft();
    banner.hidden = true;
    return;
  }
  if (decision.action === "adopt") {
    // No draft of their own: carry the caption in, as the preview page promised.
    if (preview) {
      const draft = {
        caption: String(preview.caption).slice(0, 2200),
        title: "", description: "", visibility: "private", madeForKids: false,
        selectedContentId: null, selectedAccountIds: [], submission: null, savedAt: Date.now(),
      };
      try {
        localStorage.setItem(localDraftKey(), JSON.stringify(draft));
        localStorage.removeItem(PREVIEW_DRAFT_KEY);
      } catch { /* A storage restriction must not stop the editor. */ }
      restoreLocalDraft(draft);
      banner.hidden = true;
      return;
    }
    banner.hidden = true;
    return;
  }
  // Conflict: show both, let the person decide.
  const previewText = String(preview.caption || "").trim();
  const ownedText = String(owned.caption || owned.title || "").trim();
  const shorten = (text) => (text.length > 80 ? `${text.slice(0, 80).trimEnd()}…` : text);
  setText("#draft-conflict-copy",
    `Preview: “${shorten(previewText)}”. Current draft: “${shorten(ownedText)}”. Choose which caption to keep. Nothing is overwritten until you choose.`);
  banner.hidden = false;
}

/* ── Draft banners ─────────────────────────────────────────── */

function renderDraftBanners() {
  renderLegacyDraftOffer();
  renderPreviewConflict();
}

function showUnlock() {
  window.location.assign("/login");
}

/* ── Library ───────────────────────────────────────────────── */

function renderContent() {
  const list = $("#content-list");
  const empty = $("#content-empty");
  const description = $("#content-empty-description");
  list.replaceChildren();
  setText("#content-count", authenticated ? String(contentItems.length) : "—");
  setText("#content-nav-count", authenticated ? String(contentItems.length) : "—");

  if (!authenticated) {
    $("#content-empty-title").textContent = "Sign in to view your library";
    description.hidden = true;
    $("#content-empty-action").hidden = false;
    $("#content-empty-action").textContent = "Sign in";
    empty.hidden = false;
    list.hidden = true;
    $("#content-storage-hint").textContent = "";
    $("#content-storage-hint").hidden = true;
    return;
  }
  if (!contentStorageConfigured) {
    $("#content-empty-title").textContent = "Media storage isn't available";
    description.textContent = "Uploads are turned off for this Relay deployment. You can still publish media that is already in the library.";
    description.hidden = false;
    $("#content-empty-action").hidden = true;
    empty.hidden = false;
    list.hidden = true;
    $("#content-storage-hint").textContent = "";
    $("#content-storage-hint").hidden = true;
    return;
  }
  $("#content-empty-action").hidden = false;
  $("#content-storage-hint").textContent = localMediaStorage
    ? `Local preview only · files stay on this PC · ${formatBytes(maxUploadBytes)} max.`
    : maxUploadBytes > 0
      ? `JPEG photos and videos up to ${formatBytes(maxUploadBytes)} each.`
      : "JPEG photos and videos.";
  $("#content-storage-hint").hidden = false;
  if (!contentItems.length) {
    $("#content-empty-title").textContent = "No media yet";
    description.textContent = localMediaStorage
      ? "Upload a JPEG or video from this PC to preview it locally. Publishing is disabled in this setup."
      : "Upload a JPEG photo or a video, then send it to any connected channel.";
    description.hidden = false;
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
  const card = el("article", "media-card");
  const preview = el("div", "media-preview");
  if (item.content_type.startsWith("image/")) {
    const image = document.createElement("img");
    image.src = item.media_url;
    image.alt = item.filename;
    image.loading = "lazy";
    preview.append(image);
  } else {
    const video = document.createElement("video");
    // A metadata-only, control-free preview: the card is a chooser, not a player.
    video.src = `${item.media_url}#t=0.1`;
    video.muted = true;
    video.preload = "metadata";
    video.playsInline = true;
    preview.append(video);
    const duration = el("span", "duration-badge", "Video");
    video.addEventListener("loadedmetadata", () => {
      if (Number.isFinite(video.duration)) {
        const seconds = Math.floor(video.duration);
        duration.textContent = `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
      }
    }, { once: true });
    preview.append(duration);
  }
  const details = el("div", "media-details");
  details.append(el("strong", null, item.filename), el("small", null, formatBytes(item.size)));
  const use = el("button", "button button-secondary button-sm", "Use in post");
  use.type = "button";
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
