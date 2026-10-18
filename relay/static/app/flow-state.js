"use strict";

// Pure flow decisions shared by the workspace renderer and behavior tests.
function postJourneyState({ media, accounts, title, busy, results }) {
  if (busy) return { step: 3, state: "loading", next: "Sending to your selected channels. Keep this window open." };
  if (results.length) {
    const confirmed = results.filter((result) => result.url || result.status === "published").length;
    const ready = results.filter((result) => result.status === "ready").length;
    const attention = results.some((result) => result.error || ["failed", "expired", "unknown"].includes(result.status));
    return { step: 3, state: attention ? "recovery" : confirmed === results.length ? "complete" : "processing",
      next: `${confirmed}/${results.length} destinations confirmed.${ready ? ` ${ready} ready for Instagram confirmation.` : ""}${attention ? " Review the destination below; your draft is kept." : ""}` };
  }
  if (!media) return { step: 1, state: "empty", next: "Choose a photo or video to start your post." };
  if (!accounts.length) return { step: 2, state: "partial", next: `${media.filename} is attached. Choose a channel above.` };
  const incompatible = accounts.some((account) => account.provider === "youtube" ? !media.youtube_compatible : !media.instagram_compatible);
  if (incompatible) return { step: 2, state: "partial", next: "This file cannot go to every selected channel. Choose a compatible file or remove that channel." };
  if (accounts.some((account) => account.provider === "youtube") && !title.trim()) return { step: 2, state: "partial", next: "Add a YouTube title below your caption to continue." };
  return { step: 2, state: "ready", next: `${media.filename} · ${accounts.length} ${accounts.length === 1 ? "channel" : "channels"} · ready to send.` };
}

function workspaceCapacity(count) {
  return count >= 50 ? `${count}/50 workspaces used. This cap does not reset. Switch to an existing workspace to continue.` : null;
}
