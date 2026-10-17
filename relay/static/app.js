"use strict";
import { OAuthPopup } from "/static/oauth-popup.mjs";

const $ = (selector) => document.querySelector(selector);
const names = { instagram: "Instagram", youtube: "YouTube" };
let authenticated = false;
let accountList = [];
let pendingProvider = null;
let disconnecting = null;
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
  if (!authenticated) {
    accountList = [];
    $("#accounts-list").replaceChildren();
    $("#accounts-list").hidden = true;
    $("#empty-state").hidden = false;
    $("#account-count").textContent = "—";
    $("#nav-count").textContent = "—";
    for (const provider of Object.keys(names)) {
      $(`#${provider}-state`).textContent = providerIssues[provider] ? "Setup needed" : "Ready to connect";
      $(`#${provider}-state`).classList.remove("connected");
    }
  }
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
  $("#empty-title").textContent = "Your next connection starts here.";
  $("#empty-description").textContent = "Choose a platform above to connect your first account.";
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

async function initialize() {
  try {
    const state = await api("/session");
    authenticated = state.authenticated;
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
