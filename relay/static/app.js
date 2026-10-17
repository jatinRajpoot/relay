"use strict";
import { OAuthPopup } from "/static/oauth-popup.mjs";

const $ = (selector) => document.querySelector(selector);
const names = { instagram: "Instagram", youtube: "YouTube" };
let authenticated = false;
let accountList = [];
let pendingProvider = null;
let disconnecting = null;
let instagramMediaConfigured = false;
let activeInstagramPost = null;
let activePage = "connections";
let activeComposer = "instagram";
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
  notice("Sign-in window closed. If the provider reported an invalid redirect URI, register the callback URL in the Setup guide. Otherwise, refresh the list after completing sign-in.");
});

async function api(path, options = {}) {
  let response;
  try {
    response = await fetch(path, { credentials: "same-origin", ...options });
  } catch {
    throw new Error("Couldn't reach Relay. Check that the local service is running.");
  }
  const data = response.status === 204 ? null : await response.json();
  if (!response.ok) {
    if (response.status === 401) {
      authenticated = false;
      renderSession();
    }
    throw new Error(typeof data?.detail === "string" ? data.detail : "Something went wrong. Please try again.");
  }
  return data;
}

function notice(message, error = false, action = null) {
  const box = $("#notice");
  box.replaceChildren();
  const text = document.createElement("span");
  text.textContent = message;
  const dismiss = document.createElement("button");
  dismiss.className = action ? "text-button" : "icon-button";
  dismiss.setAttribute("aria-label", action ? action.label : "Dismiss notification");
  dismiss.textContent = action ? action.label : "×";
  dismiss.addEventListener("click", () => {
    if (action) action.run();
    else box.hidden = true;
  });
  box.append(text, dismiss);
  box.classList.toggle("error", error);
  box.hidden = false;
}

function renderSession() {
  const button = $("#session-button");
  button.classList.toggle("unlocked", authenticated);
  button.querySelector("span").textContent = authenticated ? "Lock workspace" : "Unlock workspace";
  $("#empty-foot").hidden = authenticated;
  $("#empty-connect").textContent = authenticated ? "Choose a platform" : "Unlock workspace";
  $("#empty-description").textContent = authenticated
    ? "Connect Instagram or YouTube to start publishing."
    : "Unlock your workspace to view or connect an account.";
  renderInstagramPublisher();
  renderYoutubePublisher();
  if (!authenticated) {
    accountList = [];
    activeInstagramPost = null;
    $("#accounts-list").replaceChildren();
    $("#accounts-list").hidden = true;
    $("#empty-state").hidden = false;
    $("#publish-check").hidden = true;
    $("#publish-now").hidden = true;
    $("#account-count").textContent = "—";
    $("#nav-count").textContent = "—";
    for (const provider of Object.keys(names)) {
      $(`#${provider}-state`).textContent = providerIssues[provider] ? "Setup needed" : "Ready to connect";
      $(`#${provider}-state`).classList.remove("connected");
    }
  }
  updateConnectionSummary();
}

function updateConnectionSummary() {
  const total = accountList.length;
  $("#summary-count").textContent = `${total} ${total === 1 ? "channel" : "channels"} connected`;
  $("#summary-description").textContent = !authenticated
    ? "Unlock to see your channels"
    : total ? "Choose one when you create a post" : "Connect a channel to get started";
  $("#instagram-connect-help").hidden = !authenticated || accountList.some((account) => account.provider === "instagram");
  $("#youtube-connect-help").hidden = !authenticated || accountList.some((account) => account.provider === "youtube");
}

function setPage(page, { recordHistory = true } = {}) {
  if (page !== "connections" && page !== "publish") return;
  activePage = page;
  document.querySelectorAll("[data-page-view]").forEach((view) => {
    view.hidden = view.dataset.pageView !== page;
  });
  document.querySelectorAll("[data-nav]").forEach((item) => {
    const selected = item.dataset.nav === page;
    item.classList.toggle("active", selected);
    if (item.matches("a")) {
      if (selected) item.setAttribute("aria-current", "page");
      else item.removeAttribute("aria-current");
    }
  });
  $("#breadcrumb-label").textContent = page === "connections" ? "Connections" : "Publish";
  if (window.location.hash !== `#${page}`) {
    const update = recordHistory ? "pushState" : "replaceState";
    window.history[update]({}, "", `#${page}`);
  }
  window.scrollTo({ top: 0, behavior: "smooth" });
}

function setComposer(provider) {
  if (provider !== "instagram" && provider !== "youtube") return;
  activeComposer = provider;
  document.querySelectorAll("[data-composer]").forEach((tab) => {
    const selected = tab.dataset.composer === provider;
    tab.classList.toggle("active", selected);
    tab.setAttribute("aria-selected", String(selected));
  });
  document.querySelectorAll("[data-composer-panel]").forEach((panel) => {
    const selected = panel.dataset.composerPanel === provider;
    panel.classList.toggle("active", selected);
    panel.hidden = !selected;
  });
}

function renderInstagramPublisher() {
  const select = $("#publish-account");
  const selected = select.value;
  select.replaceChildren();
  const instagramAccounts = accountList.filter((account) => account.provider === "instagram");
  if (!authenticated) {
    select.add(new Option("Unlock to choose an Instagram account", ""));
    select.disabled = true;
  } else if (!instagramAccounts.length) {
    select.add(new Option("Connect an Instagram account first", ""));
    select.disabled = true;
  } else {
    select.add(new Option("Choose an Instagram account", ""));
    for (const account of instagramAccounts) select.add(new Option(account.name, account.id));
    select.disabled = false;
    if (instagramAccounts.some((account) => account.id === selected)) select.value = selected;
  }
  if (!instagramMediaConfigured) {
    $("#publish-status").textContent = "Set INSTAGRAM_MEDIA_BUCKET to enable uploads to GCS.";
    $("#publish-start").disabled = true;
  } else {
    $("#publish-start").disabled = !authenticated || !instagramAccounts.length;
    if (!activeInstagramPost) $("#publish-status").textContent = "Media is stored in your configured public GCS bucket.";
  }
}

function renderYoutubePublisher() {
  const select = $("#youtube-publish-account");
  const selected = select.value;
  const youtubeAccounts = accountList.filter((account) => account.provider === "youtube");
  select.replaceChildren();
  if (!authenticated) {
    select.add(new Option("Unlock to choose a YouTube channel", ""));
    select.disabled = true;
  } else if (!youtubeAccounts.length) {
    select.add(new Option("Connect a YouTube account first", ""));
    select.disabled = true;
  } else {
    select.add(new Option("Choose a YouTube channel", ""));
    for (const account of youtubeAccounts) select.add(new Option(account.name, account.id));
    select.disabled = false;
    if (youtubeAccounts.some((account) => account.id === selected)) select.value = selected;
  }
  $("#youtube-publish-submit").disabled = !authenticated || !youtubeAccounts.length;
}

function showUnlock(provider = null) {
  pendingProvider = provider;
  $("#unlock-error").hidden = true;
  $("#unlock-dialog").showModal();
  $("#service-key").focus();
}

function renderAccounts() {
  const list = $("#accounts-list");
  list.replaceChildren();
  $("#account-count").textContent = String(accountList.length);
  $("#nav-count").textContent = String(accountList.length);
  $("#empty-state").hidden = accountList.length > 0;
  list.hidden = accountList.length === 0;
  $("#empty-title").textContent = "Your channels will show up here.";
  $("#empty-description").textContent = authenticated
    ? "Connect Instagram or YouTube to start publishing."
    : "Unlock your workspace to view or connect an account.";
  $("#empty-connect").textContent = authenticated ? "Choose a platform" : "Unlock workspace";
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
  renderInstagramPublisher();
  renderYoutubePublisher();
  updateConnectionSummary();
}

async function loadAccounts() {
  if (!authenticated) return;
  const result = await api("/accounts");
  accountList = result.accounts;
  renderAccounts();
}

async function connect(provider, popupAlreadyOpen = false) {
  if (providerIssues[provider]) {
    notice(providerIssues[provider], true);
    $("#setup-dialog").showModal();
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
document.querySelectorAll("[data-composer]").forEach((tab) => {
  tab.addEventListener("click", () => setComposer(tab.dataset.composer));
});
window.addEventListener("popstate", () => setPage(window.location.hash === "#publish" ? "publish" : "connections", { recordHistory: false }));
$("#empty-connect").addEventListener("click", () => {
  if (!authenticated) { showUnlock(); return; }
  $("#add-channel").scrollIntoView({ behavior: "smooth", block: "start" });
});
$("#publish-file").addEventListener("change", (event) => {
  $("#instagram-file-name").textContent = event.target.files[0]?.name || "No file selected";
});
$("#youtube-publish-file").addEventListener("change", (event) => {
  $("#youtube-file-name").textContent = event.target.files[0]?.name || "No file selected";
});
$("#publish-caption").addEventListener("input", (event) => {
  $("#caption-count").textContent = String(event.target.value.length);
});
document.querySelectorAll("[data-setup]").forEach((button) => {
  button.addEventListener("click", () => $("#setup-dialog").showModal());
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
$("#unlock-dialog").addEventListener("close", () => { $("#service-key").value = ""; });

$("#unlock-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = $("#unlock-submit");
  button.disabled = true;
  $("#unlock-error").hidden = true;
  const provider = pendingProvider;
  let popupOpened = false;
  try {
    // The unlock submit is also a user gesture: reserve the popup before login awaits.
    if (provider) {
      if (!oauthPopup.open(provider)) return;
      popupOpened = true;
    }
    await api("/session", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ service_key: $("#service-key").value.trim() }) });
    authenticated = true;
    $("#service-key").value = "";
    $("#unlock-dialog").close();
    renderSession();
    await loadAccounts();
    pendingProvider = null;
    if (provider) await connect(provider, true);
    else notice("Workspace unlocked. You're ready to connect.");
  } catch (error) {
    if (popupOpened) oauthPopup.close();
    if ($("#unlock-dialog").open) {
      $("#unlock-error").textContent = error.message;
      $("#unlock-error").hidden = false;
    } else notice(error.message, true);
  } finally { button.disabled = false; }
});

$("#session-button").addEventListener("click", async () => {
  if (!authenticated) { showUnlock(); return; }
  try {
    await api("/session", { method: "DELETE" });
    authenticated = false;
    renderSession();
    notice("Workspace locked. Your accounts are still safely connected.");
  } catch (error) { notice(error.message, true); }
});

$("#reload-accounts").addEventListener("click", async () => {
  if (!authenticated) { showUnlock(); return; }
  const button = $("#reload-accounts");
  button.disabled = true;
  try { await loadAccounts(); notice("Your connections are up to date."); }
  catch (error) { notice(error.message, true); }
  finally { button.disabled = false; }
});

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

$("#publish-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!authenticated) { showUnlock(); return; }
  const accountId = $("#publish-account").value;
  const file = $("#publish-file").files[0];
  if (!accountId || !file) { notice("Choose an Instagram account and a media file first.", true); return; }
  const button = $("#publish-start");
  button.disabled = true;
  $("#publish-check").hidden = true;
  $("#publish-now").hidden = true;
  try {
    $("#publish-status").textContent = "Uploading media to Google Cloud Storage…";
    const form = new FormData();
    form.append("file", file);
    const uploaded = await api("/instagram/media", { method: "POST", body: form });
    $("#publish-status").textContent = "Preparing Instagram post…";
    activeInstagramPost = await api("/instagram/posts", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ account_id: accountId, media_type: uploaded.media_type,
        media_url: uploaded.media_url, caption: $("#publish-caption").value }),
    });
    $("#publish-status").textContent = "Instagram is processing your " +
      (uploaded.media_type === "IMAGE" ? "photo" : "Reel") + ". Check again in about a minute.";
    $("#publish-check").hidden = false;
  } catch (error) {
    activeInstagramPost = null;
    $("#publish-status").textContent = error.message;
    notice(error.message, true);
  } finally {
    button.disabled = !instagramMediaConfigured || !authenticated;
  }
});

$("#publish-check").addEventListener("click", async () => {
  if (!activeInstagramPost) return;
  const button = $("#publish-check");
  button.disabled = true;
  try {
    activeInstagramPost = await api("/instagram/posts/" + encodeURIComponent(activeInstagramPost.id));
    $("#publish-status").textContent = "Instagram status: " + activeInstagramPost.status + ".";
    $("#publish-now").hidden = activeInstagramPost.status !== "ready";
    button.hidden = ["ready", "published", "failed", "expired"].includes(activeInstagramPost.status);
    if (activeInstagramPost.status === "published") $("#publish-status").textContent = "Your post is published.";
  } catch (error) {
    notice(error.message, true);
  } finally { button.disabled = false; }
});

$("#publish-now").addEventListener("click", async () => {
  if (!activeInstagramPost) return;
  const button = $("#publish-now");
  button.disabled = true;
  try {
    activeInstagramPost = await api("/instagram/posts/" + encodeURIComponent(activeInstagramPost.id) + "/publish", { method: "POST" });
    $("#publish-status").textContent = activeInstagramPost.status === "published"
      ? "Your post is published." : "Instagram status: " + activeInstagramPost.status + ".";
    button.hidden = true;
    $("#publish-check").hidden = activeInstagramPost.status === "published";
  } catch (error) {
    notice(error.message, true);
    $("#publish-status").textContent = error.message;
  } finally { button.disabled = false; }
});

$("#youtube-publish-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!authenticated) { showUnlock(); return; }
  const accountId = $("#youtube-publish-account").value;
  const file = $("#youtube-publish-file").files[0];
  const title = $("#youtube-publish-title").value.trim();
  if (!accountId || !file || !title) {
    notice("Choose a YouTube channel and video, then enter a title.", true);
    return;
  }
  const button = $("#youtube-publish-submit");
  button.disabled = true;
  button.querySelector("span").textContent = "Uploading video…";
  $("#youtube-publish-result").hidden = true;
  try {
    $("#youtube-publish-status").textContent = "Uploading directly to YouTube. Large videos can take a while.";
    const form = new FormData();
    form.append("account_id", accountId);
    form.append("title", title);
    form.append("description", $("#youtube-publish-description").value);
    form.append("privacy_status", $("#youtube-publish-privacy").value);
    form.append("made_for_kids", "false");
    form.append("file", file);
    const uploaded = await api("/youtube/videos", { method: "POST", body: form });
    $("#youtube-publish-status").textContent = "Video uploaded to YouTube as " + uploaded.privacy_status + ".";
    const link = $("#youtube-publish-result");
    link.href = uploaded.url;
    link.hidden = false;
  } catch (error) {
    $("#youtube-publish-status").textContent = error.message;
    notice(error.message, true);
  } finally {
    button.disabled = false;
    button.querySelector("span").textContent = "Upload video";
  }
});

async function initialize() {
  try {
    setPage(window.location.hash === "#publish" ? "publish" : "connections", { recordHistory: false });
    setComposer("instagram");
    const state = await api("/session");
    authenticated = state.authenticated;
    instagramMediaConfigured = state.publishing?.instagram_media_configured === true;
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
