"use strict";

/* ── Channels ──────────────────────────────────────────────────
   Connecting, refreshing, and disconnecting accounts. Disconnect is
   destructive, so its dialog states exactly what Relay deletes and what it
   cannot delete on the provider. */

function renderAccounts() {
  const list = $("#accounts-list");
  list.replaceChildren();
  setText("#account-count", String(accountList.length));
  setText("#nav-count", String(accountList.length));
  $("#empty-state").hidden = accountList.length > 0;
  list.hidden = accountList.length === 0;

  for (const provider of Object.keys(names)) {
    const count = accountList.filter((account) => account.provider === provider).length;
    const badge = $(`#${provider}-state`);
    badge.textContent = count ? `${count} connected` : (providerIssues[provider] ? "Setup needed" : "Ready to connect");
    badge.classList.toggle("connected", count > 0);
    badge.classList.toggle("needs-setup", !count && Boolean(providerIssues[provider]));
  }

  for (const account of accountList) {
    const row = $("#account-template").content.firstElementChild.cloneNode(true);
    const provider = account.provider;
    // The same predicate Home and the composer use. An expired YouTube token is
    // refreshed automatically before a send, so it is not a reconnect signal.
    const readiness = channelStatus(account, "channels");
    const needsReconnect = readiness.reconnect;
    row.querySelector(".account-platform").classList.add(provider);
    row.querySelector("use").setAttribute("href", `#i-${provider}`);
    row.querySelector(".account-name").textContent = account.name;
    row.querySelector(".account-platform-name").textContent = names[provider];
    const status = row.querySelector(".account-status");
    status.textContent = readiness.label;
    status.classList.toggle("expired", needsReconnect);
    status.title = readiness.unreadable
      ? "Relay can't read this channel's saved authorization. Reconnect it to send again."
      : readiness.expired && !readiness.reconnect
        ? "Relay refreshes this channel's access automatically before sending."
        : `Authorization expires ${new Date(account.expires_at * 1000).toLocaleString()}`;
    const reconnect = row.querySelector(".reconnect-account");
    reconnect.setAttribute("aria-label", `Reconnect ${account.name}`);
    reconnect.addEventListener("click", () => connect(provider));
    const disconnect = row.querySelector(".disconnect-account");
    disconnect.setAttribute("aria-label", `Disconnect ${account.name}`);
    disconnect.addEventListener("click", () => {
      disconnecting = account;
      $("#disconnect-description").textContent = `${account.name} · ${names[provider]}`;
      openDialog($("#disconnect-dialog"), disconnect);
    });
    list.append(row);
  }
  renderComposer();
  renderOverview();
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
  renderOverview();
}

/* ── Connect ───────────────────────────────────────────────── */

function uploadRecovery(error, input, retry) {
  if ([400, 413, 415, 422].includes(error.status)) {
    return { label: "Choose another file", run: () => input.click() };
  }
  return { label: "Retry now", run: retry };
}

window.handleOAuthResult = async ({ provider, result }) => {
  resetConnectButtons();
  try { await loadAccounts(); } catch (error) { notice(error.message, true, { label: "Retry now", run: () => $("#reload-accounts").click() }); return; }
  if (activePage === "analytics") await loadAnalytics();
  if (result === "connected") {
    notice(`${names[provider]} connected. Your post is still here.`);
  } else {
    const messages = {
      cancelled: "Sign-in was cancelled. You can connect again whenever you're ready.",
      expired: "That sign-in request expired. Start again when you're ready.",
      connection: "We couldn't finish connecting. Try again, or ask the person who runs Relay to check the app's callback settings.",
    };
    const message = messages[result] || messages.connection;
    notice(message, true);
    if ($("#channel-sheet").open) showJourneyRecovery(message, () => connect(provider), $("#channel-sheet-error"), "Try again");
  }
};

window.handleOAuthClosed = async () => {
  resetConnectButtons();
  try { await loadAccounts(); } catch (error) { notice(error.message, true, { label: "Retry now", run: () => $("#reload-accounts").click() }); return; }
  if (activePage === "analytics") await loadAnalytics();
  notice("Sign-in window closed. If the connection didn't appear, try Connect again.");
};

/* Deployment setup is an operator task. The person in front of Relay gets one
   plain sentence; the exact configuration steps stay in the setup guide, which
   is opened deliberately rather than shown as a toast. */
function providerIssueMessage(provider) {
  return `${names[provider]} sign-in isn't available on this deployment yet. Ask whoever runs Relay to finish its setup.`;
}

async function connect(provider, popupAlreadyOpen = false) {
  if (providerIssues[provider]) {
    notice(providerIssueMessage(provider), true);
    openDialog($("#setup-dialog"));
    return;
  }
  if (!providerConfigured[provider]) {
    notice(`${names[provider]} isn't configured on this deployment yet.`, true);
    return;
  }
  if (!authenticated) { showUnlock(); return; }
  const buttons = document.querySelectorAll("[data-connect]");
  const button = document.querySelector(`[data-connect="${provider}"]`);
  const label = button.querySelector("span");
  try {
    // Opening before the first await preserves the browser's user-activation permission.
    if (!popupAlreadyOpen && !window.oauthPopup.open(provider)) return;
    buttons.forEach((item) => { item.disabled = true; });
    label.textContent = `Sign in to ${names[provider]}…`;
    const result = await api(`/oauth/${provider}/authorize?ui=true&popup=true`, { method: "POST" });
    window.oauthPopup.navigate(result.authorization_url);
    notice(`Finish connecting ${names[provider]} in the sign-in window. This workspace stays open.`, false, {
      label: "Cancel sign-in",
      run() {
        window.oauthPopup.close();
        resetConnectButtons();
        notice("Sign-in cancelled. Your connections haven't changed.");
      },
    });
  } catch (error) {
    window.oauthPopup.close();
    if ($("#channel-sheet").open) showJourneyRecovery(`${error.message} Your post stays here.`, () => connect(provider), $("#channel-sheet-error"));
    notice(error.message, true, { label: "Retry now", run: () => connect(provider) });
    if (!authenticated) showUnlock();
    resetConnectButtons();
  }
}

/* ── Wiring ────────────────────────────────────────────────── */

document.querySelectorAll("[data-connect]").forEach((button) => {
  button.addEventListener("click", () => connect(button.dataset.connect));
});

document.querySelectorAll("[data-nav]").forEach((item) => {
  item.addEventListener("click", (event) => {
    event.preventDefault();
    const target = item.dataset.nav;
    if ($("#more-sheet").open) $("#more-sheet").close();
    setPage(target);
  });
});

document.querySelectorAll("[data-preview-platform]").forEach((tab) => {
  tab.addEventListener("click", () => {
    activePreviewPlatform = tab.dataset.previewPlatform;
    renderComposerPreview();
  });
  tab.addEventListener("keydown", (event) => {
    const tabs = [...document.querySelectorAll("[data-preview-platform]")].filter((item) => !item.hidden);
    const current = tabs.indexOf(tab);
    if (current < 0) return;
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

document.querySelectorAll("[data-history-filter]").forEach((tab) => {
  tab.addEventListener("click", () => {
    historyFilter = tab.dataset.historyFilter;
    renderPostHistory();
  });
});

$("#open-media-picker").addEventListener("click", () => {
  if (!authenticated) { showUnlock(); return; }
  renderMediaPicker();
  openDialog($("#media-picker-dialog"), $("#open-media-picker"));
});

$("#composer-media-browse").addEventListener("click", () => {
  if (!authenticated) { showUnlock(); return; }
  renderMediaPicker();
  openDialog($("#media-picker-dialog"), $("#composer-media-browse"));
});

$("#save-draft").addEventListener("click", () => saveLocalDraft());

$("#composer-preview-toggle").addEventListener("click", (event) => {
  const button = event.currentTarget;
  const preview = $("#composer-preview");
  const expanded = button.getAttribute("aria-expanded") !== "true";
  button.setAttribute("aria-expanded", String(expanded));
  preview.classList.toggle("is-expanded", expanded);
  button.querySelector("span").textContent = expanded ? "Hide post preview" : "Preview your post";
});

const mobileActionBar = $(".composer-action-bar");
if (mobileActionBar && "IntersectionObserver" in window) {
  const deliverySectionsInView = new Set();
  const actionReleaseObserver = new IntersectionObserver((entries) => {
    for (const entry of entries) {
      if (entry.isIntersecting) deliverySectionsInView.add(entry.target);
      else deliverySectionsInView.delete(entry.target);
    }
    mobileActionBar.classList.toggle("is-static", deliverySectionsInView.size > 0);
  }, { rootMargin: "0px 0px -48px 0px" });
  [$("#publish-results-section"), $("#recovered-section")]
    .filter(Boolean)
    .forEach((section) => actionReleaseObserver.observe(section));
}

$("#composer-media-action").addEventListener("click", () => {
  if (!authenticated) { showUnlock(); return; }
  if (!contentStorageConfigured) {
    notice("Media storage isn't available on this deployment yet.", true);
    return;
  }
  $("#publish-file").click();
});

$("#composer-media-picker-action").addEventListener("click", () => {
  $("#media-picker-dialog").close();
  $("#publish-file").click();
});

function openContentUpload() {
  if (!authenticated) { showUnlock(); return; }
  if (!contentStorageConfigured) {
    notice("Media storage isn't available on this deployment yet.", true);
    return;
  }
  $("#content-upload-status").textContent = "";
  openDialog($("#upload-dialog"), $("#content-upload-trigger"));
}

async function uploadDroppedFile(file) {
  if (!authenticated) { showUnlock(); return; }
  if (!contentStorageConfigured) {
    notice("Media storage isn't available on this deployment yet.", true);
    return;
  }
  if (!file) return;
  if (maxUploadBytes > 0 && file.size > maxUploadBytes) {
    const message = `${file.name} is ${formatBytes(file.size)}. Choose a file no larger than ${formatBytes(maxUploadBytes)}.`;
    notice(message, true, {
      label: "Choose another file",
      run() {
        if (activePage === "publish") {
          $("#publish-file").click();
          return;
        }
        setPage("content");
        openContentUpload();
        $("#content-file").click();
      },
    });
    return;
  }
  const form = new FormData();
  form.append("file", file);
  notice(`Adding ${file.name} to your media library…`);
  try {
    const item = await api("/content", { method: "POST", body: form });
    await loadContent();
    if (activePage === "publish") {
      selectedContentId = item.id;
      renderComposer();
      scheduleDraftSave();
    }
    notice("Media added to your library.");
  } catch (error) {
    notice(`${error.message} ${file.name} is still selected in this window.`, true, {
      ...uploadRecovery(error, activePage === "publish" ? $("#publish-file") : $("#content-file"), () => uploadDroppedFile(file)),
    });
  }
}

let dragDepth = 0;
document.addEventListener("dragenter", (event) => {
  if (!event.dataTransfer?.types.includes("Files")) return;
  event.preventDefault();
  dragDepth += 1;
  $("#global-drop-overlay").hidden = false;
});
document.addEventListener("dragover", (event) => {
  if (event.dataTransfer?.types.includes("Files")) event.preventDefault();
});
document.addEventListener("dragleave", (event) => {
  if (!event.dataTransfer?.types.includes("Files")) return;
  dragDepth = Math.max(0, dragDepth - 1);
  if (!dragDepth) $("#global-drop-overlay").hidden = true;
});
document.addEventListener("drop", (event) => {
  if (!event.dataTransfer?.files.length) return;
  event.preventDefault();
  dragDepth = 0;
  $("#global-drop-overlay").hidden = true;
  void uploadDroppedFile(event.dataTransfer.files[0]);
});

$("#content-upload-trigger").addEventListener("click", openContentUpload);

$("#publish-upload-trigger")?.addEventListener("click", () => {
  if (!authenticated) { showUnlock(); return; }
  if (!contentStorageConfigured) {
    notice("Media storage isn't available on this deployment yet.", true);
    return;
  }
  $("#publish-file").click();
});

$("#publish-file").addEventListener("change", async (event) => {
  const file = event.target.files[0];
  if (!file) return;
  if (!authenticated) { showUnlock(); return; }
  if (!contentStorageConfigured) {
    notice("Media storage isn't available on this deployment yet.", true);
    return;
  }
  const status = $("#publish-upload-status");
  $("#publish-upload-name").textContent = file.name;
  status.hidden = false;
  if (maxUploadBytes > 0 && file.size > maxUploadBytes) {
    const message = `${file.name} is ${formatBytes(file.size)}. Choose a file no larger than ${formatBytes(maxUploadBytes)}.`;
    status.textContent = message;
    status.setAttribute("role", "alert");
    $("#publish-file").setAttribute("aria-invalid", "true");
    notice(message, true, { label: "Choose another file", run: () => $("#publish-file").click() });
    return;
  }
  $("#publish-file").removeAttribute("aria-invalid");
  status.textContent = "Adding your file to the media library…";
  status.setAttribute("role", "status");
  try {
    const form = new FormData();
    form.append("file", file);
    const item = await api("/content", { method: "POST", body: form });
    await loadContent();
    selectedContentId = item.id;
    renderComposer();
    scheduleDraftSave();
    status.textContent = "Added to your library.";
    $("#publish-file").value = "";
    $("#publish-upload-name").textContent = "No file selected";
  } catch (error) {
    const message = `${error.message} ${file.name} is still selected.`;
    status.textContent = message;
    status.setAttribute("role", "alert");
    showJourneyRecovery(message, () => $("#publish-file").click(), status);
    notice(message, true, uploadRecovery(error, $("#publish-file"), () => $("#publish-file").click()));
  } finally {
    renderComposerActions();
  }
});

$("#refresh-history").addEventListener("click", () => {
  if (!authenticated) { showUnlock(); return; }
  void loadPostHistory();
});
$("#history-unlock").addEventListener("click", () => showUnlock());
$("#history-load-more").addEventListener("click", () => void loadMoreAttempts());

$("#overview-primary-action").addEventListener("click", () => {
  if (!authenticated) { showUnlock(); return; }
  // Both Home and the rail open the composer. A restored draft is already in
  // the form, so this never discards it.
  setPage("publish");
});

$("#home-resume-draft").addEventListener("click", () => {
  restoreLocalDraft();
  setPage("publish");
});

$("#draft-import-accept").addEventListener("click", () => recoverLegacyDraft());
$("#draft-import-dismiss").addEventListener("click", () => {
  dismissLegacyDraft(activeWorkspaceId);
  notice("The older draft was left in this browser. You can still find it under its old key.");
});
$("#draft-conflict-use-preview").addEventListener("click", () => {
  const preview = readPreviewDraft();
  if (preview) applyPreviewCaption(preview);
});
$("#draft-conflict-keep").addEventListener("click", () => {
  discardPreviewDraft();
  notice("Kept your current draft. The preview caption was discarded.");
});

window.addEventListener("popstate", () => {
  const page = routeFromHash();
  if (page) setPage(page, { recordHistory: false });
  else if (window.location.hash) {
    // An unknown fragment is not a route. Restore the current page's hash.
    window.history.replaceState({}, "", `#${activePage}`);
  }
});

window.addEventListener("hashchange", () => {
  const page = routeFromHash();
  if (page && page !== activePage) setPage(page, { recordHistory: false });
});

$("#analytics-range").addEventListener("change", (event) => {
  analyticsDays = Number(event.target.value);
  void loadAnalytics();
});
$("#refresh-analytics").addEventListener("click", () => { void loadAnalytics(); });
$("#analytics-unlock").addEventListener("click", () => showUnlock());

$("#content-file").addEventListener("change", (event) => {
  $("#content-file-name").textContent = event.target.files[0]?.name || "No file selected";
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
  openDialog($("#workspace-dialog"), $("#new-workspace"));
  const capacity = workspaceCapacity($("#workspace-select").options.length);
  $("#workspace-submit").disabled = Boolean(capacity);
  if (capacity) showJourneyRecovery(capacity, () => { $("#workspace-dialog").close(); $("#workspace-select").focus(); }, $("#workspace-error"), "Switch workspace");
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
    if (error.status === 409 && error.message.includes("50")) {
      showJourneyRecovery(workspaceCapacity(50), () => { $("#workspace-dialog").close(); $("#workspace-select").focus(); }, $("#workspace-error"), "Switch workspace");
    }
  } finally { button.disabled = false; }
});

$("#reload-accounts").addEventListener("click", async () => {
  if (!authenticated) { showUnlock(); return; }
  const button = $("#reload-accounts");
  button.disabled = true;
  try { await loadAccounts(); notice("Your connections are up to date."); }
  catch (error) { notice(error.message, true, { label: "Retry now", run: () => $("#reload-accounts").click() }); }
  finally { button.disabled = false; }
});

$("#content-empty-action").addEventListener("click", () => {
  if (!authenticated) { showUnlock(); return; }
  openContentUpload();
});

$("#content-upload-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!authenticated) { showUnlock(); return; }
  const file = $("#content-file").files[0];
  if (!file) { notice("Choose a photo or video to upload.", true); return; }
  if (maxUploadBytes > 0 && file.size > maxUploadBytes) {
    const message = `${file.name} is ${formatBytes(file.size)}. Choose a file no larger than ${formatBytes(maxUploadBytes)}.`;
    $("#content-upload-status").textContent = message;
    $("#content-upload-status").setAttribute("role", "alert");
    $("#content-file").setAttribute("aria-invalid", "true");
    notice(message, true, { label: "Choose another file", run: () => $("#content-file").click() });
    return;
  }
  $("#content-file").removeAttribute("aria-invalid");
  const button = $("#content-upload-submit");
  button.disabled = true;
  button.querySelector("span").textContent = "Uploading…";
  $("#content-upload-status").textContent = "Saving your media to the library…";
  $("#content-upload-status").setAttribute("role", "status");
  try {
    const form = new FormData();
    form.append("file", file);
    await api("/content", { method: "POST", body: form });
    $("#content-file").value = "";
    $("#content-file-name").textContent = "No file selected";
    await loadContent();
    $("#upload-dialog").close();
    notice("Media added to your library.");
  } catch (error) {
    const message = `${error.message} ${file.name} is still selected.`;
    $("#content-upload-status").textContent = message;
    $("#content-upload-status").setAttribute("role", "alert");
    showJourneyRecovery(message, () => $("#content-upload-form").requestSubmit(), $("#content-upload-status"));
    notice(message, true, uploadRecovery(error, $("#content-file"), () => $("#content-upload-form").requestSubmit()));
  } finally {
    button.querySelector("span").textContent = "Upload media";
    renderComposerActions();
  }
});

$("#reload-content").addEventListener("click", async () => {
  if (!authenticated) { showUnlock(); return; }
  const button = $("#reload-content");
  button.disabled = true;
  try { await loadContent(); notice("Your library is up to date."); }
  catch (error) { notice(error.message, true, { label: "Retry now", run: () => $("#reload-content").click() }); }
  finally { button.disabled = false; }
});

$("#publish-caption").addEventListener("input", () => {
  setText("#caption-count", String($("#publish-caption").value.length));
  renderComposerPreview();
  renderComposerActions();
  scheduleDraftSave();
});
$("#youtube-publish-title").addEventListener("input", () => {
  renderComposerDetails();
  renderComposerPreview();
  renderComposerActions();
  scheduleDraftSave();
});
$("#youtube-publish-description").addEventListener("input", () => {
  renderComposerPreview();
  scheduleDraftSave();
});
$("#youtube-publish-privacy").addEventListener("change", () => scheduleDraftSave());
$("#youtube-made-for-kids").addEventListener("change", () => scheduleDraftSave());

initSkipLink();

$("#confirm-disconnect").addEventListener("click", async () => {
  if (!disconnecting) return;
  const button = $("#confirm-disconnect");
  button.disabled = true;
  try {
    await api(`/accounts/${encodeURIComponent(disconnecting.id)}`, { method: "DELETE" });
    $("#disconnect-dialog").close();
    await loadAccounts();
    notice("Channel disconnected. Its delivery records were removed from Relay.");
    disconnecting = null;
  } catch (error) {
    $("#disconnect-dialog").close();
    notice(error.message, true, { label: "Check channels", run: () => $("#reload-accounts").click() });
  } finally { button.disabled = false; }
});
