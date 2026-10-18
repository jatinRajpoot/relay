"use strict";

window.handleOAuthResult = async ({provider, result}) => {
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
};

window.handleOAuthClosed = async () => {
  resetConnectButtons();
  try { await loadAccounts(); } catch (error) { notice(error.message, true); return; }
  if (activePage === "analytics") await loadAnalytics();
  notice("Sign-in window closed. If the provider reported an invalid redirect URI, register the callback URL in the Setup guide. Otherwise, refresh the list after completing sign-in.");
};

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
    if (!popupAlreadyOpen && !window.oauthPopup.open(provider)) return;
    buttons.forEach((button) => { button.disabled = true; });
    label.textContent = `Sign in to ${names[provider]}…`;
    const result = await api(`/oauth/${provider}/authorize?ui=true&popup=true`, { method: "POST" });
    window.oauthPopup.navigate(result.authorization_url);
    notice(`Finish connecting ${names[provider]} in the sign-in window. This workspace will stay open.`, false, {
      label: "Cancel sign-in",
      run() {
        window.oauthPopup.close();
        resetConnectButtons();
        notice("Sign-in cancelled. Your existing connections haven't changed.");
      },
    });
  } catch (error) {
    window.oauthPopup.close();
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
document.querySelectorAll("[data-preview-platform]").forEach((tab) => {
  tab.addEventListener("click", () => {
    activePreviewPlatform = tab.dataset.previewPlatform;
    renderComposerPreview();
  });
});
document.querySelectorAll("[data-history-filter]").forEach((tab) => {
  tab.addEventListener("click", () => {
    historyFilter = tab.dataset.historyFilter;
    document.querySelectorAll("[data-history-filter]").forEach((item) => {
      const active = item === tab;
      item.classList.toggle("active", active);
      item.setAttribute("aria-selected", String(active));
    });
    renderPostHistory();
  });
});
$("#open-media-picker").addEventListener("click", () => {
  if (!authenticated) { showUnlock(); return; }
  renderComposerMedia();
  $("#media-picker-dialog").showModal();
});
$("#save-draft").addEventListener("click", saveLocalDraft);
$("#composer-media-action").addEventListener("click", () => {
  if (!authenticated) { showUnlock(); return; }
  if ($("#media-picker-dialog").open) $("#media-picker-dialog").close();
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

async function uploadDroppedFile(file) {
  if (!authenticated) { showUnlock(); return; }
  if (!contentStorageConfigured) {
    notice("Media storage needs setup before uploads can be saved.", true);
    return;
  }
  if (!file) return;
  const form = new FormData();
  form.append("file", file);
  notice(`Adding ${file.name} to the media library…`);
  try {
    const item = await api("/content", { method: "POST", body: form });
    await loadContent();
    if (activePage === "publish") {
      selectedContentId = item.id;
      renderComposer();
    }
    notice("Media added to your library.");
  } catch (error) { notice(error.message, true); }
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
document.querySelectorAll("[data-details-tab]").forEach((tab) => {
  tab.addEventListener("keydown", (event) => {
    const tabs = [...document.querySelectorAll("[data-details-tab]")].filter((item) => !item.hidden);
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
  if (activeDetailsTab !== "youtube") renderComposerDetails();
  renderComposerPreview();
});
$("#youtube-publish-title").addEventListener("input", () => {
  syncPublishButtons();
  if (activeDetailsTab === "youtube") renderComposerDetails();
  renderComposerPreview();
});
$("#youtube-publish-description").addEventListener("input", () => { syncPublishButtons(); renderComposerPreview(); });
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
