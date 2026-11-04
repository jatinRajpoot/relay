"use strict";

/* API transport: one place that turns an HTTP response into data or a safe message.
   Provider bodies, stack traces, and configuration names never reach the interface. */

const API_FALLBACK = "Relay couldn't finish that request. Try again in a moment.";

const STATUS_MESSAGES = {
  400: "Relay couldn't accept that request. Check the details and try again.",
  401: "Your session ended. Sign in to continue.",
  403: "This workspace can't make that change. Reopen Relay at its configured address.",
  404: "That item no longer exists. Refresh and try again.",
  409: "Relay couldn't complete that step in its current state. Refresh and try again.",
  413: "That file is larger than this workspace allows.",
  415: "That file type isn't supported here.",
  422: "Some details weren't accepted. Check the highlighted fields.",
  429: "Too many attempts. Wait a few minutes, then try again.",
  500: "Relay hit an unexpected problem. Your work is unchanged; try again.",
  502: "A connected service didn't respond as expected. Try again in a moment.",
  503: "This feature isn't available on this deployment yet.",
  504: "A connected service took too long to answer. Try again in a moment.",
};

/* Text that looks like an internal diagnostic is replaced rather than shown. */
function isUnsafeDetail(text) {
  return /(traceback|stack trace|<!doctype|<html|\.py\b|\.js:\d+|\bat\s+[\w$.]+\s*\()/i.test(text) ||
    /(INSTAGRAM_[A-Z_]+|YOUTUBE_[A-Z_]+|GOOGLE_[A-Z_]+|SERVICE_API_KEY|TOKEN_ENCRYPTION_KEY|MAX_UPLOAD_BYTES|DATABASE_[A-Z_]+|BASE_URL)/.test(text);
}

/* A short, single-line, human-readable detail, or null when it isn't safe to show. */
function safeDetail(value) {
  if (typeof value !== "string") return null;
  const collapsed = value.replace(/\s+/g, " ").trim();
  if (!collapsed || collapsed.length > 240) return null;
  if (isUnsafeDetail(collapsed)) return null;
  return collapsed;
}

function statusMessage(status) {
  return STATUS_MESSAGES[status] || (status >= 500
    ? "Relay hit an unexpected problem. Try again in a moment."
    : API_FALLBACK);
}

/* Pure: map a response to {ok, data, message, status}. Never throws on bad bodies. */
function parseApiResponse(status, text) {
  let data = null;
  let parsed = false;
  if (typeof text === "string" && text.length) {
    try {
      data = JSON.parse(text);
      parsed = true;
    } catch {
      data = null;
    }
  }
  if (status >= 200 && status < 300) {
    return { ok: true, status, data, message: null, malformed: Boolean(text) && !parsed };
  }
  const detail = parsed && data && typeof data === "object" ? safeDetail(data.detail) : null;
  // A storage failure states whether the write is known not to have been applied
  // ("not_applied", safe to retry) or may have been ("unknown", verify first).
  // It describes the storage write only, never a provider operation.
  const outcome = parsed && data && typeof data === "object" && data.outcome === "not_applied"
    ? "not_applied"
    : parsed && data && typeof data === "object" && data.outcome === "unknown"
      ? "unknown"
      : null;
  return { ok: false, status, data: parsed ? data : null, message: detail || statusMessage(status), malformed: false, outcome };
}

function apiError(message, status, extra = {}) {
  const error = new Error(message);
  if (status) error.status = status;
  Object.assign(error, extra);
  return error;
}

function networkMessage(cause) {
  const hasDraft = typeof activePage !== "undefined" && activePage === "publish" &&
    typeof saveLocalDraft === "function";
  const draftSaved = hasDraft && saveLocalDraft({ announce: false });
  const recovery = draftSaved
    ? "Your draft is saved in this browser."
    : hasDraft
      ? "Your input is still in this window."
      : "Try again when the connection is back.";
  return cause && cause.name === "TimeoutError"
    ? `Relay took too long to respond. ${recovery}`
    : `Couldn't reach Relay. ${recovery}`;
}

async function api(path, options = {}) {
  let response;
  try {
    const headers = new Headers(options.headers);
    if (typeof activeWorkspaceId !== "undefined" && activeWorkspaceId) {
      headers.set("X-Workspace-ID", activeWorkspaceId);
    }
    const method = String(options.method || "GET").toUpperCase();
    const signal = options.signal || (method === "GET" && typeof AbortSignal !== "undefined" && AbortSignal.timeout
      ? AbortSignal.timeout(30_000)
      : undefined);
    response = await fetch(path, { credentials: "same-origin", ...options, headers, signal });
  } catch (cause) {
    throw apiError(networkMessage(cause), 0, { network: true });
  }

  let text = "";
  if (response.status !== 204) {
    try {
      text = await response.text();
    } catch {
      text = "";
    }
  }
  const result = parseApiResponse(response.status, text);

  if (!result.ok) {
    if (result.status === 401) {
      if (typeof saveLocalDraft === "function") saveLocalDraft({ announce: false });
      if (typeof showSessionRecovery === "function") showSessionRecovery();
    }
    throw apiError(result.message, result.status, result.outcome ? { outcome: result.outcome } : {});
  }
  if (response.status === 204) return null;
  if (result.malformed) {
    // A 2xx we cannot read is not a success we can act on.
    throw apiError("Relay sent a response it couldn't read. Refresh before retrying.", result.status, { malformed: true });
  }
  return result.data;
}
