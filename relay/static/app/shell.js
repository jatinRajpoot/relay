async function api(path, options = {}) {
  let response;
  try {
    const headers = new Headers(options.headers);
    if (activeWorkspaceId) headers.set("X-Workspace-ID", activeWorkspaceId);
    const method = String(options.method || "GET").toUpperCase();
    const signal = options.signal || (method === "GET" && AbortSignal.timeout ? AbortSignal.timeout(30_000) : undefined);
    response = await fetch(path, { credentials: "same-origin", ...options, headers, signal });
  } catch (cause) {
    const hasDraft = activePage === "publish" && typeof saveLocalDraft === "function";
    const draftSaved = hasDraft && saveLocalDraft({ announce: false });
    const recovery = draftSaved ? "Your draft is saved in this browser." : hasDraft ? "Your input is still in this window." : "Try again when the connection is back.";
    const message = cause?.name === "TimeoutError"
      ? `Relay took too long to respond. ${recovery}`
      : `Couldn't reach Relay. ${recovery}`;
    const error = new Error(message);
    error.network = true;
    throw error;
  }
  const data = response.status === 204 ? null : await response.json();
  if (!response.ok) {
    if (response.status === 401) {
      saveLocalDraft({ announce: false });
      showSessionRecovery();
    }
    const error = new Error(typeof data?.detail === "string" ? data.detail : "Relay couldn't finish that request. Try again in a moment.");
    error.status = response.status;
    throw error;
  }
  return data;
}

function icon(name) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("class", "icon");
  const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
  use.setAttribute("href", `#i-${name}`);
  svg.append(use);
  return svg;
}

function dismissToast(toast) {
  if (toast.dataset.leaving) return;
  toast.dataset.leaving = "true";
  toast.classList.add("leaving");
  window.setTimeout(() => toast.remove(), 200);
}

function notice(message, error = false, action = null) {
  const stack = $("#toasts");
  if (!stack) return;
  const toast = document.createElement("div");
  toast.className = `toast${error ? " error" : ""}`;
  toast.append(icon(error ? "alert" : "check"));
  toast.querySelector(".icon").classList.add("toast-icon");
  const text = document.createElement("span");
  text.className = "toast-text";
  text.textContent = message;
  toast.append(text);
  if (action) {
    const run = document.createElement("button");
    run.type = "button";
    run.className = "toast-action";
    run.textContent = action.label;
    run.addEventListener("click", () => {
      action.run();
      dismissToast(toast);
    });
    toast.append(run);
  }
  const close = document.createElement("button");
  close.type = "button";
  close.className = "toast-close";
  close.setAttribute("aria-label", "Dismiss notification");
  close.textContent = "×";
  close.addEventListener("click", () => dismissToast(toast));
  toast.append(close);
  stack.append(toast);
  while (stack.children.length > 3) stack.firstElementChild.remove();
  const timer = window.setTimeout(() => dismissToast(toast), error ? 9000 : 5500);
  toast.addEventListener("mouseenter", () => window.clearTimeout(timer), { once: true });
}

function renderSession() {
  const button = $("#session-button");
  button.classList.remove("unlocked");
  button.setAttribute("aria-label", authenticated ? "Lock workspace" : "Unlock workspace");
  button.title = authenticated ? "Lock workspace" : "Unlock workspace";
  button.querySelector("span").textContent = authenticated ? "Lock workspace" : "Unlock workspace";
  button.querySelector("use").setAttribute("href", authenticated ? "#i-lock" : "#i-unlock");
  $("#empty-connect").textContent = authenticated ? "Connect a channel" : "Unlock workspace";
  if (!authenticated) {
    if (instagramStatusTimer !== null) window.clearTimeout(instagramStatusTimer);
    instagramStatusTimer = null;
    accountList = [];
    contentItems = [];
    selectedAccountIds.clear();
    selectedContentId = null;
    activeInstagramPosts = [];
    youtubeUploadResults = [];
    analyticsData = null;
    analyticsLoading = false;
    analyticsError = null;
    analyticsRequestId += 1;
    postHistory = [];
    historyLoading = false;
    historyLoaded = false;
    historyError = null;
    historyRequestId += 1;
    publishing = false;
    $("#accounts-list").replaceChildren();
    $("#accounts-list").hidden = true;
    $("#empty-state").hidden = false;
    $("#publish-results-section").hidden = true;
    $("#account-count").textContent = "—";
    $("#nav-count").textContent = "—";
    for (const provider of Object.keys(names)) {
      $(`#${provider}-state`).textContent = providerIssues[provider] ? "Setup needed" : "Ready to connect";
      $(`#${provider}-state`).classList.remove("connected");
    }
  }
  renderContent();
  renderComposer();
  renderWorkspaceFlow();
  renderAnalytics();
  renderPostHistory();
}

function renderWorkspaceFlow() {
  const button = $("#overview-primary-action");
  if (!button) return;

  let label = "Create a post";
  let destination = "publish";
  let symbol = "plus";
  if (!authenticated) {
    label = "Unlock workspace";
    destination = "unlock";
    symbol = "unlock";
  } else if (readLocalDraft()) {
    label = "Continue your draft";
    symbol = "arrow";
  }
  button.querySelector("span").textContent = label;
  button.querySelector("use").setAttribute("href", `#i-${symbol}`);
  button.dataset.nextAction = destination;
  renderOverview();
  renderJourney();
}
