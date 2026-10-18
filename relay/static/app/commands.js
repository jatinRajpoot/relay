"use strict";

document.querySelectorAll("[data-theme-toggle]").forEach((button) => {
  const currentTheme = document.documentElement.dataset.theme || "light";
  button.querySelector("use").setAttribute("href", currentTheme === "light" ? "#i-sun" : "#i-moon");
  button.setAttribute("aria-label", `Switch to ${currentTheme === "light" ? "dark" : "light"} theme`);
  button.addEventListener("click", () => {
    const theme = document.documentElement.dataset.theme === "light" ? "dark" : "light";
    document.documentElement.dataset.theme = theme;
    try { localStorage.setItem("relay-theme", theme); } catch { /* Theme remains active for this page. */ }
    button.querySelector("use").setAttribute("href", theme === "light" ? "#i-sun" : "#i-moon");
    button.setAttribute("aria-label", `Switch to ${theme === "light" ? "dark" : "light"} theme`);
  });
});

/* ── Command palette ───────────────────────────────────────── */
const paletteCommands = [
  { label: "Studio", hint: "Workspace activity", icon: "home", group: "Go to", run: () => setPage("overview") },
  { label: "Create a post", hint: "Compose", icon: "plus", group: "Go to", run: () => setPage("publish") },
  { label: "Library", hint: "Upload and reuse", icon: "media", group: "Go to", run: () => setPage("content") },
  { label: "Channels", hint: "Connect accounts", icon: "link", group: "Go to", run: () => setPage("connections") },
  { label: "Insights", hint: "Performance", icon: "chart", group: "Go to", run: () => setPage("analytics") },
  { label: "Published", hint: "Posts and status", icon: "history", group: "Go to", run: () => setPage("history") },
  { label: "Log in", hint: "Open your account", icon: "lock", group: "Actions", run: () => showUnlock() },
  { label: "Log out", hint: "Sign out of this browser", icon: "lock", group: "Actions", run: () => lockWorkspace() },
  { label: "Connect Instagram", hint: "OAuth popup", icon: "instagram", group: "Actions", run: () => connect("instagram") },
  { label: "Connect YouTube", hint: "OAuth popup", icon: "youtube", group: "Actions", run: () => connect("youtube") },
  { label: "Setup guide", hint: "Callback URLs", icon: "book", group: "Actions", run: () => $("#setup-dialog").showModal() },
  { label: "API reference", hint: "Opens /docs", icon: "external", group: "Actions", run: () => window.open("/docs", "_blank", "noopener") },
];

let paletteMatches = [];
let paletteIndex = 0;

function openPalette() {
  const dialog = $("#palette-dialog");
  if (dialog.open) { dialog.close(); return; }
  $("#palette-input").value = "";
  renderPalette("");
  dialog.showModal();
  $("#palette-input").focus();
}

function renderPalette(query) {
  const list = $("#palette-list");
  const needle = query.trim().toLowerCase();
  paletteMatches = paletteCommands.filter((command) =>
    !needle || `${command.label} ${command.hint} ${command.group}`.toLowerCase().includes(needle));
  paletteIndex = 0;
  list.replaceChildren();
  if (!paletteMatches.length) {
    const empty = document.createElement("p");
    empty.className = "palette-empty";
    empty.textContent = "No matching commands.";
    list.append(empty);
    return;
  }
  let group = null;
  paletteMatches.forEach((command, index) => {
    if (command.group !== group) {
      group = command.group;
      const heading = document.createElement("p");
      heading.className = "palette-group";
      heading.textContent = group;
      list.append(heading);
    }
    const item = document.createElement("button");
    item.type = "button";
    item.className = "palette-item";
    item.setAttribute("role", "option");
    item.setAttribute("aria-selected", String(index === paletteIndex));
    item.append(icon(command.icon));
    const label = document.createElement("span");
    label.textContent = command.label;
    item.append(label);
    const hint = document.createElement("small");
    hint.textContent = command.hint;
    item.append(hint);
    item.addEventListener("click", () => runPalette(index));
    item.addEventListener("mousemove", () => {
      paletteIndex = index;
      [...list.querySelectorAll(".palette-item")].forEach((node, i) =>
        node.setAttribute("aria-selected", String(i === index)));
    });
    list.append(item);
  });
}

function runPalette(index) {
  const command = paletteMatches[index];
  $("#palette-dialog").close();
  if (command) window.setTimeout(() => command.run(), 0);
}

function movePalette(delta) {
  if (!paletteMatches.length) return;
  paletteIndex = (paletteIndex + delta + paletteMatches.length) % paletteMatches.length;
  const items = [...$("#palette-list").querySelectorAll(".palette-item")];
  items.forEach((node, i) => node.setAttribute("aria-selected", String(i === paletteIndex)));
  items[paletteIndex]?.scrollIntoView({ block: "nearest" });
}

$("#palette-input").addEventListener("input", (event) => renderPalette(event.target.value));
$("#palette-input").addEventListener("keydown", (event) => {
  if (event.key === "ArrowDown") { event.preventDefault(); movePalette(1); }
  else if (event.key === "ArrowUp") { event.preventDefault(); movePalette(-1); }
  else if (event.key === "Enter") { event.preventDefault(); runPalette(paletteIndex); }
});
$("#palette-dialog").addEventListener("click", (event) => {
  if (event.target === $("#palette-dialog")) $("#palette-dialog").close();
});

/* ── Keyboard shortcuts ────────────────────────────────────── */
const pageKeys = { h: "overview", n: "publish", l: "content", c: "connections", a: "analytics", y: "history" };
let pendingGoto = false;

async function lockWorkspace() {
  if (!authenticated) { showUnlock(); return; }
  try {
    await api("/session", { method: "DELETE" });
    authenticated = false;
    renderSession();
    window.location.replace("/");
  } catch (error) { notice(error.message, true, { label: "Retry now", run: () => lockWorkspace() }); }
}

function isTyping(event) {
  const node = event.target;
  return node instanceof HTMLElement &&
    (node.tagName === "INPUT" || node.tagName === "TEXTAREA" || node.tagName === "SELECT" || node.isContentEditable);
}

document.addEventListener("keydown", (event) => {
  if (event.defaultPrevented) return;
  const meta = event.metaKey || event.ctrlKey;
  if (meta && event.key.toLowerCase() === "k") {
    event.preventDefault();
    openPalette();
    return;
  }
  if (isTyping(event) || meta || event.altKey) return;
  if ($("#palette-dialog").open) return;
  const openDialog = document.querySelector("dialog[open]");
  if (openDialog) return;

  if (event.key === "n") { event.preventDefault(); setPage("publish"); return; }
  if (event.key === "g") { pendingGoto = true; window.setTimeout(() => { pendingGoto = false; }, 1200); return; }
  if (pendingGoto && pageKeys[event.key]) {
    event.preventDefault();
    pendingGoto = false;
    setPage(pageKeys[event.key]);
  }
});

window.initialize = async function initialize() {
  try {
    const initialPage = window.location.hash.slice(1);
    setPage(["overview", "analytics", "history", "connections", "content", "publish"].includes(initialPage) ? initialPage : "overview", { recordHistory: false });
    const state = await api("/session");
    authenticated = state.authenticated;
    if (!authenticated) { window.location.replace("/login"); return; }
    activeWorkspaceId = state.workspace?.id || null;
    $("#workspace-tools").hidden = !state.user;
    $("#account-label").textContent = state.user?.name || "Legacy workspace";
    $("#account-label").title = state.user?.email || "Service key access";
    for (const workspace of state.workspaces || []) {
      const option = document.createElement("option");
      option.value = workspace.id;
      option.textContent = workspace.name;
      $("#workspace-select").append(option);
    }
    if (activeWorkspaceId) $("#workspace-select").value = activeWorkspaceId;
    instagramMediaConfigured = state.publishing?.instagram_media_configured === true;
    contentStorageConfigured = state.publishing?.content_storage_configured === true;
    maxUploadBytes = state.publishing?.max_upload_bytes || 0;
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
    await loadContent();
    if (authenticated) await loadPostHistory();
    if (activePage === "analytics") await loadAnalytics();
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
