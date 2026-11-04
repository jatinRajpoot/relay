"use strict";

function icon(name) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("class", "icon");
  const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
  use.setAttribute("href", `#i-${name}`);
  svg.append(use);
  return svg;
}

/* ── Notifications ─────────────────────────────────────────────
   Toasts are for confirmations and recoverable errors only. Anything that
   describes a persistent state belongs inline next to the control.

   Accessibility rules this implements:
   - The toast stack is visual only; announcements go to #live-polite /
     #live-assertive, so an actionable toast is not read as one noisy string.
   - A toast never disappears while it holds focus. Dismissing it moves focus
     to the nearest sensible control instead of dropping it to <body>.
   - Hover or focus pauses the timer; leaving restarts it. */

const toastTimers = new WeakMap();
const toastLifetime = { info: 6000, error: 10000 };
let lastToastTrigger = null;

function announce(message, { assertive = false } = {}) {
  const region = $(assertive ? "#live-assertive" : "#live-polite");
  if (!region) return;
  // Clearing first makes a repeated identical message announce again.
  region.textContent = "";
  window.setTimeout(() => { region.textContent = message; }, 30);
}

function pauseToast(toast) {
  const timer = toastTimers.get(toast);
  if (timer) {
    window.clearTimeout(timer);
    toastTimers.delete(toast);
  }
}

function armToast(toast, delay) {
  pauseToast(toast);
  toastTimers.set(toast, window.setTimeout(() => {
    toastTimers.delete(toast);
    dismissToast(toast, { restoreFocus: true });
  }, delay));
}

function dismissToast(toast, { restoreFocus = false } = {}) {
  if (!toast || toast.dataset.leaving) return;
  pauseToast(toast);
  const heldFocus = toast.contains(document.activeElement);
  toast.dataset.leaving = "true";
  toast.classList.add("leaving");
  if (heldFocus) {
    // Never leave focus on a node that is about to be removed.
    const target = (lastToastTrigger && document.contains(lastToastTrigger) && !lastToastTrigger.disabled)
      ? lastToastTrigger
      : document.querySelector("#main-content h1, #main-content button, #main-content a");
    target?.focus?.({ preventScroll: true });
  }
  window.setTimeout(() => toast.remove(), 200);
  if (restoreFocus) { /* focus was handled above when the toast held it */ }
}

function notice(message, error = false, action = null) {
  const stack = $("#toasts");
  if (!stack) return;
  const trigger = document.activeElement instanceof HTMLElement ? document.activeElement : null;
  if (trigger && trigger !== document.body) lastToastTrigger = trigger;

  const toast = el("div", `toast${error ? " error" : ""}`);
  const symbol = icon(error ? "alert" : "check");
  symbol.classList.add("toast-icon");
  toast.append(symbol, el("span", "toast-text", message));
  if (action) {
    const run = el("button", "toast-action", action.label);
    run.type = "button";
    run.addEventListener("click", () => {
      action.run();
      dismissToast(toast);
    });
    toast.append(run);
  }
  const close = el("button", "toast-close", "×");
  close.type = "button";
  close.setAttribute("aria-label", "Dismiss notification");
  close.addEventListener("click", () => dismissToast(toast));
  toast.append(close);
  stack.append(toast);
  while (stack.children.length > 2) {
    const oldest = stack.firstElementChild;
    if (oldest.contains(document.activeElement)) break;
    dismissToast(oldest);
  }

  announce(action ? `${message} ${action.label} is available.` : message, { assertive: error });
  armToast(toast, error ? toastLifetime.error : toastLifetime.info);
  toast.addEventListener("mouseenter", () => pauseToast(toast));
  toast.addEventListener("mouseleave", () => {
    if (!toast.contains(document.activeElement)) armToast(toast, toastLifetime.info);
  });
  toast.addEventListener("focusin", () => pauseToast(toast));
  toast.addEventListener("focusout", (event) => {
    if (!toast.contains(event.relatedTarget) && !toast.dataset.leaving) {
      armToast(toast, toastLifetime.info);
    }
  });
}

/* ── Session-wide rendering ────────────────────────────────── */

function renderSession() {
  const button = $("#session-button");
  button.setAttribute("aria-label", authenticated ? "Lock workspace" : "Unlock workspace");
  button.title = authenticated ? "Lock workspace" : "Unlock workspace";
  button.querySelector("span").textContent = authenticated ? "Lock workspace" : "Unlock workspace";
  button.querySelector("use").setAttribute("href", authenticated ? "#i-lock" : "#i-unlock");

  if (!authenticated) {
    stopStatusPolling();
    accountList = [];
    contentItems = [];
    selectedAccountIds.clear();
    selectedContentId = null;
    activeInstagramPosts = [];
    youtubeUploadResults = [];
    recoveredAttempts = [];
    recoveredNextCursor = null;
    recoveredLoaded = false;
    recoveredError = null;
    activeSubmission = null;
    analyticsData = null;
    analyticsLoading = false;
    analyticsError = null;
    analyticsRequestId += 1;
    postHistory = [];
    historyLoading = false;
    historyLoaded = false;
    historyError = null;
    activityAttempts = [];
    activityCursor = null;
    activityLoaded = false;
    activityStale = false;
    historyRequestId += 1;
    publishing = false;
    $("#accounts-list").replaceChildren();
    $("#accounts-list").hidden = true;
    $("#empty-state").hidden = false;
    setText("#account-count", "—");
    setText("#nav-count", "—");
    for (const provider of Object.keys(names)) {
      $(`#${provider}-state`).textContent = providerIssues[provider] ? "Setup needed" : "Ready to connect";
      $(`#${provider}-state`).classList.remove("connected");
    }
  }
  renderContent();
  renderComposer();
  renderOverview();
  renderAnalytics();
  renderPostHistory();
}
