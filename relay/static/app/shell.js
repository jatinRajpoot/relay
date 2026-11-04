"use strict";

// Feature scripts are loaded in order by index.html and share this page state.
const $ = (selector) => document.querySelector(selector);
const names = { instagram: "Instagram", youtube: "YouTube" };
let authenticated = false;
let accountList = [];
let contentItems = [];
let activeWorkspaceId = null;
let activeUserId = null;
let disconnecting = null;
let instagramMediaConfigured = false;
let contentStorageConfigured = false;
let localMediaStorage = false;
let maxUploadBytes = 0;
let activeInstagramPosts = [];
let youtubeUploadResults = [];
let instagramStatusTimer = null;
let instagramStatusCheckRunning = false;
let analyticsData = null;
let analyticsLoading = false;
let analyticsError = null;
let analyticsRequestId = 0;
let postHistory = [];
let historyLoading = false;
let historyLoaded = false;
let historyError = null;
let historyRequestId = 0;
let analyticsDays = 30;
let selectedContentId = null;
let selectedAccountIds = new Set();
let activePreviewPlatform = "instagram";
let historyFilter = "all";
let publishing = false;
let activePage = "overview";
const providerConfigured = { instagram: true, youtube: true };
const providerIssues = { instagram: null, youtube: null };

/* `activeSubmission`, `recoveredAttempts`, and `activityAttempts` live in
   publishing.js and history.js next to the code that owns them. */

function resetConnectButtons() {
  document.querySelectorAll("[data-connect]").forEach((button) => {
    const provider = button.dataset.connect;
    button.disabled = !providerConfigured[provider];
    button.querySelector("span").textContent = providerConfigured[provider]
      ? `Connect ${names[provider]}` : "Setup needed";
  });
}

/* ── Shared small helpers ─────────────────────────────────── */

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
}

function setText(selector, value) {
  const node = $(selector);
  if (node) node.textContent = value;
}

function toggleHidden(selector, hidden) {
  const node = $(selector);
  if (node) node.hidden = hidden;
}

/* Dialogs: return focus to the control that opened them. */
const dialogReturnFocus = new WeakMap();

document.addEventListener("click", (event) => {
  const opener = event.target instanceof Element ? event.target.closest("[data-close]") : null;
  if (!opener) return;
  const dialog = document.getElementById(opener.dataset.close);
  if (dialog && dialog.open) dialog.close();
});

document.querySelectorAll("dialog").forEach((dialog) => {
  dialog.addEventListener("close", () => {
    const previous = dialogReturnFocus.get(dialog);
    if (previous && document.contains(previous) && !previous.disabled) previous.focus();
    dialogReturnFocus.delete(dialog);
  });
});

function openDialog(dialog, opener) {
  if (!dialog || dialog.open) return;
  const active = document.activeElement;
  if (active instanceof HTMLElement && active !== document.body) dialogReturnFocus.set(dialog, active);
  else if (opener instanceof HTMLElement) dialogReturnFocus.set(dialog, opener);
  dialog.showModal();
}
