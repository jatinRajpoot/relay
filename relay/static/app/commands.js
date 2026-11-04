"use strict";

/* Theme, command palette, keyboard shortcuts, and app bootstrap. */

function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  try { localStorage.setItem("relay-theme", theme); } catch { /* theme still applies to this page */ }
  document.querySelectorAll("[data-theme-toggle]").forEach((button) => {
    const use = button.querySelector("use");
    if (use) use.setAttribute("href", theme === "light" ? "#i-sun" : "#i-moon");
    button.setAttribute("aria-label", `Switch to ${theme === "light" ? "dark" : "light"} theme`);
  });
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.setAttribute("content", theme === "light" ? "#f3f0e7" : "#141a16");
}

function preferredTheme() {
  try {
    const stored = localStorage.getItem("relay-theme");
    if (stored === "light" || stored === "dark") return stored;
  } catch { /* fall through to the system preference */ }
  return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

applyTheme(document.documentElement.dataset.theme || preferredTheme());

document.querySelectorAll("[data-theme-toggle]").forEach((button) => {
  button.addEventListener("click", () => {
    applyTheme(document.documentElement.dataset.theme === "light" ? "dark" : "light");
  });
});

/* ── Command palette ─────────────────────────────────────────
   A complete combobox/listbox: the input owns aria-expanded, aria-controls and
   aria-activedescendant; options carry stable ids; Escape closes and focus
   returns to whatever opened the dialog. */

const paletteCommands = [
  { label: "Home", hint: "Resume work and recent activity", icon: "home", group: "Go to", run: () => setPage("overview") },
  { label: "Create a post", hint: "Compose", icon: "plus", group: "Go to", run: () => setPage("publish") },
  { label: "Library", hint: "Upload and reuse", icon: "media", group: "Go to", run: () => setPage("content") },
  { label: "Channels", hint: "Connect accounts", icon: "link", group: "Go to", run: () => setPage("connections") },
  { label: "Activity", hint: "What Relay delivered", icon: "history", group: "Go to", run: () => setPage("history") },
  { label: "Insights", hint: "Performance", icon: "chart", group: "Go to", run: () => setPage("analytics") },
  { label: "Log in", hint: "Open your account", icon: "lock", group: "Actions", run: () => showUnlock() },
  { label: "Log out", hint: "Sign out of this browser", icon: "lock", group: "Actions", run: () => lockWorkspace() },
  { label: "Connect Instagram", hint: "OAuth popup", icon: "instagram", group: "Actions", run: () => connect("instagram") },
  { label: "Connect YouTube", hint: "OAuth popup", icon: "youtube", group: "Actions", run: () => connect("youtube") },
  { label: "Setup guide", hint: "Callback URLs", icon: "book", group: "Actions", run: () => openDialog($("#setup-dialog")) },
  { label: "API reference", hint: "Opens /docs", icon: "external", group: "Actions", run: () => window.open("/docs", "_blank", "noopener") },
];

let paletteMatches = [];
let paletteIndex = 0;
let paletteOpener = null;

function openPalette() {
  const dialog = $("#palette-dialog");
  if (dialog.open) { dialog.close(); return; }
  paletteOpener = document.activeElement instanceof HTMLElement ? document.activeElement : null;
  $("#palette-input").value = "";
  $("#palette-input").setAttribute("aria-expanded", "true");
  renderPalette("");
  openDialog(dialog);
  $("#palette-input").focus();
}

function closePalette() {
  const dialog = $("#palette-dialog");
  if (dialog.open) dialog.close();
  $("#palette-input").setAttribute("aria-expanded", "false");
  $("#palette-input").removeAttribute("aria-activedescendant");
}

function renderPalette(query) {
  const list = $("#palette-list");
  const input = $("#palette-input");
  const needle = query.trim().toLowerCase();
  paletteMatches = paletteCommands.filter((command) =>
    !needle || `${command.label} ${command.hint} ${command.group}`.toLowerCase().includes(needle));
  paletteIndex = 0;
  list.replaceChildren();
  if (!paletteMatches.length) {
    list.append(el("p", "palette-empty", "No matching commands."));
    input.removeAttribute("aria-activedescendant");
    input.setAttribute("aria-expanded", "false");
    return;
  }
  input.setAttribute("aria-expanded", "true");
  let group = null;
  let groupNode = null;
  paletteMatches.forEach((command, index) => {
    if (command.group !== group) {
      group = command.group;
      // A listbox may only own options and groups. The heading labels its group
      // instead of sitting loose in the listbox as a paragraph.
      groupNode = el("div", "palette-section");
      groupNode.setAttribute("role", "group");
      const headingId = `palette-group-${group.replace(/\s+/g, "-").toLowerCase()}`;
      groupNode.setAttribute("aria-labelledby", headingId);
      const heading = el("p", "palette-group", group);
      heading.id = headingId;
      groupNode.append(heading);
      list.append(groupNode);
    }
    const item = el("button", "palette-item");
    item.type = "button";
    item.id = `palette-option-${index}`;
    item.setAttribute("role", "option");
    item.setAttribute("aria-selected", String(index === paletteIndex));
    item.append(icon(command.icon), el("span", null, command.label), el("small", null, command.hint));
    item.addEventListener("click", () => runPalette(index));
    item.addEventListener("mousemove", () => {
      if (paletteIndex === index) return;
      paletteIndex = index;
      syncPaletteSelection();
    });
    groupNode.append(item);
  });
  syncPaletteSelection();
}

function syncPaletteSelection() {
  const items = [...$("#palette-list").querySelectorAll(".palette-item")];
  items.forEach((node, index) => node.setAttribute("aria-selected", String(index === paletteIndex)));
  const active = items[paletteIndex];
  const input = $("#palette-input");
  if (active) {
    input.setAttribute("aria-activedescendant", active.id);
    // Keep the active option in view without moving DOM focus off the input.
    active.scrollIntoView({ block: "nearest" });
  } else {
    input.removeAttribute("aria-activedescendant");
  }
}

function runPalette(index) {
  const command = paletteMatches[index];
  closePalette();
  if (command) window.setTimeout(() => command.run(), 0);
}

function movePalette(delta) {
  if (!paletteMatches.length) return;
  paletteIndex = (paletteIndex + delta + paletteMatches.length) % paletteMatches.length;
  syncPaletteSelection();
}

$("#palette-input").addEventListener("input", (event) => renderPalette(event.target.value));
$("#palette-input").addEventListener("keydown", (event) => {
  if (event.key === "ArrowDown") { event.preventDefault(); movePalette(1); }
  else if (event.key === "ArrowUp") { event.preventDefault(); movePalette(-1); }
  else if (event.key === "Home" && event.ctrlKey) { event.preventDefault(); paletteIndex = 0; syncPaletteSelection(); }
  else if (event.key === "End" && event.ctrlKey) {
    event.preventDefault();
    paletteIndex = Math.max(0, paletteMatches.length - 1);
    syncPaletteSelection();
  }
  else if (event.key === "Enter") { event.preventDefault(); runPalette(paletteIndex); }
  else if (event.key === "Escape") {
    event.preventDefault();
    closePalette();
    if (paletteOpener && document.contains(paletteOpener)) paletteOpener.focus();
  }
});
$("#palette-dialog").addEventListener("click", (event) => {
  if (event.target === $("#palette-dialog")) closePalette();
});
$("#palette-dialog").addEventListener("close", () => {
  $("#palette-input").setAttribute("aria-expanded", "false");
});

/* ── "More" sheet (mobile) ─────────────────────────────────── */

const moreSheet = $("#more-sheet");
$("#mobile-more").addEventListener("click", () => {
  openDialog(moreSheet, $("#mobile-more"));
  $("#mobile-more").setAttribute("aria-expanded", "true");
});
moreSheet.addEventListener("close", () => $("#mobile-more").setAttribute("aria-expanded", "false"));
$("#more-palette").addEventListener("click", () => {
  moreSheet.close();
  openPalette();
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
  if (document.querySelector("dialog[open]")) return;

  if (event.key === "n") { event.preventDefault(); setPage("publish"); return; }
  if (event.key === "g") { pendingGoto = true; window.setTimeout(() => { pendingGoto = false; }, 1200); return; }
  if (pendingGoto && pageKeys[event.key]) {
    event.preventDefault();
    pendingGoto = false;
    setPage(pageKeys[event.key]);
  }
});

/* ── Bootstrap ─────────────────────────────────────────────── */

window.initialize = async function initialize() {
  try {
    const initialPage = routeFromHash();
    setPage(initialPage || "overview", { recordHistory: false });
    if (!initialPage && window.location.hash) {
      // A stale or unknown fragment is replaced, not treated as a page.
      window.history.replaceState({}, "", `#${activePage}`);
    }
    const state = await api("/session");
    authenticated = state.authenticated;
    if (!authenticated) { window.location.replace("/login"); return; }
    activeWorkspaceId = state.workspace?.id || null;
    activeUserId = state.user?.id || null;
    // Ownership is proven by this authenticated bootstrap: the workspace came
    // from the session's own owner-filtered lookup. Only now may a draft key be
    // derived, and only for this workspace.
    $("#workspace-tools").hidden = !state.user;
    $("#workspace-select").replaceChildren();
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
    localMediaStorage = state.publishing?.local_media_storage === true;
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
        button.querySelector("span").textContent = "Setup needed";
        $(`#${provider}-state`).textContent = "Setup needed";
      }
    }
    await loadAccounts();
    await loadContent();
    if (authenticated) await loadPostHistory();
    // Restore the owned draft first, then offer older work. The preview caption
    // is adopted only when nothing of the person's own exists.
    restoreLocalDraft();
    renderDraftBanners();
    renderComposer();
    await loadRecoveredAttempts();
    $("#journey-recovery").hidden = true;
    if (activePage === "analytics") await loadAnalytics();
    const params = new URLSearchParams(window.location.search);
    const connected = params.get("connected");
    const error = params.get("oauth_error");
    if (Object.hasOwn(names, connected)) notice(`${names[connected]} connected. Welcome back.`);
    else if (error) {
      const messages = {
        cancelled: "Sign-in was cancelled. Your existing connections haven't changed.",
        expired: "That sign-in link expired or belongs to another browser. Start again when you're ready.",
      };
      notice(messages[error] || "We couldn't finish connecting. Try again from Channels.", true);
    }
    if (connected || error) window.history.replaceState({}, "", "/");
    renderOverview();
    renderDraftBanners();
    renderComposer();
  } catch (error) {
    showJourneyRecovery("Relay couldn't finish loading. Your draft is still here.", () => window.initialize());
    notice(error.message, true);
  }
};
