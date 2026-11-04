"use strict";

/* Pure composer and delivery decisions. No DOM, so Node tests can exercise them directly. */

/* Whether Relay can send to a channel right now.

   One predicate for Home, Channels, and the composer, so the three surfaces can
   never disagree about a channel's health. It uses only metadata the server
   already returns, and it keeps the providers' refresh semantics distinct:

   - An unreadable stored credential can never send. Reconnecting is the only
     remedy, whatever the expiry says.
   - Instagram tokens are long-lived and the publish path does not refresh them,
     so an expired one is a hard stop that only reconnecting clears.
   - YouTube access tokens are refreshed automatically before a send. An expired
     token is therefore *not* a reconnect signal while a refresh token exists;
     only an expired token with no refresh token is dead.

   `has_refresh_token === false` (not a falsy check) keeps an older backend that
   does not report the field from being read as broken. */
function channelReadiness(account, now = Date.now()) {
  const unreadable = Boolean(account) && account.credentials_readable === false;
  const expiresAt = Number(account && account.expires_at) || 0;
  const expired = expiresAt > 0 && expiresAt * 1000 <= now;
  const refreshable = Boolean(account) && account.provider === "youtube" &&
    account.has_refresh_token !== false;
  const expiredHard = expired && !refreshable;
  const reconnect = unreadable || expiredHard;
  const reason = unreadable
    ? "unreadable"
    : expiredHard
      ? "expired"
      : expired
        ? "refreshable"
        : "ok";
  return {
    ready: !reconnect,
    reconnect,
    reason,
    unreadable,
    expired,
    refreshable,
  };
}

/* The visible verdict for a channel, derived from channelReadiness so Home,
   Channels, and the composer cannot disagree. Only the wording differs: Home is
   a glance, Channels names the repair. */
function channelStatus(account, surface = "channels", now = Date.now()) {
  const readiness = channelReadiness(account, now);
  if (readiness.reconnect) {
    return {
      ...readiness,
      label: surface === "home" ? "Reconnect" : "Reconnect needed",
      tone: "expired",
    };
  }
  return { ...readiness, label: "Connected", tone: "connected" };
}

/* Which platform sections the composer must show for the current selection. */
function composerSections(accounts) {
  const providers = new Set((accounts || []).map((account) => account.provider));
  return {
    instagram: providers.has("instagram"),
    youtube: providers.has("youtube"),
  };
}

/* Why the primary action is unavailable, in the order a person would fix it. */
function composerBlockers({ media, accounts, instagramCaption, youtubeTitle, publishing, hasResults }) {
  const blockers = [];
  if (publishing) return blockers;
  if (hasResults) return blockers;
  if (!media) blockers.push({ field: "media", message: "Choose a photo or video." });
  if (!accounts || !accounts.length) blockers.push({ field: "accounts", message: "Choose at least one channel." });
  if (media && accounts && accounts.length) {
    const incompatible = accounts.filter((account) =>
      account.provider === "youtube" ? !media.youtube_compatible : !media.instagram_compatible);
    if (incompatible.length) {
      blockers.push({
        field: "media",
        message: `This file can't go to ${incompatible.map((account) => account.name).join(", ")}. Choose a compatible file.`,
      });
    }
  }
  const sections = composerSections(accounts);
  if (sections.youtube && !String(youtubeTitle || "").trim()) {
    blockers.push({ field: "youtube-title", message: "Add a YouTube title." });
  }
  return blockers;
}

/* The one-line summary above the action bar. When something still blocks the
   send, the summary never claims the post is ready; the blocker next to the
   button names the requirement. */
function composerSummary({ media, accounts, publishing, hasResults, results, blockers = [], held = 0 }) {
  if (publishing) {
    const total = (results || []).length;
    return { headline: "Sending…", detail: total ? `${total} destination${total === 1 ? "" : "s"} in progress.` : "Sending your post." };
  }
  if (hasResults) {
    const outcome = deliveryOutcome(results || []);
    return {
      headline: outcome.headline,
      detail: outcome.detail,
    };
  }
  const count = (accounts || []).length;
  if (!media && !count) return { headline: "Choose media to begin", detail: "Then pick where it should go." };
  if (!media) return { headline: `${count} channel${count === 1 ? "" : "s"} selected`, detail: "Add a photo or video to continue." };
  if (!count) return { headline: `${media.filename} is ready`, detail: "Choose at least one channel to send it." };
  if (blockers.length) {
    return {
      headline: `${media.filename} · ${count} channel${count === 1 ? "" : "s"}`,
      detail: "",
    };
  }
  if (held > 0) {
    const sending = count - held;
    return {
      headline: sending
        ? `Ready for ${sending} of ${count} channel${count === 1 ? "" : "s"}`
        : "Nothing to send yet",
      detail: sending
        ? "The other channel is still unconfirmed and is not included."
        : "The selected channels are still unconfirmed. Check their status instead.",
    };
  }
  return {
    headline: `Ready for ${count} channel${count === 1 ? "" : "s"}`,
    detail: media.youtube_compatible || media.instagram_compatible ? "Review the copy, then send." : "This file needs a compatible channel.",
  };
}

/* Per-destination outcome, mirroring the server's states without inventing any.
   `unknown` is never counted as a failure and never lets confirmed reach total.
   `ready` is not "in progress": the provider finished and is waiting for an
   explicit confirmation, so it gets its own phrase. `not_sent` is the client's
   own state for a request Relay refused before dispatch: it is definitely not
   published, but it is not a provider verdict either. */
function deliveryOutcome(results) {
  const total = results.length;
  const confirmed = results.filter((r) => r.status === "published" || (!r.status && r.url)).length;
  const failed = results.filter((r) =>
    r.status === "failed" || r.status === "expired" ||
    (!r.status && r.error && !r.uncertain)).length;
  const notSent = results.filter((r) => r.status === "not_sent").length;
  const unknown = results.filter((r) => r.status === "unknown" || r.uncertain).length;
  const ready = results.filter((r) => r.status === "ready").length;
  const pending = results.filter((r) =>
    ["pending", "submitted", "processing", "publishing"].includes(r.status)).length;
  let headline;
  if (confirmed === total) headline = `Published to ${total} channel${total === 1 ? "" : "s"}`;
  else if (confirmed) headline = `Published to ${confirmed} of ${total}`;
  else if (unknown) headline = "Delivery not confirmed";
  else if (failed) headline = "Delivery failed";
  else if (notSent) headline = "Not sent";
  else if (ready) headline = "Ready to confirm";
  else headline = "Waiting for confirmation";
  const parts = [];
  if (confirmed) parts.push(`${confirmed} confirmed`);
  if (ready) parts.push(`${ready} ready to confirm`);
  if (pending) parts.push(`${pending} still in progress`);
  if (unknown) parts.push(`${unknown} unconfirmed`);
  if (failed) parts.push(`${failed} not published`);
  if (notSent) parts.push(`${notSent} not sent`);
  return { total, confirmed, failed, notSent, unknown, pending, ready, headline, detail: parts.join(" · ") };
}

/* Draft ownership: a draft belongs to one signed-in user in one workspace.
   An unowned legacy draft is never adopted into an account. */
function draftStorageKey({ userId, workspaceId }) {
  if (!userId || !workspaceId) return null;
  return `relay:draft:${userId}:${workspaceId}`;
}

/* Legacy keys written before ownership existed. They are readable only to show
   a "this browser has an older draft" notice; they are never restored
   automatically. See legacyDraftImport() in recovery.js for the eligibility
   rule and content.js for the explicit recovery path. */
function legacyDraftKey(workspaceId) {
  return `relay:draft:${workspaceId || "legacy"}`;
}

function workspaceCapacity(count) {
  return count >= 50 ? `${count}/50 workspaces used. This cap does not reset. Switch to an existing workspace to continue.` : null;
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    channelReadiness,
    channelStatus,
    composerSections,
    composerBlockers,
    composerSummary,
    deliveryOutcome,
    draftStorageKey,
    legacyDraftKey,
    workspaceCapacity,
  };
}
