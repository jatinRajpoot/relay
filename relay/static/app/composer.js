"use strict";

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
  const emptyAction = $("#composer-media-action");
  const item = contentItems.find((content) => content.id === selectedContentId);
  if (!authenticated) {
    emptyTitle.textContent = "Unlock your workspace to see your media";
    emptyAction.textContent = "Unlock workspace";
    empty.hidden = false;
    attachment.hidden = true;
    grid.hidden = true;
    return;
  }
  if (!contentStorageConfigured) {
    emptyTitle.textContent = "Media storage needs setup";
    emptyAction.hidden = false;
    emptyAction.textContent = "View media setup";
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
    const remove = document.createElement("button");
    remove.className = "attachment-remove";
    remove.type = "button";
    remove.setAttribute("aria-label", "Remove attached media");
    remove.textContent = "×";
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
    attachment.append(preview, remove);
    attachment.hidden = false;
    empty.hidden = true;
    grid.hidden = false;
  } else {
    attachment.hidden = true;
    if (!contentItems.length) {
      emptyTitle.textContent = "Your library is ready for a first upload";
      empty.hidden = false;
      grid.hidden = true;
      return;
    }
    empty.hidden = !contentItems.length;
    grid.hidden = !contentItems.length;
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
    details.append(filename, metadata);
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
      scheduleDraftSave();
    });
    const name = document.createElement("span");
    name.className = "profile-pill-name";
    name.textContent = account.name;
    choice.append(checkbox, makeProfileAvatar(account, account.provider), name);
    container.append(choice);
  }
  if (!accountList.length) {
    const empty = document.createElement("span");
    empty.className = "profile-pills-empty";
    empty.textContent = authenticated ? "No channels connected" : "Unlock your workspace";
    const action = document.createElement("button");
    action.type = "button";
    action.className = "button button-quiet button-sm";
    action.textContent = authenticated ? "Connect a channel" : "Unlock workspace";
    action.addEventListener("click", () => authenticated ? setPage("connections") : showUnlock());
    container.append(empty, action);
  }
  const selected = accountList.filter((account) => selectedAccountIds.has(account.id));
  $("#selected-account-count").textContent = selected.length + " selected";
  $("#composer-action-summary").textContent = `Posting to ${selected.length} ${selected.length === 1 ? "channel" : "channels"}`;
}

function renderComposerDetails() {
  const selected = accountList.filter((account) => selectedAccountIds.has(account.id));
  const hasInstagram = selected.some((account) => account.provider === "instagram");
  const hasYoutube = selected.some((account) => account.provider === "youtube");
  $("#instagram-tab").hidden = !hasInstagram;
  $("#youtube-tab").hidden = !hasYoutube;
  if ((activeDetailsTab === "instagram" && !hasInstagram) || (activeDetailsTab === "youtube" && !hasYoutube)) activeDetailsTab = "master";
  $("#instagram-details").hidden = activeDetailsTab === "youtube";
  $("#instagram-details").setAttribute("aria-labelledby", activeDetailsTab === "instagram" ? "instagram-tab" : "master-tab");
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
  const isYoutube = activeDetailsTab === "youtube";
  $("#active-character-count").textContent = String(isYoutube ? $("#youtube-publish-title").value.length : $("#publish-caption").value.length);
  $("#active-character-limit").textContent = isYoutube ? "/ 100" : "/ 2,200";
}

function renderComposerPreview() {
  const item = contentItems.find((content) => content.id === selectedContentId);
  const media = $("#composer-preview-media");
  media.replaceChildren();
  if (item) {
    media.classList.add("has-media");
    const node = document.createElement(item.content_type.startsWith("image/") ? "img" : "video");
    node.src = item.media_url;
    node.alt = "Post media preview";
    if (node instanceof HTMLVideoElement) {
      node.muted = true;
      node.controls = true;
      node.playsInline = true;
      node.preload = "metadata";
    }
    media.append(node);
    $("#preview-file-name").textContent = "Media attached";
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
  const availableProviders = [...new Set(selected.map((account) => account.provider))];
  document.querySelectorAll("[data-preview-platform]").forEach((tab) => {
    tab.hidden = availableProviders.length > 0 && !availableProviders.includes(tab.dataset.previewPlatform);
    const active = tab.dataset.previewPlatform === activePreviewPlatform;
    tab.classList.toggle("active", active);
    tab.setAttribute("aria-selected", String(active));
  });
  if (availableProviders.length && !availableProviders.includes(activePreviewPlatform)) activePreviewPlatform = availableProviders[0];
  document.querySelectorAll("[data-preview-platform]").forEach((tab) => {
    const active = tab.dataset.previewPlatform === activePreviewPlatform;
    tab.classList.toggle("active", active);
    tab.setAttribute("aria-selected", String(active));
  });
  const account = selected.find((candidate) => candidate.provider === activePreviewPlatform) || selected[0];
  const accountName = $("#composer-preview-account");
  const avatarSlot = $("#composer-preview-account-avatar");
  if (account) {
    accountName.textContent = account.name;
    const avatar = makeProfileAvatar(account, account.provider, "preview-account-avatar");
    avatar.id = "composer-preview-account-avatar";
    avatarSlot.replaceWith(avatar);
  } else {
    accountName.textContent = "Select a channel";
    avatarSlot.replaceChildren(document.createTextNode("R"));
    avatarSlot.className = "profile-avatar";
  }
  const title = $("#composer-preview-title");
  const copy = $("#composer-preview-copy");
  if (activePreviewPlatform === "youtube" && $("#youtube-publish-title").value.trim()) {
    title.textContent = $("#youtube-publish-title").value.trim();
    title.hidden = false;
    copy.textContent = $("#youtube-publish-description").value.trim() || $("#publish-caption").value.trim() || "Your description will appear here.";
  } else {
    title.textContent = "";
    title.hidden = true;
    copy.textContent = $("#publish-caption").value.trim() || "Your caption will appear here.";
  }
}

function renderComposer() {
  selectedAccountIds = new Set([...selectedAccountIds].filter((id) => accountList.some((account) => account.id === id)));
  if (selectedContentId && !contentItems.some((item) => item.id === selectedContentId)) selectedContentId = null;
  renderComposerMedia();
  renderComposerDestinations();
  renderComposerDetails();
  renderComposerPreview();
  syncPublishButtons();
  $("#active-character-count").textContent = String(activeDetailsTab === "youtube" ? $("#youtube-publish-title").value.length : $("#publish-caption").value.length);
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
