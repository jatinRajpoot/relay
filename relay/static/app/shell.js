async function api(path, options = {}) {
  let response;
  try {
    const headers = new Headers(options.headers);
    if (activeWorkspaceId) headers.set("X-Workspace-ID", activeWorkspaceId);
    response = await fetch(path, { credentials: "same-origin", ...options, headers });
  } catch {
    throw new Error("Couldn't reach Relay. Check that the local service is running.");
  }
  const data = response.status === 204 ? null : await response.json();
  if (!response.ok) {
    if (response.status === 401) {
      authenticated = false;
      renderSession();
      window.location.replace("/login");
    }
    throw new Error(typeof data?.detail === "string" ? data.detail : "Something went wrong. Please try again.");
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
  $("#empty-description").textContent = authenticated
    ? "Connect Instagram or YouTube to start publishing."
    : "Unlock your workspace to view or connect an account.";
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
  const hasChannel = authenticated && accountList.length > 0;
  const hasMedia = authenticated && contentItems.length > 0;
  let status = "Unlock to begin";
  let description = "Unlock your workspace, then connect a channel to get started.";
  if (authenticated && !hasChannel) {
    status = "Connect your first channel";
    description = "Start by connecting Instagram or YouTube. You can add more channels later.";
  } else if (hasChannel && !contentStorageConfigured) {
    status = "Media storage needs setup";
    description = "Your channels are connected. Configure media storage to add content and publish.";
  } else if (hasChannel && !hasMedia) {
    status = "Add your first media";
    description = "Your channels are connected. Upload a photo or video to continue.";
  } else if (hasChannel && hasMedia) {
    status = "Ready to publish";
    description = "Your workspace is ready. Create a post whenever you are.";
  }
  $("#workflow-status").textContent = status;
  $("#workflow-description").textContent = description;
  const states = {
    channels: !authenticated ? "upcoming" : hasChannel ? "complete" : "current",
    media: hasChannel ? (hasMedia ? "complete" : "current") : "upcoming",
    publish: hasMedia ? "current" : "upcoming",
  };
  $("#workflow-card").hidden = hasChannel;
  document.querySelectorAll("[data-flow-step]").forEach((step, index) => {
    const state = states[step.dataset.flowStep];
    step.dataset.flowState = state;
    step.querySelector(".flow-number").textContent = state === "complete" ? "✓" : String(index + 1);
    if (state === "current") step.setAttribute("aria-current", "step");
    else step.removeAttribute("aria-current");
  });
  renderOverview();
}
