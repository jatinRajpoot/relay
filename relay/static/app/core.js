"use strict";

// Feature scripts are loaded in order by index.html and share this page state.
const $ = (selector) => document.querySelector(selector);
const names = { instagram: "Instagram", youtube: "YouTube" };
let authenticated = false;
let accountList = [];
let contentItems = [];
let activeWorkspaceId = null;
let disconnecting = null;
let instagramMediaConfigured = false;
let contentStorageConfigured = false;
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
let activeDetailsTab = "master";
let activePreviewPlatform = "instagram";
let historyFilter = "all";
let publishing = false;
let activePage = "overview";
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
