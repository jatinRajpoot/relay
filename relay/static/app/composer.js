"use strict";

function profileInitials(name) {
  return String(name || "?").trim().split(/\s+/).slice(0, 2).map((part) => part[0]).join("").toUpperCase() || "?";
}

function makeProfileAvatar(account, provider, size = "") {
  const avatar = document.createElement("span");
  avatar.className = `profile-avatar ${provider} ${size}`.trim();
  // Decorative: the account name is always adjacent in text.
  avatar.setAttribute("aria-hidden", "true");
  const initials = document.createElement("span");
  initials.className = "profile-avatar-initials";
  initials.textContent = profileInitials(account.name);
  avatar.append(initials);
  if (account.avatar_url) {
    const image = document.createElement("img");
    image.src = account.avatar_url;
    image.alt = "";
    image.loading = "lazy";
    image.referrerPolicy = "no-referrer";
    image.addEventListener("error", () => image.remove(), { once: true });
    avatar.append(image);
  }
  const badge = document.createElement("span");
  badge.className = "profile-platform-badge";
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
  use.setAttribute("href", `#i-${provider}`);
  svg.append(use);
  badge.append(svg);
  avatar.append(badge);
  return avatar;
}

function canUseMedia(item, provider) {
  return provider === "instagram" ? item.instagram_compatible : item.youtube_compatible;
}

function mediaKind(item) {
  return item.content_type.startsWith("image/") ? "Photo" : "Video";
}

function selectedAccounts() {
  return accountList.filter((account) => selectedAccountIds.has(account.id));
}

function selectedMedia() {
  return contentItems.find((item) => item.id === selectedContentId) || null;
}

/* ── Media ─────────────────────────────────────────────────── */

function renderComposerMedia() {
  const empty = $("#composer-media-empty");
  const attachment = $("#composer-selected-attachment");
  const item = selectedMedia();
  attachment.replaceChildren();

  if (!authenticated) {
    $("#composer-media-empty-title").textContent = "Sign in to choose media";
    $("#composer-media-empty-copy").textContent = "Your library is private to your workspace.";
    $("#composer-media-action").hidden = true;
    $("#composer-media-browse").hidden = true;
    empty.hidden = false;
    attachment.hidden = true;
    return;
  }

  $("#composer-media-action").hidden = false;
  $("#composer-media-browse").hidden = false;

  if (!contentStorageConfigured) {
    $("#composer-media-empty-title").textContent = "Media storage isn't available";
    $("#composer-media-empty-copy").textContent = "This Relay deployment can't accept uploads yet. Ask the person who runs Relay to finish storage setup.";
    $("#composer-media-action").hidden = true;
    $("#composer-media-browse").hidden = true;
    empty.hidden = false;
    attachment.hidden = true;
    return;
  }

  if (!item) {
    $("#composer-media-empty-title").textContent = "Choose a photo or video";
    $("#composer-media-empty-copy").textContent = localMediaStorage
      ? "Upload a JPEG or video from this PC. It stays in the local library for preview."
      : contentItems.length
        ? `Pick one of your ${contentItems.length} saved file${contentItems.length === 1 ? "" : "s"}, or upload something new.`
        : "Upload a JPEG photo or a video to start. It stays in your Library for reuse.";
    $("#composer-media-action").textContent = contentItems.length ? "Upload a file" : "Upload a file";
    $("#composer-media-browse").hidden = !contentItems.length;
    empty.hidden = false;
    attachment.hidden = true;
    return;
  }

  const preview = el("span", "attachment-thumbnail");
  const isImage = item.content_type.startsWith("image/");
  const node = document.createElement(isImage ? "img" : "video");
  // A short media fragment makes the browser paint the first frame instead of a black box.
  node.src = isImage ? item.media_url : `${item.media_url}#t=0.1`;
  node.alt = "";
  node.addEventListener("error", () => {
    const fallback = el("span", "media-preview-fallback");
    fallback.append(icon("media"), el("small", null, "Preview unavailable"));
    preview.replaceChildren(fallback);
  }, { once: true });
  if (node instanceof HTMLVideoElement) {
    node.muted = true;
    node.playsInline = true;
    node.preload = "metadata";
  }
  preview.append(node);

  const meta = el("span", "attachment-meta");
  meta.append(el("strong", null, item.filename), el("small", null, `${mediaKind(item)} · ${formatBytes(item.size)}`));

  const remove = el("button", "attachment-remove", "×");
  remove.type = "button";
  remove.disabled = publishing;
  remove.setAttribute("aria-label", "Remove attached media");
  remove.addEventListener("click", () => {
    const previousContentId = selectedContentId;
    selectedContentId = null;
    renderComposer();
    scheduleDraftSave();
    notice("Media removed from this post.", false, {
      label: "Undo",
      run() {
        selectedContentId = previousContentId;
        renderComposer();
        scheduleDraftSave();
      },
    });
  });

  attachment.append(preview, meta, remove);
  attachment.hidden = false;
  empty.hidden = true;
}

function renderMediaPicker() {
  const grid = $("#composer-media-grid");
  const empty = $("#composer-media-picker-empty");
  grid.replaceChildren();
  if (!authenticated) {
    empty.hidden = false;
    grid.hidden = true;
    $("#composer-media-picker-empty-title").textContent = "Sign in to see your media";
    return;
  }
  if (!contentItems.length) {
    empty.hidden = false;
    grid.hidden = true;
    $("#composer-media-picker-empty-title").textContent = "Your media library is empty";
    return;
  }
  empty.hidden = true;
  grid.hidden = false;
  for (const item of contentItems) {
    const option = document.createElement("button");
    option.type = "button";
    option.className = "composer-media-option";
    option.setAttribute("aria-pressed", String(item.id === selectedContentId));
    option.disabled = publishing;
    if (item.id === selectedContentId) option.classList.add("selected");
    const preview = el("span", "composer-media-preview");
    if (item.content_type.startsWith("image/")) {
      const image = document.createElement("img");
      image.src = item.media_url;
      image.alt = "";
      image.loading = "lazy";
      preview.append(image);
    } else {
      const video = document.createElement("video");
      video.src = `${item.media_url}#t=0.1`;
      video.muted = true;
      video.playsInline = true;
      video.preload = "metadata";
      preview.append(video);
      preview.append(el("span", "media-play-marker", "Video"));
    }
    const details = el("span", "composer-media-copy");
    details.append(el("strong", null, item.filename), el("small", null, `${mediaKind(item)} · ${formatBytes(item.size)}`));
    option.append(preview, details);
    option.addEventListener("click", () => selectComposerMedia(item));
    grid.append(option);
  }
}

function selectComposerMedia(item) {
  if (publishing) return;
  const picker = $("#media-picker-dialog");
  if (picker.open) picker.close();
  const previousContentId = selectedContentId;
  const previousAccountIds = new Set(selectedAccountIds);
  if (selectedContentId === item.id) {
    selectedContentId = null;
  } else {
    const blocked = accountList.filter((account) => selectedAccountIds.has(account.id) && !canUseMedia(item, account.provider));
    blocked.forEach((account) => selectedAccountIds.delete(account.id));
    selectedContentId = item.id;
    const compatible = accountList.filter((account) => canUseMedia(item, account.provider));
    if (!selectedAccountIds.size && compatible.length === 1) selectedAccountIds.add(compatible[0].id);
    if (blocked.length) {
      const providers = [...new Set(blocked.map((account) => names[account.provider]))].join(" and ");
      notice(`This ${mediaKind(item).toLowerCase()} can’t go to ${providers}.`, false, {
        label: "Undo",
        run() {
          selectedContentId = previousContentId;
          selectedAccountIds = new Set(previousAccountIds);
          renderComposer();
          scheduleDraftSave();
        },
      });
    }
  }
  renderComposer();
  scheduleDraftSave();
}

/* ── Destinations ──────────────────────────────────────────── */

/* Re-rendering the destination list replaces its checkboxes. After a toggle or
   an Undo, return focus to the same destination so keyboard use is continuous. */
function focusDestination(accountId) {
  const pill = [...document.querySelectorAll("#composer-destinations .profile-pill")]
    .find((candidate) => candidate.dataset.accountId === accountId);
  const input = pill && pill.querySelector("input");
  if (input && !input.disabled) input.focus({ preventScroll: true });
}

function renderComposerDestinations() {
  const container = $("#composer-destinations");
  container.replaceChildren();
  const media = selectedMedia();
  for (const account of accountList) {
    const selected = selectedAccountIds.has(account.id);
    const compatible = !media || canUseMedia(media, account.provider);
    const configured = account.provider === "instagram"
      ? instagramMediaConfigured || localMediaStorage
      : contentStorageConfigured;
    // One shared predicate (flow-state.js channelReadiness) decides whether this
    // channel can send: an unreadable credential, an expired Instagram token, or
    // an expired YouTube token with no refresh token. An expired YouTube token
    // that Relay can refresh is *not* a reconnect signal.
    const readiness = channelReadiness(account);
    const reconnect = readiness.reconnect;
    const choice = document.createElement("label");
    choice.className = "profile-pill" + (selected ? " selected" : "") + (!compatible ? " incompatible" : "") + (reconnect ? " needs-reconnect" : "");
    choice.dataset.accountId = account.id;
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.checked = selected;
    checkbox.disabled = publishing || !authenticated || !configured || !compatible || reconnect;
    // The visible name is the label; the state is carried by aria-checked
    // semantics of the checkbox itself, not by a redundant aria-label.
    checkbox.addEventListener("change", () => {
      const previous = new Set(selectedAccountIds);
      if (checkbox.checked) selectedAccountIds.add(account.id);
      else selectedAccountIds.delete(account.id);
      renderComposer();
      // Re-rendering replaces the checkbox node. Put focus back on the same
      // destination so a keyboard user does not land on <body>.
      focusDestination(account.id);
      scheduleDraftSave();
      notice(`${account.name} ${checkbox.checked ? "added to" : "removed from"} this post.`, false, {
        label: "Undo",
        run() { selectedAccountIds = previous; renderComposer(); focusDestination(account.id); scheduleDraftSave(); },
      });
    });
    const avatar = makeProfileAvatar(account, account.provider);
    const name = el("span", "profile-pill-name", account.name);
    const platform = el("span", "profile-pill-platform", names[account.provider]);
    const copy = el("span", "profile-pill-copy");
    copy.append(name, platform);
    const notes = el("span", "profile-pill-notes");
    if (reconnect) notes.append(el("span", "profile-pill-note", "reconnect"));
    else if (!configured) notes.append(el("span", "profile-pill-note", localMediaStorage ? "storage needed" : "reconnect"));
    else if (!compatible) notes.append(el("span", "profile-pill-note", media && media.content_type.startsWith("image/") ? "needs video" : "incompatible"));
    choice.append(checkbox, avatar, copy, notes);
    container.append(choice);
  }

  const empty = $("#composer-destinations-empty");
  empty.hidden = Boolean(accountList.length);
  $("#journey-add-channel").textContent = authenticated ? "Connect a channel" : "Sign in";

  const count = selectedAccounts().length;
  setText("#selected-account-count", count
    ? `${count} selected`
    : accountList.length ? "None selected" : "No channels yet");
}

/* ── Platform sections ─────────────────────────────────────── */

function renderComposerDetails() {
  const sections = composerSections(selectedAccounts());
  $("#instagram-details").hidden = !sections.instagram;
  $("#youtube-details").hidden = !sections.youtube;
  $("#publish-caption").disabled = publishing;
  $("#youtube-publish-title").disabled = publishing;
  $("#youtube-publish-description").disabled = publishing;
  $("#youtube-publish-privacy").disabled = publishing;
  $("#youtube-made-for-kids").disabled = publishing;
  setText("#caption-count", String($("#publish-caption").value.length));
  const titleMissing = sections.youtube && !$("#youtube-publish-title").value.trim();
  $("#youtube-publish-title").setAttribute("aria-invalid", String(titleMissing));
  $("#youtube-title-hint").textContent = titleMissing ? "Required · add a title before publishing" : "Required · 1–100 characters";
  $("#youtube-title-hint").classList.toggle("is-error", titleMissing);
  // The Instagram caption section only appears for an Instagram destination, so
  // a YouTube-only post never implies it uses an Instagram caption.
  const bothPlatforms = sections.instagram && sections.youtube;
  const switcher = $(".preview-switcher");
  if (switcher) switcher.hidden = !bothPlatforms;
  document.querySelectorAll("[data-preview-platform]").forEach((tab) => {
    tab.hidden = !bothPlatforms;
  });
}

/* ── Preview ───────────────────────────────────────────────── */

function renderComposerPreview() {
  const item = selectedMedia();
  const media = $("#composer-preview-media");
  media.replaceChildren();
  if (item) {
    media.classList.add("has-media");
    const node = document.createElement(item.content_type.startsWith("image/") ? "img" : "video");
    node.alt = "Preview of the selected media";
    const updateMediaRatio = () => {
      const width = node instanceof HTMLVideoElement ? node.videoWidth : node.naturalWidth;
      const height = node instanceof HTMLVideoElement ? node.videoHeight : node.naturalHeight;
      if (width && height) media.style.aspectRatio = `${width} / ${height}`;
    };
    node.addEventListener(node instanceof HTMLVideoElement ? "loadedmetadata" : "load", updateMediaRatio, { once: true });
    node.addEventListener("error", () => {
      media.style.removeProperty("aspect-ratio");
      const fallback = el("span", "media-preview-fallback");
      fallback.append(icon("media"), el("small", null, "Preview unavailable"));
      media.replaceChildren(fallback);
    }, { once: true });
    if (node instanceof HTMLVideoElement) {
      node.muted = true;
      node.controls = true;
      node.playsInline = true;
      node.preload = "metadata";
    }
    media.append(node);
    node.src = item.media_url;
    setText("#preview-file-name", "Media attached");
    setText("#preview-file-meta", `${mediaKind(item)} · ${formatBytes(item.size)}`);
  } else {
    media.classList.remove("has-media");
    media.style.removeProperty("aspect-ratio");
    const placeholder = el("span");
    placeholder.append(icon("media"), el("small", null, "Your media will appear here"));
    media.append(placeholder);
    setText("#preview-file-name", "No media selected");
    setText("#preview-file-meta", "Choose media");
  }

  const accounts = selectedAccounts();
  const availableProviders = [...new Set(accounts.map((account) => account.provider))];
  if (availableProviders.length && !availableProviders.includes(activePreviewPlatform)) {
    activePreviewPlatform = availableProviders[0];
  }
  document.querySelectorAll("[data-preview-platform]").forEach((tab) => {
    const selected = tab.dataset.previewPlatform === activePreviewPlatform;
    tab.setAttribute("aria-selected", String(selected));
    tab.tabIndex = selected ? 0 : -1;
  });
  // The panel is labelled by whichever tab is selected, so the association
  // follows the platform instead of pointing at a fixed tab.
  const panel = $("#preview-panel");
  const selectedTab = document.querySelector(`[data-preview-platform="${activePreviewPlatform}"]`);
  if (panel && selectedTab && selectedTab.id) panel.setAttribute("aria-labelledby", selectedTab.id);

  const account = accounts.find((candidate) => candidate.provider === activePreviewPlatform) || accounts[0];
  setText("#composer-preview-heading", `Preview · ${names[activePreviewPlatform] || "channel"}`);
  const accountName = $("#composer-preview-account");
  const avatarSlot = $("#composer-preview-account-avatar");
  if (account) {
    accountName.textContent = account.name;
    const avatar = makeProfileAvatar(account, account.provider, "preview-account-avatar");
    avatar.id = "composer-preview-account-avatar";
    avatarSlot.replaceWith(avatar);
  } else {
    accountName.textContent = activePreviewPlatform === "youtube" ? "YouTube channel" : "Instagram channel";
    avatarSlot.replaceChildren(document.createTextNode("R"));
    avatarSlot.className = "profile-avatar";
  }

  const targets = $("#composer-preview-destinations");
  targets.replaceChildren();
  setText("#preview-target-count", accounts.length ? `${accounts.length} selected` : "No channels selected");

  const title = $("#composer-preview-title");
  const copy = $("#composer-preview-copy");
  const youtubeTitle = $("#youtube-publish-title").value.trim();
  const youtubeDescription = $("#youtube-publish-description").value.trim();
  const caption = $("#publish-caption").value.trim();
  if (activePreviewPlatform === "youtube") {
    title.textContent = youtubeTitle || "Your video title";
    title.hidden = false;
    copy.textContent = youtubeDescription || caption || "Your description will appear here.";
  } else {
    title.textContent = "";
    title.hidden = true;
    copy.textContent = caption || "Your caption will appear here.";
  }
}

/* ── Action bar ────────────────────────────────────────────── */

function renderComposerActions() {
  const media = selectedMedia();
  const accounts = selectedAccounts();
  const results = currentResults();
  const hasResults = results.length > 0;
  const blockers = composerBlockers({
    media,
    accounts,
    instagramCaption: $("#publish-caption").value,
    youtubeTitle: $("#youtube-publish-title").value,
    publishing,
    hasResults,
  });
  // Destinations that are already resolved or uncertain are never re-sent, so
  // they are not counted in the button. Saying "2 destinations" while sending
  // one would be a false promise.
  const held = activeSubmission
    ? accounts.filter((account) => {
      const entry = activeSubmission.destinations[account.id];
      return entry && ["sent", "unknown", "processing", "ready", "published", "failed", "expired"].includes(entry.state);
    }).length
    : 0;
  const sendableCount = Math.max(0, accounts.length - held);
  const summary = composerSummary({ media, accounts, publishing, hasResults, results, blockers, held });
  setText("#composer-action-summary", summary.headline);
  // The unmet requirement stays visible next to the disabled control and is
  // referenced by aria-describedby. A disabled button is not a WCAG text
  // failure, but an unreadable one is still a usability defect, so the colour
  // is set in CSS to remain legible rather than faded.
  const blocker = $("#composer-blocker");
  const blockerText = localMediaStorage && !hasResults
    ? "Local preview only. Publishing is disabled in this setup."
    : !publishing && !hasResults && blockers.length ? blockers[0].message : "";
  blocker.hidden = !blockerText;
  blocker.textContent = blockerText;

  // Never print the same sentence twice, and never promise a send that is
  // blocked. The blocker sits next to the disabled control; the hint carries
  // the step after it, or the next-step detail once nothing blocks the send.
  const hintText = localMediaStorage && !hasResults
    ? "Uploaded files stay on this PC and can be previewed here."
    : hasResults || publishing
    ? summary.detail
    : blockers.length === 0
      ? summary.detail
      : blockers.length > 1
        ? `Then ${blockers[1].message.charAt(0).toLowerCase()}${blockers[1].message.slice(1)}`
        : "";
  setText("#composer-action-hint", hintText);

  const button = $("#publish-submit");
  button.disabled = localMediaStorage || publishing || hasResults || blockers.length > 0 || sendableCount <= 0;
  button.querySelector("span").textContent = localMediaStorage
    ? "Preview only"
    : publishing
      ? "Sending…"
      : hasResults ? "Delivery status" : `Send to ${sendableCount} destination${sendableCount === 1 ? "" : "s"}`;
  button.hidden = hasResults;

  $("#save-draft").hidden = hasResults;
  $("#session-button").disabled = publishing;
  $("#workspace-select").disabled = publishing;
  $("#new-workspace").disabled = publishing;
  $("#content-file").disabled = !authenticated || !contentStorageConfigured || publishing;
  $("#content-upload-submit").disabled = !authenticated || !contentStorageConfigured || publishing;
  $("#publish-file").disabled = !authenticated || !contentStorageConfigured || publishing;
  $("#open-media-picker").disabled = !authenticated || publishing;
  $("#composer-media-action").disabled = !authenticated || !contentStorageConfigured || publishing;
}

function renderComposer() {
  // A channel Relay cannot send to is not a valid destination: it is shown as
  // reconnectable and never counted in "Send to N". The predicate is shared with
  // Home and Channels so the three surfaces cannot disagree.
  selectedAccountIds = new Set([...selectedAccountIds].filter((id) => {
    const account = accountList.find((candidate) => candidate.id === id);
    return account && channelReadiness(account).ready;
  }));
  if (selectedContentId && !contentItems.some((item) => item.id === selectedContentId)) selectedContentId = null;
  renderComposerMedia();
  renderComposerDestinations();
  renderComposerDetails();
  renderComposerPreview();
  renderComposerActions();
  renderDeliverySection();
  renderRecoveredSection();
}
